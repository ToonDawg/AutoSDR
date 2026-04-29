"""Lead import endpoints — preview + commit, with and without ``mapping_config``.

Ticket 0004 adds an optional ``mapping_config`` form field to both
``/api/leads/import/preview`` and ``/api/leads/import/commit``. These tests
pin the wire contract end-to-end so the frontend can rely on the shape.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from autosdr.webhook import create_app


def _client() -> TestClient:
    return TestClient(create_app(run_scheduler_task=False), raise_server_exceptions=False)


def _ndjson(rows: list[dict]) -> bytes:
    return ("\n".join(json.dumps(r) for r in rows) + "\n").encode("utf-8")


def test_preview_returns_columns_with_suggestions(fresh_db, workspace_factory):
    workspace_factory()
    body = _ndjson(
        [
            {
                "name": "Biz A",
                "phone": "0413 123 456",
                "category": "Retail",
                "address": "1 Bay Tce, Wynnum QLD",
                "website": "https://a.example",
                "reviewDetails": [{"author": "x", "stars": 5}],
                "plusCode": "7G2P+RP Wynnum",
            },
            {
                "name": "Biz B",
                "phone": "0413 222 333",
                "category": "Cafe",
                "address": "2 Park Rd, Milton QLD",
                "website": "https://b.example",
                "reviewDetails": [{"author": "y", "stars": 4}],
                "plusCode": "6F7H+P5 Milton",
            },
        ]
    )

    with _client() as client:
        resp = client.post(
            "/api/leads/import/preview",
            files={"file": ("leads.json", body, "application/json")},
        )
    assert resp.status_code == 200, resp.text
    j = resp.json()
    assert j["total_rows"] == 2
    assert j["would_import"] == 2
    by_name = {c["name"]: c for c in j["columns"]}
    assert by_name["phone"]["suggested_target"] == "phone"
    assert by_name["phone"]["suggestion_confidence"] == "high"
    # Apify-only column not promoted to a core field.
    assert by_name["reviewDetails"]["suggested_target"] is None


def test_preview_with_mapping_config_overrides_alias_pick(
    fresh_db, workspace_factory
):
    """Preview must apply the operator's mapping. With ``contactNumber`` mapped
    to ``phone``, a row that has both ``phone="TBD"`` and a real
    ``contactNumber`` should preview as importable."""

    workspace_factory()
    body = _ndjson(
        [
            {
                "name": "Biz",
                "phone": "TBD",
                "contactNumber": "0413 123 456",
            }
        ]
    )
    mapping = json.dumps({"mapping": {"phone": "contactNumber"}})

    with _client() as client:
        # Without mapping → 0 imports (alias map locks onto "TBD").
        resp_default = client.post(
            "/api/leads/import/preview",
            files={"file": ("leads.json", body, "application/json")},
        )
        assert resp_default.json()["would_import"] == 0

        # With mapping → 1 import.
        resp_mapped = client.post(
            "/api/leads/import/preview",
            files={"file": ("leads.json", body, "application/json")},
            data={"mapping_config": mapping},
        )
        assert resp_mapped.status_code == 200, resp_mapped.text
        assert resp_mapped.json()["would_import"] == 1


def test_commit_with_mapping_drops_noisy_keys_from_raw_data(
    fresh_db, workspace_factory
):
    workspace_factory()
    body = _ndjson(
        [
            {
                "name": "Biz",
                "phone": "0413 123 456",
                "category": "Retail",
                "reviewDetails": [{"author": "x", "stars": 5}] * 20,
            }
        ]
    )
    mapping = json.dumps({"drop_from_raw": ["reviewDetails"]})

    with _client() as client:
        resp = client.post(
            "/api/leads/import/commit",
            files={"file": ("leads.json", body, "application/json")},
            data={"mapping_config": mapping},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["imported_count"] == 1

    with _client() as client:
        leads_resp = client.get("/api/leads")
    leads = leads_resp.json()["leads"]
    assert len(leads) == 1
    assert "reviewDetails" not in leads[0]["raw_data"]


def test_commit_invalid_mapping_config_returns_422(fresh_db, workspace_factory):
    """Garbage in the mapping_config form field must NOT be silently swallowed."""

    workspace_factory()
    body = _ndjson([{"name": "Biz", "phone": "0413 123 456"}])

    with _client() as client:
        # Malformed JSON.
        bad_json = client.post(
            "/api/leads/import/commit",
            files={"file": ("leads.json", body, "application/json")},
            data={"mapping_config": "{not json"},
        )
        assert bad_json.status_code == 422
        assert bad_json.json()["error"] == "invalid_mapping_config"

        # Unknown top-level key (operator typo: ``drop_form_raw`` not ``drop_from_raw``).
        bad_shape = client.post(
            "/api/leads/import/commit",
            files={"file": ("leads.json", body, "application/json")},
            data={
                "mapping_config": json.dumps({"drop_form_raw": ["reviewDetails"]})
            },
        )
        assert bad_shape.status_code == 422
        assert bad_shape.json()["error"] == "invalid_mapping_config"

        # Mapping target not in core fields.
        bad_target = client.post(
            "/api/leads/import/commit",
            files={"file": ("leads.json", body, "application/json")},
            data={
                "mapping_config": json.dumps(
                    {"mapping": {"profession_grade": "phone"}}
                )
            },
        )
        assert bad_target.status_code == 422


def test_commit_without_mapping_config_is_backward_compatible(
    fresh_db, workspace_factory
):
    """Existing callers that don't send ``mapping_config`` must keep working."""

    workspace_factory()
    body = _ndjson(
        [
            {
                "name": "Biz",
                "phone": "0413 123 456",
                "category": "Retail",
                "rating": 5,
            }
        ]
    )

    with _client() as client:
        resp = client.post(
            "/api/leads/import/commit",
            files={"file": ("leads.json", body, "application/json")},
        )
    assert resp.status_code == 200
    assert resp.json()["imported_count"] == 1
