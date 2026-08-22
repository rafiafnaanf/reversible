"""The reversible runtime: intercept, execute, record."""

from __future__ import annotations

import inspect
from typing import Any, Callable

from .action import ActionRecord, ActionType
from .journal import JournalSink, filter_records, next_seq
from .logging import get_logger
from .registry import ToolMetadata, registry
from .rollback import RollbackEngine, RollbackResult
from .stack import ActionStack

log = get_logger()


class Runtime:
    """Records effectful tool calls onto an action stack.

    Only tools registered via ``@reversible`` / ``@compensable`` are
    recorded. Undecorated tools (thinking, reading, pure computation)
    execute normally but are **not** recorded.

    Recovery operations are never executed here - they are only recorded.
    They run when rollback is explicitly requested.

    Identity: ``agent_id`` / ``session_id`` tag every record so a shared
    journal can be filtered and rolled back per agent/session.

    Durability: pass ``sink=JournalSink(path)`` to write each recorded
    action through to an append-only JSONL journal. Without a sink the
    runtime is in-memory only.
    """

    def __init__(
        self,
        *,
        agent_id: str = "",
        session_id: str = "",
        sink: JournalSink | None = None,
    ) -> None:
        self._stack = ActionStack()
        self._counter = 0
        self._agent_id = agent_id
        self._session_id = session_id
        self._sink = sink

    # -- recording ---------------------------------------------------------

    def call(self, tool: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Execute ``tool`` and, if registered, record it on the stack.

        Returns the tool's original result.

        ``@execute``-decorated tools follow their declared policy:
        ``skip`` → not recorded; ``record`` / ``sandbox`` → recorded as
        record-only K (audit; reversal is manual or coarse).
        """
        metadata = registry.lookup(tool)
        policy = registry.execute_policy(tool)

        if metadata is None and policy is None:
            log.debug("[SKIP] %s(...) - not registered, not recorded", tool.__name__)
            return tool(*args, **kwargs)

        if policy == "skip":
            log.debug(
                "[SKIP] %s(...) - @execute policy=skip, not recorded", tool.__name__
            )
            return tool(*args, **kwargs)

        result = tool(*args, **kwargs)

        # @execute tools: record-only K (no auto-recovery).
        if metadata is None:
            recovery_args, recovery_kwargs = (), {}
            action_type = ActionType.COMPENSABLE
            recovery = registry.lookup_by_name("noop") or (lambda *a, **k: None)
            verify = None
        else:
            recovery_args, recovery_kwargs = self._resolve_recovery_args(
                metadata, tool, args, kwargs
            )
            action_type = metadata.action_type
            recovery = metadata.recovery
            verify = metadata.verify

        record = ActionRecord(
            id=self._next_id(),
            action=tool,
            args=args,
            kwargs=kwargs,
            action_type=action_type,
            recovery=recovery,
            recovery_args=recovery_args,
            recovery_kwargs=recovery_kwargs,
            result=result,
            verify=verify,
            agent_id=self._agent_id,
            session_id=self._session_id,
            seq=0,  # assigned atomically by the sink below
        )
        self._stack.push(record)
        if self._sink is not None:
            seq = self._sink.append_with_seq(record)
            record.seq = seq
            log.info("[JRNL] appended to %s", self._sink.path)
        else:
            record.seq = self._next_seq()

        log.info("[EXEC] %s", _format_call(tool, args, kwargs))
        log.info(
            "[LOG ] %s → %s",
            action_type.value,
            _format_call(recovery, recovery_args, recovery_kwargs),
        )
        return result

    # -- inspection --------------------------------------------------------

    def history(self) -> list[ActionRecord]:
        """Return the recorded actions, oldest first."""
        return list(self._stack)

    def __len__(self) -> int:
        return len(self._stack)

    # -- rollback ----------------------------------------------------------

    def checkpoint(self) -> int:
        """Return a marker for the current stack position.

        The marker is the next ``seq`` that will be assigned (without
        consuming it). ``rollback_to`` recovers only records with
        ``seq >= checkpoint``, leaving earlier actions in the stack.
        """
        if self._sink is not None:
            return next_seq(self._sink.path)
        return getattr(self, "_seq_counter", 0) + 1

    def rollback_to(
        self,
        checkpoint: int,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> RollbackResult:
        """Undo only the actions recorded after ``checkpoint``.

        Recovers records with ``seq >= checkpoint`` (LIFO), leaving earlier
        actions in the stack. Scoped by identity like :meth:`rollback`.
        """
        records = self._collect_records(agent_id, session_id)
        target = [r for r in records if r.seq >= checkpoint]
        return self._rollback_records(records, target)

    def rollback(
        self,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> RollbackResult:
        """Undo recorded actions in LIFO order, optionally scoped by identity.

        With a sink, reads the journal and recovers the matching records
        (descending ``seq``). Without a sink, recovers the in-memory stack.

        Each recovery is verified (``verify_recovery``) before the action is
        dropped. On failure the engine stops, keeps the failed action, and
        returns a ``RollbackResult`` - it never claims the environment was
        restored.
        """
        records = self._collect_records(agent_id, session_id)
        return self._rollback_records(records, records)

    # -- internals ---------------------------------------------------------

    def _collect_records(
        self,
        agent_id: str | None,
        session_id: str | None,
    ) -> list[Any]:
        """Gather the records to consider, scoped by identity."""
        if self._sink is not None:
            from .journal import read_journal

            records = read_journal(self._sink.path)
            return filter_records(
                records, agent_id=agent_id, session_id=session_id
            )
        records = list(self._stack)
        if agent_id is not None:
            records = [r for r in records if r.agent_id == agent_id]
        if session_id is not None:
            records = [r for r in records if r.session_id == session_id]
        return records

    def _rollback_records(
        self,
        records: list[Any],
        target: list[Any],
    ) -> RollbackResult:
        """Recover ``target`` (subset of ``records``), then re-sync the stack.

        Keeps only records not recovered; failed actions stay in the stack.
        """
        engine = RollbackEngine(target)
        result = engine.rollback()
        recovered = set(result.recovered)
        self._stack.clear()
        for r in records:
            if str(r.seq) not in recovered:
                self._stack.push(r)
        return result

    def _next_id(self) -> str:
        self._counter += 1
        return f"{self._counter:03d}"

    def _next_seq(self) -> int:
        """Global sequence number - from the sink if present, else local."""
        if self._sink is not None:
            return next_seq(self._sink.path)
        self._seq_counter = getattr(self, "_seq_counter", 0) + 1
        return self._seq_counter

    def _resolve_recovery_args(
        self,
        metadata: ToolMetadata,
        tool: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> tuple[tuple[Any, ...], dict[str, Any]]:
        """Derive the recovery call's arguments from the original call.

        If the decorator specified ``recovery_args`` / ``recovery_kwargs``
        selectors, bind the original call to the tool's parameters and pick
        the named values. Otherwise forward the original args/kwargs
        unchanged.
        """
        if not metadata.recovery_args and not metadata.recovery_kwargs:
            return args, dict(kwargs)

        bound = inspect.signature(tool).bind(*args, **kwargs)
        bound.apply_defaults()
        params = bound.arguments

        recovery_args = tuple(params[name] for name in metadata.recovery_args)
        recovery_kwargs = {
            recovery_name: params[original_name]
            for recovery_name, original_name in metadata.recovery_kwargs.items()
        }
        return recovery_args, recovery_kwargs


def _format_call(
    fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]
) -> str:
    parts = [repr(a) for a in args]
    parts += [f"{k}={v!r}" for k, v in kwargs.items()]
    return f"{fn.__name__}({', '.join(parts)})"
