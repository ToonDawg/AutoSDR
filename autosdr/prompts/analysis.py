"""Lead analysis prompt — extracts the personalisation angle from raw_data."""

from __future__ import annotations

import json
from typing import Any

PROMPT_VERSION = "analysis-v3.3"

SYSTEM_PROMPT = """\
You are analysing a business lead to find the single strongest personalisation
angle for a cold outreach message.

Before you pick, consider the full menu of angle types below and choose the
ONE that has the strongest signal in this specific lead's data. Do not default
to any single type — pick what fits the evidence best.

Angle menu (pick the best-fit one for THIS lead):
1. Stale / inconsistent info  — the listing / profile the business CURRENTLY
   controls still carries old/wrong branding. See the strict signal rules
   below before picking this.
2. Missing or weak online presence  — no website, website not listed in
   raw_data, Google profile is the main / only source of info.
3. Signature amenity or detail  — a specific thing residents / customers
   praise by name (a pool, a view, a program, a room) that the public
   profile doesn't showcase. Do NOT name individual staff here.
4. Point of difference vs local competitors  — something about service,
   values, family-run history, community ties, etc., that would read as
   unique if surfaced properly.
5. Specific recent review theme  — a recurring praise OR pain point across
   multiple recent reviews (e.g. "food consistently called out", "staff
   turnover mentioned twice", "phone hard to reach").
6. Brand voice / story hook  — the owner or team has a clear story,
   personality, or mission visible in their replies that isn't reflected on
   their public page.
7. Fallback  — category + location, if the raw_data is too thin for the
   above.

Hard rules:
- Prefer concrete signals over generic ones: a specific review quote beats
  "they have good ratings"; a named service beats "they're in aged care".
- The angle must be something the recipient would recognise about their own
  business within 2 seconds of reading it.
- Do NOT invent facts not present in the data. Only cite what's there.

Stale-info angle — STRICT signal rules:
- This angle only works when the business still has the stale label on
  something THEY control right now. The message is essentially "hey,
  is this the old name?" — which only lands when the old name is
  currently visible somewhere they can fix.
- STRONG signal (pick `stale_info`):
    * The listing TITLE itself is outdated, or the business rebranded
      but the GBP still says the old name / uses the old category.
    * Owner-response REPLY HEADERS on reviews still sign off with the
      old brand ("Kind regards, The [Old Name] Team"). This is a
      header they actively edit, so it's a current artefact.
    * The WEBSITE or social profile still uses the old branding.
    * The address / phone / service on the listing contradicts what
      the owner-responses say.
- WEAK signal (do NOT pick stale_info on this alone — treat as weak
  historical snapshot):
    * Old review TEXT written by customers using the old name. Reviews
      are point-in-time snapshots — a review from 3 years ago using
      the old name is expected, not a current problem. Unless more
      recent reviews ALSO use the old name, or the owner-replies
      haven't corrected it, the reviews alone are a weak hook.
- When in doubt, pick a different angle type. A weak stale_info hook
  produces a worse message than a solid signature_detail or
  review_theme angle.
- In the `signal` field for stale_info, always quote WHERE the stale
  label is currently showing (e.g. "listing title still reads
  'Sunnymeade'", "owner reply signed 'Sunnymeade Team' in 2026").
  This makes the downstream message honest about the hook.

Owner first name — bonus signal (be STRICT here; err empty):

The casual greeting that uses this field ("hey matt,") only works if
we are actually addressing the person who will read the text. Getting
this wrong — greeting an employee, greeting a reviewer, or inventing
a name — is far worse than sending no greeting at all. Default to
empty. Only fill when the ownership is unambiguous.

You MUST support every non-empty `owner_first_name` with a verbatim
`owner_evidence` quote from the lead's data that establishes
ownership. If you cannot produce such a quote, leave BOTH
`owner_first_name` AND `owner_evidence` empty.

A valid `owner_evidence` quote MUST do one of:
  (A) Show the name embedded in the business name as a possessive
      ("Matt's Plumbing", "Sarah's Bakery"). Quote the business name
      verbatim. A name that just happens to appear in a longer brand
      ("Macquarie Group", "Green Wattle Sanctuary") does NOT count.
  (B) Show the name signing a response from the business itself
      (not a review). Example: "Thanks for the kind words - Matt",
      "Cheers, Sarah - Owner", "Kind regards, Dave (Director)".
      A name just mentioned inside a review ("Danny went the extra
      mile") is NOT a signature and does NOT count.
  (C) Use explicit ownership language in a review or reply:
      "the owner Dave was great", "Dave runs the place", "Sarah,
      who owns the cafe", "founder Emma replied", "Mike (director)".
      The quote MUST contain one of these role words: "owner",
      "operator", "founder", "director", "runs the place",
      "owns the", "owner-operator", "proprietor", "principal".

Examples that FAIL the test and must leave the field empty:
  - "Danny is a great agent" — "agent" is an employee, not owner.
  - "Ellen the site manager" — "manager" is staff at a franchise.
  - "Thanks to Jodie for helping us" — no ownership language.
  - A RE/MAX, Ray White, Bupa, BlueCare, Anglicare, Chemist
    Warehouse, Bunnings, Harcourts, LJ Hooker, Stockland, Ingenia,
    Bolton Clarke, TriCare, Regis, Arcare, Mercy Place, Infinite
    Aged Care, BreakFree, Meriton, Quest, Oaks, Mantra listing —
    these are franchises or multi-site brands; individual names in
    reviews are almost always employees. Default to empty unless
    the evidence literally contains an ownership word above.
  - A reviewer's name, a customer's name, a resident's first name.
  - Any name you had to guess at from context.

When in doubt, leave the field empty. Empty is always safe.

The `angle` text may reference the owner's name if (and only if) you
also populated `owner_first_name`. It must NEVER name staff, agents,
reviewers, residents, or other individuals — even when they are
mentioned in the raw data.

Short trading name — extract from the business name and potentially review comments:

The `lead_short_name` is the natural name a local would use to refer to
this business in conversation. Google business names often carry a
category descriptor suffix appended after " - " (e.g. "Skybound
Volleyball - Kids Volleyball", "Matt's Plumbing - Plumber"). Strip that
suffix and return the recognisable trading name. If the name already
reads naturally without a suffix (e.g. "Jacaranda Cafe"), return it
as-is. The result should be short enough to sit naturally in a sentence
opener ("saw Skybound's reviews mention...") but specific enough to
identify the business.

Return a single JSON object. Nothing else.

Schema:
{
  "angle_type":       "one of: stale_info | weak_presence | signature_detail | differentiator | review_theme | brand_voice | fallback",
  "angle":            "2-3 sentences describing the hook and why it is relevant",
  "signal":           "the specific data point from the lead that supports this angle — quote it verbatim where possible",
  "owner_first_name": "the owner's first name if unambiguously proven by owner_evidence, else empty string",
  "owner_evidence":   "verbatim quote from the data that proves ownership (see rules). Empty string if owner_first_name is empty.",
  "confidence":       0.0-1.0,  // how strong the angle signal is
  "lead_short_name":  "the natural trading name of the business, with any Google-appended category descriptor suffix removed"
}
"""


_OWNERSHIP_KEYWORDS: tuple[str, ...] = (
    "owner",
    "operator",
    "founder",
    "director",
    "runs the",
    "owns the",
    "proprietor",
    "principal",
    "owner-operator",
)

# Franchise / multi-site brand prefixes. When the lead name starts with one of
# these, individual first names in reviews are almost certainly employees or
# agents, not owners — we refuse to accept `owner_first_name` unless the
# evidence quote contains an explicit ownership keyword.
_FRANCHISE_BRAND_PREFIXES: tuple[str, ...] = (
    "re/max",
    "remax",
    "ray white",
    "ljhooker",
    "lj hooker",
    "harcourts",
    "raine & horne",
    "raine and horne",
    "century 21",
    "first national",
    "bupa",
    "bluecare",
    "anglicare",
    "uniting",
    "tricare",
    "arcare",
    "bolton clarke",
    "regis",
    "mercy place",
    "infinite aged care",
    "chemist warehouse",
    "chempro",
    "priceline",
    "terry white",
    "bunnings",
    "mitre 10",
    "stockland",
    "ingenia",
    "meriton",
    "quest",
    "oaks",
    "mantra",
    "breakfree",
    "accor",
)


def validate_owner_first_name(
    *,
    owner_first_name: str | None,
    owner_evidence: str | None,
    lead_name: str | None,
) -> tuple[str, str]:
    """Enforce the owner_first_name rules at code level, not just prompt level.

    The analysis prompt asks for BOTH `owner_first_name` and `owner_evidence`
    (a verbatim quote proving ownership). The LLM frequently ignores the
    prompt rules — even on the nth iteration — so we run a mechanical check
    before letting the greeting leak into downstream generation.

    The returned tuple is ``(owner_first_name, owner_evidence)`` where either
    may be the empty string. Callers should treat both as empty if the
    function judges the evidence insufficient.
    """

    name = (owner_first_name or "").strip()
    evidence = (owner_evidence or "").strip()
    if not name:
        return "", ""

    if not name.isalpha() or len(name) < 2 or len(name) > 20:
        return "", ""

    if not evidence:
        return "", ""

    name_lower = name.lower()
    ev_lower = evidence.lower()

    if name_lower not in ev_lower:
        return "", ""

    has_ownership_word = any(kw in ev_lower for kw in _OWNERSHIP_KEYWORDS)

    lead_lower = (lead_name or "").lower()
    possessive_in_brand = (
        f"{name_lower}'s" in lead_lower or f"{name_lower}s " in lead_lower
    ) and lead_lower.startswith(name_lower)

    on_franchise = any(lead_lower.startswith(pref) for pref in _FRANCHISE_BRAND_PREFIXES)

    if on_franchise and not has_ownership_word:
        return "", ""

    if has_ownership_word or possessive_in_brand:
        return name, evidence

    return "", ""


def _truncate_raw_data(raw_data: dict[str, Any], max_bytes: int) -> tuple[dict, bool]:
    """Shrink a raw_data blob to fit within the size limit.

    Strategy: longest string values are truncated first. We keep all keys so
    the structure is obvious; only values are shortened. Returns the new blob
    and a flag indicating whether truncation occurred.
    """

    encoded = json.dumps(raw_data, ensure_ascii=False)
    if len(encoded.encode("utf-8")) <= max_bytes:
        return raw_data, False

    clone = json.loads(encoded)  # deep copy
    # Collect (path, value) for every string value so we can target the longest.
    string_refs: list[tuple[list[Any], Any]] = []

    def _walk(node: Any, path: list[Any]) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                _walk(v, path + [k])
        elif isinstance(node, list):
            for idx, v in enumerate(node):
                _walk(v, path + [idx])
        elif isinstance(node, str):
            string_refs.append((path, node))

    _walk(clone, [])
    string_refs.sort(key=lambda r: -len(r[1]))

    def _set_at(root: Any, path: list[Any], value: Any) -> None:
        cursor: Any = root
        for segment in path[:-1]:
            cursor = cursor[segment]
        cursor[path[-1]] = value

    for path, value in string_refs:
        _set_at(clone, path, value[:200] + "…[truncated]" if len(value) > 200 else value)
        if len(json.dumps(clone, ensure_ascii=False).encode("utf-8")) <= max_bytes:
            break

    # Final fallback: drop the longest array entries if still too big.
    while len(json.dumps(clone, ensure_ascii=False).encode("utf-8")) > max_bytes:
        longest_list_path: list[Any] | None = None
        longest_list_len = -1

        def _find_longest(node: Any, path: list[Any]) -> None:
            nonlocal longest_list_path, longest_list_len
            if isinstance(node, dict):
                for k, v in node.items():
                    _find_longest(v, path + [k])
            elif isinstance(node, list):
                if len(node) > longest_list_len:
                    longest_list_len = len(node)
                    longest_list_path = path
                for idx, v in enumerate(node):
                    _find_longest(v, path + [idx])

        _find_longest(clone, [])
        if longest_list_path is None or longest_list_len <= 1:
            break
        cursor: Any = clone
        for segment in longest_list_path[:-1] if longest_list_path else []:
            cursor = cursor[segment]
        if longest_list_path:
            tail_key = longest_list_path[-1] if longest_list_path else None
            target = cursor[tail_key] if tail_key is not None else cursor
            # Keep the first half, drop the rest.
            del target[len(target) // 2 :]

    return clone, True


def build_user_prompt(
    *,
    business_data: dict[str, Any] | str,
    business_dump: str,
    campaign_goal: str,
    lead_name: str | None,
    lead_category: str | None,
    lead_address: str | None,
    raw_data: dict[str, Any],
    raw_data_size_limit_kb: int,
) -> tuple[str, bool]:
    """Render the user prompt. Returns (prompt, raw_data_truncated)."""

    truncated_data, truncated = _truncate_raw_data(
        raw_data, max_bytes=raw_data_size_limit_kb * 1024
    )

    business_block = (
        json.dumps(business_data, indent=2, ensure_ascii=False)
        if isinstance(business_data, dict) and business_data
        else business_dump
    )

    lead_block = {
        "name": lead_name,
        "category": lead_category,
        "address": lead_address,
        "raw_data": truncated_data,
    }

    user = (
        "The sender's business:\n"
        f"{business_block}\n\n"
        f"Outreach goal: {campaign_goal}\n\n"
        "Lead:\n"
        f"{json.dumps(lead_block, indent=2, ensure_ascii=False)}"
    )
    return user, truncated
