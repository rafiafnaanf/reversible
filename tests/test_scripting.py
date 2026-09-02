"""Tests for the scripting paths: verify-in-journal, pass env, fingerprints."""

import json
import os

import pytest

from reversible import JournalSink, Runtime, read_journal, reversible
from reversible.journal import tombstoned_seqs
from reversible.recovery_builtin import delete_file
from reversible.registry import registry
from reversible.rollback import RollbackEngine


def _mk_runtime(tmp_path):
    sink = JournalSink(tmp_path / "j.jsonl")

    def path_gone(path):
        return not os.path.exists(path)

    @reversible(inverse=delete_file, inverse_args=("path",), verify=path_gone)
    def create_file(path, content):
        with open(path, "w") as fh:
            fh.write(content)

    runtime = Runtime(agent_id="a", session_id="sess", sink=sink)
    return runtime, create_file, sink


def test_verify_name_survives_journal_and_runs_at_rollback(tmp_path):
    """Named verify predicates are serialized and run on journal rollback."""
    runtime, create_file, sink = _mk_runtime(tmp_path)
    runtime.call(create_file, str(tmp_path / "new.txt"), "x")

    record = read_journal(sink.path)[0]
    assert record.verify == "path_gone"

    result = RollbackEngine(read_journal(sink.path)).rollback()
    assert result.ok and result.recovered
    assert not (tmp_path / "new.txt").exists()


def test_verify_failure_fails_closed_and_stays_pending(tmp_path):
    """A recovery that does not restore fails loudly and is not tombstoned."""
    sink = JournalSink(tmp_path / "j.jsonl")
    target = tmp_path / "f.txt"
    target.write_text("x")

    def path_gone(path):
        return not os.path.exists(path)

    registry.register_verify("path_gone", path_gone)
    # recovery is noop (does not restore) but verify demands restoration:
    sink.append({
        "agent_id": "a", "namespace": "", "session_id": "s", "tool": "write",
        "args": {"path": str(target)}, "action_type": "R", "recovery": "noop",
        "recovery_args": [str(target)], "recovery_kwargs": {}, "is_error": False,
        "verify": "path_gone",
    })

    result = RollbackEngine(read_journal(sink.path)).rollback()

    assert not result.ok  # cannot claim restored
    assert result.failed and "verification failed" in result.failed[0][1]
    assert tombstoned_seqs(sink.path) == set()  # stays pending


def test_unresolvable_verify_name_fails_loudly(tmp_path):
    sink = JournalSink(tmp_path / "j.jsonl")
    sink.append({
        "agent_id": "a", "namespace": "", "session_id": "s", "tool": "write",
        "args": {}, "action_type": "R", "recovery": "noop",
        "recovery_args": [], "recovery_kwargs": {}, "is_error": False,
        "verify": "ghost_verify",
    })
    result = RollbackEngine(read_journal(sink.path)).rollback()
    assert not result.ok and "no verify registered" in result.failed[0][1]


def test_lambda_verify_is_not_serialized(tmp_path, caplog):
    """Lambdas cannot be named: in-memory verify only, journal unverified."""
    runtime, create_file, sink = _mk_runtime(tmp_path)
    import os

    @reversible(inverse=delete_file, inverse_args=("path",),
                verify=lambda path: not os.path.exists(path))
    def create_lambda(path, content):
        with open(path, "w") as fh:
            fh.write(content)

    runtime.call(create_lambda, str(tmp_path / "l.txt"), "x")
    assert read_journal(sink.path)[0].verify is None


def test_runtime_inherits_journal_coords_from_env(tmp_path, monkeypatch):
    """Pass path: env vars configure an unconfigured Runtime."""
    from reversible.journal import JournalSink as JS

    journal = tmp_path / "env-journal.jsonl"
    monkeypatch.setenv("REVERSIBLE_JOURNAL", str(journal))
    monkeypatch.setenv("REVERSIBLE_AGENT_ID", "child-script")
    monkeypatch.setenv("REVERSIBLE_SESSION_ID", "parent-session")
    monkeypatch.setenv("REVERSIBLE_NAMESPACE", "pi")

    def noop():
        pass

    registry.register_recovery("noop_test", noop)

    @reversible(inverse=noop, namespace="pi")
    def touch():
        pass

    runtime = Runtime()  # no args: everything from env
    assert runtime._agent_id == "child-script"
    assert runtime._session_id == "parent-session"
    assert runtime._namespace == "pi"
    assert runtime._sink is not None and str(runtime._sink.path) == str(journal)

    runtime.call(touch)
    record = read_journal(journal)[0]
    assert (record.agent_id, record.session_id, record.namespace) == (
        "child-script", "parent-session", "pi",
    )


def test_explicit_args_beat_env(tmp_path, monkeypatch):
    monkeypatch.setenv("REVERSIBLE_AGENT_ID", "from-env")
    runtime = Runtime(agent_id="explicit")
    assert runtime._agent_id == "explicit"


def test_journal_record_round_trips_verify(tmp_path):
    sink = JournalSink(tmp_path / "j.jsonl")
    sink.append({
        "agent_id": "a", "namespace": "", "session_id": "s", "tool": "t",
        "args": {}, "action_type": "R", "recovery": "noop", "recovery_args": [],
        "recovery_kwargs": {}, "is_error": False, "verify": "path_gone",
    })
    record = read_journal(sink.path)[0]
    assert record.verify == "path_gone"
    assert json.loads(json.dumps(record.to_dict()))["verify"] == "path_gone"
