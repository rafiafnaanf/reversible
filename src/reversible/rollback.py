"""Rollback engine: undo/compensate recorded actions in LIFO order.

"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .action import ActionRecord
from .exceptions import ReversibleError
from .journal import JournalRecord
from .logging import get_logger
from .registry import registry

log = get_logger()


class RollbackError(ReversibleError):
    """Raised when rollback stops on a recovery failure."""

    def __init__(self, record: ActionRecord | JournalRecord, cause: Exception) -> None:
        self.record = record
        self.cause = cause
        super().__init__(
            f"rollback failed at {record}: {cause!r} "
            f"(action left in stack for inspection/retry)"
        )


@dataclass
class RollbackResult:
    """Structured outcome of a rollback pass.

    ``recovered``: seqs undone. ``failed``: (seq, error) pairs - these stay
    pending for retry. ``stopped`` is a slight misnomer under
    ``continue_on_error`` (the pass kept going): read it as "not fully
    clean", i.e. ``ok`` is False.
    """

    recovered: list[str] = field(default_factory=list)  # seqs recovered
    failed: list[tuple[str, str]] = field(default_factory=list)  # (seq, error)
    stopped: bool = False

    @property
    def ok(self) -> bool:
        return not self.failed and not self.stopped


def _resolve_recovery(
    recovery: Callable[..., Any] | str,
    namespace: str = "",
) -> Callable[..., Any] | None:
    """Resolve a recovery name to a callable via the name-keyed registry.

    Resolves within ``namespace`` first, then falls back to the global
    (built-in) namespace.
    """
    if callable(recovery):
        return recovery
    return registry.lookup_by_name(str(recovery), namespace=namespace)


class RollbackEngine:
    """Executes recoveries for a set of records, LIFO, scoped by identity.

    Records may be in-m ``ActionRecord`` (holding callables) or journal
    ``JournalRecord`` (holding names). The engine resolves names via the
    registry, runs each recovery, then verifies (``verify_recovery``) before
    removing the record. On failure it stops, keeps the failed record, and
    reports - never claiming the environment was restored.
    """

    def __init__(
        self,
        records: list[ActionRecord | JournalRecord],
        continue_on_error: bool = False,
    ) -> None:
        """Rollback engine over ``records`` (LIFO by seq).

        ``continue_on_error=False`` (default): stop at the first failed
        recovery - strict, all-or-nothing per pass.
        ``continue_on_error=True``: keep going past failures, undoing what
        it can and reporting every failure at the end. Still reports
        ``ok=False`` / ``stopped=True`` so it never claims full restoration.
        """
        self._continue_on_error = continue_on_error
        # Sort by seq descending: LIFO / retire in program order.
        self._records = sorted(records, key=lambda r: r.seq, reverse=True)
        self._pending: list[ActionRecord | JournalRecord] = list(self._records)

    def rollback(
        self, on_recovered: Callable[[str], None] | None = None
    ) -> RollbackResult:
        """Run recoveries LIFO; optionally mark each seq as it is undone.

        ``on_recovered`` is called with each recovered seq immediately after
        its recovery succeeds - the CLI/runtime use it to write rollback
        markers incrementally, so a run killed mid-pass keeps the markers
        for the seqs it already completed.
        """
        result = RollbackResult()
        for record in self._pending:
            try:
                self._recover_one(record)
                result.recovered.append(str(record.seq))
                if on_recovered is not None:
                    try:
                        on_recovered(str(record.seq))
                    except Exception:  # noqa: BLE001 - marking is best-effort
                        log.exception(
                            "[UNDO] failed to write rollback marker for %s",
                            record.seq,
                        )
                log.info("[UNDO] %s → OK", record)
            except Exception as exc:  # noqa: BLE001
                result.failed.append((str(record.seq), str(exc)))
                result.stopped = True
                log.error("[UNDO] %s → FAIL: %s", record, exc)
                if not self._continue_on_error:
                    break
        # Only drop the records we actually recovered.
        self._pending = [r for r in self._pending if str(r.seq) not in result.recovered]
        return result

    def _recover_one(self, record: ActionRecord | JournalRecord) -> None:
        recovery = _resolve_recovery(record.recovery, namespace=record.namespace)
        if recovery is None:
            raise RollbackError(
                record,
                RuntimeError(f"no recovery registered for {record.recovery!r}"),
            )
        recovery(*record.recovery_args, **record.recovery_kwargs)
        # Verify restoration (the ASLR mechanism) - proves, not assumes.
        if isinstance(record, ActionRecord):
            record.verify_recovery()

    @property
    def pending(self) -> list[ActionRecord | JournalRecord]:
        return list(self._pending)
