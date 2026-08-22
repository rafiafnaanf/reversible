"""Tests for the namespace collision fix.

Two modules/agents may both register a recovery named the same thing (e.g.
both named ``X``). The namespace field separates them so rollback resolves
the correct recovery for each record.
"""

import os

from reversible import JournalSink, Runtime, reversible, registry


def test_two_namespaces_same_recovery_name_do_not_collide(tmp_path):
    """Two agents with a recovery named 'X' resolve independently."""
    calls = []

    # coding-agent's X recovery
    def coding_X(path: str) -> None:
        calls.append(("coding", path))

    # ghidra-mcp's X recovery (same name, different behavior)
    def ghidra_X(path: str) -> None:
        calls.append(("ghidra", path))

    # Register both under their own namespaces.
    registry.register_recovery("X", coding_X, namespace="coding-agent")
    registry.register_recovery("X", ghidra_X, namespace="ghidra-mcp")

    # Resolve each within its namespace.
    assert registry.lookup_by_name("X", namespace="coding-agent") is coding_X
    assert registry.lookup_by_name("X", namespace="ghidra-mcp") is ghidra_X
    # Global (built-in) namespace still resolves built-ins.
    assert registry.lookup_by_name("delete_file") is not None


def test_rollback_uses_record_namespace(tmp_path):
    """A journal record resolves its recovery within its own namespace."""
    sink = JournalSink(tmp_path / "j.jsonl")
    calls = []

    def coding_X(path: str) -> None:
        calls.append(("coding", path))
        if os.path.exists(path):
            os.remove(path)

    def ghidra_X(path: str) -> None:
        calls.append(("ghidra", path))
        if os.path.exists(path):
            os.remove(path)

    registry.register_recovery("X", coding_X, namespace="coding-agent")
    registry.register_recovery("X", ghidra_X, namespace="ghidra-mcp")

    @reversible(inverse=registry.lookup_by_name("X", namespace="coding-agent"),
                inverse_args=("path",))
    def create_file(path: str, content: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)

    # coding-agent records; its record carries namespace="coding-agent".
    runtime = Runtime(agent_id="coding-agent", namespace="coding-agent",
                      sink=sink)
    runtime.call(create_file, str(tmp_path / "f.txt"), "x")

    # Rollback should use coding_X (the coding-agent namespace), not ghidra_X.
    from reversible.rollback import RollbackEngine
    from reversible.journal import read_journal

    records = read_journal(sink.path)
    engine = RollbackEngine(records)
    result = engine.rollback()

    assert result.ok
    assert calls == [("coding", str(tmp_path / "f.txt"))]  # coding_X used
    assert not os.path.exists(tmp_path / "f.txt")


def test_record_serializes_namespace(tmp_path):
    """ActionRecord.to_journal carries the namespace through."""
    sink = JournalSink(tmp_path / "j.jsonl")

    def delete_file(path: str) -> None:
        if os.path.exists(path):
            os.remove(path)

    @reversible(inverse=delete_file, inverse_args=("path",))
    def create_file(path: str, content: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)

    runtime = Runtime(agent_id="a", namespace="my-ns", sink=sink)
    runtime.call(create_file, str(tmp_path / "x.txt"), "x")

    from reversible.journal import read_journal

    records = read_journal(sink.path)
    assert records[0].namespace == "my-ns"
    assert records[0].recovery == "delete_file"


def test_register_recovery_warns_on_collision(caplog):
    """Overwriting a same-named recovery in a namespace warns (not silent)."""
    import logging

    from reversible import registry as reg

    def fn_a(path: str) -> None:
        return None

    def fn_b(path: str) -> None:
        return None

    with caplog.at_level(logging.WARNING, logger="reversible"):
        reg.register_recovery("collide", fn_a, namespace="ns-warn")
        reg.register_recovery("collide", fn_b, namespace="ns-warn")

    assert any("overwriting recovery" in r.message for r in caplog.records)


def test_register_recovery_same_fn_no_warning(caplog):
    """Re-registering the SAME function under a name is not a collision."""
    import logging

    from reversible import registry as reg

    def fn(path: str) -> None:
        return None

    with caplog.at_level(logging.WARNING, logger="reversible"):
        reg.register_recovery("same", fn, namespace="ns-ok")
        reg.register_recovery("same", fn, namespace="ns-ok")

    assert not any("overwriting recovery" in r.message for r in caplog.records)
