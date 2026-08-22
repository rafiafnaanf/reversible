"""Tests for the race-condition fix (cross-language journal lock).

The journal is multi-writer. The lock serializes the read-max + append
critical section so concurrent writers never produce duplicate seqs.
"""

import json
import threading

from reversible import JournalSink, read_journal


def _append(sink: JournalSink, agent: str, n: int) -> list[int]:
    seqs = []
    for i in range(n):
        rec = {
            "agent_id": agent,
            "session_id": "s",
            "tool": "write",
            "args": {"path": f"/x/{i}"},
            "action_type": "R",
            "recovery": "restore_file",
            "recovery_args": [],
            "recovery_kwargs": {},
            "is_error": False,
        }
        seq = sink.append_with_seq(rec)
        seqs.append(seq)
    return seqs


def test_concurrent_writers_produce_unique_seqs(tmp_path):
    """Two threads appending concurrently must never collide on seq."""
    path = tmp_path / "journal.jsonl"
    sink = JournalSink(path)
    n = 50

    results: list[list[int]] = []
    threads = [
        threading.Thread(target=lambda: results.append(_append(sink, f"agent-{i}", n)))
        for i in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    all_seqs = [s for sub in results for s in sub]
    assert len(all_seqs) == 2 * n
    assert len(set(all_seqs)) == 2 * n  # no duplicates
    assert sorted(all_seqs) == list(range(1, 2 * n + 1))  # contiguous, no gaps


def test_append_with_seq_assigns_and_persists(tmp_path):
    path = tmp_path / "journal.jsonl"
    sink = JournalSink(path)

    seq1 = sink.append_with_seq({"agent_id": "a", "tool": "write", "action_type": "R",
                                 "recovery": "noop", "recovery_args": [],
                                 "recovery_kwargs": {}, "is_error": False})
    seq2 = sink.append_with_seq({"agent_id": "b", "tool": "edit", "action_type": "R",
                                 "recovery": "noop", "recovery_args": [],
                                 "recovery_kwargs": {}, "is_error": False})

    assert seq1 == 1
    assert seq2 == 2

    records = read_journal(path)
    assert [r.seq for r in records] == [1, 2]


def test_lock_released_after_append(tmp_path):
    """After an append, the lock file must be gone (released)."""
    path = tmp_path / "journal.jsonl"
    sink = JournalSink(path)
    sink.append_with_seq({"agent_id": "a", "tool": "write", "action_type": "R",
                         "recovery": "noop", "recovery_args": [],
                         "recovery_kwargs": {}, "is_error": False})
    assert not (tmp_path / "journal.jsonl.lock").exists()


def test_lock_blocks_concurrent_holder(tmp_path):
    """While the lock is held, a second writer must wait, not collide."""
    import pytest

    from reversible.journal import JournalLock

    path = tmp_path / "journal.jsonl"
    lock = JournalLock(path)
    lock.acquire()
    try:
        lock2 = JournalLock(path, retries=5, delay=0.001)
        with pytest.raises(TimeoutError):
            lock2.acquire()
    finally:
        lock.release()
