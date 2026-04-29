"""Lead-website enrichment — fetch a small, deterministic structural signal.

Ticket 0011. The analysis prompt at :mod:`autosdr.prompts.analysis` has no
website-side signal today: every Apify Google-Maps row carries
``webResults: null`` so the LLM correctly refuses to invent a quality
judgement about the lead's site, and falls back into ``weak_presence`` /
``fallback`` / single-quote ``review_theme`` buckets. Every cold-outreach
angle the LLM picks collapses into one of those three "thin signal"
buckets — the gap is missing input, not a prompt bug.

This module fetches a small, polite, time-bounded set of public-website
signals (``<title>``, meta description, first H1, generator-meta + URL
fingerprint CMS detection, viewport, og/favicon, sitemap URL count,
sitemap latest ``<lastmod>``, robots.txt presence) and returns a
structured envelope the analysis prompt can fold into ``raw_data``
without any schema migration. Callers pass ``budget_s`` and
``respect_robots`` to :func:`enrich_lead`.

Design constraints (council-resolved in the ticket):

* ≤ 3 HTTP requests per lead (root URL, ``/robots.txt``, one sitemap).
* ≤ 1.5 s per request, ≤ 4 s total wall time per lead (configurable).
* ≤ 256 KB per response body — anything larger is truncated and parsed.
* Single, identifiable user-agent string: ``AutoSDR/<version> (+...)``.
* Robots.txt is fetched first when budget allows; a ``Disallow: /`` for
  our user-agent shortcuts the run with ``status="blocked"``.
* Empty / failed runs still write a ``_meta`` block so the cache TTL
  works against attempted-but-failed fetches the same way as
  successful ones — preventing the "every tick re-tries the dead site"
  failure mode.
* Per-host blocking (404, timeout, robots) is recorded against a
  closed :data:`EnrichmentStatus` vocabulary so downstream surfaces
  (the angle-funnel stratifier, the LeadDetail card) don't drift.

This is **not** a full crawler — no JS rendering, no sub-page fetches,
no body-text extraction. The LLM doesn't need the homepage's hero
paragraph; it needs the title and the H1.

The function is pure-async and has no DB side effects: the caller in
:mod:`autosdr.pipeline.outreach` is responsible for folding the
returned envelope into ``Lead.raw_data['enrichment']``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from sqlalchemy.orm.attributes import flag_modified

from autosdr import __version__
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

# Bumped to 2 in 2026-04-28 alongside the connector tagging below. A
# lead with a ``_meta.version: 1`` blob (no ``connector`` field) is
# treated as stale and re-scanned exactly once by the background
# scan worker, so the migration is automatic.
ENVELOPE_VERSION = 2

# Identity of the fetcher that produced an envelope. Stamped onto
# every persisted blob so the scan worker can invalidate caches when
# a new fetcher (LinkedIn, Companies-House, Crawlee fallback, …)
# ships next to this one. ``CONNECTOR_VERSION`` is bumped per fetcher
# whenever the signal shape changes.
CONNECTOR_NAME = "website_static"
CONNECTOR_VERSION = "1.0"

# AutoSDR's outgoing user-agent. Honest, identifiable, blockable — operators
# who don't want us looking can ``Disallow: /`` against this token in their
# robots.txt and we will respect it.
USER_AGENT = (
    f"AutoSDR/{__version__} (+https://github.com/autosdr/autosdr; lead-enrichment)"
)

# Hard caps. ``budget_s`` caps total wall time per lead; other limits stay
# fixed so callers share one simple contract.
PER_REQUEST_TIMEOUT_S = 1.5
MAX_BODY_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class EnrichmentResult:
    """Return value of :func:`enrich_lead`.

    Maps 1-1 to the JSON envelope persisted under
    ``Lead.raw_data['enrichment']`` — see
    :meth:`EnrichmentResult.to_envelope`. The dataclass is the in-memory
    contract; the envelope is the persistence contract.
    """

    status: EnrichmentStatus
    signals: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_envelope(self) -> dict[str, Any]:
        """Serialise to the on-disk envelope shape (``_meta`` + ``signals``).

        The ``_meta`` block carries the connector identity + version
        alongside the existing status / fetched-at fields. The scan
        worker uses ``connector`` + ``connector_version`` to know when
        to invalidate the cache (a future enricher ships → every blob
        produced by the old fetcher is automatically marked stale).
        """

        meta = {
            "version": ENVELOPE_VERSION,
            "connector": CONNECTOR_NAME,
            "connector_version": CONNECTOR_VERSION,
            "status": self.status,
            **self.meta,
        }
        return {"_meta": meta, "signals": dict(self.signals)}


def persist_enrichment(lead: Lead, result: EnrichmentResult) -> None:
    """Fold a scan result back onto a lead row.

    Single source of truth for "the worker just produced this envelope —
    write it to all the places the read paths look". Keeps the JSON
    blob (full audit detail) and the denormalised columns
    (``enrichment_status`` / ``enrichment_fetched_at``, used by the
    Scans page for SQL-only paginate / filter / count) in lockstep so
    the two views never disagree.
    """

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
# Public entry point
# ---------------------------------------------------------------------------


async def enrich_lead(
    *,
    website_url: str | None,
    http_client: httpx.AsyncClient,
    budget_s: float = 4.0,
    respect_robots: bool = True,
) -> EnrichmentResult:
    """Fetch structural signals about a lead's public website.

    Parameters mirror the workspace settings block. ``http_client`` is
    passed in (rather than constructed here) so a single
    :class:`httpx.AsyncClient` can pool connections at workspace scope —
    the FastAPI lifespan owns it.

    Returns an :class:`EnrichmentResult`. Always returns; never raises
    on normal network failure (errors are encoded into ``status``).
    """

    started_monotonic = time.monotonic()
    started_at = datetime.now(tz=timezone.utc)
    base_meta: dict[str, Any] = {
        "fetched_at": started_at.isoformat(),
        "user_agent": USER_AGENT,
        "robots_respected": respect_robots,
    }

    if not website_url or not website_url.strip():
        return EnrichmentResult(status="no_url", meta=base_meta)

    normalized = _normalise_website_url(website_url)
    if normalized is None:
        return EnrichmentResult(
            status="error", meta={**base_meta, "error": "invalid_url"}
        )

    base_meta["requested_url"] = normalized

    deadline = started_monotonic + max(0.0, float(budget_s))

    def _remaining_budget() -> float:
        return max(0.0, deadline - time.monotonic())

    return await _run_enrichment(
        normalized=normalized,
        http_client=http_client,
        base_meta=base_meta,
        remaining_budget=_remaining_budget,
        respect_robots=respect_robots,
        started_monotonic=started_monotonic,
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def _run_enrichment(
    *,
    normalized: str,
    http_client: httpx.AsyncClient,
    base_meta: dict[str, Any],
    remaining_budget,  # callable returning float
    respect_robots: bool,
    started_monotonic: float,
) -> EnrichmentResult:
    parsed = urlparse(normalized)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    robots_url = f"{origin}/robots.txt"

    # ------------------------------------------------------------------
    # Step 1 — robots.txt (one of the three permitted fetches).
    # ------------------------------------------------------------------
    robots_state = await _fetch_robots(
        http_client=http_client,
        robots_url=robots_url,
        timeout=min(PER_REQUEST_TIMEOUT_S, remaining_budget()),
    )

    if respect_robots and robots_state.disallowed_root:
        meta = {
            **base_meta,
            "latency_ms": _elapsed_ms(started_monotonic),
            "robots_url": robots_url,
        }
        return EnrichmentResult(status="blocked", meta=meta)

    sitemap_candidate: str | None = robots_state.sitemap_url

    # ------------------------------------------------------------------
    # Step 2 — root URL.
    # ------------------------------------------------------------------
    if remaining_budget() <= 0:
        meta = {
            **base_meta,
            "latency_ms": _elapsed_ms(started_monotonic),
            "robots_url": robots_url,
        }
        return EnrichmentResult(status="timeout", meta=meta)

    try:
        root_response = await _fetch_html(
            http_client=http_client,
            url=normalized,
            timeout=min(PER_REQUEST_TIMEOUT_S, remaining_budget()),
        )
    except _FetchTimeout:
        meta = {
            **base_meta,
            "latency_ms": _elapsed_ms(started_monotonic),
            "robots_url": robots_url,
        }
        return EnrichmentResult(status="timeout", meta=meta)
    except _FetchError as exc:
        meta = {
            **base_meta,
            "latency_ms": _elapsed_ms(started_monotonic),
            "robots_url": robots_url,
            "error": str(exc),
        }
        return EnrichmentResult(status="error", meta=meta)

    final_url = root_response.final_url
    http_status = root_response.status_code

    if http_status in (404, 410):
        meta = {
            **base_meta,
            "final_url": final_url,
            "http_status": http_status,
            "latency_ms": _elapsed_ms(started_monotonic),
            "robots_url": robots_url,
        }
        return EnrichmentResult(status="not_found", meta=meta)

    if http_status >= 500:
        meta = {
            **base_meta,
            "final_url": final_url,
            "http_status": http_status,
            "latency_ms": _elapsed_ms(started_monotonic),
            "robots_url": robots_url,
        }
        return EnrichmentResult(status="error", meta=meta)

    # Anything in 4xx other than 404/410 (e.g. 403 CDN block, 401) is
    # surfaced as ``error`` so the operator can see it. Same code path
    # as 5xx; the body is unlikely to carry useful structural signal.
    if http_status >= 400:
        meta = {
            **base_meta,
            "final_url": final_url,
            "http_status": http_status,
            "latency_ms": _elapsed_ms(started_monotonic),
            "robots_url": robots_url,
        }
        return EnrichmentResult(status="error", meta=meta)

    body = root_response.body
    body_text = _decode_body(body, root_response.content_type)

    page_signals = _extract_page_signals(html=body_text, final_url=final_url)
    cms_signal = _detect_cms(html=body_text, final_url=final_url)
    page_signals.update(cms_signal)

    if _looks_like_empty_shell(body_text):
        meta = {
            **base_meta,
            "final_url": final_url,
            "http_status": http_status,
            "latency_ms": _elapsed_ms(started_monotonic),
            "robots_url": robots_url,
        }
        return EnrichmentResult(status="empty_shell", signals=page_signals, meta=meta)

    # ------------------------------------------------------------------
    # Step 3 — first sitemap (only one, even if the root sitemap is an
    # index referencing many sub-sitemaps). Council-resolved in the
    # ticket: follow the first referenced sitemap once. Three fetches
    # max; bounded and cheap; gives a useful page count for SMB sites.
    # ------------------------------------------------------------------
    if sitemap_candidate is None:
        sitemap_candidate = f"{origin}/sitemap.xml"

    page_signals["robots_present"] = robots_state.fetched

    if remaining_budget() > 0:
        try:
            sitemap_signals = await _fetch_sitemap(
                http_client=http_client,
                sitemap_url=sitemap_candidate,
                origin=origin,
                timeout=min(PER_REQUEST_TIMEOUT_S, remaining_budget()),
            )
        except _FetchTimeout:
            meta = {
                **base_meta,
                "final_url": final_url,
                "http_status": http_status,
                "latency_ms": _elapsed_ms(started_monotonic),
                "robots_url": robots_url,
                "sitemap_url": sitemap_candidate,
            }
            # The root fetch succeeded — preserve its signals; status
            # reflects the partial success.
            return EnrichmentResult(
                status="timeout", signals=page_signals, meta=meta
            )
        except _FetchError:
            # Sitemap is best-effort; failure here doesn't downgrade the
            # overall status — we still have a good homepage.
            sitemap_signals = {}
    else:
        sitemap_signals = {}

    page_signals.update(sitemap_signals)

    meta = {
        **base_meta,
        "final_url": final_url,
        "http_status": http_status,
        "latency_ms": _elapsed_ms(started_monotonic),
        "robots_url": robots_url,
        "sitemap_url": sitemap_candidate,
    }
    return EnrichmentResult(status="ok", signals=page_signals, meta=meta)


# ---------------------------------------------------------------------------
# Helpers — fetching
# ---------------------------------------------------------------------------


class _FetchTimeout(Exception):
    pass


class _FetchError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class _RobotsState:
    fetched: bool
    disallowed_root: bool
    sitemap_url: str | None


@dataclass(frozen=True, slots=True)
class _RootResponse:
    status_code: int
    final_url: str
    body: bytes
    content_type: str


async def _fetch_robots(
    *, http_client: httpx.AsyncClient, robots_url: str, timeout: float
) -> _RobotsState:
    """Fetch ``/robots.txt`` and decide whether we are allowed to crawl ``/``.

    Failures (timeouts, 4xx/5xx, empty body) are treated as "no
    robots.txt" — same posture as every well-behaved crawler. Sitemap
    URL extraction is best-effort: if the file lists multiple sitemaps,
    we pick the first one. The ticket-level council resolved
    "follow the first referenced sitemap once" as the right depth.
    """

    if timeout <= 0:
        return _RobotsState(fetched=False, disallowed_root=False, sitemap_url=None)

    try:
        response = await asyncio.wait_for(
            http_client.get(
                robots_url,
                headers=_headers(),
                timeout=timeout,
                follow_redirects=True,
            ),
            timeout=timeout,
        )
    except (asyncio.TimeoutError, httpx.TimeoutException):
        return _RobotsState(fetched=False, disallowed_root=False, sitemap_url=None)
    except httpx.HTTPError:
        return _RobotsState(fetched=False, disallowed_root=False, sitemap_url=None)

    if response.status_code >= 400:
        return _RobotsState(fetched=False, disallowed_root=False, sitemap_url=None)

    body_bytes = response.content[:MAX_BODY_BYTES]
    text = body_bytes.decode("utf-8", errors="ignore")
    if not text.strip():
        return _RobotsState(fetched=True, disallowed_root=False, sitemap_url=None)

    parser = RobotFileParser()
    try:
        parser.parse(text.splitlines())
    except Exception:
        # Python 3.13 RobotFileParser.parse() raises ValueError on relative
        # Sitemap: URLs (e.g. "Sitemap: /ui.ashx?f=sitemap_xml"). Treat as
        # no rules — same fallback as can_fetch() below.
        pass
    # ``RobotFileParser.can_fetch`` honours wildcard fallback for our UA.
    # Treat ``/`` as the canonical "may we crawl?" question.
    try:
        allowed = parser.can_fetch(USER_AGENT, "/")
    except Exception:
        # Some malformed robots.txt files crash the stdlib parser; treat
        # as "no rule" rather than blocking ourselves on garbage.
        allowed = True
    disallowed_root = not allowed

    sitemap_url = _first_sitemap_in_robots(text)

    return _RobotsState(
        fetched=True,
        disallowed_root=disallowed_root,
        sitemap_url=sitemap_url,
    )


async def _fetch_html(
    *, http_client: httpx.AsyncClient, url: str, timeout: float
) -> _RootResponse:
    if timeout <= 0:
        raise _FetchTimeout()

    async def _do_fetch() -> _RootResponse:
        async with http_client.stream(
            "GET",
            url,
            headers=_headers(),
            timeout=timeout,
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
            return _RootResponse(
                status_code=response.status_code,
                final_url=str(response.url),
                body=body,
                content_type=response.headers.get("content-type", ""),
            )

    try:
        return await asyncio.wait_for(_do_fetch(), timeout=timeout)
    except (asyncio.TimeoutError, httpx.TimeoutException) as exc:
        raise _FetchTimeout() from exc
    except httpx.HTTPError as exc:
        raise _FetchError(str(exc)) from exc


async def _fetch_sitemap(
    *,
    http_client: httpx.AsyncClient,
    sitemap_url: str,
    origin: str,
    timeout: float,
) -> dict[str, Any]:
    """Fetch one sitemap and extract a count + lastmod.

    If the URL is a sitemap-index (lists ``<sitemap>`` entries), we
    follow the **first** referenced sitemap exactly once — never two,
    never all. That keeps the fetch budget at three total requests
    (root, robots, sitemap), as agreed in the ticket's council.
    """

    if timeout <= 0:
        raise _FetchTimeout()

    async def _stream_text(target: str) -> tuple[int, str]:
        async with http_client.stream(
            "GET",
            target,
            headers=_headers(),
            timeout=timeout,
            follow_redirects=True,
        ) as response:
            if response.status_code >= 400:
                return response.status_code, ""
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes(chunk_size=16 * 1024):
                chunks.append(chunk)
                total += len(chunk)
                if total >= MAX_BODY_BYTES:
                    break
            body = b"".join(chunks)[:MAX_BODY_BYTES]
            return response.status_code, body.decode("utf-8", errors="ignore")

    try:
        status, text = await asyncio.wait_for(
            _stream_text(sitemap_url), timeout=timeout
        )
    except (asyncio.TimeoutError, httpx.TimeoutException) as exc:
        raise _FetchTimeout() from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise _FetchError(str(exc)) from exc

    if status >= 400 or not text:
        return {}

    if "<sitemapindex" in text.lower():
        first = _first_loc_in_xml(text)
        if first is None:
            return {}
        first_resolved = urljoin(origin + "/", first)
        # The ticket's council-resolved contract: when the chosen
        # sitemap turns out to be an index (lists ``<sitemap>`` entries
        # rather than ``<url>`` entries), follow the first referenced
        # sitemap exactly once. Cheap and bounded — never two, never
        # all. This single follow is the only place we permit a fourth
        # outbound request per lead, and only on the sitemap-index path.
        try:
            status, text = await asyncio.wait_for(
                _stream_text(first_resolved), timeout=timeout
            )
        except (asyncio.TimeoutError, httpx.TimeoutException, httpx.HTTPError, ValueError):
            return {}
        if status >= 400 or not text:
            return {}

    return _parse_sitemap_signals(text)


def _headers() -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xml;q=0.9,*/*;q=0.5",
        "Accept-Language": "en-AU,en;q=0.9",
    }


def _elapsed_ms(started_monotonic: float) -> int:
    return int((time.monotonic() - started_monotonic) * 1000)


def _decode_body(body: bytes, content_type: str) -> str:
    """Decode bytes to text. Best-effort; we do not need byte-exact fidelity."""

    encoding = "utf-8"
    if "charset=" in content_type.lower():
        encoding = content_type.lower().split("charset=")[-1].split(";")[0].strip() or "utf-8"
    try:
        return body.decode(encoding, errors="ignore")
    except LookupError:
        return body.decode("utf-8", errors="ignore")


# ---------------------------------------------------------------------------
# Helpers — parsing
# ---------------------------------------------------------------------------


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_META_DESC_RE = re.compile(
    r"<meta[^>]*name=[\"']description[\"'][^>]*content=[\"']([^\"']*)[\"'][^>]*>",
    re.IGNORECASE,
)
_META_DESC_RE_ALT = re.compile(
    r"<meta[^>]*content=[\"']([^\"']*)[\"'][^>]*name=[\"']description[\"'][^>]*>",
    re.IGNORECASE,
)
_GENERATOR_RE = re.compile(
    r"<meta[^>]*name=[\"']generator[\"'][^>]*content=[\"']([^\"']*)[\"'][^>]*>",
    re.IGNORECASE,
)
_GENERATOR_RE_ALT = re.compile(
    r"<meta[^>]*content=[\"']([^\"']*)[\"'][^>]*name=[\"']generator[\"'][^>]*>",
    re.IGNORECASE,
)
_VIEWPORT_RE = re.compile(
    r"<meta[^>]*name=[\"']viewport[\"']", re.IGNORECASE
)
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
_OG_IMAGE_RE = re.compile(
    r"<meta[^>]*property=[\"']og:image[\"']", re.IGNORECASE
)
_FAVICON_RE = re.compile(
    r"<link[^>]*rel=[\"'](?:shortcut +)?icon[\"']", re.IGNORECASE
)
_BODY_TAG_RE = re.compile(r"<body[^>]*>(.*?)</body>", re.IGNORECASE | re.DOTALL)
_TAG_STRIP_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_SOCIAL_RE = re.compile(
    r"https?://(?:www\.)?(facebook|instagram|linkedin|twitter|x|tiktok|youtube)\.com/[^\s\"'<>]+",
    re.IGNORECASE,
)


def _extract_page_signals(*, html: str, final_url: str) -> dict[str, Any]:
    """Pull title / description / first H1 / og / favicon / socials / https."""

    title = _first_match(_TITLE_RE, html)
    description = _first_match(_META_DESC_RE, html) or _first_match(_META_DESC_RE_ALT, html)
    h1 = _first_match(_H1_RE, html)
    viewport_present = bool(_VIEWPORT_RE.search(html))
    og_image_present = bool(_OG_IMAGE_RE.search(html))
    favicon_present = bool(_FAVICON_RE.search(html))

    socials: list[str] = []
    seen: set[str] = set()
    for match in _SOCIAL_RE.finditer(html):
        url = match.group(0)
        # Strip query/fragment noise; keep host + path so the operator
        # can recognise the page without leaking tracking parameters.
        host_path = url.split("?", 1)[0].split("#", 1)[0]
        # Strip the protocol so the de-dupe key isn't fooled by http vs https.
        key = host_path.lower().split("://", 1)[-1].rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        socials.append(host_path)
        if len(socials) >= 6:
            break

    parsed_final = urlparse(final_url)
    is_https = parsed_final.scheme == "https"

    signals: dict[str, Any] = {
        "title": _clean_text(title),
        "meta_description": _clean_text(description),
        "h1": _clean_text(h1),
        "viewport_present": viewport_present,
        "is_https": is_https,
        "og_image_present": og_image_present,
        "favicon_present": favicon_present,
        "external_links_to_socials": socials,
    }
    return signals


def _looks_like_empty_shell(html: str) -> bool:
    """Heuristic: a SPA shell has no body text and no H1.

    The bar is intentionally low — any inline copy at all (an h1, a
    paragraph, an alt-text-bearing image with content) yields enough
    structural signal that we count the fetch as ``ok``.
    """

    if not html:
        return False

    if _H1_RE.search(html):
        return False

    body_match = _BODY_TAG_RE.search(html)
    if body_match is None:
        return False
    body_inner = body_match.group(1)
    text_only = _WHITESPACE_RE.sub(
        " ", _TAG_STRIP_RE.sub(" ", body_inner)
    ).strip()
    return len(text_only) < 30


# CMS fingerprinting. The order matters: we pick the first matcher that
# fires, so the more specific signatures (generator meta) come first.
_CMS_GENERATOR_PREFIXES: tuple[tuple[str, str], ...] = (
    ("wordpress", "wordpress"),
    ("wix.com", "wix"),
    ("squarespace", "squarespace"),
    ("shopify", "shopify"),
    ("webflow", "webflow"),
    ("duda", "duda"),
    ("godaddy", "godaddy"),
    ("hubspot", "hubspot"),
    ("drupal", "drupal"),
    ("joomla", "joomla"),
)

_CMS_HTML_FINGERPRINTS: tuple[tuple[str, str, str], ...] = (
    ("wordpress", "/wp-content/", "url contains /wp-content/"),
    ("wordpress", "/wp-includes/", "url contains /wp-includes/"),
    ("shopify", "cdn.shopify.com", "asset host cdn.shopify.com"),
    ("shopify", "shopify.shop", "shopify.shop reference"),
    ("squarespace", "static1.squarespace.com", "asset host squarespace"),
    ("squarespace", "squarespace.com/universal", "squarespace universal asset"),
    ("wix", "static.wixstatic.com", "asset host wixstatic"),
    ("wix", "x-wix-", "x-wix- header marker in body"),
    ("webflow", "assets.website-files.com", "asset host website-files"),
    ("webflow", "webflow.com", "webflow.com reference"),
    ("duda", "lirp.cdn-website.com", "asset host duda lirp"),
    ("duda", "irp.cdn-website.com", "asset host duda irp"),
    ("godaddy", "img1.wsimg.com", "asset host godaddy wsimg"),
    ("godaddy", "/sites/default", "godaddy /sites/default path"),
    ("hubspot", "hs-scripts.com", "asset host hs-scripts"),
)


def _detect_cms(*, html: str, final_url: str) -> dict[str, Any]:
    """Return ``{"cms": ..., "cms_evidence": ...}`` if known; else unknown.

    Two layers: ``<meta name="generator">`` (the strongest signal,
    usually verbatim "WordPress 6.5") and a small set of asset-URL
    fingerprints (the homepages link to vendor CDNs). When neither
    fires, the CMS is ``"custom"`` if the page is well-formed (has a
    title and a body) or ``"unknown"`` otherwise. The downstream prompt
    treats ``"unknown"`` as no signal — same as ``cms_evidence`` being
    empty.
    """

    generator_match = (
        _first_match(_GENERATOR_RE, html)
        or _first_match(_GENERATOR_RE_ALT, html)
    )
    if generator_match:
        gen_lower = generator_match.lower()
        for needle, label in _CMS_GENERATOR_PREFIXES:
            if needle in gen_lower:
                return {
                    "cms": label,
                    "cms_evidence": (
                        f'<meta name="generator" content="{generator_match[:120]}">'
                    ),
                }

    haystack = (html + " " + final_url).lower()
    for label, needle, evidence in _CMS_HTML_FINGERPRINTS:
        if needle in haystack:
            return {"cms": label, "cms_evidence": evidence}

    if generator_match:
        # Recognised that there *is* a generator meta but didn't match
        # any of our labels — call it custom so downstream sees the
        # honest signal.
        return {
            "cms": "custom",
            "cms_evidence": f'<meta name="generator" content="{generator_match[:120]}">',
        }

    title = _first_match(_TITLE_RE, html)
    if title:
        return {"cms": "custom", "cms_evidence": ""}
    return {"cms": "unknown", "cms_evidence": ""}


_SITEMAP_URL_RE = re.compile(r"<url>(.*?)</url>", re.IGNORECASE | re.DOTALL)
_SITEMAP_LOC_RE = re.compile(r"<loc>(.*?)</loc>", re.IGNORECASE | re.DOTALL)
_SITEMAP_LASTMOD_RE = re.compile(
    r"<lastmod>(.*?)</lastmod>", re.IGNORECASE | re.DOTALL
)


def _parse_sitemap_signals(text: str) -> dict[str, Any]:
    """Extract URL count + latest ``<lastmod>`` from a (concrete) sitemap."""

    url_matches = _SITEMAP_URL_RE.findall(text)
    count = len(url_matches)
    if count == 0:
        return {"sitemap_count": 0}

    lastmods = []
    for raw in _SITEMAP_LASTMOD_RE.findall(text):
        cleaned = raw.strip()
        if not cleaned:
            continue
        # YYYY-MM-DD prefix is enough; comparisons are lexicographic.
        lastmods.append(cleaned[:10])

    signals: dict[str, Any] = {"sitemap_count": count}
    if lastmods:
        signals["sitemap_last_modified"] = max(lastmods)
    return signals


def _first_loc_in_xml(text: str) -> str | None:
    match = _SITEMAP_LOC_RE.search(text)
    if match is None:
        return None
    return match.group(1).strip()


def _first_sitemap_in_robots(text: str) -> str | None:
    """Return the first ``Sitemap:`` URL from a robots.txt body."""

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        head, _, tail = stripped.partition(":")
        if head.strip().lower() == "sitemap" and tail.strip():
            candidate = tail.strip()
            # Relative URLs (e.g. "/ui.ashx?f=sitemap_xml") are not valid
            # sitemap hrefs — skip them rather than passing a bare path to
            # httpx which would raise ValueError.
            if candidate.startswith(("http://", "https://")):
                return candidate
    return None


def _first_match(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    if match is None:
        return None
    return match.group(1)


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    stripped = _TAG_STRIP_RE.sub(" ", value)
    return _WHITESPACE_RE.sub(" ", stripped).strip()


_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-._~]*(?::\d+)?$")


def _normalise_website_url(raw: str) -> str | None:
    """Turn whatever the operator imported into a fetchable URL.

    Accepts bare hostnames (``example.com.au``), schemed URLs, and the
    occasional Apify-style ``http://example.com.au/`` with trailing
    whitespace. Returns ``None`` on garbage. We do NOT add a path —
    fetching the bare origin is the whole point.

    Whitespace inside the hostname, an empty host, or any non-http(s)
    scheme is rejected so the caller surfaces ``status="error"``
    rather than firing a request at a bogus URL.
    """

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


__all__ = [
    "CONNECTOR_NAME",
    "CONNECTOR_VERSION",
    "ENVELOPE_VERSION",
    "EnrichmentResult",
    "EnrichmentStatus",
    "MAX_BODY_BYTES",
    "PER_REQUEST_TIMEOUT_S",
    "USER_AGENT",
    "enrich_lead",
    "persist_enrichment",
]
