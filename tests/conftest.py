"""Shared pytest fixtures."""

import pytest

from reversible.registry import registry


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Snapshot the global registry around each test.

    ``@reversible`` auto-registers inverses by name in the global
    namespace, so a test-local fake named like a built-in (``delete_file``)
    permanently clobbers it for every later test in the process. Restoring
    the snapshot keeps tests order-independent.
    """
    snap = registry.snapshot()
    yield
    registry.restore(snap)
