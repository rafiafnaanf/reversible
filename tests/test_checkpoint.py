"""Tests for checkpoints."""

import os

from reversible import JournalSink, Runtime, reversible


def _make_runtime(tmp_path, sink=None, agent_id="demo"):
    def delete_file(path: str) -> None:
        if os.path.exists(path):
            os.remove(path)

    @reversible(inverse=delete_file, inverse_args=("path",))
    def create_file(path: str, content: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)

    runtime = Runtime(agent_id=agent_id, sink=sink)
    return runtime, create_file


def test_checkpoint_rollback_to_leaves_earlier(tmp_path):
    """A, B, CHECKPOINT, C, D → rollback_to undoes only D, C."""
    runtime, create_file = _make_runtime(tmp_path)

    runtime.call(create_file, str(tmp_path / "a.txt"), "a")
    runtime.call(create_file, str(tmp_path / "b.txt"), "b")

    checkpoint = runtime.checkpoint()

    runtime.call(create_file, str(tmp_path / "c.txt"), "c")
    runtime.call(create_file, str(tmp_path / "d.txt"), "d")

    result = runtime.rollback_to(checkpoint)

    assert result.ok
    # C and D recovered; A and B remain.
    assert not os.path.exists(tmp_path / "c.txt")
    assert not os.path.exists(tmp_path / "d.txt")
    assert os.path.exists(tmp_path / "a.txt")
    assert os.path.exists(tmp_path / "b.txt")
    assert len(runtime) == 2


def test_checkpoint_with_sink(tmp_path):
    """Checkpoint + rollback_to works on a journal-backed runtime."""
    sink = JournalSink(tmp_path / "j.jsonl")
    runtime, create_file = _make_runtime(tmp_path, sink=sink)

    runtime.call(create_file, str(tmp_path / "a.txt"), "a")
    checkpoint = runtime.checkpoint()
    runtime.call(create_file, str(tmp_path / "b.txt"), "b")

    result = runtime.rollback_to(checkpoint)

    assert result.ok
    assert not os.path.exists(tmp_path / "b.txt")
    assert os.path.exists(tmp_path / "a.txt")
    assert len(runtime) == 1


def test_checkpoint_at_start_rolls_back_everything(tmp_path):
    """A checkpoint before any action → rollback_to undoes all."""
    runtime, create_file = _make_runtime(tmp_path)

    checkpoint = runtime.checkpoint()  # before any action
    runtime.call(create_file, str(tmp_path / "a.txt"), "a")
    runtime.call(create_file, str(tmp_path / "b.txt"), "b")

    result = runtime.rollback_to(checkpoint)

    assert result.ok
    assert not os.path.exists(tmp_path / "a.txt")
    assert not os.path.exists(tmp_path / "b.txt")
    assert len(runtime) == 0


def test_checkpoint_does_not_consume_seq(tmp_path):
    """checkpoint() must not increment the seq counter."""
    runtime, create_file = _make_runtime(tmp_path)

    runtime.call(create_file, str(tmp_path / "a.txt"), "a")
    cp = runtime.checkpoint()
    runtime.call(create_file, str(tmp_path / "b.txt"), "b")

    records = runtime.history()
    # a = seq 1, b = seq 2; checkpoint was 2 (next after a)
    assert records[0].seq == 1
    assert records[1].seq == 2
    assert cp == 2
