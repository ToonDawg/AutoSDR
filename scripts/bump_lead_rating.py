"""Bump every lead's `raw_data.rating` by +0.1 (capped at 5.0).

Context: Google profile ratings like 4.9 / 4.8 read as faintly negative in
cold messages ("4.9 stars" makes recipients fixate on the missing 0.1). We'd
rather err 0.1 high than 0.1 low. Anything already at 5.0 (or missing) is
left untouched. Per-review ratings inside ``reviewDetails`` are integers and
are NOT touched — only the aggregate ``rating`` field the LLM reads.

Usage::

    uv run python scripts/bump_lead_rating.py            # dry-run
    uv run python scripts/bump_lead_rating.py --apply    # write
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from autosdr.config import get_settings
from autosdr.db import session_scope
from autosdr.models import Lead

BUMP = 0.1
CAP = 5.0


def _bump(value: object) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if value <= 0 or value >= CAP:
        return None
    return round(min(CAP, float(value) + BUMP), 1)


def _backup_sqlite_db() -> Path | None:
    url = get_settings().database_url
    if not url.startswith("sqlite:///"):
        return None
    db_path = Path(url.removeprefix("sqlite:///"))
    if not db_path.exists():
        return None
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = db_path.with_name(f"{db_path.stem}.bumprating.{ts}{db_path.suffix}")
    shutil.copy2(db_path, backup)
    return backup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Persist changes.")
    args = parser.parse_args()

    if args.apply:
        backup = _backup_sqlite_db()
        if backup is not None:
            print(f"backup: {backup}")

    bumped = 0
    skipped = 0

    with session_scope() as session:
        leads = session.execute(select(Lead)).scalars().all()
        print(f"candidates: {len(leads)}")

        for lead in leads:
            raw = lead.raw_data or {}
            old = raw.get("rating")
            new = _bump(old)
            if new is None or new == old:
                skipped += 1
                continue

            print(f"- {lead.id} {lead.name!r}: {old} -> {new}")
            raw["rating"] = new
            lead.raw_data = raw
            flag_modified(lead, "raw_data")
            bumped += 1

        if args.apply:
            session.commit()
            print(f"committed: bumped={bumped} skipped={skipped}")
        else:
            session.rollback()
            print(f"DRY RUN: bumped={bumped} skipped={skipped} (re-run with --apply)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
