"""Standalone enrichment runner / diagnostics tool (crawlee-only).

Pulls leads that need enrichment from the DB and runs them through a
crawlee ``BeautifulSoupCrawler`` — single fetcher, browser-like
headers, automatic session pool + retry, no httpx fallback.

Usage::

    # Diagnose a single URL (no DB access)
    uv run python scripts/enrich_leads.py --url https://pcrestore.com.au

    # Dry-run on 50 unenriched leads — show what would happen
    uv run python scripts/enrich_leads.py --limit 50

    # Actually enrich 500 leads and persist
    uv run python scripts/enrich_leads.py --limit 500 --apply

    # Retry timeouts and errors only
    uv run python scripts/enrich_leads.py --statuses timeout,error --apply

The script also captures a per-signal fill-rate report so you can see
which extracted fields are reliable across the cohort.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

# Make sure the repo root is importable when running as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from autosdr.db import session_scope
from autosdr.enrichment import EnrichmentResult, persist_enrichment
from autosdr.enrichment_extract import extract_signals_from_soup
from autosdr.models import Lead

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


CONNECTOR_NAME = "crawlee_bs4"
CONNECTOR_VERSION = "2.0"

# 404 / 410 are content signals (the lead's site is dead) — we want to
# see them in the default handler instead of bouncing through retry.
_IGNORE_STATUS_CODES = [404, 410]


# ---------------------------------------------------------------------------
# Crawlee runner
# ---------------------------------------------------------------------------


async def _enrich_with_crawlee(urls: list[str]) -> dict[str, EnrichmentResult]:
    """Fetch all URLs with one crawlee crawler and return per-URL envelopes.

    Status mapping:

    * 2xx               → ``ok``  (signals extracted)
    * 404 / 410         → ``not_found``
    * other 4xx / 5xx   → ``error`` (retried by crawlee first)
    * timeout / network → ``timeout`` / ``error`` via failed_request_handler
    """

    try:
        from crawlee.crawlers import (
            BeautifulSoupCrawler,
            BeautifulSoupCrawlingContext,
        )
        from crawlee.basic_crawler import BasicCrawlingContext
    except ImportError:
        try:
            from crawlee.crawlers import (
                BeautifulSoupCrawler,
                BeautifulSoupCrawlingContext,
                BasicCrawlingContext,
            )
        except ImportError:
            log.error(
                "crawlee not installed. Run: uv sync (it is now a hard dep)."
            )
            sys.exit(1)

    results: dict[str, EnrichmentResult] = {}
    url_set = list(dict.fromkeys(urls))

    crawler = BeautifulSoupCrawler(
        max_requests_per_crawl=len(url_set) + 10,
        max_request_retries=2,
        ignore_http_error_status_codes=_IGNORE_STATUS_CODES,
    )

    @crawler.router.default_handler
    async def handler(context: BeautifulSoupCrawlingContext) -> None:
        request_url = str(context.request.url)
        final_url = (
            getattr(context.request, "loaded_url", None) or request_url
        )
        http_status = getattr(context.http_response, "status_code", None)

        base_meta: dict = {
            "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
            "connector": CONNECTOR_NAME,
            "connector_version": CONNECTOR_VERSION,
            "requested_url": request_url,
            "final_url": final_url,
            "http_status": http_status,
        }

        if http_status in (404, 410):
            results[request_url] = EnrichmentResult(
                status="not_found",
                meta=base_meta,
            )
            return

        signals = extract_signals_from_soup(
            soup=context.soup,
            final_url=final_url,
            http_status=http_status,
        )
        results[request_url] = EnrichmentResult(
            status="ok",
            signals=signals,
            meta=base_meta,
        )

    @crawler.failed_request_handler
    async def failed_handler(
        context: "BasicCrawlingContext", error: Exception
    ) -> None:
        request_url = str(context.request.url)
        err_name = type(error).__name__
        err_msg = str(error)[:200]

        # Best-effort timeout vs error split. Crawlee surfaces httpx /
        # http-client TimeoutException through SessionError or as a
        # bare TimeoutError depending on transport.
        is_timeout = (
            "timeout" in err_name.lower()
            or "timeout" in err_msg.lower()
        )
        status = "timeout" if is_timeout else "error"

        results[request_url] = EnrichmentResult(
            status=status,
            meta={
                "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
                "connector": CONNECTOR_NAME,
                "connector_version": CONNECTOR_VERSION,
                "requested_url": request_url,
                "error": f"{err_name}: {err_msg}",
            },
        )

    await crawler.run(url_set)

    # Backstop — anything that never got a result (shouldn't happen but
    # crawlee can drop URLs e.g. on dedup) gets a synthetic error.
    for url in urls:
        if url not in results:
            results[url] = EnrichmentResult(
                status="error",
                meta={
                    "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
                    "connector": CONNECTOR_NAME,
                    "connector_version": CONNECTOR_VERSION,
                    "requested_url": url,
                    "error": "no_result_from_crawlee",
                },
            )
    return results


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _load_leads(statuses: list[str | None], limit: int) -> list[dict]:
    from sqlalchemy import or_

    conditions = []
    for s in statuses:
        if s is None:
            conditions.append(Lead.enrichment_status.is_(None))
        else:
            conditions.append(Lead.enrichment_status == s)

    with session_scope() as session:
        q = select(Lead).where(or_(*conditions)).limit(limit)
        leads = session.execute(q).scalars().all()
        return [
            {
                "id": lead.id,
                "website": lead.website,
                "name": lead.name or "",
                "enrichment_status": lead.enrichment_status,
            }
            for lead in leads
        ]


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _signal_fill_rate(results: list[EnrichmentResult]) -> dict[str, float]:
    """Per-signal % of `ok` results where the signal is non-empty."""

    ok_results = [r for r in results if r.status == "ok"]
    if not ok_results:
        return {}

    fill: Counter = Counter()
    keys: set[str] = set()
    for r in ok_results:
        for k, v in r.signals.items():
            keys.add(k)
            if _is_non_empty(v):
                fill[k] += 1

    total = len(ok_results)
    return {k: fill[k] / total for k in sorted(keys)}


def _is_non_empty(v) -> bool:
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, (list, tuple, dict)):
        return len(v) > 0
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v > 0
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Standalone AutoSDR lead enricher (crawlee-only)"
    )
    p.add_argument("--url", help="Diagnose a single URL and exit")
    p.add_argument(
        "--statuses",
        default="null",
        help="Comma-separated enrichment statuses to (re-)process. "
        "Use 'null' for unenriched. Default: null",
    )
    p.add_argument("--limit", type=int, default=100, help="Max leads to process")
    p.add_argument(
        "--apply",
        action="store_true",
        help="Write results back to DB. Without this flag the script is a dry-run.",
    )
    p.add_argument(
        "--report",
        action="store_true",
        help="Print per-signal fill-rate after the run.",
    )
    return p.parse_args()


async def diagnose_url(url: str) -> None:
    log.info("Diagnosing %s via crawlee BeautifulSoupCrawler", url)
    results = await _enrich_with_crawlee([url])
    result = results[url]
    log.info("  status   = %s", result.status)
    log.info("  meta     = %s", json.dumps(result.meta, indent=2, default=str))
    if result.signals:
        log.info("  signals  = %s", json.dumps(result.signals, indent=2, default=str))


async def main() -> None:
    args = _parse_args()

    if args.url:
        await diagnose_url(args.url)
        return

    raw_statuses = [s.strip() for s in args.statuses.split(",")]
    statuses: list[str | None] = [
        None if s == "null" else s for s in raw_statuses
    ]

    log.info(
        "Loading up to %d leads with statuses %s ...", args.limit, raw_statuses
    )
    lead_dicts = _load_leads(statuses, args.limit)
    log.info("Loaded %d leads", len(lead_dicts))

    if not lead_dicts:
        log.info("Nothing to do.")
        return

    if not args.apply:
        log.info("DRY-RUN mode — pass --apply to write results to the DB")

    urls = [ld["website"] for ld in lead_dicts if ld.get("website")]
    id_by_url = {ld["website"]: ld["id"] for ld in lead_dicts if ld.get("website")}

    t0 = time.monotonic()
    results = await _enrich_with_crawlee(urls)
    elapsed = time.monotonic() - t0

    stats: Counter = Counter()
    for url, result in results.items():
        stats[result.status] += 1
        lead_id = id_by_url.get(url)
        if args.apply and lead_id is not None:
            with session_scope() as session:
                lead = session.get(Lead, lead_id)
                if lead is not None:
                    persist_enrichment(lead, result)

    log.info("Done in %.1f s — %s", elapsed, dict(stats))

    if args.report:
        fills = _signal_fill_rate(list(results.values()))
        if fills:
            log.info("Per-signal fill-rate (over %d ok results):", stats["ok"])
            for k, frac in sorted(fills.items(), key=lambda kv: -kv[1]):
                log.info("  %-28s %5.1f%%", k, frac * 100)


if __name__ == "__main__":
    asyncio.run(main())
