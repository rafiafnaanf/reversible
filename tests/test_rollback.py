"""Tests for the rollback engine."""

import os

import pytest

from reversible import (
    JournalSink,
    RollbackResult,
    Runtime,
    compensable,
    reversible,
)


def test_rollback_r_reverses_lifo_order(tmp_path):
    """A → B → C rollback must run C⁻¹ → B⁻¹ → A⁻¹."""
    calls = []
    created = []

    def delete_file(path: str) -> None:
        calls.append(("delete", path))
        if os.path.exists(path):
            os.remove(path)

    @reversible(inverse=delete_file, inverse_args=("path",))
    def create_file(path: str, content: str) -> None:
        created.append(path)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)

    runtime = Runtime()
    for i in range(3):
        runtime.call(create_file, str(tmp_path / f"f{i}.txt"), str(i))

    result = runtime.rollback()
    assert result.ok
    # LIFO: delete f2, f1, f0
    assert [c[1] for c in calls] == [
        str(tmp_path / "f2.txt"),
        str(tmp_path / "f1.txt"),
        str(tmp_path / "f0.txt"),
    ]
    assert len(runtime) == 0


def test_rollback_k_compensates(tmp_path):
    """K actions run their compensation during rollback."""
    calls = []

    def cancel(message: str) -> None:
        calls.append(("cancel", message))

    @compensable(compensation=cancel, compensation_args=("message",))
    def send(message: str) -> None:
        calls.append(("send", message))

    runtime = Runtime()
    runtime.call(send, "hello")

    result = runtime.rollback()
    assert result.ok
    assert calls == [("send", "hello"), ("cancel", "hello")]


def test_rollback_scoped_by_agent(tmp_path):
    """rollback(agent_id=...) only undoes that agent's actions."""
    calls = []

    def delete_file(path: str) -> None:
        calls.append(("delete", path))
        if os.path.exists(path):
            os.remove(path)

    @reversible(inverse=delete_file, inverse_args=("path",))
    def create_file(path: str, content: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)

    sink = JournalSink(tmp_path / "j.jsonl")
    a = Runtime(agent_id="A", sink=sink)
    b = Runtime(agent_id="B", sink=sink)

    a.call(create_file, str(tmp_path / "a.txt"), "a")
    b.call(create_file, str(tmp_path / "b.txt"), "b")
    a.call(create_file, str(tmp_path / "a2.txt"), "a2")

    result = b.rollback(agent_id="B")
    assert result.ok
    assert len(result.recovered) == 1
    # A's files remain
    assert os.path.exists(tmp_path / "a.txt")
    assert os.path.exists(tmp_path / "a2.txt")
    assert not os.path.exists(tmp_path / "b.txt")


def test_rollback_failure_stops_and_keeps_failed(tmp_path):
    """On recovery failure: stop, keep failed action, surface error."""
    calls = []

    def delete_file(path: str) -> None:
        calls.append(("delete", path))
        if os.path.exists(path):
            os.remove(path)

    # Recovery that fails for f1.txt specifically.
    def failing_delete(path: str) -> None:
        if path.endswith("f1.txt"):
            raise RuntimeError("boom")
        delete_file(path)

    @reversible(inverse=failing_delete, inverse_args=("path",))
    def create_file(path: str, content: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)

    runtime = Runtime()
    runtime.call(create_file, str(tmp_path / "f0.txt"), "0")
    runtime.call(create_file, str(tmp_path / "f1.txt"), "1")
    runtime.call(create_file, str(tmp_path / "f2.txt"), "2")

    result = runtime.rollback()

    assert not result.ok
    assert result.stopped
    assert len(result.failed) == 1
    # f2 recovered (LIFO first), f1 failed, f0 NOT attempted (stopped).
    assert not os.path.exists(tmp_path / "f2.txt")  # f2 recovered
    assert os.path.exists(tmp_path / "f0.txt")  # never recovered
    # f0, f1 remain in stack (f2 recovered)
    assert len(runtime) == 2


def test_rollback_verifies_restoration(tmp_path):
    """verify_recovery() runs after recovery; failure surfaces."""
    calls = []

    def delete_file(path: str) -> None:
        calls.append(("delete", path))
        if os.path.exists(path):
            os.remove(path)

    @reversible(
        inverse=delete_file,
        inverse_args=("path",),
        verify=lambda path: not os.path.exists(path),
    )
    def create_file(path: str, content: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)

    runtime = Runtime()
    runtime.call(create_file, str(tmp_path / "v.txt"), "v")

    # Recovery deletes the file → verify passes.
    result = runtime.rollback()
    assert result.ok
    assert not os.path.exists(tmp_path / "v.txt")


def test_rollback_result_ok_property():
    assert RollbackResult(recovered=["1"], failed=[]).ok
    assert not RollbackResult(recovered=["1"], failed=[("1", "err")]).ok
    assert not RollbackResult(recovered=["1"], failed=[], stopped=True).ok
