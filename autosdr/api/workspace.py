"""Workspace settings + identity.

Endpoints:

* ``GET /api/workspace`` — current workspace row (business info + settings).
* ``PATCH /api/workspace/settings`` — deep-merge updates into ``settings``
  and hot-reapply the LLM keys + connector so changes take effect without
  a restart. The Settings page in the UI hits this constantly.
* ``POST /api/workspace/connector/test`` — probe the connector without
  saving. Used by the "Test connection" button on the Settings page so the
  operator can verify SMSGate / TextBee creds before committing them.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from sqlalchemy.orm.attributes import flag_modified

from autosdr import killswitch
from autosdr.api.deps import db_session, require_workspace
from autosdr.api.schemas import (
    ConnectorTestRequest,
    ConnectorTestResult,
    WorkspaceOut,
    WorkspacePatch,
    WorkspaceSettingsPatch,
)
from autosdr.config import merge_workspace_settings
from autosdr.connectors import (
    BaseConnector,
    ConnectorError,
    build_connector,
    get_connector,
    rebuild_connector,
)
from autosdr.llm import apply_llm_provider_keys
from autosdr.workspace_settings import load_workspace_settings_optional

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/workspace", tags=["workspace"])


@router.get("", response_model=WorkspaceOut)
def get_workspace() -> WorkspaceOut:
    with db_session() as session:
        workspace = require_workspace(session)
        return WorkspaceOut.model_validate(workspace)


@router.patch("", response_model=WorkspaceOut)
def patch_workspace(payload: WorkspacePatch) -> WorkspaceOut:
    """Update identity fields (business name / dump / tone prompt)."""

    updates = payload.model_dump(exclude_unset=True)
    with db_session() as session:
        workspace = require_workspace(session)
        for field_name, value in updates.items():
            setattr(workspace, field_name, value)
        session.flush()
        session.refresh(workspace)
        return WorkspaceOut.model_validate(workspace)


@router.patch("/settings", response_model=WorkspaceOut)
def patch_settings(payload: WorkspaceSettingsPatch) -> WorkspaceOut:
    """Deep-merge updates into ``workspace.settings`` and hot-apply them.

    After the merge we:
      1. Push any LLM provider keys into ``os.environ`` for LiteLLM.
      2. Rebuild the cached connector singleton so connector swaps /
         credential rotations / override-recipient changes take effect
         immediately.

    This is what lets the operator change their Gemini key in the UI and
    send their very next message without restarting the process.
    """

    updates = payload.model_dump(exclude_unset=True)
    with db_session() as session:
        workspace = require_workspace(session)
        merged = merge_workspace_settings(workspace.settings or {}, updates)
        workspace.settings = merged
        flag_modified(workspace, "settings")
        session.flush()
        session.refresh(workspace)
        out = WorkspaceOut.model_validate(workspace)

    apply_llm_provider_keys(out.settings)
    rebuild_connector(out.settings)
    return out


@router.post("/connector/test", response_model=ConnectorTestResult)
async def test_connector(payload: ConnectorTestRequest | None = None) -> ConnectorTestResult:
    """Probe the connector's ``validate_config`` without persisting changes.

    Two modes:

    * **Saved** — empty body (or no ``type`` field). Tests the currently
      cached singleton, so the button on Settings verifies the exact
      connector the runtime is using right now.
    * **Unsaved** — body carries a ``{type, textbee|smsgate}`` blob. We
      build an ephemeral connector from that override merged on top of the
      saved settings so the operator can click Test *before* Save.

    Construction errors (missing creds, malformed URL) are reported as
    ``ok=false`` with the exception message in ``detail``. The same applies
    to :class:`~autosdr.killswitch.KillSwitchTripped` from the validate hot
    path: we open a :func:`~autosdr.killswitch.allow_manual_send` context
    so a user-requested pause doesn't produce a misleading "connector
    broken" result. Shutdown still aborts.
    """

    connector: BaseConnector | None = None
    connector_type_hint = "unknown"

    try:
        if payload is not None and payload.type is not None:
            saved = load_workspace_settings_optional() or {}
            overridden = dict(saved)
            connector_cfg = dict(overridden.get("connector") or {})
            connector_cfg["type"] = payload.type
            if payload.textbee is not None:
                connector_cfg["textbee"] = {
                    **(connector_cfg.get("textbee") or {}),
                    **payload.textbee,
                }
            if payload.smsgate is not None:
                connector_cfg["smsgate"] = {
                    **(connector_cfg.get("smsgate") or {}),
                    **payload.smsgate,
                }
            overridden["connector"] = connector_cfg
            # A connection test probes the real target directly, so strip
            # the saved override recipient — otherwise every probe would
            # route to the rehearsal phone instead of validating the creds
            # against the actual gateway.
            rehearsal = dict(overridden.get("rehearsal") or {})
            rehearsal["override_to"] = None
            overridden["rehearsal"] = rehearsal

            connector_type_hint = payload.type
            connector = build_connector(overridden)
        else:
            connector = get_connector()
            connector_type_hint = getattr(connector, "connector_type", "unknown")
    except ConnectorError as exc:
        return ConnectorTestResult(
            ok=False, detail=str(exc), connector_type=connector_type_hint
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("connector build failed during test")
        return ConnectorTestResult(
            ok=False,
            detail=f"could not build connector: {exc}",
            connector_type=connector_type_hint,
        )

    try:
        with killswitch.allow_manual_send():
            ok, detail = await connector.validate_config()
    except killswitch.KillSwitchTripped:
        return ConnectorTestResult(
            ok=False,
            detail="system is shutting down — try again after restart",
            connector_type=connector.connector_type,
        )
    except Exception as exc:
        logger.warning("connector validate_config raised: %s", exc)
        return ConnectorTestResult(
            ok=False,
            detail=f"validate_config raised: {exc}",
            connector_type=connector.connector_type,
        )

    return ConnectorTestResult(
        ok=ok, detail=detail, connector_type=connector.connector_type
    )


__all__ = ["router"]
