"""Backfill historical ``llm_call.cost_usd`` rows.

Populates ``LlmCall.cost_usd`` for legacy rows where the column is
``NULL`` by applying :func:`autosdr.llm.pricing.cost_for` to each row's
``(model, tokens_in, tokens_out)``.

Rules:

* Only rows with ``cost_usd IS NULL`` are considered.
* If ``cost_for(...)`` returns a float, write that value.
* If ``cost_for(...)`` returns ``None`` (unknown model with non-zero
  tokens), leave the row as ``NULL`` so ``unpriced_calls`` remains
  truthful.

Usage:

    /Users/tunoa.johnson/code/AutoSDR/.venv/bin/python scripts/backfill_llm_call_costs.py
    /Users/tunoa.johnson/code/AutoSDR/.venv/bin/python scripts/backfill_llm_call_costs.py --apply
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from autosdr.config import get_settings
from autosdr.db import session_scope
from autosdr.llm.pricing import cost_for
from autosdr.models import LlmCall


def _backup_sqlite_db() -> Path | None:
    url = get_settings().database_url
    if not url.startswith("sqlite:///"):
        return None
    db_path = Path(url.removeprefix("sqlite:///"))
    if not db_path.exists():
        return None
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = db_path.with_name(f"{db_path.stem}.llm-cost-backfill.{ts}{db_path.suffix}")
    shutil.copy2(db_path, backup)
    return backup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Persist changes")
    args = parser.parse_args()

    if args.apply:
        backup = _backup_sqlite_db()
        if backup is not None:
            print(f"backup: {backup}")

    with session_scope() as session:
        rows = session.execute(
            select(LlmCall).where(LlmCall.cost_usd.is_(None))
        ).scalars().all()

        candidates = len(rows)
        updated = 0
        still_unpriced = 0

        for row in rows:
            priced = cost_for(row.model, row.tokens_in, row.tokens_out)
            if priced is None:
                still_unpriced += 1
                continue
            row.cost_usd = float(priced)
            updated += 1

        if args.apply:
            session.commit()
            print(
                "committed: "
                f"candidates={candidates} "
                f"updated={updated} "
                f"still_unpriced={still_unpriced}"
            )
        else:
            session.rollback()
            print(
                "DRY RUN: "
                f"candidates={candidates} "
                f"would_update={updated} "
                f"still_unpriced={still_unpriced}"
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
