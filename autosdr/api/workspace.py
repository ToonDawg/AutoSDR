"""Workspace settings + identity.

Two endpoints matter here:

* ``GET /api/workspace`` — current workspace row (business info + settings).
* ``PATCH /api/workspace/settings`` — deep-merge updates into ``settings``
  and hot-reapply the LLM keys + connector so changes take effect without
  a restart. The Settings page in the UI hits this constantly.
"""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy.orm.attributes import flag_modified

from autosdr.api.deps import db_session, require_workspace
from autosdr.api.schemas import WorkspaceOut, WorkspacePatch, WorkspaceSettingsPatch
from autosdr.config import merge_workspace_settings
from autosdr.connectors import rebuild_connector
from autosdr.llm import apply_llm_provider_keys

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
         credential rotations / dry-run flips take effect immediately.

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


__all__ = ["router"]
