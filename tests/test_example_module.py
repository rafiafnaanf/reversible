"""Tests for the example custom tool module.

Verifies the pattern documented in examples/example_tool_module.py actually
works: decorated tools are recorded, read-only tools are not, and recovery
metadata is attached correctly.
"""

import os
import sys
from pathlib import Path

import pytest

# The example lives outside the package (under examples/). Import it as a
# module so tests exercise the exact same code a developer would copy.
EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
sys.path.insert(0, str(EXAMPLES))

import example_tool_module as ex  # noqa: E402


def test_create_file_is_recorded_as_reversible(tmp_path):
    runtime = ex.Runtime(agent_id="test", session_id="s1")
    path = str(tmp_path / "hello.txt")
    runtime.call(ex.create_file, path, "Hello world")

    assert len(runtime) == 1
    record = runtime.history()[0]
    assert record.action is ex.create_file
    assert record.action_type.value == "R"
    assert record.recovery is ex.delete_file
    # recovery_args were forwarded by the inverse_args=("path",) selector
    assert record.recovery_args == (path,)


def test_read_only_tool_is_not_recorded(tmp_path):
    runtime = ex.Runtime(agent_id="test", session_id="s1")
    path = str(tmp_path / "hello.txt")
    runtime.call(ex.create_file, path, "Hello world")
    runtime.call(ex.read_file, path)

    assert len(runtime) == 1  # only create_file recorded
    record = runtime.history()[0]
    assert record.action is ex.create_file  # read_file was skipped


def test_compensable_tool_is_recorded(tmp_path):
    runtime = ex.Runtime(agent_id="test", session_id="s1")
    runtime.call(ex.send_notification, "hi")

    assert len(runtime) == 1
    record = runtime.history()[0]
    assert record.action_type.value == "K"
    assert record.recovery is ex.cancel_notification
    assert record.recovery_args == ("hi",)


def test_example_module_imports_cleanly():
    assert callable(ex.create_file)
    assert callable(ex.send_notification)
    assert callable(ex.read_file)
    assert hasattr(ex, "Runtime")
    assert hasattr(ex, "reversible")
    assert hasattr(ex, "compensable")