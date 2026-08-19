"""Tests for the durable journal: serialization, identity, filtering, sink."""

import json

import pytest

from reversible import (
    ActionRecord,
    ActionType,
    JournalSink,
    Runtime,
    filter_records,
    next_seq,
    read_journal,
    reversible,
)


def make_record(
    agent_id: str = "agent-a",
    session_id: str = "sess-1",
    seq: int = 1,
    tool_name: str = "create_file",
    action_type: ActionType = ActionType.REVERSIBLE,
) -> ActionRecord:
    def create_file(path: str, content: str) -> str:
        return f"wrote {path}"

    def delete_file(path: str) -> None:
        return None

    return ActionRecord(
        id="001",
        action=create_file,
        args=("hello.txt", "hello"),
        kwargs={},
        action_type=action_type,
        recovery=delete_file,
        recovery_args=("hello.txt",),
        recovery_kwargs={},
        result="wrote hello.txt",
        agent_id=agent_id,
        session_id=session_id,
        seq=seq,
    )


def test_record_to_journal_serializes_callables_to_names():
    data = make_record().to_journal()
    assert data["tool"] == "create_file"
    assert data["recovery"] == "delete_file"
    assert data["action_type"] == "R"
    assert data["agent_id"] == "agent-a"
    assert data["session_id"] == "sess-1"
    assert data["seq"] == 1
    # args are name-keyed (bound to the action's signature)
    assert data["args"] == {"path": "hello.txt", "content": "hello"}
    assert data["recovery_args"] == ["hello.txt"]
    assert data["result_summary"] == "'wrote hello.txt'"  # repr-based


def test_journal_sink_roundtrip(tmp_path):
    sink = JournalSink(tmp_path / "journal.jsonl")
    sink.append(make_record())
    sink.append(make_record(agent_id="agent-b", session_id="sess-2", seq=2))

    records = read_journal(tmp_path / "journal.jsonl")
    assert len(records) == 2
    assert records[0].agent_id == "agent-a"
    assert records[0].tool == "create_file"
    assert records[0].recovery == "delete_file"
    assert records[1].agent_id == "agent-b"
    assert records[1].seq == 2


def test_journal_lines_are_valid_json(tmp_path):
    sink = JournalSink(tmp_path / "j.jsonl")
    sink.append(make_record())
    with open(tmp_path / "j.jsonl", encoding="utf-8") as fh:
        for line in fh:
            json.loads(line)  # must parse


def test_next_seq_increments(tmp_path):
    path = tmp_path / "j.jsonl"
    assert next_seq(path) == 1
    JournalSink(path).append(make_record(seq=1))
    assert next_seq(path) == 2
    JournalSink(path).append(make_record(seq=2))
    assert next_seq(path) == 3


def test_filter_records_by_agent_and_session(tmp_path):
    path = tmp_path / "j.jsonl"
    sink = JournalSink(path)
    sink.append(make_record(agent_id="pi", session_id="s1", seq=1))
    sink.append(make_record(agent_id="email", session_id="e1", seq=2))
    sink.append(make_record(agent_id="pi", session_id="s2", seq=3))

    records = read_journal(path)
    assert filter_records(records, agent_id="pi") == [records[0], records[2]]
    assert filter_records(records, agent_id="pi", session_id="s1") == [records[0]]
    assert filter_records(records, agent_id="email") == [records[1]]


def test_runtime_sink_writes_through(tmp_path):
    calls = []

    def delete_file(path: str) -> None:
        calls.append(("delete", path))

    @reversible(inverse=delete_file, inverse_args=("path",))
    def create_file(path: str, content: str) -> None:
        calls.append(("create", path, content))

    path = tmp_path / "j.jsonl"
    runtime = Runtime(agent_id="pi", session_id="sess-x", sink=JournalSink(path))
    runtime.call(create_file, "a.txt", "hi")

    records = read_journal(path)
    assert len(records) == 1
    assert records[0].agent_id == "pi"
    assert records[0].session_id == "sess-x"
    assert records[0].tool == "create_file"
    assert records[0].recovery == "delete_file"
    assert records[0].recovery_args == ["a.txt"]


def test_runtime_without_sink_has_no_journal_side_effect():
    runtime = Runtime()
    assert runtime._sink is None


def test_malformed_line_is_skipped(tmp_path):
    path = tmp_path / "j.jsonl"
    path.write_text("{not json}\n" + json.dumps(make_record().to_journal()) + "\n")
    records = read_journal(path)
    assert len(records) == 1
    assert records[0].tool == "create_file"
