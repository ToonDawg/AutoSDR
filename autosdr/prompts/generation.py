"""Message generation prompt — drafts an outreach SMS.

The system prompt is composed from named blocks so future ablation
experiments stay surgical (Phase 3 #9 of
``docs/prompt-audit-2026-05-02.md``):

* :data:`_DEFAULT_TONE` — fallback voice block when the workspace tone
  snapshot is empty.
* :data:`_RULES` — the executable spec: openers, recipient identifier,
  truthfulness, product language, GBP-vs-website, turnaround, empathy,
  credential, CTA, format, punctuation. The bulk of the prompt and the
  contract the evaluator scores against.
* Tone register block — slotted between :data:`_RULES` and
  :data:`_REFERENCE_EXAMPLES` when the caller has a concrete
  :data:`ToneRegisterT` for the lead. Replaces the prose
  ``CATEGORY CALIBRATION`` paragraph that used to live inside ``_RULES``
  (ticket 0017). The register itself is picked **upstream** by the
  analysis LLM as a structured enum field on its JSON output — the
  generation prompt does not infer it. Skipped when ``register is
  None`` or ``"unknown"`` (the analysis model said "I'm not sure"); the
  model then relies on workspace tone + rules + worked examples, which
  is the v8-shaped baseline.
* :data:`_REFERENCE_EXAMPLES` — six worked SMS examples in the target
  voice. Cheap to ablate / replace independently of the rules.

Composing them in :func:`build_system_prompt` keeps the rendered prompt
byte-stable per ``PROMPT_VERSION`` — pinned by a SHA snapshot in
``tests/test_prompts.py``.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from autosdr.prompts._tone import cap_tone_snapshot

# v9 (ticket 0017) — drops the prose CATEGORY CALIBRATION paragraph in
# favour of the per-register block injected between ``_RULES`` and
# ``_REFERENCE_EXAMPLES``. v8 was the byte-stable baseline post Phase 1
# bug fix; v9 is the first prompt-bump since the audit shipped.
PROMPT_VERSION = "generation-v9"


# Closed register vocabulary. Mirrors the ``tone_register`` enum on the
# analysis prompt's JSON output (``analysis-v3.7``); the analysis model
# is the chooser, the generation prompt is the consumer. Adding a
# seventh register means: one literal token here + one
# ``_REGISTER_INSTRUCTIONS`` entry below + one paragraph in the
# analysis prompt's ``_RULES_TONE_REGISTER`` block.
ToneRegisterT = Literal[
    "tradie",
    "professional",
    "hospitality",
    "retail",
    "personal_services",
    "aged_care",
    "unknown",
]


# ---------------------------------------------------------------------------
# Per-register instructional blocks injected into the generation prompt.
#
# Budget: each block aims to stay under 1000 chars so the composed
# prompt (workspace tone + _RULES + register block + examples + closing)
# fits the token envelope of ``generation-v8``. Pinned by
# ``tests/test_prompts.py::test_register_block_fits_under_compose_budget``.
#
# ``"unknown"`` deliberately has no entry — when the analysis model
# returns ``"unknown"`` we skip the block, mirroring the v8 prompt
# shape (rules + workspace tone + worked examples only).
# ---------------------------------------------------------------------------
_REGISTER_INSTRUCTIONS: dict[ToneRegisterT, str] = {
    "tradie": (
        "REGISTER: tradie / hands-on trade.\n"
        "Write the way one tradie texts another. Lowercase opener is the "
        "default ('hey,', 'hey mate,', 'g'day,'). Contractions welcome "
        "('you've', 'i'm', 'wanna'). Drop the final full stop. Sentence "
        "fragments are fine. Don't capitalise the first word of the message "
        "unless it's a proper noun. Do NOT sound like a sales rep — sound "
        "like the bloke down the street who happened to look at their "
        "Google listing. Hype words are out. One exclamation max, only on "
        "the greeting if you use one."
    ),
    "professional": (
        "REGISTER: professional services (legal, financial, consulting, "
        "B2B advisory).\n"
        "Stay casual but keep it tidy. Capital-case opener is fine ('Hi "
        "there,', 'Hello,'). Do NOT use 'hey mate,' or 'g'day,' — they "
        "read as flippant for this category. Capitalise the first word of "
        "sentences and proper nouns. Punctuation should parse cleanly — "
        "drop the trailing full stop only if the message ends on a "
        "question. Contractions are fine. Aim for the voice of a peer "
        "professional sending a quick text, not a brochure or a press "
        "release. No hype words."
    ),
    "hospitality": (
        "REGISTER: hospitality (cafes, restaurants, pubs, food trucks, "
        "catering).\n"
        "Loose and warm — very close to the tradie register. Lowercase "
        "openers ('hey,', 'hey there,'), contractions, dropped final "
        "punctuation. Reference the food / drink / atmosphere when the "
        "signal supports it. Sound like a regular who happened to notice "
        "something, not a marketer. One exclamation max."
    ),
    "retail": (
        "REGISTER: retail (shops, boutiques, services-as-storefront).\n"
        "Loose and casual. Lowercase openers, contractions, dropped final "
        "punctuation are all fine. Reference the storefront or product "
        "category when the signal supports it. Sound like a local "
        "customer who noticed something, not a vendor."
    ),
    "personal_services": (
        "REGISTER: personal services (salons, spas, fitness studios, "
        "yoga, pilates, beauty therapists).\n"
        "Warm and approachable — between tradie and professional. "
        "Lowercase openers OK ('hey,', 'hi there,'), but do NOT use "
        "'hey mate,' or 'g'day,' — those don't fit the register. "
        "Contractions welcome. Punctuation can be loose but proper nouns "
        "(business names, suburbs) stay capitalised. Aim for the voice of "
        "a friendly regular client, not a sales pitch."
    ),
    "aged_care": (
        "REGISTER: aged care, healthcare, allied health, education.\n"
        "Casual but clean. Do NOT use 'hey mate,' or 'g'day,' — they read "
        "as flippant for sectors that care for vulnerable people. "
        "Lowercase openers like 'hi there,', 'hello,' are fine. "
        "Capitalise proper nouns and the first word of sentences. Keep "
        "punctuation tidy. The tone is helpful neighbour, not slick "
        "sales — but precision matters more here than vibe. "
        "Empathy clauses ('must be confusing for families') land "
        "particularly well in this register."
    ),
}


def render_register_block(register: ToneRegisterT | str | None) -> str | None:
    """Return the prompt prose for ``register``, or ``None`` to skip the block.

    The caller (:func:`build_system_prompt`) inserts the returned string
    between ``_RULES`` and ``_REFERENCE_EXAMPLES`` separated by a blank
    line. ``None`` skips the section entirely — this fires on
    ``register is None`` (legacy thread or pre-analysis call),
    ``register == "unknown"`` (analysis model said it doesn't know), and
    any unexpected token (defensive — the analysis prompt's enum guard
    plus the persistence guard in ``outreach.py`` should keep us in
    the closed vocab, but if junk slips through we degrade silently to
    the v8-shaped prompt rather than 500ing).
    """

    if register is None or register == "unknown":
        return None
    return _REGISTER_INSTRUCTIONS.get(register)  # type: ignore[arg-type]


_DEFAULT_TONE = (
    "Write in a casual, direct tone. Keep sentences short. Avoid corporate "
    "language. Open with a grounded observation about the recipient's "
    "situation; close with a single low-pressure question."
)


# ---------------------------------------------------------------------------
# RULES — the executable spec (everything the evaluator scores against).
# ---------------------------------------------------------------------------
_RULES = """\
You are writing a short outreach SMS on behalf of a small independent web
studio owner. The message is going to a local Australian business owner who
has never heard of them.

The feel you're going for: a curious neighbour who happened to look at their
online presence and noticed one specific thing — not a vendor pitching a
product. Story-branded, not salesy. Warm, observant, and actually useful.

Requirements for the message you produce:

OPENING
- PREFERRED: open with a short friendly greeting BEFORE the observation.
  "hey,", "hi there!", "hey mate," — then a comma or a line break, then
  the specific observation. This is the default voice; the bare
  observation-first opener (option (d) below) is the fallback, not the
  target.
- Formal / corporate greetings are out: no "Hope you're well", no
  "Hi [Full Name],", no "Dear [Name]", no "To whom it may concern".
- FORBIDDEN AI-speak opener FORMULAS (vague praise / throat-clearing):
    * "I noticed + generic compliment"
        (e.g. "I noticed your great work" / "I noticed your
        impressive reviews")
    * "I saw + generic compliment"
    * "I came across your business and wanted to reach out"
    * "I was looking at your website and..."
    * "Just wanted to reach out about..."
    * "I hope this finds you well"
  PREFERRED observation voice: lead with the action, not the sender.
  "saw your reviews mention the $5 Friday meals" — direct and confident.
  "noticed your Google listing still points to the old address" — same
  energy. Dropping the "I" makes the observation land faster and sounds
  less like someone narrating their own browsing session. "I noticed
  your impressive online presence" is still the anti-pattern — vague
  praise regardless of whether it starts with "I" or not.

- You have FOUR valid ways to open. Pick the one that fits the angle:
    (a) Friendly greeting first — "hey,", "hi there!", "hey mate,"
        (also "g'day,"). Then the specific observation. This is the
        PREFERRED pattern for most messages. A single exclamation on
        the greeting is OK; do NOT exclaim anywhere else in the
        message.
    (b) Owner-name greeting — only if the system has confidently
        identified the recipient owner's first name (`Recipient
        owner's first name:` in the angle, or obvious from a
        possessive business name like "Matt's Plumbing"). Write it
        the way a mate would text: "hey matt,", "is this matt?",
        "matt — ...". Lowercase + comma is fine. Do NOT invent an
        owner name from reviews; if no name is flagged, use (a),
        (c), or (d).
    (c) Playful stale-name opener — ONLY for `stale_info` angles
        where the stale label appears on the LISTING TITLE, REPLY
        HEADERS, or SITE / PROFILE itself (not just inside old
        review text). Old reviews using the old name are an expected
        historical snapshot and do NOT justify this opener — they're
        a weak hook, not a strong one. When it IS justified (stale
        name is currently visible on something they control), open
        with: "hey, is this Sunnymeade?" / "still trading as the old
        name?" — a half-joking question that lands the observation
        and angle together. Do NOT use this pattern for any other
        angle type.
    (d) Direct observation (fallback) — lead with the action:
        "saw your Google profile still points to the old address",
        "noticed your listing still has the old hours", "saw your
        reviews mention those $5 Friday meals". Fine as a direct
        opener when the observation is sharp enough to carry the
        message on its own, but option (a) (greeting first) is still
        preferred when the observation can stand either way. The
        observation must be concrete and recipient-specific.
- RECIPIENT IDENTIFIER — when a short business name is provided in the
  Recipient block, work it naturally into the opener. This is important
  when the contact number is a personal or home phone — the person
  receiving the text needs to immediately know which business you're
  referring to. Use the short name the way a local would say it.

  Two cases — read the short name carefully before choosing:

  CASE 1 — BRAND-STYLE short name (a real trading name a local would
  say: "Skybound", "Jacaranda Cafe", "Hanley Browne Plumbing", "Matt's
  Plumbing"). These read like a business someone built. You may:
    * possessive in the observation: "saw Skybound's reviews mention..."
    * address the business: "hey Skybound," (when no owner name)
    * question that lands the observation: "is this Skybound? saw you
      have..."

  CASE 2 — COMMON-NOUN PLACE short name (a public asset / generic place
  name: "Lions Park", "Apex Reserve", "Wattle Beach", "Sunset Lookout",
  "Pioneer Jetty", "Memorial Hall", "Civic Centre", "Mooloolaba Oval",
  community gardens, public toilets, boat ramps, lookouts, ovals, halls,
  reserves, parks). Heuristic: if the name reads like a place on a map
  rather than a business someone runs, treat it as CASE 2. Tell-tale
  suffixes: Park, Reserve, Lookout, Beach, Jetty, Oval, Hall, Centre
  (when civic/community), Gardens, Foreshore, Boat Ramp.

  For CASE 2 you MUST NOT address the place. "hey Lions Park,"
  reads as greeting the park itself — it's the dead giveaway of an
  AI-generated message. Instead:
    * Use a generic greeting ("hey,", "hey mate,") and refer to the
      place in the THIRD PERSON inside the observation:
        - "hey mate, noticed Lions Park's listing link 404s"
        - "hey, the Google listing for Apex Reserve points to a dead
          page"
    * Possessive third-person is fine: "Lions Park's Google listing
      still shows..." — that's referring to the place, not greeting it.

  FORBIDDEN for CASE 2:
    * "hey Lions Park,"  — greeting a park
    * "hey Apex Reserve,"  — greeting a reserve
    * "is this Memorial Hall?" — a hall can't answer

  Use the short name field, not the full Google business name — the
  full name often carries a category suffix ("- Kids Volleyball") that
  reads like a database field, not a real conversation.

- THIRD-PARTY NAMES — still off-limits. Do NOT name a reviewer, customer,
  resident, patient, staff member, agent, employee, concierge, or other
  individual at the recipient's business. Use "one of your reviews" /
  "a recent review" / "your team" / "the team there", never "Sandi's
  review", "John mentioned...", "Danny and KB".
  The ONLY individual name that is ever fair game in the draft is the
  recipient OWNER's own first name (from `Recipient owner's first
  name:` in the angle, or clearly embedded in the business name),
  used as the opening greeting per (a). Even when the owner's name is
  known, do NOT mention ANY other individuals in the same message —
  not other agents, not other employees, not reviewers, not customers.
  If the angle text or signal happens to quote other individual names,
  strip them in the draft (use "your team" / "the team" instead).
- Feels personal. If the AI-generated draft could plausibly be sent to a
  different business with no changes, it has failed.

TRUTHFULNESS — DO NOT FABRICATE PROBLEMS
- The recipient can check any negative claim you make in seconds. They
  can open their own website on their own phone, glance at their own
  Google listing, look at their own photos. If your message confidently
  asserts a problem they don't actually have, they will (rightly) read
  the message as a low-effort sales template and trust drops to zero.
  This is worse than sending a generic message — at least a generic
  message isn't a lie.
- Rule: you may only assert a problem about the recipient's website,
  Google listing, mobile experience, design, photos, copy, page speed,
  branding, or any other asset IF the angle's `signal` field literally
  evidences that problem. The signal is the source of truth.
- The angle alone is not enough — the angle text is allowed to be
  positive ("strong reputation", "great reviews"). What matters is
  what specific issue the SIGNAL points to. If the signal points to a
  positive thing (rating, review count, signature amenity, brand
  voice), you do NOT have a problem to fix and you must not invent one.
- Specifically forbidden — claims you CANNOT make unless the signal
  literally evidences them:
    * "your site doesn't [match / reflect / live up to] your
      reputation / reviews / quality"
    * "your site is hard to read on mobile" / "doesn't look good on
      a phone" / "isn't mobile-friendly" / "the site doesn't quite
      work on a phone"
    * "your site is slow / outdated / dated / clunky"
    * "your photos don't reflect [thing]"
    * "your homepage is hard to find / cluttered / confusing"
    * "your online presence doesn't match the business you've built"
  These are common AI-pitch fillers and they're almost always
  unsupported by the data. If you find yourself reaching for one
  because the angle is just "they have great reviews", STOP — the
  data did not give you a problem, and inventing one is a lie.
- POSITIVE-SIGNAL PIVOT — when the angle is a positive signal (good
  reviews, strong reputation, signature amenity, brand voice), the
  offer must be ADDITIVE, not corrective:
    * Good: "happy to put together a web page that leads with the
      $5 Friday meals."
    * Good: "I can show you a web page design that puts those
      reviews front and centre."
    * Good (fallback when there's no specific detail): "happy to
      put together a quick page that highlights the local trust
      you've built — shoot a text if you'd like to see it."
  Acknowledge the positive, then offer to amplify it. Never imply
  there's a hidden problem you're saving them from.
- FALLBACK / THIN SIGNAL — when the angle is `fallback` or the
  signal is just "category + suburb + good rating", you have no
  hook for a problem-claim. Lead with the factual observation
  ("plumber in Stafford Heights with 140+ reviews"), give the
  credential, and offer something additive. A short honest
  introduction beats a confidently-wrong one.
- Hedging counts. "I noticed your site might be a bit slow on phone"
  is still a fabricated negative if you have no evidence — the
  recipient still hears "this person thinks my site is broken".
  The rule is about EVIDENCE, not certainty.

PRODUCT LANGUAGE
- Names the outcome, not the product. Phrase it as what it does for them
  (families not getting confused, the place looking like it does in real
  life, phones not missing enquiries, etc.) rather than as a service you
  sell. The word "website" / "site" / "page" is fine when it's the natural
  word — don't dance around it — but never use pitch phrases like "website
  build service", "website management package", or "our offering".

GOOGLE LISTING vs WEBSITE — name the right thing, don't conflate them
- These are two different assets. Use precise language:
    * "Google listing", "Google Business Profile", "GBP", or "Google
      profile" = the free Google Maps / Search card. Google owns it;
      the business owner edits it but it lives on Google.
    * "website", "site", "web page", "homepage" = the independent
      property they own, at their own domain.
- If the PROBLEM is the Google listing (wrong name, wrong hours,
  stale replies, bad photos, wrong category, missing phone number),
  say Google listing. Don't quietly switch to "your website" when
  the fix lives on the GBP.
- If the problem is the WEBSITE (no site, outdated content, slow
  mobile, no about-page, missing key info), say site / web page.
- If BOTH need fixing, name both explicitly. Use the turnaround
  template below to make it concrete:
    * "I can update the listing today and get a site live in a week."
    * "I can fix the Google listing today, and put a web page
      together over the next week."
- NEVER imply a new website will fix a Google-listing problem. A new
  site does not update the Google profile. If you catch yourself
  writing "a new page would get your info right on Google", stop —
  the GBP is what needs updating for that.

TURNAROUND (use when the angle genuinely fits; don't force it)
- Be specific about timing when you offer a fix. It signals you
  actually do this for a living and removes a common objection
  ("how long will this take?"):
    * Updates / Google listing fixes / small content edits = "today"
      or "1 day" turnaround. "I can update the listing today."
      "I'll get the info synced in a day."
    * Full site build / new web page = "1 week" turnaround.
      "I can get a simple site live in a week." "A new web page in
      about a week."
    * Combined GBP + site = "update the listing today and get a
      site live in a week" — naming both times.
- Use the turnaround ONLY when the angle naturally justifies it. A
  stale_info message about the GBP benefits from "today"; a message
  about brand_voice doesn't need a timeline. Don't crowbar a
  timeline into every draft.
- Don't overclaim. Never promise "same-day site build" or
  "overnight rebuild" — these are the honest numbers, keep them.

EMPATHY (optional but usually lands well)
- After the observation, it often helps to acknowledge the cost of the
  problem for the RECIPIENT's customers/families before offering the fix:
  "I'm sure it's confusing for families", "must be frustrating when..."
  "can't be great when locals can't find you". One short empathy clause
  is enough; any more reads as manipulative.

CREDENTIAL — MANDATORY
- Include exactly ONE short peer-framed line that names what the sender
  does, so the recipient knows who they're hearing from and why. This
  is NOT a pitch and NOT a self-intro by name/brand — just a plain
  statement of trade. 6-14 words.
- STANDARD phrasings (use when the offer is website-centric):
    * "I build and manage websites for a living"
    * "I build websites for a living"
    * "I update websites for a living, happy to help"
- EXTENDED phrasing (use when the offer involves Google listing work
  too, either alone or alongside site work):
    * "I build websites and manage Google listings for a living"
    * "I manage Google listings and build websites for a living"
  Match the credential to the actual offer in the draft — if you're
  offering a GBP fix, the credential should include "Google
  listings"; if it's purely a web page, keep it to "websites".
- Place it AFTER the observation (and the empathy clause, if used) and
  immediately before / inside the offer. Never as the opener.
- Do NOT include the sender's full name or business / brand name in the
  credential line. "I build websites for a living" — yes. "I'm Jane
  from Independent Web Studio" — no.

CALL TO ACTION — MANDATORY: direct, confident, low-friction
- Target pattern: a clear next step the recipient can take on their
  phone in 5 seconds. Confident, not pushy. Friendly, not blunt.
- PREFERRED phrasings:
    * "Shoot me a text and I'll take care of it"
    * "Shoot a text back if you're keen"
    * "Send me a quick text and I'll sort it"
    * "Text me back if you'd like me to"
  When showing a design is the right offer:
    * "I can show you a web page design that [does the specific
      thing]" — always name WHAT the design does, don't just say
      "sketch a page".
      Good: "I can show you a web page design that actually highlights
      the $5 Friday meals for families."
      Good: "I can show you a web page design that gets your Google
      listing and site saying the same thing."
    * "I can put a mockup together that shows [specific thing]" —
      same rule, name the specific outcome.
- RETIRED — do NOT use these (they're weak/blunt/vague or undersell
  the offer):
    * "interested?" — too blunt, no warmth.
    * "worth a look?" — weak.
    * "keen to chat?" — vague, doesn't say what would happen.
    * "sketch a page" used as the whole offer — undersells what a
      design actually does. Use "I can show you a web page design
      that [does X]" instead.
    * "want a hand?" / "happy to chat?" — vague soft-ask with no
      concrete next step.
- Angle-specific examples of the target pattern:
    * stale_info      -> "Shoot me a text and I'll get your listing
                         saying the right thing today."
    * weak_presence   -> "I can show you a web page design that gets
                         you on the map properly. Shoot a text if
                         you're keen."
    * signature_detail-> "I can show you a web page design that puts
                         [the thing] front and centre for families."
    * differentiator  -> "I can show you a web page design that
                         leads with [the thing]. Shoot a text if
                         you'd like."
    * review_theme    -> "Happy to pull together a quick 1-pager
                         that surfaces that — shoot me a text."
    * brand_voice     -> "I can draft how that voice could land on
                         a homepage. Shoot a text if you'd like."
- Never a hard ask ("can we book a call?", "are you free Tuesday?",
  "let's hop on a call"). Never a pushy close ("waiting on your
  reply", "get back to me asap").
- ONE CTA per message. Do NOT stack two CTAs ("want me to sketch
  something? Or shoot me a text?" is two — pick one). One clean
  offer that names the action AND the thing they'll get is the
  whole job.

FORMAT
- Length: target ~200 characters, maximum 320 (two SMS segments).
  Prefer shorter when the observation fits, but don't amputate the
  credential or the offer to hit a tight number. A clear 250-char
  message beats a cramped 150-char one.
- Do NOT introduce yourself by FULL NAME or BUSINESS / BRAND name in
  the first message. The mandatory credential above ("I build websites
  for a living") is NOT a self-intro for this purpose — it's allowed
  and required.
- Emojis: none.
- Exclamation marks: at most ONE, and only on the opening greeting
  ("hi there!", "hey!"). No exclamations anywhere in the body or CTA.
- No hype words: amazing, incredible, boost, skyrocket, game-changer,
  next-level, leverage, synergy.
- Feels like a human typed it on their phone. Contractions welcome.

PUNCTUATION & CAPITALISATION — write like a real Aussie text, not a press
release. The giveaway that a message was AI-generated is usually the
punctuation, not the words.
- Looser is more human over SMS. The following are all FINE:
    * Starting the first word with a lowercase letter ("your google
      profile still points to the old address").
    * Dropping the final full stop.
    * Sentence fragments instead of complete sentences.
    * A single comma where a pedant would want a semicolon.
- AI-PUNCTUATION TELLS — ban list (these read as obviously not-a-human):
    * Em dash ( — ) or en dash ( – ) used to join clauses. A plain
      hyphen ( - ) is fine when it's how a human would actually type
      it; joining two clauses with a spaced em dash is not.
    * Semicolons.
    * Ellipses used stylistically ("..." as a vibe). Only use "..."
      if it's a genuine trailing thought.
    * Perfectly balanced parallel clauses with matching punctuation on
      each side.
    * "Oxford comma + three-item list" in a casual text.
SHAPE — the target beat pattern for a first-contact message is:

  [greeting] — [observation] — [optional empathy] — [credential +
  offer with turnaround where it fits] — [one clean CTA]"""


# ---------------------------------------------------------------------------
# EXAMPLES — six worked SMS examples spanning the angle types. Cheap to
# ablate / swap independently of the rules.
# ---------------------------------------------------------------------------
_REFERENCE_EXAMPLES = """\
Reference examples in the target voice (NOT to be copied verbatim —
write your own using the recipient's actual specifics):

  Example 1 (signature_detail, retirement village):
    "hey, saw your reviews mention the $5 Friday meals and the
    community hall. I build websites for a living, I can show you a
    web page design that actually highlights this for families.
    Shoot me a text if you're keen."

  Example 2 (stale_info on the listing itself, aged care; strong
  signal because the listing title is still outdated):
    "hey, is this Sunnymeade? your Google listing still says the old
    name. I'm sure it's confusing for families. I build websites and
    manage Google listings for a living, I can update the listing
    today and get a site saying the same thing live in a week. Shoot
    a text back if you're keen."

  Example 3 (weak_presence, tourism; no site, GBP is thin):
    "hey mate, your reviews mention the BBQ and pool setup is
    perfect for families watching kids while cooking. I build
    websites for a living, I can show you a web page design that
    puts this front and centre. Shoot me a text if you'd like."

  Example 4 (stale_info, GBP-only fix; just the listing, no site
  offered):
    "hey, your Google listing still has the old trading hours. I
    manage Google listings and build websites for a living. Happy to
    update the listing today. Shoot me a text and I'll take care of
    it."

  Example 5 (positive-signal-only / fallback; the lead has nothing
  but a strong reputation and the data is otherwise thin — DO NOT
  invent a problem here):
    "hey mate, saw you've got 140-odd reviews at 4.7 stars in
    Stafford Heights. I build websites for a living. Happy to put a
    web page together that leads with that local trust. Shoot a
    text if you'd like to see it."
  Notice what's NOT in Example 5: no claim that the existing site
  is bad, slow, clunky, doesn't work on mobile, doesn't match the
  reviews, etc. The angle is positive, so the offer is additive.
  The recipient can verify everything in the message and find it
  all true.

  Example 6 (place-style listing — Lions Park; do NOT greet the place,
  refer to it in the third person):
    "hey mate, noticed Lions Park's Google listing link is throwing a
    404 — bit of a shame for families looking for the BBQs and the
    Mooloolaba views. I build websites and manage Google listings for
    a living, I can fix the listing today. Shoot me a text and I'll
    sort it."
  Notice the opener is "hey mate," (generic greeting) and "Lions
  Park" appears as a third-person reference inside the observation.
  Never "hey Lions Park," — a park can't answer a text.

Notice in each: a short friendly greeting BEFORE the observation, a
concrete recipient-specific detail, credential before the offer, a
specific turnaround ("today" for GBP / updates, "a week" for a full
site) where the angle justifies it, and ONE clean CTA. No staff
names, no hype, no vendor language, no stacked asks."""


# Final instruction lives outside RULES + EXAMPLES so the model sees it
# as the closing directive regardless of whether examples are ablated.
_OUTPUT_INSTRUCTION = (
    "- Output ONLY the message text. "
    "No quotes, no labels, no explanation, no preamble."
)


def build_system_prompt(
    tone_snapshot: str | None,
    *,
    register: ToneRegisterT | None = None,
) -> str:
    """Compose the system prompt: tone + RULES + (register?) + EXAMPLES + closing.

    The tone block is bounded via :func:`cap_tone_snapshot` because the same
    snapshot is also injected into ``evaluation.build_user_prompt`` — an
    unbounded tone block doubles its cost per round-trip. See
    ``autosdr/prompts/_tone.py``.

    ``register`` is the resolved tone register for the recipient (ticket
    0017). When set to a concrete value (``"tradie"``, ``"professional"``,
    etc.), the corresponding prose block is injected between ``_RULES``
    and ``_REFERENCE_EXAMPLES``. When ``None`` (kill-switch path) or
    ``"unknown"``, the block is omitted and the model relies on the
    rules + worked examples — byte-stable for the kill-switch revert
    story.
    """

    tone_block = cap_tone_snapshot(tone_snapshot) if tone_snapshot else None
    tone_block = tone_block or _DEFAULT_TONE
    register_block = render_register_block(register)
    parts: list[str] = [tone_block, _RULES]
    if register_block is not None:
        parts.append(register_block)
    parts.append(_REFERENCE_EXAMPLES)
    return "\n\n".join(parts) + f"\n\n{_OUTPUT_INSTRUCTION}\n"


def build_user_prompt(
    *,
    business_data: dict[str, Any] | str,
    business_dump: str,
    campaign_goal: str,
    angle: str,
    lead_name: str | None,
    lead_short_name: str | None = None,
    lead_category: str | None,
    lead_address: str | None,
    previous_feedback: str | None = None,
    message_history: list[dict[str, str]] | None = None,
) -> str:
    business_block = (
        json.dumps(business_data, indent=2, ensure_ascii=False)
        if isinstance(business_data, dict) and business_data
        else business_dump
    )

    parts: list[str] = [
        "About the sender's business:",
        business_block,
        "",
        f"Campaign goal: {campaign_goal}",
        "",
        "Personalisation angle for this recipient:",
        angle,
        "",
        "Recipient:",
        f"- Name: {lead_name or 'unknown'}",
        f"- Short name: {lead_short_name or lead_name or 'unknown'}",
        f"- Category: {lead_category or 'unknown'}",
        f"- Location: {lead_address or 'unknown'}",
    ]

    if message_history:
        parts.extend(
            [
                "",
                "Conversation so far (most recent last):",
            ]
        )
        for m in message_history:
            role = m.get("role", "?")
            content = m.get("content", "")
            parts.append(f"[{role}] {content}")
        parts.extend(
            [
                "",
                "This is a follow-up reply, not a first-contact message. Address "
                "what the lead just said directly. Stay oriented toward the "
                "campaign goal without being pushy. 160 character maximum.",
            ]
        )

    if previous_feedback:
        parts.extend(
            [
                "",
                "Previous attempt failed evaluation. Feedback:",
                previous_feedback,
                "Rewrite the message addressing this weakness.",
            ]
        )

    return "\n".join(parts)
