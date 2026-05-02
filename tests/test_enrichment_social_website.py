"""Truth-table tests for ``is_social_website`` and the shared social vocab.

Ticket 0014 makes a social profile (Facebook page, LinkedIn page,
etc.) sitting in ``Lead.website`` a priority signal. The picker
(``_next_queued_leads``) and the API serialiser (``LeadOut`` /
``CampaignOut.queued_priority_count``) both depend on
:func:`autosdr.enrichment.is_social_website` agreeing with the
homepage extractor's ``_SOCIAL_RE`` on what counts as a social
host. These tests pin both: the host detection truth table
(positives, negatives, malformed input) and the cross-module
vocab invariant (extractor regex tracks ``SOCIAL_HOSTS``).
"""

from __future__ import annotations

import pytest

from autosdr.enrichment import SOCIAL_HOSTS, is_social_website
from autosdr.enrichment_extract import _SOCIAL_RE
from autosdr.enrichment_vocab import SOCIAL_HOSTS as VOCAB_SOCIAL_HOSTS


@pytest.mark.parametrize(
    "url,expected",
    [
        # Bare hostnames + scheme variants → matched by platform.
        ("https://facebook.com/Acme", "facebook"),
        ("http://facebook.com/Acme", "facebook"),
        ("https://www.facebook.com/Acme", "facebook"),
        ("https://m.facebook.com/Acme", "facebook"),
        ("https://www.linkedin.com/company/acme", "linkedin"),
        ("https://linkedin.com/in/jdoe", "linkedin"),
        ("https://www.instagram.com/acme/", "instagram"),
        ("https://twitter.com/acme", "twitter"),
        ("https://x.com/acme", "x"),
        ("https://www.tiktok.com/@acme", "tiktok"),
        ("https://youtube.com/@acme", "youtube"),
        ("https://www.youtube.com/channel/UC123", "youtube"),
        # Schemeless input — the predicate must not require https://.
        ("facebook.com/Acme", "facebook"),
        ("www.linkedin.com/in/jdoe", "linkedin"),
        # Trailing whitespace from messy CSV imports.
        ("  https://facebook.com/Acme  ", "facebook"),
    ],
)
def test_is_social_website_positives(url: str, expected: str) -> None:
    assert is_social_website(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        # Real corporate websites that mention socials in path/query
        # must NOT trigger — host-only match.
        "https://acme.com/facebook-ads",
        "https://acme.com.au/?ref=linkedin",
        "https://acme.com/about/our-tiktok-strategy",
        # Look-alike / suffix collisions.
        "https://notfacebook.com/foo",  # different registrable host
        "https://facebookish.com/foo",
        # Country-code TLDs are not in scope (no .com.au lookup).
        "https://facebook.com.au/foo",
        # Empty / missing / garbage.
        None,
        "",
        "   ",
        "://",
        "ftp://facebook.com/",  # urlparse parses but scheme is wrong;
                                 # we still match host though — see note.
    ],
)
def test_is_social_website_negatives(url: str | None) -> None:
    # Note: ``ftp://facebook.com/`` is allowed today — host is still
    # ``facebook.com``. The predicate is host-shaped, not
    # scheme-shaped. If we ever care, tighten in a follow-up.
    if url == "ftp://facebook.com/":
        assert is_social_website(url) == "facebook"
        return
    assert is_social_website(url) is None


def test_vocab_is_re_exported_from_enrichment() -> None:
    """``autosdr.enrichment.SOCIAL_HOSTS`` must alias the vocab module.

    Existing call sites import from ``autosdr.enrichment``; the move
    of the literal set into ``autosdr.enrichment_vocab`` is a refactor,
    not a rename of the public symbol.
    """

    assert SOCIAL_HOSTS is VOCAB_SOCIAL_HOSTS


@pytest.mark.parametrize("platform", sorted(VOCAB_SOCIAL_HOSTS))
def test_extract_regex_tracks_vocab(platform: str) -> None:
    """Adding a platform to ``SOCIAL_HOSTS`` must light up ``_SOCIAL_RE``.

    Without this invariant the homepage extractor and the
    ``Lead.website`` predicate could drift — leads with a
    LinkedIn URL would be flagged as priority but the homepage
    body would silently stop reporting LinkedIn links (or
    vice-versa).
    """

    sample = f"<a href='https://{platform}.com/handle'>x</a>"
    match = _SOCIAL_RE.search(sample)
    assert match is not None, f"_SOCIAL_RE missing platform {platform!r}"
    assert match.group(1).lower() == platform
