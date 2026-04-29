"""Tests for the lead-website enrichment surface (crawlee-backed).

The previous httpx-MockTransport tests were replaced with pure-parser
tests after the crawlee swap (see plan
``crawlee-only enrichment richer signals``). The parser is the
shared :func:`autosdr.enrichment_extract.extract_signals_from_soup`,
used by both the standalone script and the production scan worker.

Network-touching crawlee integration is deliberately out of scope here
— we cover it via the live-test report in
``data/crawlee-test-report-20260429.md`` and one happy-path integration
test that hits a tiny in-process aiohttp server (added separately if
needed). Keeping the unit suite pure makes the matrix below cheap to
extend as new signals land.
"""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup

from autosdr.enrichment import (
    CONNECTOR_NAME,
    CONNECTOR_VERSION,
    ENVELOPE_VERSION,
    EnrichmentResult,
    normalise_website_url,
    persist_enrichment,
)
from autosdr.enrichment_extract import (
    SNIPPET_CHARS,
    extract_signals_from_soup,
)


WP_HOMEPAGE = """
<!doctype html>
<html lang="en-AU">
  <head>
    <title>Hanley Browne Plumbing — Stafford Heights</title>
    <meta name="description" content="Family-owned emergency plumber, 24/7 callouts.">
    <meta name="viewport" content="width=device-width">
    <meta name="generator" content="WordPress 6.5">
    <meta property="og:image" content="https://example.com.au/cover.jpg">
    <meta property="og:title" content="Hanley Browne Plumbing">
    <meta property="og:description" content="24/7 Brisbane plumbers">
    <meta property="og:site_name" content="Hanley Browne">
    <link rel="icon" href="/favicon.ico">
    <link rel="canonical" href="https://example.com.au/">
    <script type="application/ld+json">{}</script>
  </head>
  <body>
    <h1>24/7 plumbing in Brisbane</h1>
    <p>ABN 12 345 678 901. Call us now.</p>
    <a href="https://www.facebook.com/hanleybrowne">Facebook</a>
    <a href="mailto:hi@hanleybrowne.com.au">Email</a>
    <a href="tel:+61400000000">Call</a>
    <a href="/about">About</a>
    <a href="https://supplier.example.com/">Supplier</a>
    <script src="/wp-content/themes/hanley/main.js"></script>
    <footer>© 2026 Hanley Browne Plumbing</footer>
  </body>
</html>
"""


def _parse(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


# ---------------------------------------------------------------------------
# extract_signals_from_soup — kitchen-sink shape
# ---------------------------------------------------------------------------


def test_extract_signals_full_homepage() -> None:
    soup = _parse(WP_HOMEPAGE)
    signals = extract_signals_from_soup(
        soup=soup,
        final_url="https://example.com.au/",
        http_status=200,
    )

    # original, prompt-visible signals
    assert signals["title"] == "Hanley Browne Plumbing — Stafford Heights"
    assert signals["h1"] == "24/7 plumbing in Brisbane"
    assert signals["meta_description"].startswith("Family-owned")
    assert signals["viewport_present"] is True
    assert signals["og_image_present"] is True
    assert signals["favicon_present"] is True
    assert signals["is_https"] is True
    assert signals["external_links_to_socials"] == [
        "https://www.facebook.com/hanleybrowne",
    ]
    assert signals["cms"] == "wordpress"
    assert signals["cms_evidence"].startswith("<meta name=\"generator\"")

    # kitchen-sink additions
    assert signals["http_status"] == 200
    assert signals["word_count"] > 0
    assert isinstance(signals["text_snippet"], str)
    assert len(signals["text_snippet"]) <= SNIPPET_CHARS
    assert "ABN 12 345 678 901" in signals["text_snippet"]
    assert "<" not in signals["text_snippet"]  # HTML stripped
    assert signals["lang"] == "en-AU"
    assert signals["canonical_url"] == "https://example.com.au/"
    assert signals["copyright_year"] == "2026"
    assert signals["abn"] == "12345678901"
    assert signals["acn"] == ""
    assert signals["email_addresses"] == ["hi@hanleybrowne.com.au"]
    assert signals["phone_numbers"] == ["+61400000000"]
    assert signals["script_block_count"] >= 2
    assert signals["jsonld_present"] is True
    assert signals["og_title"] == "Hanley Browne Plumbing"
    assert signals["og_description"] == "24/7 Brisbane plumbers"
    assert signals["og_site_name"] == "Hanley Browne"
    assert signals["internal_link_count"] >= 1  # /about
    assert signals["external_link_count"] >= 1  # supplier.example.com


def test_extract_signals_empty_html_yields_safe_defaults() -> None:
    signals = extract_signals_from_soup(
        soup=_parse("<html></html>"),
        final_url="https://example.com/",
        http_status=200,
    )
    assert signals["title"] == ""
    assert signals["h1"] == ""
    assert signals["word_count"] == 0
    assert signals["text_snippet"] == ""
    assert signals["abn"] == ""
    assert signals["acn"] == ""
    assert signals["email_addresses"] == []
    assert signals["phone_numbers"] == []
    assert signals["external_links_to_socials"] == []
    assert signals["jsonld_present"] is False


def test_extract_signals_acn_when_present() -> None:
    html = """
    <html><body><p>Trading entity ACN 123 456 789. </p></body></html>
    """
    signals = extract_signals_from_soup(
        soup=_parse(html), final_url="https://example.com/", http_status=200
    )
    assert signals["acn"] == "123456789"


@pytest.mark.parametrize(
    "html, expected_cms",
    [
        ('<html><head><meta name="generator" content="Wix.com Website Builder"></head><body><h1>x</h1></body></html>', "wix"),
        ('<html><head><meta name="generator" content="Squarespace"></head><body><h1>x</h1></body></html>', "squarespace"),
        ('<html><body><script src="https://cdn.shopify.com/s/foo.js"></script><h1>x</h1></body></html>', "shopify"),
        ('<html><body><img src="https://static.wixstatic.com/foo.png"><h1>x</h1></body></html>', "wix"),
        ('<html><body><img src="https://img1.wsimg.com/foo.png"><h1>x</h1></body></html>', "godaddy"),
    ],
)
def test_extract_signals_cms_detection(html: str, expected_cms: str) -> None:
    signals = extract_signals_from_soup(
        soup=_parse(html), final_url="https://example.com/", http_status=200
    )
    assert signals["cms"] == expected_cms


def test_extract_signals_truncates_lists() -> None:
    socials = "".join(
        f'<a href="https://www.facebook.com/page{i}">x</a>' for i in range(20)
    )
    signals = extract_signals_from_soup(
        soup=_parse(f"<html><body>{socials}</body></html>"),
        final_url="https://example.com/",
        http_status=200,
    )
    assert len(signals["external_links_to_socials"]) <= 6


def test_extract_signals_internal_external_link_buckets() -> None:
    html = (
        '<html><body>'
        '<a href="/page-a">a</a>'
        '<a href="https://www.example.com/page-b">b</a>'
        '<a href="https://other.example/page-c">c</a>'
        '<a href="mailto:hi@x.com">e</a>'
        '<a href="#anchor">x</a>'
        '</body></html>'
    )
    signals = extract_signals_from_soup(
        soup=_parse(html),
        final_url="https://www.example.com/",
        http_status=200,
    )
    assert signals["internal_link_count"] == 2
    assert signals["external_link_count"] == 1


# ---------------------------------------------------------------------------
# normalise_website_url
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("example.com", "https://example.com"),
        ("https://example.com/", "https://example.com/"),
        ("  example.com.au  ", "https://example.com.au"),
        ("", None),
        (None, None),
        ("ftp://example.com", None),
        ("not a url", None),
    ],
)
def test_normalise_website_url(raw, expected) -> None:
    assert normalise_website_url(raw) == expected


# ---------------------------------------------------------------------------
# Envelope shape + persist_enrichment
# ---------------------------------------------------------------------------


def test_to_envelope_carries_connector_metadata() -> None:
    result = EnrichmentResult(
        status="ok",
        signals={"title": "x"},
        meta={"fetched_at": "2026-04-29T00:00:00+00:00", "http_status": 200},
    )
    env = result.to_envelope()
    assert env["_meta"]["version"] == ENVELOPE_VERSION
    assert env["_meta"]["connector"] == CONNECTOR_NAME
    assert env["_meta"]["connector_version"] == CONNECTOR_VERSION
    assert env["_meta"]["status"] == "ok"
    assert env["_meta"]["http_status"] == 200
    assert env["signals"] == {"title": "x"}


def test_persist_enrichment_writes_columns_and_blob() -> None:
    class _Lead:
        raw_data = {"foo": "bar"}
        enrichment_status = None
        enrichment_fetched_at = None

    lead = _Lead()
    result = EnrichmentResult(
        status="not_found",
        meta={"fetched_at": "2026-04-29T01:23:45+00:00", "http_status": 404},
    )

    # ``flag_modified`` requires SQLAlchemy state — patch by monkey-patching
    # only the call, since this is a stand-in object. We test the value
    # writes; the actual ORM flagging is exercised by the integration tests.
    import autosdr.enrichment as _e

    _e.flag_modified = lambda obj, attr: None

    persist_enrichment(lead, result)

    assert lead.enrichment_status == "not_found"
    assert lead.raw_data["foo"] == "bar"
    assert lead.raw_data["enrichment"]["_meta"]["status"] == "not_found"
    assert lead.enrichment_fetched_at is not None
