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
