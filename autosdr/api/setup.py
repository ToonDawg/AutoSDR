"""First-run setup wizard endpoints.

The frontend calls ``GET /api/setup/required`` on every mount. If the server
has no workspace row yet the single-page app redirects to ``/setup`` and
collects business info, an LLM key, and a connector choice. Submitting the
wizard POSTs to ``/api/setup``, which creates the workspace row, seeds
``settings`` from :func:`default_workspace_settings`, and — crucially — hot-
loads the new LLM key into the process and rebuilds the connector so the
operator can start sending without restarting.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from autosdr.api.deps import db_session
from autosdr.api.schemas import SetupRequest, SetupStatus, WorkspaceOut
from autosdr.config import default_workspace_settings
from autosdr.connectors import rebuild_connector
from autosdr.llm import apply_llm_provider_keys
from autosdr.models import Workspace

router = APIRouter(prefix="/api/setup", tags=["setup"])


@router.get("/required", response_model=SetupStatus)
def is_setup_required() -> SetupStatus:
    """Return whether the setup wizard still needs to run.

    ``setup_required=true`` = no workspace row yet. The SPA reads this on
    mount to decide whether to force-navigate the user to /setup.
    """

    with db_session() as session:
        workspace = session.query(Workspace).first()
        if workspace is None:
            return SetupStatus(setup_required=True)
        return SetupStatus(setup_required=False, workspace_id=workspace.id)


@router.post("", response_model=WorkspaceOut, status_code=status.HTTP_201_CREATED)
def run_setup(payload: SetupRequest) -> WorkspaceOut:
    """Create the single workspace row from wizard input.

    Fails with 409 if a workspace already exists — setup is idempotent at
    the "is there one?" level but refuses to clobber existing config. Use
    ``PATCH /api/workspace`` / ``PATCH /api/workspace/settings`` to edit.
    """

    with db_session() as session:
        if session.query(Workspace).first() is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "workspace_already_exists"},
            )

        settings = default_workspace_settings()

        # LLM provider + key.
        settings["llm"]["provider_api_keys"][payload.llm_provider] = (
            payload.llm_api_key.strip()
        )
        if payload.model_main:
            settings["llm"]["model_main"] = payload.model_main
            # For a single-key setup we assume the same family is fine for
            # the subsidiary roles too — operator can split them later in
            # the Settings page.
            settings["llm"]["model_analysis"] = payload.model_main

        # Connector.
        settings["connector"]["type"] = payload.connector_type
        if payload.connector_type == "textbee":
            if payload.textbee is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"error": "textbee_credentials_required"},
                )
            settings["connector"]["textbee"].update(
                payload.textbee.model_dump(exclude_none=True)
            )
        elif payload.connector_type == "smsgate":
            if payload.smsgate is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"error": "smsgate_credentials_required"},
                )
            settings["connector"]["smsgate"].update(
                payload.smsgate.model_dump(exclude_none=True)
            )

        workspace = Workspace(
            business_name=payload.business_name.strip(),
            business_dump=payload.business_dump.strip(),
            tone_prompt=(payload.tone_prompt or "").strip() or None,
            settings=settings,
        )
        session.add(workspace)
        session.flush()
        session.refresh(workspace)
        out = WorkspaceOut.model_validate(workspace)

    # Hot-apply the new config so the operator doesn't have to restart.
    apply_llm_provider_keys(out.settings)
    rebuild_connector(out.settings)

    return out


__all__ = ["router"]
