"""Test fixtures for stepflow.

Provides isolated in-memory SQLite databases and helper factories
for building PipelineGraphs and mock StepRunners in tests.
"""

import pytest

from stepflow.core import StepFlow


@pytest.fixture
def sf():
    """StepFlow instance backed by an in-memory SQLite database.

    Each test gets a fresh, isolated instance. All stepflow_* tables
    are created automatically.
    """
    return StepFlow(":memory:")


@pytest.fixture
def sf_tmp(tmp_path):
    """StepFlow instance backed by a file-based SQLite database.

    Use when you need to simulate process crashes by re-opening the
    database from the same file path.
    """
    db_path = str(tmp_path / "test.db")
    return StepFlow(db_path)
