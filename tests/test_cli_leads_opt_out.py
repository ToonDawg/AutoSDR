"""CLI: ``autosdr leads opt-out`` — manual do-not-contact override.

Mirrors the inbound STOP-keyword shortcut but for the off-channel case
(operator hears about the opt-out via phone / email / etc.). Must:

- Find the lead by exact ``contact_uri`` *or* by phone-number
  normalisation, so the operator can pass either ``+61400000123`` or
  ``0400 000 123`` and hit the same row.
- Set ``do_not_contact_at`` + ``do_not_contact_reason``.
- Be idempotent: re-running on an already-flagged lead is a no-op.
- Refuse to run without ``--yes`` unless an interactive operator types
  ``y`` at the prompt.
"""

from __future__ import annotations

from typer.testing import CliRunner

from autosdr.cli import app
from autosdr.models import Lead, Workspace


runner = CliRunner()


def _seed_lead(fresh_db, workspace_factory, contact_uri: str = "+61400000777") -> str:
    ws_id = workspace_factory()
    with fresh_db() as session:
        ws = session.get(Workspace, ws_id)
        lead = Lead(
            workspace_id=ws.id,
            name="Phoned-In Lead",
            contact_uri=contact_uri,
            contact_type="mobile",
            category="Cafe",
            address="Brisbane QLD",
            raw_data={},
            import_order=1,
            source_file="seed.csv",
            status="new",
        )
        session.add(lead)
        session.flush()
        return lead.id


def test_opt_out_with_yes_flag_marks_lead(fresh_db, workspace_factory):
    lead_id = _seed_lead(fresh_db, workspace_factory)

    result = runner.invoke(
        app, ["leads", "opt-out", "+61400000777", "--yes"]
    )

    assert result.exit_code == 0, result.output
    assert "opted out" in result.output

    with fresh_db() as session:
        lead = session.get(Lead, lead_id)
        assert lead.do_not_contact_at is not None
        assert lead.do_not_contact_reason == "manual"


def test_opt_out_normalises_local_phone_format(fresh_db, workspace_factory):
    lead_id = _seed_lead(fresh_db, workspace_factory, contact_uri="+61400000777")

    # Operator types the local-format AU number; CLI must hit the same lead.
    result = runner.invoke(
        app, ["leads", "opt-out", "0400 000 777", "--yes"]
    )

    assert result.exit_code == 0, result.output
    with fresh_db() as session:
        lead = session.get(Lead, lead_id)
        assert lead.do_not_contact_at is not None


def test_opt_out_is_idempotent_on_already_flagged_lead(fresh_db, workspace_factory):
    from datetime import datetime, timezone

    lead_id = _seed_lead(fresh_db, workspace_factory)
    with fresh_db() as session:
        lead = session.get(Lead, lead_id)
        lead.do_not_contact_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        lead.do_not_contact_reason = "opt_out:STOP"
        session.flush()

    result = runner.invoke(
        app, ["leads", "opt-out", "+61400000777", "--yes"]
    )

    assert result.exit_code == 0, result.output
    assert "already opted out" in result.output

    with fresh_db() as session:
        lead = session.get(Lead, lead_id)
        # Reason is preserved — re-running does not overwrite.
        assert lead.do_not_contact_reason == "opt_out:STOP"


def test_opt_out_unknown_contact_uri_errors(fresh_db, workspace_factory):
    workspace_factory()

    result = runner.invoke(
        app, ["leads", "opt-out", "+61400999999", "--yes"]
    )

    assert result.exit_code == 1
    assert "no lead" in result.output.lower()


def test_opt_out_without_yes_aborts_on_no(fresh_db, workspace_factory):
    lead_id = _seed_lead(fresh_db, workspace_factory)

    # Without --yes, typer.confirm reads from stdin; "n\n" → aborted.
    result = runner.invoke(
        app, ["leads", "opt-out", "+61400000777"], input="n\n"
    )

    assert result.exit_code == 1, result.output
    assert "aborted" in result.output.lower()

    with fresh_db() as session:
        lead = session.get(Lead, lead_id)
        assert lead.do_not_contact_at is None


def test_opt_out_without_yes_proceeds_on_y(fresh_db, workspace_factory):
    lead_id = _seed_lead(fresh_db, workspace_factory)

    result = runner.invoke(
        app, ["leads", "opt-out", "+61400000777"], input="y\n"
    )

    assert result.exit_code == 0, result.output
    with fresh_db() as session:
        lead = session.get(Lead, lead_id)
        assert lead.do_not_contact_at is not None
        assert lead.do_not_contact_reason == "manual"


def test_opt_out_custom_reason_stored(fresh_db, workspace_factory):
    lead_id = _seed_lead(fresh_db, workspace_factory)

    result = runner.invoke(
        app,
        [
            "leads",
            "opt-out",
            "+61400000777",
            "--yes",
            "--reason",
            "manual:phoned-in",
        ],
    )

    assert result.exit_code == 0, result.output
    with fresh_db() as session:
        lead = session.get(Lead, lead_id)
        assert lead.do_not_contact_reason == "manual:phoned-in"
