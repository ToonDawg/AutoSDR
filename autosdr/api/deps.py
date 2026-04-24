"""FastAPI dependencies shared across routers.

The helpers here wrap the two patterns that show up everywhere:

1. "I need the single Workspace row." If it doesn't exist the caller must
   redirect the operator to the setup wizard — :func:`require_workspace`
   raises 409 with ``{setup_required: true}`` so the fetch wrapper on the
   frontend knows to do that redirect automatically.
2. "I need a DB session." We reuse :func:`autosdr.db.session_scope` rather
   than building a dedicated FastAPI dependency so the same helpers work
   from tests and the CLI.
"""

from __future__ import annotations

from contextlib import contextmanager

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from autosdr.db import session_scope as _session_scope
from autosdr.models import Workspace

SETUP_REQUIRED_STATUS = status.HTTP_409_CONFLICT


@contextmanager
def db_session():
    with _session_scope() as session:
        yield session


def require_workspace(session: Session) -> Workspace:
    """Return the single workspace row, or 409 if setup hasn't run yet.

    Any API route that depends on workspace context calls this. The 409
    body matches the contract the frontend fetch wrapper looks for when
    deciding whether to redirect to /setup.
    """

    workspace = session.query(Workspace).first()
    if workspace is None:
        raise HTTPException(
            status_code=SETUP_REQUIRED_STATUS,
            detail={"setup_required": True},
        )
    return workspace


__all__ = ["SETUP_REQUIRED_STATUS", "db_session", "require_workspace"]
