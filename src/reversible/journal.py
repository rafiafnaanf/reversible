"""Durable journal: append-only JSONL, the cross-language contract.

The journal is the single source of truth for recorded agent actions. Any
harness (pi extension in TypeScript, MCP middleware, pure-Python Runtime)
appends one JSON object per line. The Python engine reads it for history
and (later) rollback.

Format guarantees:

* One JSON object per line, newline-terminated.
* ``seq`` is a global monotonic sequence number for cross-agent LIFO order.
* ``agent_id`` / ``session_id`` tag each record so rollback can be scoped.
* Multi-writer (each agent appends its own lines) / single-reader (engine).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .action import ActionRecord, ActionType
from .logging import get_logger

log = get_logger()


@dataclass
class JournalRecord:
    """A serializable journal entry (pure JSON fields)."""

    seq: int
    agent_id: str
    session_id: str
    tool: str
    args: dict[str, Any]
    action_type: str  # "R" | "K"
    recovery: str
    recovery_args: list[Any]
    recovery_kwargs: dict[str, Any]
    is_error: bool = False
    result_summary: str = ""
    ts: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "tool": self.tool,
            "args": self.args,
            "action_type": self.action_type,
            "recovery": self.recovery,
            "recovery_args": self.recovery_args,
            "recovery_kwargs": self.recovery_kwargs,
            "is_error": self.is_error,
            "result_summary": self.result_summary,
            "ts": self.ts,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JournalRecord":
        return cls(
            seq=int(data.get("seq", 0)),
            agent_id=str(data.get("agent_id", "")),
            session_id=str(data.get("session_id", "")),
            tool=str(data.get("tool", "")),
            args=dict(data.get("args", {}) or {}),
            action_type=str(data.get("action_type", "")),
            recovery=str(data.get("recovery", "")),
            recovery_args=list(data.get("recovery_args", []) or []),
            recovery_kwargs=dict(data.get("recovery_kwargs", {}) or {}),
            is_error=bool(data.get("is_error", False)),
            result_summary=str(data.get("result_summary", "")),
            ts=str(data.get("ts", "")),
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _callable_name(fn: Callable[..., Any] | str) -> str:
    return fn.__name__ if callable(fn) else str(fn)


def _summary(result: Any, limit: int = 200) -> str:
    """Short, JSON-safe summary of a result (journal stays small)."""
    if result is None:
        return ""
    try:
        text = repr(result)
    except Exception:  # pragma: no cover - defensive
        text = "<unrepr>"
    if len(text) > limit:
        text = text[:limit] + "..."
    return text


def record_to_journal(record: ActionRecord) -> dict[str, Any]:
    """Project an in-memory ``ActionRecord`` onto a journal dict.

    Callables become names; positional args are bound to the action's
    parameter names so the journal is always name-keyed.
    """
    action = record.action
    args_obj: dict[str, Any] = {}
    if callable(action):
        import inspect

        try:
            bound = inspect.signature(action).bind(*record.args, **record.kwargs)
            bound.apply_defaults()
            args_obj = dict(bound.arguments)
        except (TypeError, ValueError):
            # Positional-only / unboundable - fall back to raw list.
            args_obj = {"_args": list(record.args)}
            if record.kwargs:
                args_obj.update(record.kwargs)
    else:
        # Already a name (e.g. from a journal replay).
        args_obj = dict(record.kwargs)
        if record.args:
            args_obj = {"_args": list(record.args), **record.kwargs}

    return {
        "seq": record.seq,
        "agent_id": record.agent_id,
        "session_id": record.session_id,
        "tool": _callable_name(record.action),
        "args": args_obj,
        "action_type": record.action_type.value,
        "recovery": _callable_name(record.recovery),
        "recovery_args": list(record.recovery_args),
        "recovery_kwargs": dict(record.recovery_kwargs),
        "is_error": False,
        "result_summary": _summary(record.result),
        "ts": record.ts or _now_iso(),
    }


class JournalLock:
    """Cross-language advisory lock via O_EXCL lock-file creation.

    The journal is multi-writer (Python runtime, TS pi extension, MCP
    middleware). To make ``seq`` assignment atomic across languages, writers
    take an exclusive lock by atomically creating ``<journal>.lock`` with
    ``O_CREAT|O_EXCL``, retrying until they win. On success they append and
    then remove the lock file.

    This is a spin-lock (not blocking), which is fine for a prototype: the
    critical section (read-max + append) is microseconds.
    """

    def __init__(
        self,
        journal_path: str | Path,
        retries: int = 1000,
        delay: float = 0.001,
    ) -> None:
        self.lock_path = Path(str(journal_path) + ".lock")
        self.retries = retries
        self.delay = delay
        self._fd: int | None = None

    def acquire(self) -> None:
        import time

        for _ in range(self.retries):
            try:
                self._fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self._fd, str(os.getpid()).encode())
                return
            except FileExistsError:
                time.sleep(self.delay)
        raise TimeoutError(f"could not acquire journal lock: {self.lock_path}")

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            pass

    def __enter__(self) -> "JournalLock":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


class JournalSink:
    """Append-only JSONL writer.

    Multi-writer safe for short lines on POSIX (``O_APPEND`` + fsync).
    ``seq`` assignment is serialized via a cross-language lock file.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _write_line(self, line: str) -> None:
        """Append a single line + fsync. Caller holds the lock."""
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())

    def append(self, record: ActionRecord | JournalRecord | dict[str, Any]) -> None:
        data = record.to_dict() if isinstance(record, JournalRecord) else (
            record.to_journal() if isinstance(record, ActionRecord) else record
        )
        line = json.dumps(data, default=str) + "\n"
        with JournalLock(self.path):
            self._write_line(line)

    def append_with_seq(
        self, record: ActionRecord | JournalRecord | dict[str, Any]
    ) -> int:
        """Atomically assign the next seq and append, under one lock.

        This is the race-free path: the read-max + assign + append happen as
        one critical section, so no other writer can interleave a duplicate
        seq. Returns the assigned seq.
        """
        data = record.to_dict() if isinstance(record, JournalRecord) else (
            record.to_journal() if isinstance(record, ActionRecord) else record
        )
        with JournalLock(self.path):
            seq = next_seq(self.path)
            data["seq"] = seq
            line = json.dumps(data, default=str) + "\n"
            self._write_line(line)
        return seq


def read_journal(path: str | Path) -> list[JournalRecord]:
    """Read all records from a journal file, oldest first."""
    records: list[JournalRecord] = []
    p = Path(path)
    if not p.exists():
        return records
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(JournalRecord.from_dict(json.loads(line)))
            except (json.JSONDecodeError, ValueError) as exc:
                log.warning("[JRNL] skipping malformed line: %s", exc)
    return records


def next_seq(path: str | Path) -> int:
    """Return the next global sequence number (max existing + 1)."""
    records = read_journal(path)
    return (max((r.seq for r in records), default=0)) + 1


def filter_records(
    records: list[JournalRecord],
    agent_id: str | None = None,
    session_id: str | None = None,
) -> list[JournalRecord]:
    """Filter journal records by identity, preserving file order."""
    out = records
    if agent_id is not None:
        out = [r for r in out if r.agent_id == agent_id]
    if session_id is not None:
        out = [r for r in out if r.session_id == session_id]
    return out
