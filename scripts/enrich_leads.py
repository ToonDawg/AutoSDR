"""Standalone enrichment runner / diagnostics tool.

Pulls leads that need enrichment from the DB and runs them through
:func:`autosdr.enrichment.enrich_lead`. Designed to be run outside the app
so you can tune concurrency, SSL, and user-agent without touching production.

Usage::

    # Diagnose a single URL (no DB access)
    uv run python scripts/enrich_leads.py --url https://pcrestore.com.au

    # Dry-run on 50 unenriched leads — show what would happen
    uv run python scripts/enrich_leads.py --limit 50

    # Actually enrich 500 leads, 20 concurrent, skip SSL verification
    uv run python scripts/enrich_leads.py --limit 500 --concurrency 20 --ssl-no-verify --apply

    # Retry timeouts and errors only
    uv run python scripts/enrich_leads.py --statuses timeout,error --apply

    # Use crawlee BeautifulSoupCrawler (install: uv add crawlee[beautifulsoup])
    uv run python scripts/enrich_leads.py --limit 100 --use-crawlee --apply

Crawlee notes
-------------
crawlee's BeautifulSoupCrawler sends browser-like headers and handles
connection pooling + retry automatically — good for sites that block the
plain httpx UA. It does NOT render JS (same as the current fetcher), but the
browser headers are often enough to unblock CDN/WAF blocks.

Install: ``uv add crawlee[beautifulsoup]``
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
from sqlalchemy import select

# Make sure the repo root is importable when running as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from autosdr.db import session_scope
from autosdr.enrichment import EnrichmentResult, enrich_lead, persist_enrichment
from autosdr.models import Lead

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# Browser-like headers that are less likely to be blocked than our custom UA.
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


# ---------------------------------------------------------------------------
# Single-URL diagnosis
# ---------------------------------------------------------------------------


async def diagnose_url(url: str, ssl_no_verify: bool) -> None:
    """Fetch one URL and print a detailed breakdown of what happened."""

    log.info("Diagnosing %s (ssl_verify=%s)", url, not ssl_no_verify)

    async with httpx.AsyncClient(verify=not ssl_no_verify) as client:
        # 1. Raw fetch with AutoSDR UA
        t0 = time.monotonic()
        try:
            r = await client.get(
                url,
                headers={"User-Agent": "AutoSDR/0.1.0 (+https://github.com/autosdr/autosdr; lead-enrichment)"},
                timeout=5.0,
                follow_redirects=True,
            )
            autosdr_status = r.status_code
            autosdr_ms = int((time.monotonic() - t0) * 1000)
            log.info("  AutoSDR UA  → HTTP %s  (%d ms)", autosdr_status, autosdr_ms)
        except Exception as exc:
            log.info("  AutoSDR UA  → FAIL  %s: %s", type(exc).__name__, exc)

        # 2. Raw fetch with browser UA
        t0 = time.monotonic()
        try:
            r = await client.get(
                url,
                headers=BROWSER_HEADERS,
                timeout=5.0,
                follow_redirects=True,
            )
            browser_status = r.status_code
            browser_ms = int((time.monotonic() - t0) * 1000)
            log.info("  Browser UA  → HTTP %s  (%d ms)", browser_status, browser_ms)
        except Exception as exc:
            log.info("  Browser UA  → FAIL  %s: %s", type(exc).__name__, exc)

        # 3. Full enrich_lead result
        result = await enrich_lead(
            website_url=url,
            http_client=client,
            budget_s=8.0,
            respect_robots=True,
        )
        log.info("  enrich_lead → status=%s", result.status)
        if result.signals:
            log.info("  signals     = %s", json.dumps(result.signals, indent=2))
        if result.meta.get("error"):
            log.info("  error       = %s", result.meta["error"])


# ---------------------------------------------------------------------------
# Crawlee fallback
# ---------------------------------------------------------------------------


async def _enrich_with_crawlee(urls: list[str]) -> dict[str, EnrichmentResult]:
    """Use crawlee BeautifulSoupCrawler to fetch a list of URLs.

    Returns a dict mapping url → EnrichmentResult. Requires crawlee[beautifulsoup]:
        uv add crawlee[beautifulsoup]
    """
    try:
        from crawlee.crawlers import BeautifulSoupCrawler, BeautifulSoupCrawlingContext
    except ImportError:
        log.error("crawlee not installed. Run: uv add crawlee[beautifulsoup]")
        sys.exit(1)

    results: dict[str, EnrichmentResult] = {}
    url_set = set(urls)

    async def handler(context: BeautifulSoupCrawlingContext) -> None:
        url = str(context.request.url)
        soup = context.soup
        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else ""
        h1_tag = soup.find("h1")
        h1 = h1_tag.get_text(strip=True) if h1_tag else ""
        desc_tag = soup.find("meta", attrs={"name": "description"})
        description = desc_tag.get("content", "") if desc_tag else ""
        viewport = bool(soup.find("meta", attrs={"name": "viewport"}))
        og_image = bool(soup.find("meta", attrs={"property": "og:image"}))
        favicon = bool(soup.find("link", rel=lambda r: r and "icon" in r))

        signals = {
            "title": title,
            "meta_description": description,
            "h1": h1,
            "viewport_present": viewport,
            "og_image_present": og_image,
            "favicon_present": favicon,
            "is_https": str(context.request.url).startswith("https"),
            "external_links_to_socials": [],
        }
        results[url] = EnrichmentResult(
            status="ok",
            signals=signals,
            meta={
                "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
                "connector": "crawlee_bs4",
                "connector_version": "1.0",
            },
        )

    crawler = BeautifulSoupCrawler(max_requests_per_crawl=len(urls) + 10)
    crawler.router.default_handler(handler)
    await crawler.run(list(url_set))

    # Fill in error for any URL that didn't get a result
    for url in urls:
        if url not in results:
            results[url] = EnrichmentResult(
                status="error",
                meta={
                    "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
                    "connector": "crawlee_bs4",
                    "error": "no_result_from_crawlee",
                },
            )
    return results


# ---------------------------------------------------------------------------
# DB bulk processing
# ---------------------------------------------------------------------------


def _load_leads(statuses: list[str | None], limit: int) -> list[Lead]:
    from sqlalchemy import or_, null

    conditions = []
    for s in statuses:
        if s is None:
            conditions.append(Lead.enrichment_status.is_(None))
        else:
            conditions.append(Lead.enrichment_status == s)

    with session_scope() as session:
        q = select(Lead).where(or_(*conditions)).limit(limit)
        leads = session.execute(q).scalars().all()
        # Detach so we can mutate in a separate session
        return [
            {
                "id": lead.id,
                "website": lead.website,
                "name": lead.name or "",
                "enrichment_status": lead.enrichment_status,
            }
            for lead in leads
        ]


async def _process_batch_httpx(
    lead_dicts: list[dict],
    concurrency: int,
    ssl_no_verify: bool,
    budget_s: float,
    apply: bool,
) -> Counter:
    semaphore = asyncio.Semaphore(concurrency)
    stats: Counter = Counter()
    done = 0
    total = len(lead_dicts)

    async with httpx.AsyncClient(verify=not ssl_no_verify) as client:

        async def process_one(ld: dict) -> None:
            nonlocal done
            async with semaphore:
                result = await enrich_lead(
                    website_url=ld["website"],
                    http_client=client,
                    budget_s=budget_s,
                    respect_robots=True,
                )
                stats[result.status] += 1
                done += 1
                if done % 50 == 0 or done == total:
                    pct = done / total * 100
                    log.info("  %d/%d (%.0f%%) — %s", done, total, pct, dict(stats))

                if apply:
                    with session_scope() as session:
                        lead = session.get(Lead, ld["id"])
                        if lead is not None:
                            persist_enrichment(lead, result)
                else:
                    log.debug(
                        "  DRY-RUN id=%s url=%s → %s",
                        ld["id"],
                        ld["website"],
                        result.status,
                    )

        await asyncio.gather(*[process_one(ld) for ld in lead_dicts])

    return stats


async def _process_batch_crawlee(
    lead_dicts: list[dict],
    apply: bool,
) -> Counter:
    urls = [ld["website"] for ld in lead_dicts if ld.get("website")]
    id_by_url = {ld["website"]: ld["id"] for ld in lead_dicts if ld.get("website")}

    results = await _enrich_with_crawlee(urls)
    stats: Counter = Counter()

    for url, result in results.items():
        stats[result.status] += 1
        lead_id = id_by_url.get(url)
        if apply and lead_id is not None:
            with session_scope() as session:
                lead = session.get(Lead, lead_id)
                if lead is not None:
                    persist_enrichment(lead, result)

    log.info("crawlee done: %s", dict(stats))
    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Standalone AutoSDR lead enricher")
    p.add_argument("--url", help="Diagnose a single URL and exit")
    p.add_argument(
        "--statuses",
        default="null",
        help="Comma-separated enrichment statuses to (re-)process. "
        "Use 'null' for unenriched. Default: null",
    )
    p.add_argument("--limit", type=int, default=100, help="Max leads to process")
    p.add_argument(
        "--concurrency", type=int, default=10, help="Concurrent HTTP requests"
    )
    p.add_argument(
        "--budget-s",
        type=float,
        default=6.0,
        help="Per-lead time budget in seconds (default 6.0)",
    )
    p.add_argument(
        "--ssl-no-verify",
        action="store_true",
        help="Skip SSL certificate verification (for sites with bad certs)",
    )
    p.add_argument(
        "--use-crawlee",
        action="store_true",
        help="Use crawlee BeautifulSoupCrawler instead of raw httpx "
        "(needs: uv add crawlee[beautifulsoup])",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Write results back to DB. Without this flag the script is a dry-run.",
    )
    return p.parse_args()


async def main() -> None:
    args = _parse_args()

    if args.url:
        await diagnose_url(args.url, ssl_no_verify=args.ssl_no_verify)
        return

    # Parse status list — "null" → None, everything else is a string
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

    t0 = time.monotonic()

    if args.use_crawlee:
        log.info("Using crawlee BeautifulSoupCrawler ...")
        stats = await _process_batch_crawlee(lead_dicts, apply=args.apply)
    else:
        log.info(
            "Using httpx (concurrency=%d ssl_verify=%s) ...",
            args.concurrency,
            not args.ssl_no_verify,
        )
        stats = await _process_batch_httpx(
            lead_dicts,
            concurrency=args.concurrency,
            ssl_no_verify=args.ssl_no_verify,
            budget_s=args.budget_s,
            apply=args.apply,
        )

    elapsed = time.monotonic() - t0
    log.info("Done in %.1f s — results: %s", elapsed, dict(stats))


if __name__ == "__main__":
    asyncio.run(main())
