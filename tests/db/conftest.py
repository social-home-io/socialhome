"""Fixtures for the database / migration tests."""

import pytest

from tests.db.connections import close_all


@pytest.fixture(autouse=True)
def _close_migration_connections():
    """Close every connection the test opened via ``tests.db.connections``."""
    yield
    close_all()
