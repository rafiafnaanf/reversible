"""Recovery metadata registry.

The decorators register a tool's recovery metadata here. The runtime looks
up the metadata when a tool is called, so it knows whether to record the
call and what recovery operation to attach to the record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .action import ActionType


@dataclass(frozen=True)
class ToolMetadata:
    """Recovery metadata attached to a registered tool.

    ``recovery_args`` / ``recovery_kwargs`` are *selectors*: names of the
    original call's parameters whose values are forwarded to the recovery
    operation. If both are empty, the recovery is called with the original
    args/kwargs unchanged.

    ``verify`` is an optional post-condition: a callable that runs *after*
    recovery and asserts the observable state matches expectation. It is
    how volatile state (e.g. ASLR) is verified — read back the value, don't
    trust the claim that recovery ran.
    """

    action_type: ActionType
    recovery: Callable[..., Any]
    recovery_args: tuple[str, ...] = ()
    recovery_kwargs: dict[str, str] = field(default_factory=dict)
    verify: Callable[..., Any] | None = None


class RecoveryRegistry:
    """Maps tools to their recovery metadata.

    Two lookup spaces:

    * ``by_function`` — decorators register tools here (identity-keyed).
    * ``by_name`` — recovery operations registered by name, so journal
      records (which store ``recovery`` as a string) can resolve to
      callables at rollback time. Built-in recoveries (``delete_file``,
      ``restore_file``, …) live here.

    Plus an ``execute_policies`` map for ``@execute``-decorated tools.
    """

    def __init__(self) -> None:
        self._tools: dict[Callable[..., Any], ToolMetadata] = {}
        self._by_name: dict[str, Callable[..., Any]] = {}
        self._execute_policies: dict[Callable[..., Any], str] = {}

    def register(self, tool: Callable[..., Any], metadata: ToolMetadata) -> None:
        self._tools[tool] = metadata

    def lookup(self, tool: Callable[..., Any]) -> ToolMetadata | None:
        return self._tools.get(tool)

    def is_registered(self, tool: Callable[..., Any]) -> bool:
        return tool in self._tools

    # -- execution policy (@execute) ---------------------------------------

    def register_execute(self, tool: Callable[..., Any], policy: str) -> None:
        """Mark a tool as binary execution with a declared policy."""
        self._execute_policies[tool] = policy

    def execute_policy(self, tool: Callable[..., Any]) -> str | None:
        """Return the declared execution policy for a tool, if any."""
        return self._execute_policies.get(tool)

    # -- name-keyed recovery operations ------------------------------------

    def register_recovery(self, name: str, fn: Callable[..., Any]) -> None:
        """Register a recovery operation by name (resolvable at rollback)."""
        self._by_name[name] = fn

    def lookup_by_name(self, name: str) -> Callable[..., Any] | None:
        """Resolve a recovery name (from a journal record) to a callable."""
        return self._by_name.get(name)

    def __len__(self) -> int:
        return len(self._tools) + len(self._by_name)


# Default global registry shared by the decorators and the runtime.
registry = RecoveryRegistry()
