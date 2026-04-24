"""Error-shape normalisation for the API.

FastAPI's default :class:`HTTPException` handler wraps ``detail`` in a
``{"detail": ...}`` envelope. Our frontend and CLI both expect the detail
*itself* to be the response body — the 409 setup-required check on
``body.setup_required`` only works if the handler drops the envelope,
and the rest of the routers already pass dict details shaped as
``{"error": "<snake_case>"}``.

We also have a handful of legacy raises that pass a bare string detail.
Rather than chase them all, the handler here normalises both forms:

* ``raise HTTPException(detail={"error": "thread_not_found"})``  →
  body = ``{"error": "thread_not_found"}``
* ``raise HTTPException(detail="invalid_json")``                 →
  body = ``{"error": "invalid_json"}``
* ``raise HTTPException(detail={"setup_required": True})``      →
  body = ``{"setup_required": true}``   (preserved verbatim)

Registering this handler is the last step of :func:`autosdr.webhook.create_app`.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.requests import Request
from fastapi.responses import JSONResponse


def _body_for(detail: Any) -> dict[str, Any]:
    if isinstance(detail, dict):
        return detail
    return {"error": str(detail)}


async def _http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=_body_for(exc.detail),
        headers=exc.headers or None,
    )


def install_exception_handlers(app: FastAPI) -> None:
    """Attach the normalised HTTPException handler to ``app``."""

    app.add_exception_handler(HTTPException, _http_exception_handler)


__all__ = ["install_exception_handlers"]
