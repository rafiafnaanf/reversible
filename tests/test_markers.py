"""Tests for rollback markers: idempotent undo via tombstoned seqs."""

import json

from reversible import JournalSink, read_journal
from reversible.journal import (
    mark_rolled_back,
    read_rollback_markers,
    tombstoned_seqs,
)
from reversible.recovery_builtin import delete_file
from reversible.rollback import RollbackEngine


def _write_records(sink: JournalSink, tmp_path, n: int = 3) -> list[str]:
    seqs = []
    for i in range(n):
        target = tmp_path / f"f{i}.txt"
        target.write_text("x")
        seq = sink.append_with_seq(
            {
                "agent_id": "pi",
                "namespace": "pi",
                "session_id": "s",
                "tool": "write",
                "args": {"path": str(target)},
                "action_type": "R",
                "recovery": "delete_file",
                "recovery_args": [str(target)],
                "recovery_kwargs": {},
                "is_error": False,
            }
        )
        seqs.append(str(seq))
    return seqs


def test_marker_appended_and_skipped_by_read_journal(tmp_path):
    J = tmp_path / "j.jsonl"
    sink = JournalSink(J)
    _write_records(sink, tmp_path)

    mark_rolled_back(J, recovered=["2", "3"], failed=[])

    lines = [json.loads(l) for l in J.read_text().splitlines() if l.strip()]
    assert lines[-1]["type"] == "rollback"
    assert lines[-1]["recovered"] == ["2", "3"]

    # Markers are not actions: read_journal returns only the 3 records.
    assert len(read_journal(J)) == 3
    assert len(read_rollback_markers(J)) == 1


def test_tombstoned_seqs_union_across_markers(tmp_path):
    J = tmp_path / "j.jsonl"
    sink = JournalSink(J)
    _write_records(sink, tmp_path, n=4)

    mark_rolled_back(J, recovered=["4"], failed=[])
    mark_rolled_back(J, recovered=["3"], failed=[])

    assert tombstoned_seqs(J) == {"3", "4"}


def test_rollback_is_idempotent(tmp_path, capsys):
    """A second rollback after a full one is a no-op, not a double undo.

    Exercises the real orchestration path (the CLI): it writes the marker
    after recovering, so the second run finds nothing pending.
    """
    from reversible.cli import main as cli_main

    J = tmp_path / "j.jsonl"
    sink = JournalSink(J)
    seqs = _write_records(sink, tmp_path)
    files = sorted(tmp_path.glob("f*.txt"))

    assert cli_main(["rollback", "--journal", str(J)]) == 0
    leftover = [f.name for f in files if f.exists()]
    assert not leftover, f"files survived rollback: {leftover}"

    # Second pass: recovered seqs are tombstoned -> nothing pending.
    assert cli_main(["rollback", "--journal", str(J)]) == 0
    out = capsys.readouterr().out
    assert "no records to undo" in out
    assert tombstoned_seqs(J) == set(seqs)


def test_failed_seqs_stay_pending(tmp_path):
    """Failed actions are not tombstoned: they remain pending for retry."""
    J = tmp_path / "j.jsonl"
    sink = JournalSink(J)
    target1, target2 = tmp_path / "f0.txt", tmp_path / "f1.txt"
    target1.write_text("x")
    target2.write_text("x")

    # seq 1 recovers fine; seq 2 has an unresolvable recovery (fails).
    s1 = str(sink.append_with_seq({
        "agent_id": "pi", "namespace": "pi", "session_id": "s",
        "tool": "write", "args": {"path": str(target1)}, "action_type": "R",
        "recovery": "delete_file", "recovery_args": [str(target1)],
        "recovery_kwargs": {}, "is_error": False,
    }))
    s2 = str(sink.append_with_seq({
        "agent_id": "pi", "namespace": "pi", "session_id": "s",
        "tool": "write", "args": {"path": str(target2)}, "action_type": "R",
        "recovery": "ghost_recovery", "recovery_args": [str(target2)],
        "recovery_kwargs": {}, "is_error": False,
    }))

    records = read_journal(J)
    result = RollbackEngine(records).rollback()  # strict: stops at seq 2
    assert result.recovered == []
    assert result.failed and result.failed[0][0] == s2

    # Orchestration marks the run: failed seqs stay pending, not tombstoned.
    mark_rolled_back(J, result.recovered, [s for s, _ in result.failed])
    pending = [
        str(r.seq) for r in read_journal(J) if str(r.seq) not in tombstoned_seqs(J)
    ]
    assert pending == [s1, s2]  # nothing tombstoned: nothing was recovered


def test_partial_rollback_to_checkpoint_tombstones_only_target(tmp_path):
    J = tmp_path / "j.jsonl"
    sink = JournalSink(J)
    seqs = _write_records(sink, tmp_path)

    target = [r for r in read_journal(J) if str(r.seq) == seqs[2]]
    RollbackEngine(target).rollback()
    mark_rolled_back(J, [seqs[2]], [])

    pending = [str(r.seq) for r in read_journal(J) if str(r.seq) not in tombstoned_seqs(J)]
    assert pending == seqs[:2]


def test_marker_writer_locked_against_append(tmp_path):
    """Marker append composes with the lock: no interleaved/torn lines."""
    J = tmp_path / "j.jsonl"
    sink = JournalSink(J)
    _write_records(sink, tmp_path)
    mark_rolled_back(J, recovered=["1"], failed=["2"])  # failed recorded too
    mark_rolled_back(J, recovered=[], failed=[])  # no-op: nothing to record

    # File stays valid JSONL; both markers readable.
    assert len(read_rollback_markers(J)) == 1  # empty marker not written
    assert tombstoned_seqs(J) == {"1"}
