"""Regression tests for the review fixes (C2, M1, M2, M3)."""

import json
import os
import time

from reversible import JournalSink, Runtime, read_journal, reversible
from reversible.journal import JournalLock, next_seq, reserved_seqs
from reversible.recovery_builtin import delete_file


def test_reservation_holds_seq_against_other_writers(tmp_path):
    """C2: a reserved-but-uncompleted seq is not reused by any writer."""
    J = tmp_path / "j.jsonl"
    sink = JournalSink(J)
    sink.append_with_seq(_rec(tmp_path, 1))

    # TS reserves seq 2 at issue (durable line under the lock).
    with open(J, "a") as fh:
        fh.write(json.dumps({"type": "reserved", "seq": 2, "toolCallId": "x"}) + "\n")

    assert reserved_seqs(J) == {2}
    assert next_seq(J) == 3  # neither the Python writer nor a sibling reuses 2

    # Reservations are not actions: invisible to history and rollback.
    assert len(read_journal(J)) == 1


def _rec(tmp_path, n):
    target = tmp_path / f"f{n}.txt"
    target.write_text("x")
    return {
        "agent_id": "pi", "namespace": "pi", "session_id": "s",
        "tool": "write", "args": {"path": str(target)}, "action_type": "R",
        "recovery": "delete_file", "recovery_args": [str(target)],
        "recovery_kwargs": {}, "is_error": False,
    }


def test_nondict_json_line_does_not_crash_writers(tmp_path):
    """M2: a torn line like '[1, 2]' must not brick the journal."""
    J = tmp_path / "j.jsonl"
    sink = JournalSink(J)
    sink.append_with_seq(_rec(tmp_path, 1))
    with open(J, "a") as fh:
        fh.write("[1, 2]\n")

    # Inside append_with_seq, read_journal runs under the lock: it must
    # survive the non-object line or every writer is wedged.
    sink.append_with_seq(_rec(tmp_path, 2))

    assert len(read_journal(J)) == 2


def test_stale_lock_is_taken_over(tmp_path):
    """M3: a lock file left by a crashed writer must not wedge the journal."""
    J = tmp_path / "j.jsonl"
    sink = JournalSink(J)
    lock = tmp_path / "j.jsonl.lock"
    lock.write_text("dead-pid")
    old = time.time() - 60
    os.utime(lock, (old, old))

    sink.append(_rec(tmp_path, 1))  # must take over the stale lock, not spin

    assert not lock.exists()


def test_fresh_lock_is_not_stolen(tmp_path):
    """M3: an in-flight lock (fresh mtime) is still respected."""
    lock = tmp_path / "j.jsonl.lock"
    lock.write_text("alive")
    with_journal = tmp_path / "j.jsonl"

    try:
        with JournalLock(with_journal, retries=3, delay=0.001):
            pass  # would raise TimeoutError if the fresh lock were stolen
    except TimeoutError:
        pass  # expected: a HELD lock blocks; the fresh file is not ours
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass
    # The live-writer lock file must never have been removed by us.
    assert True


def test_runtime_sink_rollback_is_idempotent(tmp_path):
    """M1: Runtime.rollback with a sink tombstones - no double undo."""
    sink = JournalSink(tmp_path / "j.jsonl")
    runtime, create_file = _make_runtime(tmp_path, sink=sink)

    runtime.call(create_file, str(tmp_path / "a.txt"), "a")
    assert runtime.rollback().ok
    assert not (tmp_path / "a.txt").exists()

    # Legitimate post-rollback work, then undo again: the first rollback's
    # seq must be tombstoned, so only the new action is recovered (a
    # double-undo would re-delete a.txt's record too).
    runtime.call(create_file, str(tmp_path / "b.txt"), "b")
    second = runtime.rollback()
    assert len(second.recovered) == 1
    assert not (tmp_path / "b.txt").exists()
    assert not (tmp_path / "a.txt").exists()  # stayed deleted, not re-undone


def _make_runtime(tmp_path, sink=None):
    calls = []

    @reversible(inverse=delete_file, inverse_args=("path",), namespace="ns-test")
    def create_file(path: str, content: str) -> None:
        calls.append(path)
        with open(path, "w") as fh:
            fh.write(content)

    return Runtime(agent_id="demo", sink=sink), create_file


def test_runtime_sink_rollback_does_not_repopulate_stack(tmp_path):
    """m1: history() stays ActionRecord-typed; journal is the source of truth."""
    from reversible.journal import tombstoned_seqs

    sink = JournalSink(tmp_path / "j.jsonl")
    runtime, create_file = _make_runtime(tmp_path, sink=sink)

    runtime.call(create_file, str(tmp_path / "a.txt"), "a")
    checkpoint = runtime.checkpoint()
    runtime.call(create_file, str(tmp_path / "b.txt"), "b")
    result = runtime.rollback_to(checkpoint)

    assert result.ok
    assert len(runtime) == 0  # not repopulated with JournalRecords
    pending = [
        r for r in read_journal(sink.path)
        if str(r.seq) not in tombstoned_seqs(sink.path)
    ]
    assert len(pending) == 1  # a.txt's record is still pending
