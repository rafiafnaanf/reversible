"""Reversible Agent Runtime — record effectful agent tool calls.

Stage 1 (command → stack): decorated tool calls are executed and recorded
onto an action stack with their recovery operations. Recovery is never
executed during normal execution.
"""

from .action import ActionRecord, ActionType
from .decorators import compensable, execute, reversible
from .exceptions import ReversibleError
from .journal import JournalRecord, JournalSink, filter_records, next_seq, read_journal, record_to_journal
from .logging import configure_logging, get_logger
from .recovery_builtin import register_builtin_recoveries
from .registry import RecoveryRegistry, ToolMetadata, registry
from .rollback import RollbackEngine, RollbackError, RollbackResult
from .runtime import Runtime
from .stack import ActionStack

__all__ = [
    "ActionRecord",
    "ActionStack",
    "ActionType",
    "JournalRecord",
    "JournalSink",
    "RecoveryRegistry",
    "RollbackEngine",
    "RollbackError",
    "RollbackResult",
    "ReversibleError",
    "Runtime",
    "ToolMetadata",
    "compensable",
    "configure_logging",
    "execute",
    "filter_records",
    "get_logger",
    "next_seq",
    "read_journal",
    "record_to_journal",
    "register_builtin_recoveries",
    "registry",
    "reversible",
]
