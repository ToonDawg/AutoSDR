"""Importer — phone normalisation, mobile detection, CSV + NDJSON formats."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autosdr.importer import ImportRowResult, import_file, normalise_phone
from autosdr.models import ContactType, Lead, LeadStatus


# ---------------------------------------------------------------------------
# normalise_phone
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected_e164,expected_type",
    [
        ("(07) 5495 4233", "+61754954233", ContactType.LANDLINE),
        ("07 5495 4233", "+61754954233", ContactType.LANDLINE),
        ("+61754954233", "+61754954233", ContactType.LANDLINE),
        ("0413 123 456", "+61413123456", ContactType.MOBILE),
        ("1800 692 273", "+611800692273", ContactType.TOLL_FREE),
    ],
)
def test_normalise_phone_australian_variants(raw, expected_e164, expected_type):
    e164, contact_type = normalise_phone(raw, region_hint="AU")
    assert e164 == expected_e164
    assert contact_type == expected_type


def test_normalise_phone_invalid():
    e164, contact_type = normalise_phone("not a phone", region_hint="AU")
    assert e164 is None
    assert contact_type == ContactType.UNKNOWN


def test_normalise_phone_empty():
    assert normalise_phone("", region_hint="AU") == (None, ContactType.UNKNOWN)
    assert normalise_phone(None, region_hint="AU") == (None, ContactType.UNKNOWN)


# ---------------------------------------------------------------------------
# import_file — NDJSON (matches the example-leads.json shape)
# ---------------------------------------------------------------------------


def _write_ndjson(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
        encoding="utf-8",
    )


def test_import_ndjson_respects_mobile_vs_landline(tmp_path, workspace_factory, fresh_db):
    ws_id = workspace_factory()
    path = tmp_path / "leads.json"
    rows = [
        {
            "name": "Caboolture Parklands",
            "category": "Aged Care Service",
            "address": "362-376 King St, Caboolture QLD",
            "phone": "(07) 5495 4233",
            "website": "https://example.com",
            "rating": 4,
            "reviews": 53,
        },
        {
            "name": "Mobile Test",
            "category": "Retail",
            "phone": "0413 123 456",
        },
        {
            "name": "Toll Free Service",
            "phone": "1800 692 273",
            "category": "Helpline",
        },
        {
            "name": "No Phone",
            "category": "Mystery",
        },
    ]
    _write_ndjson(path, rows)

    with fresh_db() as session:
        summary = import_file(
            session=session, workspace_id=ws_id, path=path, region_hint="AU"
        )

    assert summary.row_count == 4
    # Inserted: landline, mobile, toll_free. Skipped: no_contact_uri.
    assert summary.imported_count == 3
    assert summary.skipped_count == 1

    with fresh_db() as session:
        leads = session.query(Lead).order_by(Lead.import_order.asc()).all()
        by_name = {l.name: l for l in leads}

        landline = by_name["Caboolture Parklands"]
        assert landline.contact_uri == "+61754954233"
        assert landline.contact_type == ContactType.LANDLINE
        assert landline.status == LeadStatus.SKIPPED
        assert landline.skip_reason.startswith("not_a_mobile_number")
        # raw_data preserved: `rating`, `reviews`, plus the real `website` went to a
        # core field so it's not in raw_data.
        assert landline.raw_data.get("rating") == 4
        assert landline.raw_data.get("reviews") == 53

        mobile = by_name["Mobile Test"]
        assert mobile.contact_type == ContactType.MOBILE
        assert mobile.status == LeadStatus.NEW
        assert mobile.skip_reason is None

        toll_free = by_name["Toll Free Service"]
        assert toll_free.contact_type == ContactType.TOLL_FREE
        assert toll_free.status == LeadStatus.SKIPPED


def test_import_ndjson_dedupes_on_normalised_e164(tmp_path, workspace_factory, fresh_db):
    ws_id = workspace_factory()
    path_1 = tmp_path / "leads-1.json"
    path_2 = tmp_path / "leads-2.json"

    _write_ndjson(
        path_1,
        [{"name": "Biz", "phone": "(07) 5495 4233", "category": "Aged Care"}],
    )
    _write_ndjson(
        path_2,
        [
            {
                "name": "Biz Updated",
                "phone": "+61 7 5495 4233",
                "website": "https://biz.example.com",
                "rating": 5,
            }
        ],
    )

    with fresh_db() as session:
        s1 = import_file(session=session, workspace_id=ws_id, path=path_1)
    with fresh_db() as session:
        s2 = import_file(session=session, workspace_id=ws_id, path=path_2)

    assert s1.row_count == 1
    assert s2.row_count == 1
    assert s2.imported_count == 1  # counted as imported (updated)

    with fresh_db() as session:
        leads = session.query(Lead).all()
        assert len(leads) == 1
        l = leads[0]
        # Non-null core field (name) must not be overwritten.
        assert l.name == "Biz"
        # website was null before, so it fills in from the second import.
        assert l.website == "https://biz.example.com"
        # raw_data merges new keys.
        assert l.raw_data.get("rating") == 5


def test_import_csv(tmp_path, workspace_factory, fresh_db):
    ws_id = workspace_factory()
    path = tmp_path / "leads.csv"
    path.write_text(
        "name,phone,category,notes\n"
        "Test Mobile,0413 123 456,Retail,top priority\n"
        "No Phone,,Something,\n"
        ",07 5495 4233,Aged Care,\n",
        encoding="utf-8",
    )

    with fresh_db() as session:
        summary = import_file(
            session=session, workspace_id=ws_id, path=path, region_hint="AU"
        )

    assert summary.row_count == 3
    # Inserted: mobile (new), landline (skipped-but-imported).
    # Skipped: no-phone row.
    assert summary.imported_count == 2
    assert summary.skipped_count == 1

    with fresh_db() as session:
        leads = session.query(Lead).all()
        assert len(leads) == 2
        for l in leads:
            if l.contact_type == ContactType.MOBILE:
                assert l.status == LeadStatus.NEW
                assert l.raw_data.get("notes") == "top priority"
            else:
                assert l.status == LeadStatus.SKIPPED


def test_import_rejects_unknown_extension(tmp_path, workspace_factory, fresh_db):
    ws_id = workspace_factory()
    path = tmp_path / "leads.xlsx"
    path.write_text("nope", encoding="utf-8")
    with fresh_db() as session:
        with pytest.raises(ValueError, match="Unsupported file extension"):
            import_file(session=session, workspace_id=ws_id, path=path)


def test_row_result_record_counts_updated_as_imported():
    from autosdr.importer import ImportSummary

    summary = ImportSummary(job_id="x")
    summary.record(1, ImportRowResult("inserted"))
    summary.record(2, ImportRowResult("updated"))
    summary.record(3, ImportRowResult("skipped", reason="duplicate"))
    summary.record(4, ImportRowResult("error", reason="boom"))

    assert summary.imported_count == 2
    assert summary.skipped_count == 1
    assert summary.error_count == 1
