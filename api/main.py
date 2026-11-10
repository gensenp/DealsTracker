"""The FastAPI application.

Run it from the project root, so `import db` and the rest resolve:

    uvicorn api.main:app --reload

Everything is read-only and comes from the database. The scraping pipeline is
untouched and still owned by pipeline.py and the Streamlit app.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api import config
from api.routers import chains, cities, health, stores

log = logging.getLogger(__name__)


def _error(status_code: int, code: str, message: str,
           detail: object | None = None) -> JSONResponse:
    """Every failure leaves by this door, in the shape schemas.ErrorResponse
    documents, so a client writes one error path instead of three."""
    body: dict = {"code": code, "message": message}
    if detail is not None:
        body["detail"] = detail
    return JSONResponse(status_code=status_code, content={"error": body})


def create_app() -> FastAPI:
    app = FastAPI(
        title=config.API_TITLE,
        version=config.API_VERSION,
        description=config.API_DESCRIPTION,
    )

    # Read at startup, not at import, so a deploy can set CORS_ORIGINS in the
    # environment it actually runs in. allow_credentials stays off: the API has
    # no cookies or sessions to carry, and leaving it on would make a wildcard
    # origin illegal for no benefit.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins(),
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        """Bad query parameters as 400, not FastAPI's default 422.

        422 is defensible -- the request parsed but failed validation -- but a
        bad `limit` or an unknown `sort` is a bad request in every sense a
        frontend cares about, and one 400 path is easier to handle than two
        codes meaning the same thing. The per-field detail is kept, so /docs
        and the response still say exactly which parameter was wrong.
        """
        return _error(400, "bad_request",
                      "One or more parameters were invalid.",
                      detail=[{"field": ".".join(str(p) for p in err["loc"]),
                               "problem": err["msg"]}
                              for err in exc.errors()])

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        """Handlers raise HTTPException with either a ready-made error body or
        a plain string; both come out in the documented shape."""
        if isinstance(exc.detail, dict) and "code" in exc.detail:
            payload = dict(exc.detail)
            return _error(exc.status_code, payload.pop("code"),
                          payload.pop("message", ""),
                          payload.get("detail"))
        code = "not_found" if exc.status_code == 404 else "error"
        return _error(exc.status_code, code, str(exc.detail))

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception):
        """A stack trace is for the log, not for the caller.

        Without this an unexpected failure -- Supabase refusing a connection,
        say -- would reach the browser as an HTML traceback naming file paths
        and the database driver.
        """
        log.exception("unhandled error on %s %s", request.method, request.url)
        return _error(500, "internal_error",
                      "The server could not complete that request.")

    app.include_router(health.router)
    app.include_router(cities.router)
    app.include_router(chains.router)
    app.include_router(stores.router)
    return app


app = create_app()
