"""Shared, dependency-free vocabulary for lead-website enrichment.

Lives in its own module so :mod:`autosdr.enrichment` (the fetcher
+ social-website predicate) and :mod:`autosdr.enrichment_extract`
(the page-body signal extractor) can both import the same constants
without a circular dependency. Adding a concept that needs to be
shared between those two modules belongs here.
"""

from __future__ import annotations

# Closed vocabulary of social platforms we treat as a "no real
# website" signal when the URL itself is on `Lead.website`, and as
# a tracked external link when discovered in the homepage body.
# Adding an eighth platform is one line; the regex in
# :mod:`autosdr.enrichment_extract` and the predicate in
# :func:`autosdr.enrichment.is_social_website` both consume this set.
SOCIAL_HOSTS: frozenset[str] = frozenset(
    {
        "facebook",
        "instagram",
        "linkedin",
        "twitter",
        "x",
        "tiktok",
        "youtube",
    }
)


__all__ = ["SOCIAL_HOSTS"]
