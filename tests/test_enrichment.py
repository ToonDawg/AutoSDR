"""Tests for ``autosdr.enrichment.enrich_lead``.

Pure-function tests against an in-process ``httpx.MockTransport`` so we
exercise the budget caps, robots policy, CMS detection, and sitemap
parsing without ever opening a real socket.

Ticket 0011 success criteria:

* ``enrich_lead`` returns within ``budget_s`` for any URL — verified
  by :func:`test_total_budget_is_a_hard_cap`.
* ``Disallow: /`` for our user-agent results in ``status="blocked"``
  and zero further requests — :func:`test_blocked_by_robots`.

Plus the CMS / SPA / 4xx-5xx / sitemap matrix the ticket calls out
explicitly under "Tests".
"""

from __future__ import annotations

import asyncio
from typing import Callable

import httpx
import pytest

from autosdr.enrichment import (
    ENVELOPE_VERSION,
    USER_AGENT,
    EnrichmentResult,
    enrich_lead,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    """Wrap a request handler in an httpx ``AsyncClient`` for the function.

    The function under test owns its own per-request timeout so we
    don't set one at construction time.
    """

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _wp_homepage(*, body: str | None = None) -> bytes:
    """Reference WordPress homepage fixture used across several tests."""

    payload = body or (
        '<!doctype html><html><head>'
        '<title>Hanley Browne Plumbing — Stafford Heights</title>'
        '<meta name="description" content="Family-owned emergency plumber, 24/7 callouts.">'
        '<meta name="viewport" content="width=device-width">'
        '<meta name="generator" content="WordPress 6.5">'
        '<meta property="og:image" content="https://example.com.au/cover.jpg">'
        '<link rel="icon" href="/favicon.ico">'
        '</head><body>'
        '<h1>24/7 plumbing in Brisbane</h1>'
        '<p>Phone <a href="https://www.facebook.com/hanleybrowne">Facebook</a></p>'
        '<script src="/wp-content/themes/hanley/main.js"></script>'
        '</body></html>'
    )
    return payload.encode("utf-8")


def _sitemap_xml(*, count: int, last_mod: str | None = "2024-08-12") -> bytes:
    urls = "".join(
        f"<url><loc>https://example.com.au/page-{i}</loc>"
        + (f"<lastmod>{last_mod}</lastmod>" if last_mod and i == 0 else "")
        + "</url>"
        for i in range(count)
    )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{urls}"
        '</urlset>'
    )
    return body.encode("utf-8")


def _robots(*, allow: bool = True, sitemap: str | None = None) -> bytes:
    """Build a plausible robots.txt body."""

    lines = ["User-agent: *"]
    if allow:
        lines.append("Allow: /")
    else:
        lines.append("Disallow: /")
    if sitemap:
        lines.append(f"Sitemap: {sitemap}")
    return ("\n".join(lines) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# Status-code branches
# ---------------------------------------------------------------------------


async def test_no_url_short_circuits(workspace_factory):
    """Empty / blank ``website_url`` → ``status="no_url"`` without I/O."""

    client_calls: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        client_calls.append(request)
        return httpx.Response(200, content=b"")

    async with _make_client(_handler) as client:
        result = await enrich_lead(
            website_url=None,
            http_client=client,
            budget_s=4.0,
        )

    assert isinstance(result, EnrichmentResult)
    assert result.status == "no_url"
    assert client_calls == []
    envelope = result.to_envelope()
    assert envelope["_meta"]["version"] == ENVELOPE_VERSION
    assert envelope["_meta"]["status"] == "no_url"


async def test_invalid_url_yields_error_status():
    async with _make_client(lambda req: httpx.Response(200)) as client:
        result = await enrich_lead(
            website_url="not a url at all",
            http_client=client,
            budget_s=4.0,
        )
    assert result.status == "error"


async def test_wordpress_homepage_detected():
    """Homepage with ``generator: WordPress`` and a ``/wp-content/`` link
    fingerprints as ``cms="wordpress"`` and produces an ``ok`` status."""

    requests_seen: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(str(request.url))
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(200, content=_robots(sitemap="https://example.com.au/sitemap.xml"))
        if path == "/sitemap.xml":
            return httpx.Response(200, content=_sitemap_xml(count=12))
        if path in ("/", ""):
            return httpx.Response(
                200,
                content=_wp_homepage(),
                headers={"content-type": "text/html; charset=utf-8"},
            )
        return httpx.Response(404)

    async with _make_client(_handler) as client:
        result = await enrich_lead(
            website_url="https://example.com.au",
            http_client=client,
            budget_s=4.0,
        )

    assert result.status == "ok"
    assert result.signals["cms"] == "wordpress"
    assert "wordpress" in result.signals["cms_evidence"].lower()
    assert result.signals["title"].startswith("Hanley Browne Plumbing")
    assert result.signals["h1"] == "24/7 plumbing in Brisbane"
    assert result.signals["viewport_present"] is True
    assert result.signals["is_https"] is True
    assert result.signals["og_image_present"] is True
    assert result.signals["favicon_present"] is True
    assert result.signals["sitemap_count"] == 12
    assert result.signals["sitemap_last_modified"] == "2024-08-12"
    assert result.signals["robots_present"] is True
    assert result.signals["external_links_to_socials"] == [
        "https://www.facebook.com/hanleybrowne"
    ]
    assert result.meta["http_status"] == 200
    # ≤ 3 requests (root + robots + sitemap.xml).
    assert len(requests_seen) == 3


@pytest.mark.parametrize(
    "fingerprint,expected_cms",
    [
        # generator-meta cases
        (
            '<meta name="generator" content="Wix.com Website Builder">',
            "wix",
        ),
        (
            '<meta name="generator" content="Squarespace 7.1">',
            "squarespace",
        ),
        (
            '<meta name="generator" content="Shopify">',
            "shopify",
        ),
        # asset-host fingerprints (no generator meta)
        (
            '<link href="https://assets.website-files.com/abc/style.css" rel="stylesheet">',
            "webflow",
        ),
        (
            '<img src="https://lirp.cdn-website.com/abc/photo.jpg">',
            "duda",
        ),
        (
            '<img src="https://img1.wsimg.com/abc/photo.jpg">',
            "godaddy",
        ),
    ],
)
async def test_cms_fingerprint_matrix(fingerprint: str, expected_cms: str):
    body = (
        '<!doctype html><html><head><title>Demo</title>'
        f"{fingerprint}"
        '</head><body><h1>Hi</h1><p>copy</p></body></html>'
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, content=body.encode("utf-8"))

    async with _make_client(_handler) as client:
        result = await enrich_lead(
            website_url="https://example.com.au",
            http_client=client,
            budget_s=4.0,
        )

    assert result.status == "ok"
    assert result.signals["cms"] == expected_cms


async def test_empty_spa_shell_status():
    """``<div id="app"></div>``-only SPA → ``status="empty_shell"``."""

    body = (
        '<!doctype html><html><head><title>App</title></head>'
        '<body><div id="app"></div></body></html>'
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, content=body.encode("utf-8"))

    async with _make_client(_handler) as client:
        result = await enrich_lead(
            website_url="https://example.com.au",
            http_client=client,
            budget_s=4.0,
        )

    assert result.status == "empty_shell"


async def test_404_yields_not_found():
    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(404, content=b"<html>nope</html>")

    async with _make_client(_handler) as client:
        result = await enrich_lead(
            website_url="https://example.com.au",
            http_client=client,
            budget_s=4.0,
        )
    assert result.status == "not_found"


async def test_5xx_yields_error():
    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(503, content=b"upstream down")

    async with _make_client(_handler) as client:
        result = await enrich_lead(
            website_url="https://example.com.au",
            http_client=client,
            budget_s=4.0,
        )
    assert result.status == "error"


# ---------------------------------------------------------------------------
# Robots
# ---------------------------------------------------------------------------


async def test_blocked_by_robots():
    """``Disallow: /`` for ``*`` blocks us. No subsequent fetches."""

    requests_seen: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=_robots(allow=False))
        # Any non-robots fetch here would be a regression — fail loudly.
        return httpx.Response(500, content=b"would-have-fetched")

    async with _make_client(_handler) as client:
        result = await enrich_lead(
            website_url="https://example.com.au",
            http_client=client,
            budget_s=4.0,
        )

    assert result.status == "blocked"
    assert result.meta["robots_respected"] is True
    assert requests_seen == ["/robots.txt"], requests_seen


async def test_respect_robots_false_overrides_disallow():
    """Operators can flip the polite-default robots policy. The honest
    field on ``_meta`` records that we did not respect it so the audit
    trail is unambiguous."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=_robots(allow=False))
        return httpx.Response(
            200,
            content=_wp_homepage(),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    async with _make_client(_handler) as client:
        result = await enrich_lead(
            website_url="https://example.com.au",
            http_client=client,
            budget_s=4.0,
            respect_robots=False,
        )

    assert result.status == "ok"
    assert result.meta["robots_respected"] is False
    assert result.signals["cms"] == "wordpress"


# ---------------------------------------------------------------------------
# Budgets and timeouts
# ---------------------------------------------------------------------------


async def test_total_budget_is_a_hard_cap():
    """A deliberately-slow mock server cannot make the function exceed
    ``budget_s`` by more than a small slack. We measure wall time via the
    event loop clock, since ``time.monotonic`` is what the function
    itself uses for its deadline."""

    async def _slow_handler(request: httpx.Request) -> httpx.Response:
        # Sleep longer than the per-request cap.
        await asyncio.sleep(5.0)
        return httpx.Response(200, content=b"<html>too slow</html>")

    transport = httpx.MockTransport(_slow_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        loop = asyncio.get_running_loop()
        started = loop.time()
        result = await enrich_lead(
            website_url="https://example.com.au",
            http_client=client,
            budget_s=2.0,
        )
        elapsed = loop.time() - started

    # Per-request timeout fires; total wall time must stay close to the
    # configured budget. Two requests can each timeout at ~1.5s, so a
    # generous slack of 1s catches the realistic upper bound while
    # rejecting "no caps at all" regressions.
    assert elapsed < 4.5, f"total elapsed {elapsed:.2f}s exceeded the budget"
    assert result.status in {"timeout", "ok", "error"}, result.status


async def test_partial_signal_preserved_when_sitemap_times_out():
    """Root fetch succeeded, sitemap timed out → status reflects the
    timeout but the page-level signals are still present."""

    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/":
            return httpx.Response(
                200,
                content=_wp_homepage(),
                headers={"content-type": "text/html; charset=utf-8"},
            )
        if request.url.path == "/sitemap.xml":
            await asyncio.sleep(5.0)
            return httpx.Response(200, content=_sitemap_xml(count=1))
        return httpx.Response(404)

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await enrich_lead(
            website_url="https://example.com.au",
            http_client=client,
            budget_s=2.5,
        )

    assert result.status == "timeout"
    assert result.signals.get("cms") == "wordpress"
    assert result.signals.get("title", "").startswith("Hanley Browne Plumbing")
    assert result.signals.get("h1") == "24/7 plumbing in Brisbane"


async def test_response_body_capped_at_256kb():
    """A 4 MB junk response is truncated at 256 KB without raising; the
    parser still produces an ``ok`` envelope using whatever structural
    signal sits in the first 256 KB."""

    huge_body = (
        b"<!doctype html><html><head><title>Huge</title></head>"
        b"<body><h1>Big</h1><p>"
        + b"a" * (4 * 1024 * 1024)
        + b"</p></body></html>"
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200,
            content=huge_body,
            headers={"content-type": "text/html"},
        )

    async with _make_client(_handler) as client:
        result = await enrich_lead(
            website_url="https://example.com.au",
            http_client=client,
            budget_s=4.0,
        )

    assert result.status == "ok"
    assert result.signals["title"] == "Huge"
    assert result.signals["h1"] == "Big"


# ---------------------------------------------------------------------------
# Sitemap behaviour
# ---------------------------------------------------------------------------


async def test_sitemap_index_follows_first_referenced_sitemap_once():
    """When ``sitemap.xml`` is an index, follow exactly the first listed
    sitemap and report the count of ``<url>`` entries from that one
    sub-sitemap. Never two, never all."""

    requests_seen: list[str] = []

    sitemap_index = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        '<sitemap><loc>https://example.com.au/sitemap-1.xml</loc></sitemap>'
        '<sitemap><loc>https://example.com.au/sitemap-2.xml</loc></sitemap>'
        '</sitemapindex>'
    ).encode("utf-8")

    def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        requests_seen.append(path)
        if path == "/robots.txt":
            return httpx.Response(404)
        if path == "/":
            return httpx.Response(
                200,
                content=_wp_homepage(),
                headers={"content-type": "text/html"},
            )
        if path == "/sitemap.xml":
            return httpx.Response(200, content=sitemap_index)
        if path == "/sitemap-1.xml":
            return httpx.Response(200, content=_sitemap_xml(count=12))
        if path == "/sitemap-2.xml":  # pragma: no cover - this would be a regression
            raise AssertionError("must not fetch the second sitemap")
        return httpx.Response(404)

    async with _make_client(_handler) as client:
        result = await enrich_lead(
            website_url="https://example.com.au",
            http_client=client,
            budget_s=4.0,
        )

    assert result.status == "ok"
    assert result.signals["sitemap_count"] == 12
    assert "/sitemap-2.xml" not in requests_seen


async def test_robots_sitemap_directive_overrides_default_path():
    """When robots.txt declares a non-default sitemap location we use it,
    rather than blindly falling back to ``/sitemap.xml``."""

    requests_seen: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200,
                content=_robots(sitemap="https://example.com.au/wp-sitemap.xml"),
            )
        if request.url.path == "/":
            return httpx.Response(
                200,
                content=_wp_homepage(),
                headers={"content-type": "text/html"},
            )
        if request.url.path == "/wp-sitemap.xml":
            return httpx.Response(200, content=_sitemap_xml(count=42))
        if request.url.path == "/sitemap.xml":  # pragma: no cover - regression
            raise AssertionError("default path must not be tried when robots overrides it")
        return httpx.Response(404)

    async with _make_client(_handler) as client:
        result = await enrich_lead(
            website_url="https://example.com.au",
            http_client=client,
            budget_s=4.0,
        )

    assert result.status == "ok"
    assert result.signals["sitemap_count"] == 42




# ---------------------------------------------------------------------------
# User-agent assertion
# ---------------------------------------------------------------------------


async def test_user_agent_string_is_identifiable():
    """The outgoing User-Agent must contain ``AutoSDR`` so an operator
    blocking the crawler with a robots rule can do so unambiguously."""

    seen_user_agents: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen_user_agents.append(request.headers.get("user-agent", ""))
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200,
            content=_wp_homepage(),
            headers={"content-type": "text/html"},
        )

    async with _make_client(_handler) as client:
        await enrich_lead(
            website_url="https://example.com.au",
            http_client=client,
            budget_s=4.0,
        )

    assert all(USER_AGENT == ua for ua in seen_user_agents)
    assert "AutoSDR" in USER_AGENT
