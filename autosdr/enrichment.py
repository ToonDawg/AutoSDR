"""Lead-website enrichment — crawlee-powered polite homepage scrape.

Replaces the previous httpx-based fetcher (ticket 0011) with a
crawlee ``BeautifulSoupCrawler``-driven implementation (supersedes
ticket 0012). The public surface is unchanged:

* :func:`enrich_lead` — single-URL convenience returning an
  :class:`EnrichmentResult`.
* :func:`enrich_urls` — bulk primitive used by the standalone
  enrichment script and the scan worker; one crawler instance per
  call, automatic concurrency + retry from crawlee.
* :func:`persist_enrichment` — fold an :class:`EnrichmentResult`
  back onto a :class:`Lead` row (JSON blob + denormalised columns).

Status mapping (the closed :data:`EnrichmentStatus` vocabulary):

* ``ok``        — 2xx, signals extracted.
* ``not_found`` — 404 / 410.
* ``blocked``   — 403 / 429 (anti-bot or rate-limit).
* ``timeout``   — read timeout / connect timeout from crawlee.
* ``error``     — any other failure (5xx after retries, DNS, TLS,
                  invalid URL, body decode error).
* ``no_url``    — caller passed an empty/missing URL.

Signal extraction is delegated to :mod:`autosdr.enrichment_extract`
so the script and worker emit identical envelopes.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import urlparse

from sqlalchemy.orm.attributes import flag_modified

from autosdr import __version__
from autosdr.enrichment_extract import extract_signals_from_soup
from autosdr.enrichment_vocab import SOCIAL_HOSTS
from autosdr.models import Lead

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


EnrichmentStatus = Literal[
    "ok",
    "no_url",
    "timeout",
    "blocked",
    "empty_shell",
    "not_found",
    "error",
    "killswitch_aborted",
]

# Bumped on every fetcher swap so the scan worker auto-revalidates
# every cached envelope.
ENVELOPE_VERSION = 3
CONNECTOR_NAME = "website_crawlee"
CONNECTOR_VERSION = "2.0"

# Crawlee loads ``/robots.txt`` before the page request via ``send_request``
# without a per-request timeout; Impit's AsyncClient default is ~3s, which
# shows up as ~3012ms timeouts on slow robots. Floor the Impit client timeout
# so robots and navigation stay on the same scale as ``budget_s``.
IMPIT_TIMEOUT_FLOOR_S = 8.0

USER_AGENT = (
    f"AutoSDR/{__version__} (+https://github.com/autosdr/autosdr; lead-enrichment)"
)

# Status codes we want to handle in the request handler (rather than
# letting crawlee retry / classify them as session-blocking errors).
_NOT_FOUND_CODES = (404, 410)
_BLOCKED_CODES = (403, 429)
_HANDLED_CODES = list(_NOT_FOUND_CODES) + list(_BLOCKED_CODES)


@dataclass(frozen=True, slots=True)
class EnrichmentResult:
    status: EnrichmentStatus
    signals: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_envelope(self) -> dict[str, Any]:
        meta = {
            "version": ENVELOPE_VERSION,
            "connector": CONNECTOR_NAME,
            "connector_version": CONNECTOR_VERSION,
            "status": self.status,
            **self.meta,
        }
        return {"_meta": meta, "signals": dict(self.signals)}


def persist_enrichment(lead: Lead, result: EnrichmentResult) -> None:
    raw = dict(lead.raw_data or {})
    raw["enrichment"] = result.to_envelope()
    lead.raw_data = raw
    flag_modified(lead, "raw_data")

    lead.enrichment_status = result.status
    fetched_raw = result.meta.get("fetched_at")
    fetched: datetime
    if isinstance(fetched_raw, str) and fetched_raw:
        try:
            fetched = datetime.fromisoformat(fetched_raw.replace("Z", "+00:00"))
        except ValueError:
            fetched = datetime.now(tz=timezone.utc)
    else:
        fetched = datetime.now(tz=timezone.utc)
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    lead.enrichment_fetched_at = fetched


# ---------------------------------------------------------------------------
# URL normalisation
# ---------------------------------------------------------------------------


_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-._~]*(?::\d+)?$")


def normalise_website_url(raw: str | None) -> str | None:
    """Turn whatever the operator imported into a fetchable URL.

    Accepts bare hostnames, schemed URLs, trailing whitespace.
    Returns ``None`` on garbage (caller surfaces ``status="error"``).
    """

    if raw is None:
        return None
    candidate = raw.strip()
    if not candidate:
        return None
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    parsed = urlparse(candidate)
    if not parsed.netloc:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    if not _HOSTNAME_RE.match(parsed.netloc):
        return None
    return candidate


# ---------------------------------------------------------------------------
# Social-profile-as-website detection (ticket 0014)
# ---------------------------------------------------------------------------

# Vocab lives in :mod:`autosdr.enrichment_vocab` so the extractor regex
# in :mod:`autosdr.enrichment_extract` and this predicate share one
# source of truth without a circular import. ``SOCIAL_HOSTS`` is
# re-exported from this module for backward-compat on existing call
# sites that did ``from autosdr.enrichment import SOCIAL_HOSTS``.


def is_social_website(url: str | None) -> str | None:
    """Return the platform token if ``url``'s hostname is a social profile.

    Examples (truth table covered in
    ``tests/test_enrichment_social_website.py``):

    * ``https://facebook.com/foo``         → ``"facebook"``
    * ``https://www.linkedin.com/in/x``    → ``"linkedin"``
    * ``http://tiktok.com/@bar``           → ``"tiktok"``
    * ``https://acme.com/facebook-ads``    → ``None`` (host-only match)
    * ``None`` / ``""`` / garbage          → ``None``

    Hostname-only match — a corporate website that mentions a social
    platform in the path must not light up the predicate.
    """

    if not url:
        return None
    candidate = url.strip()
    if not candidate:
        return None
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    # Hostname suffix match: ``facebook.com`` or ``m.facebook.com``
    # both qualify; ``acme.com`` does not. Single-label
    # social-platform tokens (``x``) are matched exactly to avoid
    # accidental collisions with country-code TLDs (e.g. ``some.x``).
    for platform in SOCIAL_HOSTS:
        suffix = f"{platform}.com"
        if host == suffix or host.endswith(f".{suffix}"):
            return platform
    return None


# ---------------------------------------------------------------------------
# Bulk fetch (crawlee)
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _classify_error(error: Exception) -> EnrichmentStatus:
    """Map a crawlee failed-request exception to our status vocabulary."""

    err_name = type(error).__name__
    err_msg = str(error)

    # Crawlee SessionError on 403/429 → blocked.
    lowered = err_msg.lower()
    if "403" in err_msg or "429" in err_msg or "blocked" in lowered:
        return "blocked"
    if "404" in err_msg or "410" in err_msg:
        return "not_found"
    if "timeout" in err_name.lower() or "timeout" in lowered:
        return "timeout"
    return "error"


async def enrich_urls(
    urls: list[str],
    *,
    budget_s: float = 4.0,
    respect_robots: bool = True,
) -> dict[str, EnrichmentResult]:
    """Fetch ``urls`` with one crawlee crawler. Returns dict keyed by input URL.

    Inputs are normalised before dispatch; entries that fail
    normalisation get a synthetic ``error`` envelope. Crawlee owns
    concurrency, retry, and session rotation.
    """

    try:
        from crawlee.crawlers import (
            BeautifulSoupCrawler,
            BeautifulSoupCrawlingContext,
        )
        from crawlee.http_clients import ImpitHttpClient
    except ImportError as exc:
        raise RuntimeError(
            "crawlee is required for enrichment. Run `uv sync`."
        ) from exc

    results: dict[str, EnrichmentResult] = {}
    normalised: dict[str, str] = {}  # normalised → original input
    to_crawl: list[str] = []

    for url in urls:
        n = normalise_website_url(url)
        if n is None:
            results[url] = EnrichmentResult(
                status="no_url" if not (url and url.strip()) else "error",
                meta={
                    "fetched_at": _now_iso(),
                    "user_agent": USER_AGENT,
                    "robots_respected": respect_robots,
                    "requested_url": url,
                    **({"error": "invalid_url"} if (url and url.strip()) else {}),
                },
            )
            continue
        normalised[n] = url
        if n not in to_crawl:
            to_crawl.append(n)

    if not to_crawl:
        return results

    impit_timeout_s = max(IMPIT_TIMEOUT_FLOOR_S, float(budget_s))
    navigation_td = timedelta(seconds=impit_timeout_s)

    crawler = BeautifulSoupCrawler(
        max_requests_per_crawl=len(to_crawl) + 10,
        max_request_retries=2,
        request_handler_timeout=__as_timedelta(budget_s),
        navigation_timeout=navigation_td,
        http_client=ImpitHttpClient(timeout=impit_timeout_s),
        ignore_http_error_status_codes=_HANDLED_CODES,
        respect_robots_txt_file=respect_robots,
    )

    @crawler.router.default_handler
    async def _handler(context: BeautifulSoupCrawlingContext) -> None:
        request_url = str(context.request.url)
        original = normalised.get(request_url, request_url)
        final_url = (
            getattr(context.request, "loaded_url", None) or request_url
        )
        http_status = getattr(context.http_response, "status_code", None)

        meta_base = {
            "fetched_at": _now_iso(),
            "user_agent": USER_AGENT,
            "robots_respected": respect_robots,
            "requested_url": request_url,
            "final_url": final_url,
            "http_status": http_status,
        }

        if http_status in _NOT_FOUND_CODES:
            results[original] = EnrichmentResult(
                status="not_found", meta=meta_base
            )
            return
        if http_status in _BLOCKED_CODES:
            results[original] = EnrichmentResult(
                status="blocked", meta=meta_base
            )
            return

        signals = extract_signals_from_soup(
            soup=context.soup,
            final_url=final_url,
            http_status=http_status,
        )
        results[original] = EnrichmentResult(
            status="ok", signals=signals, meta=meta_base
        )

    @crawler.failed_request_handler
    async def _failed(context, error: Exception) -> None:
        request_url = str(context.request.url)
        original = normalised.get(request_url, request_url)
        status = _classify_error(error)
        results[original] = EnrichmentResult(
            status=status,
            meta={
                "fetched_at": _now_iso(),
                "user_agent": USER_AGENT,
                "robots_respected": respect_robots,
                "requested_url": request_url,
                "error": f"{type(error).__name__}: {str(error)[:200]}",
            },
        )

    try:
        await crawler.run(to_crawl)
    except Exception:
        logger.exception("crawlee run failed")

    # Backstop — anything crawlee dropped silently.
    for n_url, original in normalised.items():
        if original not in results:
            results[original] = EnrichmentResult(
                status="error",
                meta={
                    "fetched_at": _now_iso(),
                    "user_agent": USER_AGENT,
                    "robots_respected": respect_robots,
                    "requested_url": n_url,
                    "error": "no_result_from_crawlee",
                },
            )
    return results


def __as_timedelta(seconds: float):
    """Convert a float seconds budget to a timedelta (crawlee API)."""

    from datetime import timedelta

    return timedelta(seconds=max(0.5, float(seconds)))


# ---------------------------------------------------------------------------
# Single-URL convenience
# ---------------------------------------------------------------------------


async def enrich_lead(
    *,
    website_url: str | None,
    budget_s: float = 4.0,
    respect_robots: bool = True,
) -> EnrichmentResult:
    """Enrich one website URL. See module docstring for the status vocabulary."""

    if website_url is None or not website_url.strip():
        return EnrichmentResult(
            status="no_url",
            meta={
                "fetched_at": _now_iso(),
                "user_agent": USER_AGENT,
                "robots_respected": respect_robots,
            },
        )

    results = await enrich_urls(
        [website_url],
        budget_s=budget_s,
        respect_robots=respect_robots,
    )
    return results[website_url]


__all__ = [
    "CONNECTOR_NAME",
    "CONNECTOR_VERSION",
    "ENVELOPE_VERSION",
    "IMPIT_TIMEOUT_FLOOR_S",
    "EnrichmentResult",
    "EnrichmentStatus",
    "SOCIAL_HOSTS",
    "USER_AGENT",
    "enrich_lead",
    "enrich_urls",
    "is_social_website",
    "normalise_website_url",
    "persist_enrichment",
]
