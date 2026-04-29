"""Pure signal extraction from a parsed homepage.

Shared between the standalone scripts/enrich_leads.py runner and the
production scan worker (:mod:`autosdr.enrichment`). The function takes
a ``BeautifulSoup`` document and returns the kitchen-sink dict that is
folded into ``Lead.raw_data['enrichment']['signals']``.

Design notes:

* Pure / no I/O — easy to unit-test against fixture HTML.
* Best-effort: every field has a sensible empty default. Callers can
  treat ``""`` / ``0`` / ``[]`` as "absent" without sprinkling None
  checks.
* CMS detection lives here too so the script and worker agree on
  fingerprints. The matchers were ported from the previous
  ``autosdr.enrichment`` regex set; the BeautifulSoup re-write is a
  superset (more reliable on minified/odd HTML).
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

# Maximum stripped body chars we keep in the snippet. The analysis
# prompt doesn't want raw HTML; a 300-char taste of the homepage is
# usually enough to disambiguate a real business from a placeholder.
SNIPPET_CHARS = 300

# Cap list-shaped signals so a noisy homepage can't blow up the JSON
# blob size.
MAX_SOCIAL_LINKS = 6
MAX_EMAIL_ADDRESSES = 6
MAX_PHONE_NUMBERS = 6


# ---------------------------------------------------------------------------
# CMS fingerprints — same vocabulary as the original enrichment.py
# ---------------------------------------------------------------------------


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


_SOCIAL_RE = re.compile(
    r"https?://(?:www\.)?(facebook|instagram|linkedin|twitter|x|tiktok|youtube)\.com/[^\s\"'<>]+",
    re.IGNORECASE,
)
_WHITESPACE_RE = re.compile(r"\s+")
_COPYRIGHT_RE = re.compile(
    r"(?:©|&copy;|\(c\)|copyright)\s*(\d{4})", re.IGNORECASE
)
# ABN: 11 digits (often spaced "11 222 333 444"). ACN: 9 digits.
_ABN_RE = re.compile(r"\bABN[:\s]*((?:\d[\s-]?){10}\d)\b", re.IGNORECASE)
_ACN_RE = re.compile(r"\bACN[:\s]*((?:\d[\s-]?){8}\d)\b", re.IGNORECASE)


def extract_signals_from_soup(
    *,
    soup: BeautifulSoup,
    final_url: str,
    http_status: int | None = None,
) -> dict[str, Any]:
    """Return the kitchen-sink signal dict for one homepage.

    All keys are always present so downstream consumers (UI, prompt,
    SQL JSON-extract on the Scans page) can rely on the shape.
    """

    # Normalise the URL we'll use for internal/external link bucketing.
    final_host = urlparse(final_url).hostname or ""
    final_host_clean = final_host.lower().lstrip("www.")

    # ---- title / description / h1 -----------------------------------
    title = _text_of(soup.find("title"))
    h1 = _text_of(soup.find("h1"))
    meta_description = _meta_content(soup, name="description")

    # ---- structural meta --------------------------------------------
    viewport_present = bool(soup.find("meta", attrs={"name": _ci("viewport")}))
    og_image_present = bool(
        soup.find("meta", attrs={"property": _ci("og:image")})
    )
    favicon_present = _has_favicon(soup)

    og_title = _meta_property(soup, "og:title")
    og_description = _meta_property(soup, "og:description")
    og_site_name = _meta_property(soup, "og:site_name")

    canonical_url = _link_href(soup, rel="canonical")

    html_tag = soup.find("html")
    lang = ""
    if html_tag is not None:
        lang_attr = html_tag.get("lang")
        if isinstance(lang_attr, str):
            lang = lang_attr.strip()

    is_https = urlparse(final_url).scheme == "https"

    # ---- CMS ---------------------------------------------------------
    cms, cms_evidence = _detect_cms(soup=soup, final_url=final_url)

    # ---- body text, snippet, word count -----------------------------
    raw_text = soup.get_text(separator=" ", strip=True)
    body_text = _WHITESPACE_RE.sub(" ", raw_text).strip()
    word_count = len(body_text.split()) if body_text else 0
    text_snippet = body_text[:SNIPPET_CHARS]

    # ---- copyright year, ABN, ACN -----------------------------------
    copyright_year = ""
    years = _COPYRIGHT_RE.findall(body_text)
    if years:
        # Latest year mentioned on the homepage.
        copyright_year = max(years)

    abn = _first_normalised_digits(_ABN_RE, body_text)
    acn = _first_normalised_digits(_ACN_RE, body_text)

    # ---- emails / phones from anchors -------------------------------
    email_addresses = _hrefs_with_prefix(
        soup, prefix="mailto:", limit=MAX_EMAIL_ADDRESSES
    )
    phone_numbers = _hrefs_with_prefix(
        soup, prefix="tel:", limit=MAX_PHONE_NUMBERS
    )

    # ---- script / style blocks --------------------------------------
    script_block_count = len(soup.find_all("script"))
    style_block_count = len(soup.find_all("style"))
    jsonld_present = bool(
        soup.find_all("script", attrs={"type": "application/ld+json"})
    )

    # ---- internal vs external links ---------------------------------
    internal_link_count, external_link_count = _bucket_links(
        soup=soup,
        final_url=final_url,
        final_host_clean=final_host_clean,
    )

    # ---- socials -----------------------------------------------------
    socials = _extract_socials(soup)

    signals: dict[str, Any] = {
        # original signals (kept for backward-compat with the prompt + UI)
        "title": title,
        "h1": h1,
        "meta_description": meta_description,
        "viewport_present": viewport_present,
        "og_image_present": og_image_present,
        "favicon_present": favicon_present,
        "is_https": is_https,
        "external_links_to_socials": socials,
        "cms": cms,
        "cms_evidence": cms_evidence,
        # kitchen-sink additions
        "http_status": http_status if http_status is not None else 0,
        "word_count": word_count,
        "text_snippet": text_snippet,
        "lang": lang,
        "canonical_url": canonical_url,
        "copyright_year": copyright_year,
        "abn": abn,
        "acn": acn,
        "email_addresses": email_addresses,
        "phone_numbers": phone_numbers,
        "script_block_count": script_block_count,
        "style_block_count": style_block_count,
        "internal_link_count": internal_link_count,
        "external_link_count": external_link_count,
        "jsonld_present": jsonld_present,
        "og_title": og_title,
        "og_description": og_description,
        "og_site_name": og_site_name,
    }
    return signals


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _ci(value: str):
    """Case-insensitive attribute matcher for BeautifulSoup."""

    needle = value.lower()
    return lambda v: isinstance(v, str) and v.lower() == needle


def _text_of(tag) -> str:
    if tag is None:
        return ""
    return _WHITESPACE_RE.sub(" ", tag.get_text(" ", strip=True)).strip()


def _meta_content(soup: BeautifulSoup, *, name: str) -> str:
    tag = soup.find("meta", attrs={"name": _ci(name)})
    if tag is None:
        return ""
    val = tag.get("content", "")
    return val.strip() if isinstance(val, str) else ""


def _meta_property(soup: BeautifulSoup, prop: str) -> str:
    tag = soup.find("meta", attrs={"property": _ci(prop)})
    if tag is None:
        return ""
    val = tag.get("content", "")
    return val.strip() if isinstance(val, str) else ""


def _link_href(soup: BeautifulSoup, *, rel: str) -> str:
    tag = soup.find(
        "link", attrs={"rel": lambda r: r and rel in (r if isinstance(r, list) else [r])}
    )
    if tag is None:
        # rel may be a single string token; bs4 sometimes presents it as a list.
        tag = soup.find("link", attrs={"rel": rel})
    if tag is None:
        return ""
    href = tag.get("href", "")
    return href.strip() if isinstance(href, str) else ""


def _has_favicon(soup: BeautifulSoup) -> bool:
    for tag in soup.find_all("link"):
        rel = tag.get("rel")
        if not rel:
            continue
        rels = rel if isinstance(rel, list) else [rel]
        for r in rels:
            if isinstance(r, str) and "icon" in r.lower():
                return True
    return False


def _first_normalised_digits(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text)
    if match is None:
        return ""
    digits = re.sub(r"[\s-]", "", match.group(1))
    return digits


def _hrefs_with_prefix(
    soup: BeautifulSoup, *, prefix: str, limit: int
) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for tag in soup.find_all("a", href=True):
        href = tag.get("href", "")
        if not isinstance(href, str):
            continue
        href = href.strip()
        if not href.lower().startswith(prefix):
            continue
        value = href[len(prefix):].split("?", 1)[0].strip()
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
        if len(out) >= limit:
            break
    return out


def _bucket_links(
    *, soup: BeautifulSoup, final_url: str, final_host_clean: str
) -> tuple[int, int]:
    internal = 0
    external = 0
    for tag in soup.find_all("a", href=True):
        href = tag.get("href", "")
        if not isinstance(href, str):
            continue
        href = href.strip()
        if not href or href.startswith("#"):
            continue
        if href.lower().startswith(("mailto:", "tel:", "javascript:")):
            continue
        try:
            absolute = urljoin(final_url, href)
            host = (urlparse(absolute).hostname or "").lower().lstrip("www.")
        except Exception:
            continue
        if not host:
            continue
        if host == final_host_clean or host.endswith("." + final_host_clean):
            internal += 1
        else:
            external += 1
    return internal, external


def _extract_socials(soup: BeautifulSoup) -> list[str]:
    haystack = str(soup)
    out: list[str] = []
    seen: set[str] = set()
    for match in _SOCIAL_RE.finditer(haystack):
        url = match.group(0)
        host_path = url.split("?", 1)[0].split("#", 1)[0]
        key = host_path.lower().split("://", 1)[-1].rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        out.append(host_path)
        if len(out) >= MAX_SOCIAL_LINKS:
            break
    return out


def _detect_cms(*, soup: BeautifulSoup, final_url: str) -> tuple[str, str]:
    generator = _meta_content(soup, name="generator")
    if generator:
        gen_lower = generator.lower()
        for needle, label in _CMS_GENERATOR_PREFIXES:
            if needle in gen_lower:
                return (
                    label,
                    f'<meta name="generator" content="{generator[:120]}">',
                )

    haystack = (str(soup) + " " + final_url).lower()
    for label, needle, evidence in _CMS_HTML_FINGERPRINTS:
        if needle in haystack:
            return label, evidence

    if generator:
        return (
            "custom",
            f'<meta name="generator" content="{generator[:120]}">',
        )

    if soup.find("title"):
        return "custom", ""
    return "unknown", ""


__all__ = [
    "extract_signals_from_soup",
    "SNIPPET_CHARS",
]
