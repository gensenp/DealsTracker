"""Liveness, and whether the database is actually reachable."""
from __future__ import annotations

import logging

from fastapi import APIRouter
from sqlalchemy import func, select

import db
from api import queries
from api.deps import SessionDep
from api.schemas import HealthResponse

log = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse,
            summary="Service and database health")
def health(session: SessionDep) -> HealthResponse:
    """Report whether the API is up and whether it can read its database.

    A health check that only proves the process is running would stay green
    while Supabase is unreachable and every other endpoint returns an error, so
    this counts rows. The count doubles as a useful signal in itself: a
    deployment pointed at an empty database looks healthy here but has nothing
    to serve, and `chains: 0` says so.
    """
    try:
        chains = session.scalar(select(func.count()).select_from(db.Chain))
        stores = session.scalar(select(func.count()).select_from(db.Store))
        last_scrape = queries.last_scrape_time(session)
    except Exception:
        # Deliberately broad: any failure to read is the same answer to the
        # caller. The detail belongs in the server log, not the response --
        # but it does belong in the log, or an unreachable database becomes a
        # silent "degraded" with nothing to debug.
        log.exception("health check could not read the database")
        return HealthResponse(status="degraded", database="unreachable")

    return HealthResponse(status="ok", database="ok", chains=chains or 0,
                          stores=stores or 0,
                          last_scrape_at=db.as_utc(last_scrape))
