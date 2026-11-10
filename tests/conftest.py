"""Shared fixtures. Every test runs against an in-memory SQLite database, so
the suite needs no network, no API keys and no files on disk."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db as db_module


@pytest.fixture
def session():
    """A session against a fresh in-memory database, torn down afterwards."""
    db_module.reset_engine_for_testing("sqlite://")
    db_module.init_db()
    with db_module.session_scope() as s:
        yield s
    db_module.reset_engine_for_testing(None)


@pytest.fixture
def client(session):
    """A TestClient whose app reads the `session` fixture's database.

    The dependency override is what makes this work at all. TestClient runs the
    app in a worker thread, and an in-memory SQLite database uses SQLAlchemy's
    SingletonThreadPool -- one connection per thread -- so an un-overridden app
    would open a second, empty database in that thread and every assertion here
    would be made against nothing.

    Handing the API the fixture's own session also lets a test seed rows and
    read them back without committing first.
    """
    from fastapi.testclient import TestClient

    from api.deps import get_session
    from api.main import app

    app.dependency_overrides[get_session] = lambda: session
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
