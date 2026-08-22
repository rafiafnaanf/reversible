"""The reversible runtime: intercept, execute, record."""

from __future__ import annotations

import inspect
from typing import Any, Callable

from .action import ActionRecord
from .journal import JournalSink, next_seq
from .logging import get_logger
from .registry import ToolMetadata, registry
from .stack import ActionStack

log = get_logger()


class Runtime:
    """Records effectful tool calls onto an action stack.

    Only tools registered via ``@reversible`` / ``@compensable`` are
    recorded. Undecorated tools (thinking, reading, pure computation)
    execute normally but are **not** recorded.

    Recovery operations are never executed here — they are only recorded.
    They run when rollback is explicitly requested (later stage).

    Identity: ``agent_id`` / ``session_id`` tag every record so a shared
    journal can be filtered and rolled back per agent/session.

    Durability: pass ``sink=JournalSink(path)`` to write each recorded
    action through to an append-only JSONL journal. Without a sink the
    runtime behaves exactly as Stage 1 (in-memory only).
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
        """
        metadata = registry.lookup(tool)

        if metadata is None:
            log.debug("[SKIP] %s(...) — not registered, not recorded", tool.__name__)
            return tool(*args, **kwargs)

        result = tool(*args, **kwargs)

        recovery_args, recovery_kwargs = self._resolve_recovery_args(
            metadata, tool, args, kwargs
        )

        record = ActionRecord(
            id=self._next_id(),
            action=tool,
            args=args,
            kwargs=kwargs,
            action_type=metadata.action_type,
            recovery=metadata.recovery,
            recovery_args=recovery_args,
            recovery_kwargs=recovery_kwargs,
            result=result,
            verify=metadata.verify,
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
            metadata.action_type.value,
            _format_call(metadata.recovery, recovery_args, recovery_kwargs),
        )
        return result

    # -- inspection --------------------------------------------------------

    def history(self) -> list[ActionRecord]:
        """Return the recorded actions, oldest first."""
        return list(self._stack)

    def __len__(self) -> int:
        return len(self._stack)

    # -- internals ---------------------------------------------------------

    def _next_id(self) -> str:
        self._counter += 1
        return f"{self._counter:03d}"

    def _next_seq(self) -> int:
        """Global sequence number — from the sink if present, else local."""
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
