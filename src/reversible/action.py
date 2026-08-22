"""Action types and action records."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class ActionType(str, Enum):
    """Classification of an action's recovery semantics.

    R - reversible: an exact inverse restores the prior state.
    K - compensable: a compensation operation mitigates the effect
        (it does not necessarily restore the exact original state).
    """

    REVERSIBLE = "R"
    COMPENSABLE = "K"


@dataclass
class ActionRecord:
    """A recorded tool call plus its recovery operation.

    The record stores the arguments required for the recovery operation,
    not just the recovery function.

    Example::

        create_file("hello.txt", "hello")
        →
        id            = "001"
        action        = create_file
        action_type   = R
        recovery      = delete_file
        recovery_args = ("hello.txt",)

    Identity fields (``agent_id`` / ``session_id`` / ``seq`` / ``ts``) let a
    shared journal be filtered and rolled back per agent/session.
    """

    id: str
    action: Callable[..., Any] | str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    action_type: ActionType
    recovery: Callable[..., Any] | str
    recovery_args: tuple[Any, ...]
    recovery_kwargs: dict[str, Any]
    result: Any = None

    # -- verification (volatile state, e.g. ASLR) ---------------------------
    # Optional post-condition: runs after recovery, asserts observable state
    # matches expectation. Read back the value - don't trust the claim.
    verify: Callable[..., Any] | None = None

    # -- identity (multi-agent journal) ------------------------------------
    agent_id: str = ""
    session_id: str = ""
    seq: int = 0
    ts: str = ""

    # -- namespace (which module/agent this recovery belongs to) -----------
    # Separates same-named recoveries from different modules/agents so they
    # never collide in the name-keyed registry.
    namespace: str = ""

    def __str__(self) -> str:
        name = self.action.__name__ if callable(self.action) else self.action
        return f"{self.id} {self.action_type.value} {name}"

    def verify_recovery(self) -> None:
        """Run the post-condition after recovery, if one is registered.

        Called with the recovery's args/kwargs (the same values the recovery
        operation received). The predicate returns a truthy value when the
        state is restored; ``verify_recovery`` raises ``AssertionError`` if
        it returns falsy (recovery silently failed). Used by the rollback
        engine; callable directly for testing.
        """
        if self.verify is None:
            return
        ok = self.verify(*self.recovery_args, **self.recovery_kwargs)
        if not ok:
            raise AssertionError(
                f"verification failed for {self.action!r}: "
                f"state was not restored after recovery"
            )

    def to_journal(self) -> dict[str, Any]:
        """Serialize to a pure-JSON journal record.

        Callables become names; ``args`` become a name-keyed object (bound
        via the action's signature); ``result`` becomes a short summary.
        """
        from .journal import record_to_journal

        return record_to_journal(self)
