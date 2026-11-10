"""Shared dependencies: the database session and the paging parameters.

The session is a FastAPI dependency rather than a `with session_scope()` inside
each handler, for two reasons. One is that it is the idiomatic shape, and keeps
handlers free of transaction bookkeeping. The other is testability: a test
overrides `get_session` and the whole app reads its fixture's database.

That override is not a nicety. TestClient runs the app in a worker thread, and
an in-memory SQLite database uses SQLAlchemy's SingletonThreadPool -- one
connection per thread. Without the override the API would open its own
connection in that worker thread, find an empty database, and every test would
assert against nothing.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Iterator

from fastapi import Depends, Query
from sqlalchemy.orm import Session

import db

# Caps on how much one request may ask for. A limit is a promise about response
# size, so it is enforced rather than trusted.
MAX_PAGE_SIZE = 200
MAX_DEALS_PER_CHAIN = 100


def get_session() -> Iterator[Session]:
    """A session per request, closed when the response is done.

    Read-only, so it never commits: the API writes nothing in this phase, and a
    handler that tried would be a bug worth noticing.
    """
    db.init_db()
    session = db.new_session()
    try:
        yield session
    finally:
        session.close()


SessionDep = Annotated[Session, Depends(get_session)]


@dataclass
class PageParams:
    """limit/offset, validated.

    Offset paging rather than cursors: the row counts here are small -- a chain
    holds at most deals.MAX_ITEMS products -- there is no high-churn append
    stream for an offset to drift against, and a shopper-facing list wants to
    jump to a page rather than walk one.
    """
    limit: int
    offset: int


def page_params(
    limit: Annotated[int, Query(
        ge=1, le=MAX_PAGE_SIZE,
        description=f"Rows to return, 1-{MAX_PAGE_SIZE}.")] = 50,
    offset: Annotated[int, Query(
        ge=0, description="Rows to skip before this page.")] = 0,
) -> PageParams:
    return PageParams(limit=limit, offset=offset)


PageDep = Annotated[PageParams, Depends(page_params)]
