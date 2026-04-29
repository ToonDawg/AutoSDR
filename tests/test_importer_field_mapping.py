"""Importer — operator field-mapping (`mapping_config`).

Ticket 0004 introduces a per-import ``mapping_config`` that lets the operator
override the alias-map guesses, drop verbose source columns from
``lead.raw_data``, and explicitly opt a column into raw-data-only treatment.
This file pins the contract of ``_split_core_and_raw`` plus the round-trip
through ``import_file`` (the suggestion engine itself is exercised in
``test_importer_field_mapping_suggestions``).
"""

from __future__ import annotations

import json
from pathlib import Path

from autosdr.importer import (
    _split_core_and_raw,
    _suggest_column_target,
    import_file,
    preview_import_file,
)
from autosdr.models import Lead

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# _split_core_and_raw — backward compat (no mapping_config)
# ---------------------------------------------------------------------------


def test_split_default_unchanged_when_no_mapping_config():
    row = {"name": "Biz", "phone": "0413 123 456", "category": "Retail", "extras": {"k": 1}}
    core, raw = _split_core_and_raw(row)
    assert core == {"name": "Biz", "phone": "0413 123 456", "category": "Retail"}
    assert raw == row


def test_split_default_uses_alias_map():
    row = {"company_name": "Biz", "mobile": "0413 123 456"}
    core, raw = _split_core_and_raw(row)
    assert core == {"name": "Biz", "phone": "0413 123 456"}
    assert raw == row


# ---------------------------------------------------------------------------
# _split_core_and_raw — operator mapping
# ---------------------------------------------------------------------------


def test_mapping_overrides_alias_pick():
    """Operator says ``phone`` lives in ``contactNumber``, not the column we'd
    have guessed (``mobile``). Mapping wins."""

    row = {
        "name": "Biz",
        "mobile": "0400 000 001",
        "contactNumber": "0413 123 456",
    }
    core, _raw = _split_core_and_raw(
        row, mapping_config={"mapping": {"phone": "contactNumber"}}
    )
    assert core["phone"] == "0413 123 456"


def test_mapping_can_explicitly_disable_alias_match():
    """An ``include_in_raw_only`` of a name that would alias-match keeps it
    out of core. Useful when the operator wants ``mobile`` kept as text-only
    context for the LLM but does not want it treated as the contact URI."""

    row = {"name": "Biz", "mobile": "0413 123 456", "contactNumber": "0400 000 001"}
    core, raw = _split_core_and_raw(
        row,
        mapping_config={
            "mapping": {"phone": "contactNumber"},
            "include_in_raw_only": ["mobile"],
        },
    )
    assert core["phone"] == "0400 000 001"
    assert "mobile" in raw  # still preserved for context
    assert raw["mobile"] == "0413 123 456"


def test_drop_from_raw_omits_keys_from_raw_data():
    row = {
        "name": "Biz",
        "phone": "0413 123 456",
        "reviewDetails": [{"author": "A", "stars": 5}] * 20,
        "webResults": None,
    }
    core, raw = _split_core_and_raw(
        row,
        mapping_config={"drop_from_raw": ["reviewDetails", "webResults"]},
    )
    assert core["name"] == "Biz"
    assert core["phone"] == "0413 123 456"
    assert "reviewDetails" not in raw
    assert "webResults" not in raw


def test_mapping_unknown_canonical_target_is_silently_ignored():
    """The MappingConfig schema layer rejects unknown canonical targets at the
    API boundary; the splitter is defensive — an unknown canonical target
    must not cause a crash."""

    row = {"name": "Biz", "phone": "0413 123 456"}
    core, _raw = _split_core_and_raw(
        row, mapping_config={"mapping": {"profession_grade": "phone"}}
    )
    # ``profession_grade`` is not a core field; it must not appear in core.
    assert "profession_grade" not in core
    # Other rows still resolve normally.
    assert core["name"] == "Biz"
    assert core["phone"] == "0413 123 456"


def test_drop_does_not_remove_existing_raw_data_on_reimport(
    tmp_path, workspace_factory, fresh_db
):
    """OQ3 (council 2026-04-27): ``drop_from_raw`` is commit-only. Re-import
    must NOT retroactively prune keys that already live on existing rows."""

    ws_id = workspace_factory()

    # First import — no drop_from_raw, so reviewDetails lands in raw_data.
    path1 = tmp_path / "first.json"
    path1.write_text(
        json.dumps(
            {
                "name": "Biz",
                "phone": "0413 123 456",
                "reviewDetails": [{"author": "A"}],
            }
        ),
        encoding="utf-8",
    )
    with fresh_db() as session:
        import_file(session=session, workspace_id=ws_id, path=path1)

    with fresh_db() as session:
        lead = session.query(Lead).one()
        assert "reviewDetails" in (lead.raw_data or {})

    # Second import — same lead, now with drop_from_raw=['reviewDetails'].
    # The merged raw_data should still contain the historical key.
    path2 = tmp_path / "second.json"
    path2.write_text(
        json.dumps(
            {
                "name": "Biz",
                "phone": "0413 123 456",
                "reviewDetails": [{"author": "A"}],
                "rating": 5,
            }
        ),
        encoding="utf-8",
    )
    with fresh_db() as session:
        import_file(
            session=session,
            workspace_id=ws_id,
            path=path2,
            mapping_config={"drop_from_raw": ["reviewDetails"]},
        )

    with fresh_db() as session:
        lead = session.query(Lead).one()
        assert "reviewDetails" in (lead.raw_data or {})
        assert lead.raw_data["rating"] == 5


def test_reimport_with_same_mapping_config_is_idempotent(
    tmp_path, workspace_factory, fresh_db
):
    """Running the same file + mapping_config twice should not produce
    spurious updates. The merge logic in ``_process_row`` treats two
    identical raw_data blobs as a no-op.

    SC (ticket 0004): "Re-import idempotency: running with the same
    ``mapping_config`` twice produces no spurious updates."
    """

    ws_id = workspace_factory()
    mapping = {"drop_from_raw": ["reviewDetails"]}

    path = tmp_path / "leads.json"
    path.write_text(
        json.dumps(
            {
                "name": "Biz",
                "phone": "0413 123 456",
                "category": "Retail",
                "reviewDetails": [{"author": "A"}],
                "rating": 5,
            }
        ),
        encoding="utf-8",
    )

    with fresh_db() as session:
        s1 = import_file(
            session=session, workspace_id=ws_id, path=path, mapping_config=mapping
        )
    assert s1.imported_count == 1

    with fresh_db() as session:
        s2 = import_file(
            session=session, workspace_id=ws_id, path=path, mapping_config=mapping
        )

    # Second import sees a duplicate: same contact_uri, same raw_data after
    # filtering. _process_row reports 'duplicate_no_new_data'.
    assert s2.imported_count == 0
    assert s2.skipped_count == 1
    assert any(
        e.get("reason") == "duplicate_no_new_data" for e in s2.errors
    )

    with fresh_db() as session:
        leads = session.query(Lead).all()
        assert len(leads) == 1
        # `reviewDetails` was filtered out on first import so it should never
        # appear; the rest of raw_data should be exactly what we wrote.
        assert "reviewDetails" not in (leads[0].raw_data or {})
        assert leads[0].raw_data.get("rating") == 5


def test_preview_honours_mapping_config(tmp_path):
    """Preview must apply the same mapping the commit will — otherwise
    operators see a misleading "would import" count."""

    path = tmp_path / "leads.json"
    path.write_text(
        json.dumps(
            {
                "name": "Biz",
                "contactNumber": "0413 123 456",
                "phone": "TBD",
            }
        ),
        encoding="utf-8",
    )
    # Without mapping: alias map picks ``phone`` ("TBD") which fails to parse.
    preview_default = preview_import_file(path=path)
    assert preview_default.would_import == 0
    # With operator mapping: phone should resolve from contactNumber.
    preview_mapped = preview_import_file(
        path=path,
        mapping_config={"mapping": {"phone": "contactNumber"}},
    )
    assert preview_mapped.would_import == 1


# ---------------------------------------------------------------------------
# Suggestion engine — rule pyramid (each rule has a positive + negative case)
# ---------------------------------------------------------------------------


def test_suggest_exact_match_is_high():
    target, conf, reason = _suggest_column_target("phone", ["0413 123 456"] * 5)
    assert target == "phone"
    assert conf == "high"
    assert "exact" in reason.lower()


def test_suggest_unknown_column_with_no_signal_is_none():
    target, conf, reason = _suggest_column_target("rating", [4.5, 4.7, 5.0, 4.2, 4.9])
    assert target is None
    assert conf == "none"
    assert "no signal" in reason


def test_suggest_alias_match_is_high():
    target, conf, _ = _suggest_column_target("mobile", ["0413 123 456"] * 5)
    assert target == "phone"
    assert conf == "high"


def test_suggest_alias_negative_does_not_match_unrelated_alias():
    """A column whose name does not appear in the alias table must not be
    promoted by alias rules — even if its sample values look phone-y."""

    # Force alias-rule failure by using a column name that is NOT in
    # _CORE_ALIASES. Any phone signal must come from the heuristic, not aliasing.
    target, conf, reason = _suggest_column_target(
        "contactNumber", ["abc"] * 10  # bogus values defeat sample heuristic
    )
    assert target is None
    assert conf == "none"
    assert "no signal" in reason


def test_suggest_levenshtein_typo_is_medium():
    target, conf, reason = _suggest_column_target("namee", ["Biz Inc"] * 3)
    assert target == "name"
    assert conf == "medium"
    assert "typo" in reason


def test_suggest_levenshtein_negative_distance_too_far():
    """Distance > 2 must NOT trigger a medium suggestion."""

    target, conf, _ = _suggest_column_target("foobar", ["x"] * 3)
    assert target is None
    assert conf == "none"


def test_suggest_substring_match_is_medium():
    """A column name containing a core field name (but not exact) -> medium."""

    target, conf, reason = _suggest_column_target("phone_e164", ["+61"] * 3)
    assert target == "phone"
    assert conf == "medium"
    assert "contains" in reason


def test_suggest_substring_negative_does_not_match_when_no_core_substring():
    target, conf, _ = _suggest_column_target("notes_for_lead", ["short note"] * 3)
    assert target is None
    assert conf == "none"


def test_suggest_phone_heuristic_high_when_90pct_match():
    # 9/10 phones, 1 garbage -> 90% -> high
    values = ["0413 123 456"] * 9 + ["TBD"]
    target, conf, reason = _suggest_column_target("contactNumber", values)
    assert target == "phone"
    assert conf == "high"
    assert "90%" in reason or "100%" in reason


def test_suggest_phone_heuristic_medium_when_80pct_match():
    # 8/10 phones, 2 garbage -> 80% -> medium
    values = ["0413 123 456"] * 8 + ["TBD", "TBD"]
    target, conf, _ = _suggest_column_target("contactNumber", values)
    assert target == "phone"
    assert conf == "medium"


def test_suggest_phone_heuristic_below_80pct_no_signal():
    # 6/10 phones, 4 garbage -> 60% -> no signal
    values = ["0413 123 456"] * 6 + ["TBD", "n/a", "X", "ASK"]
    target, conf, _ = _suggest_column_target("contactNumber", values)
    assert target is None
    assert conf == "none"


def test_suggest_phone_heuristic_blocked_by_min_support_floor():
    """A column with 100% match rate but only 3 non-null cells must NOT
    claim high confidence — denominator-too-small rule (resolved OQ2)."""

    values = ["0413 123 456", "0411 111 111", "0422 222 222", None, None, None]
    target, conf, _ = _suggest_column_target("contactNumber", values)
    assert target is None
    assert conf == "none"


def test_suggest_url_heuristic_high():
    values = ["https://a.example", "http://b.example"] * 5
    target, conf, _ = _suggest_column_target("homepageLink", values)
    assert target == "website"
    assert conf == "high"


def test_suggest_url_heuristic_negative_below_threshold():
    """Low ratio of URL-shaped values + name has no website signal -> none."""

    values = ["https://a.example"] + ["just text"] * 9  # 10% -> below threshold
    target, conf, _ = _suggest_column_target("homepageLink", values)
    assert target is None
    assert conf == "none"


def test_suggest_address_heuristic_low_confidence():
    """Address heuristic is loose: even at 100% match it caps at low."""

    values = [
        "12 Bay Tce, Wynnum QLD",
        "362-376 King St, Caboolture QLD",
        "Suite 4, 88 Wickham St, Fortitude Valley QLD",
        "Level 9, 200 Adelaide St, Brisbane QLD",
        "5 Whale Cl, Caloundra QLD",
    ]
    target, conf, _ = _suggest_column_target("locationDetail", values)
    assert target == "address"
    assert conf == "low"


# ---------------------------------------------------------------------------
# Apify fixture (synthetic excerpt mirroring the documented shape)
# ---------------------------------------------------------------------------


def test_preview_apify_fixture_every_column_has_suggestion_or_none():
    """Ticket 0004 SC: import the Apify excerpt and assert every observed
    column has a suggestion (or explicit ``none``), and ``phone`` is suggested
    with ``high`` confidence."""

    fixture = FIXTURES_DIR / "apify_qld_excerpt.ndjson"
    preview = preview_import_file(path=fixture)

    by_name = {c.name: c for c in preview.columns}
    expected_names = {
        "name",
        "category",
        "address",
        "phone",
        "website",
        "reviews",
        "reviewDetails",
        "webResults",
        "searchQuery",
        "scrapedAt",
        "plusCode",
        "rating",
    }
    assert expected_names.issubset(by_name.keys())

    for col in preview.columns:
        assert col.suggestion_confidence in {"high", "medium", "low", "none"}, col

    # ``phone`` is an exact match -> high.
    assert by_name["phone"].suggested_target == "phone"
    assert by_name["phone"].suggestion_confidence == "high"
    # ``name``, ``category``, ``address``, ``website`` are exact matches too.
    for canonical in ("name", "category", "address", "website"):
        assert by_name[canonical].suggested_target == canonical
        assert by_name[canonical].suggestion_confidence == "high"
    # Apify's Apify-only columns must NOT get spuriously promoted.
    assert by_name["reviewDetails"].suggested_target is None
    assert by_name["webResults"].suggested_target is None
    assert by_name["plusCode"].suggested_target is None
    assert by_name["scrapedAt"].suggested_target is None
    assert by_name["searchQuery"].suggested_target is None
