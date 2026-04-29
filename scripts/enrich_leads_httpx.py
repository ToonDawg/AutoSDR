"""Standalone enrichment runner — httpx variant (parallel A/B against crawlee).

Companion to :mod:`scripts.enrich_leads` (crawlee). Same CLI surface,
same envelope shape, same signal extractor —
:func:`autosdr.enrichment_extract.extract_signals_from_soup` — so the
two scripts produce comparable output. The only difference is the
fetcher: this one is plain ``httpx.AsyncClient`` with browser-like
headers, an explicit concurrency cap, and a per-lead time budget.

Useful when crawlee's session pool is being too aggressive about
retiring sessions on 4xx (the 2026-04-29 cohort showed 13/20 errors
that were almost all 403/503 anti-bot, not real failures).

Usage::

    # Diagnose a single URL
    uv run python scripts/enrich_leads_httpx.py --url https://example.com.au

    # Dry-run on 50 unenriched leads
    uv run python scripts/enrich_leads_httpx.py --limit 50

    # Bulk enrich, persist
    uv run python scripts/enrich_leads_httpx.py --limit 500 --concurrency 20 --apply

    # Disable SSL verification for sites with bad certs
    uv run python scripts/enrich_leads_httpx.py --limit 50 --ssl-no-verify

    # Show per-signal fill-rate after the run (to compare against crawlee)
    uv run python scripts/enrich_leads_httpx.py --limit 20 --report
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

import httpx
from bs4 import BeautifulSoup
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


CONNECTOR_NAME = "httpx_bs4"
CONNECTOR_VERSION = "2.0"

# Browser-like headers tend to slip past the cheaper WAFs that bounce
# our identifiable AutoSDR UA. Honest about what we're doing — same
# polite posture as the crawlee variant, just a different transport.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

MAX_BODY_BYTES = 256 * 1024


# ---------------------------------------------------------------------------
# Single-lead fetch
# ---------------------------------------------------------------------------


def _classify_http_status(status_code: int) -> str:
    if status_code in (404, 410):
        return "not_found"
    if status_code in (403, 429):
        return "blocked"
    if 200 <= status_code < 400:
        return "ok"
    return "error"


async def _fetch_one(
    *,
    client: httpx.AsyncClient,
    url: str,
    budget_s: float,
) -> EnrichmentResult:
    """Fetch ``url``, parse, and return a populated envelope."""

    started_at = datetime.now(tz=timezone.utc).isoformat()
    base_meta = {
        "fetched_at": started_at,
        "connector": CONNECTOR_NAME,
        "connector_version": CONNECTOR_VERSION,
        "user_agent": BROWSER_HEADERS["User-Agent"],
        "requested_url": url,
    }

    t0 = time.monotonic()
    try:
        async with client.stream(
            "GET",
            url,
            headers=BROWSER_HEADERS,
            timeout=budget_s,
            follow_redirects=True,
        ) as response:
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes(chunk_size=16 * 1024):
                chunks.append(chunk)
                total += len(chunk)
                if total >= MAX_BODY_BYTES:
                    break
            body = b"".join(chunks)[:MAX_BODY_BYTES]
            status_code = response.status_code
            final_url = str(response.url)
            content_type = response.headers.get("content-type", "")
    except (httpx.TimeoutException, asyncio.TimeoutError) as exc:
        return EnrichmentResult(
            status="timeout",
            meta={
                **base_meta,
                "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                "latency_ms": int((time.monotonic() - t0) * 1000),
            },
        )
    except httpx.HTTPError as exc:
        return EnrichmentResult(
            status="error",
            meta={
                **base_meta,
                "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                "latency_ms": int((time.monotonic() - t0) * 1000),
            },
        )

    latency_ms = int((time.monotonic() - t0) * 1000)
    bucket = _classify_http_status(status_code)

    meta = {
        **base_meta,
        "final_url": final_url,
        "http_status": status_code,
        "latency_ms": latency_ms,
    }

    if bucket != "ok":
        return EnrichmentResult(status=bucket, meta=meta)

    # Decode body — best-effort.
    encoding = "utf-8"
    if "charset=" in content_type.lower():
        encoding = (
            content_type.lower().split("charset=")[-1].split(";")[0].strip()
            or "utf-8"
        )
    try:
        text = body.decode(encoding, errors="ignore")
    except LookupError:
        text = body.decode("utf-8", errors="ignore")

    soup = BeautifulSoup(text, "html.parser")
    signals = extract_signals_from_soup(
        soup=soup,
        final_url=final_url,
        http_status=status_code,
    )
    return EnrichmentResult(status="ok", signals=signals, meta=meta)


# ---------------------------------------------------------------------------
# Bulk runner
# ---------------------------------------------------------------------------


async def _enrich_with_httpx(
    urls: list[str],
    *,
    concurrency: int,
    ssl_no_verify: bool,
    budget_s: float,
) -> dict[str, EnrichmentResult]:
    semaphore = asyncio.Semaphore(concurrency)
    results: dict[str, EnrichmentResult] = {}

    limits = httpx.Limits(
        max_connections=max(concurrency * 2, 20),
        max_keepalive_connections=max(concurrency, 10),
    )
    async with httpx.AsyncClient(
        verify=not ssl_no_verify,
        limits=limits,
    ) as client:

        async def one(url: str) -> None:
            async with semaphore:
                results[url] = await _fetch_one(
                    client=client, url=url, budget_s=budget_s
                )

        await asyncio.gather(*[one(u) for u in urls])

    return results


# ---------------------------------------------------------------------------
# DB helpers / report (shared shape with the crawlee script)
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


def _signal_fill_rate(results: list[EnrichmentResult]) -> dict[str, float]:
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
        description="Standalone AutoSDR lead enricher (httpx variant)"
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
        "--concurrency",
        type=int,
        default=10,
        help="Concurrent HTTP requests (default 10)",
    )
    p.add_argument(
        "--budget-s",
        type=float,
        default=8.0,
        help="Per-request timeout in seconds (default 8.0)",
    )
    p.add_argument(
        "--ssl-no-verify",
        action="store_true",
        help="Skip SSL certificate verification (for sites with bad certs)",
    )
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


async def diagnose_url(url: str, ssl_no_verify: bool, budget_s: float) -> None:
    log.info("Diagnosing %s via httpx (ssl_verify=%s)", url, not ssl_no_verify)
    async with httpx.AsyncClient(verify=not ssl_no_verify) as client:
        result = await _fetch_one(client=client, url=url, budget_s=budget_s)
    log.info("  status   = %s", result.status)
    log.info("  meta     = %s", json.dumps(result.meta, indent=2, default=str))
    if result.signals:
        log.info(
            "  signals  = %s", json.dumps(result.signals, indent=2, default=str)
        )


async def main() -> None:
    args = _parse_args()

    if args.url:
        await diagnose_url(args.url, args.ssl_no_verify, args.budget_s)
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

    log.info(
        "Using httpx (concurrency=%d ssl_verify=%s budget_s=%.1f) ...",
        args.concurrency,
        not args.ssl_no_verify,
        args.budget_s,
    )

    t0 = time.monotonic()
    results = await _enrich_with_httpx(
        urls,
        concurrency=args.concurrency,
        ssl_no_verify=args.ssl_no_verify,
        budget_s=args.budget_s,
    )
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
