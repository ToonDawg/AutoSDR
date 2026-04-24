"""Smoke tests for the normalised HTTPException handler.

The handler must:

* drop the default ``{"detail": ...}`` envelope when ``detail`` is a dict,
  so the frontend's 409 ``setup_required`` check works against the raw body;
* wrap bare-string details in ``{"error": "..."}`` for uniform client code.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from autosdr.api.errors import install_exception_handlers


def _client() -> TestClient:
    app = FastAPI()
    install_exception_handlers(app)

    @app.get("/dict")
    def _dict() -> None:
        raise HTTPException(status_code=409, detail={"setup_required": True})

    @app.get("/string")
    def _string() -> None:
        raise HTTPException(status_code=400, detail="invalid_json")

    @app.get("/error-dict")
    def _error_dict() -> None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "resource": "campaign"},
        )

    return TestClient(app, raise_server_exceptions=False)


def test_dict_detail_is_returned_flat() -> None:
    res = _client().get("/dict")
    assert res.status_code == 409
    assert res.json() == {"setup_required": True}


def test_string_detail_is_wrapped_in_error_key() -> None:
    res = _client().get("/string")
    assert res.status_code == 400
    assert res.json() == {"error": "invalid_json"}


def test_dict_detail_with_error_key_is_preserved() -> None:
    res = _client().get("/error-dict")
    assert res.status_code == 404
    assert res.json() == {"error": "not_found", "resource": "campaign"}
