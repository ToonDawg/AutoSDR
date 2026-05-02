"""Privacy posture for HITL push payloads (ticket 0005, success criterion).

Every field that leaves the server in a notification payload is named
here. If a future change to ``build_hitl_payload`` adds anything else
— message content, last name, business name, lead category, raw_data
slices — these tests fail loudly.

The ``EXPECTED_FIELDS`` set is the full surface, *deliberately* small.
The Critic-mandated rule from the ticket's *Remote-access architecture*
council round: a notification glanced at off-tailnet must reveal at
most "thread X needs attention".
"""

from __future__ import annotations

from datetime import datetime, timezone

from autosdr.push import HitlPushPayload, build_hitl_payload


EXPECTED_FIELDS = frozenset(
    {
        "title",
        "body",
        "thread_id",
        "lead_first_name",
        "hitl_reason",
        "escalated_at",
        "url",
    }
)


def test_payload_dataclass_exposes_exactly_the_allowed_fields():
    """The dataclass shape *itself* is the contract — any extra field
    on the class is a privacy bug, not just an over-broad payload."""

    field_names = set(HitlPushPayload.__dataclass_fields__.keys())
    assert field_names == EXPECTED_FIELDS


def test_payload_as_dict_matches_expected_field_set():
    payload = build_hitl_payload(
        thread_id="thread-1",
        lead_name="Sarah Chen",
        hitl_reason="objection",
        escalated_at=datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc),
        dashboard_origin="http://autosdr.tail-scale.ts.net:8000",
    ).as_dict()
    assert set(payload.keys()) == EXPECTED_FIELDS


def test_payload_strips_lead_last_name_from_every_field():
    """Last name appears nowhere in the serialised payload."""

    payload = build_hitl_payload(
        thread_id="thread-1",
        lead_name="Sarah Chen O'Brien",
        hitl_reason="objection",
        escalated_at=datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc),
        dashboard_origin="http://autosdr.tail-scale.ts.net:8000",
    ).as_dict()
    serialised = " ".join(str(v) for v in payload.values())
    for forbidden in ("Chen", "O'Brien", "OBrien"):
        assert forbidden not in serialised, (
            f"payload leaked '{forbidden}' — privacy posture broken"
        )
    assert payload["lead_first_name"] == "Sarah"


def test_payload_does_not_carry_message_content():
    """Caller never even has access to a slot for it."""

    import inspect

    signature = inspect.signature(build_hitl_payload)
    forbidden_param_names = {"message", "content", "body_text", "raw_data"}
    leaked = set(signature.parameters) & forbidden_param_names
    assert not leaked, (
        f"build_hitl_payload exposes forbidden params: {leaked}"
    )


def test_payload_anonymises_when_lead_name_missing():
    """No lead name → generic title, no PII at all."""

    payload = build_hitl_payload(
        thread_id="thread-2",
        lead_name=None,
        hitl_reason="confused",
        escalated_at=datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc),
        dashboard_origin=None,
    ).as_dict()
    assert payload["lead_first_name"] == ""
    assert payload["title"] == "AutoSDR: thread needs your eye"
    assert payload["url"] == "/inbox/thread-2"
