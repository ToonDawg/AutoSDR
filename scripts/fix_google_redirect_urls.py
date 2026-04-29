"""Unwrap Google `/url?q=...` redirect URLs that leaked into ``lead.website``.

The scan importer captured Google SERP redirect links verbatim, so a chunk of
leads have a website like::

    /url?q=https://pcrestore.com.au/&opi=...&sa=U&ved=...&usg=...

This script extracts the real target from the ``q`` query param, rewrites both
``Lead.website`` and the mirrored ``raw_data['website']``, and resets the
enrichment envelope on affected leads so they re-enrich against the correct
URL on the next scan pass.

Usage::

    uv run python scripts/fix_google_redirect_urls.py            # dry-run
    uv run python scripts/fix_google_redirect_urls.py --apply    # write
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from sqlalchemy import Text, cast, or_, select
from sqlalchemy.orm.attributes import flag_modified

from autosdr.config import get_settings
from autosdr.db import session_scope
from autosdr.models import Lead

REDIRECT_MARKER = "/url?q="


def unwrap(url: str | None) -> str | None:
    if not url or REDIRECT_MARKER not in url:
        return url
    # The stored value typically starts with ``/url?q=...`` (no scheme/host),
    # so urlparse puts everything in ``path`` + ``query``. Find the marker
    # ourselves to be safe across both shapes.
    idx = url.find(REDIRECT_MARKER)
    tail = url[idx + len("/url?") :]
    qs = parse_qs(tail, keep_blank_values=True)
    target = qs.get("q", [None])[0]
    if not target:
        return None
    target = unquote(target)
    parsed = urlparse(target)
    if not parsed.scheme or not parsed.netloc:
        return None
    return target


def _backup_sqlite_db() -> Path | None:
    url = get_settings().database_url
    if not url.startswith("sqlite:///"):
        return None
    db_path = Path(url.removeprefix("sqlite:///"))
    if not db_path.exists():
        return None
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = db_path.with_name(f"{db_path.stem}.fixurls.{ts}{db_path.suffix}")
    shutil.copy2(db_path, backup)
    return backup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes. Without this flag the script is a dry-run.",
    )
    args = parser.parse_args()

    apply = args.apply

    if apply:
        backup = _backup_sqlite_db()
        if backup is not None:
            print(f"backup: {backup}")

    fixed = 0
    cleared = 0
    skipped = 0

    with session_scope() as session:
        stmt = select(Lead).where(
            or_(
                Lead.website.like(f"%{REDIRECT_MARKER}%"),
                cast(Lead.raw_data, Text).like(f"%{REDIRECT_MARKER}%"),
            )
        )
        leads = session.execute(stmt).scalars().all()
        print(f"candidates: {len(leads)}")

        for lead in leads:
            new_website = unwrap(lead.website)
            raw = lead.raw_data or {}
            raw_website_old = raw.get("website")
            raw_website_new = unwrap(raw_website_old) if isinstance(raw_website_old, str) else raw_website_old

            website_changed = new_website != lead.website
            raw_changed = raw_website_new != raw_website_old

            if not website_changed and not raw_changed:
                skipped += 1
                continue

            print(f"- {lead.id} {lead.name!r}")
            if website_changed:
                print(f"    website: {lead.website}\n         -> {new_website}")
            if raw_changed:
                print(f"    raw.website: {raw_website_old}\n             -> {raw_website_new}")

            if website_changed:
                lead.website = new_website
                fixed += 1

            if raw_changed:
                raw["website"] = raw_website_new

            had_enrichment = isinstance(raw, dict) and "enrichment" in raw
            if had_enrichment:
                raw.pop("enrichment", None)
                cleared += 1

            lead.raw_data = raw
            flag_modified(lead, "raw_data")

            if had_enrichment or lead.enrichment_status is not None:
                lead.enrichment_status = None
                lead.enrichment_fetched_at = None

        if apply:
            session.commit()
            print(f"committed: website_fixed={fixed} enrichment_cleared={cleared} skipped={skipped}")
        else:
            session.rollback()
            print(
                f"DRY RUN: website_fixed={fixed} enrichment_cleared={cleared} "
                f"skipped={skipped} (re-run with --apply to persist)"
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
