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
    """Maps tools to their recovery metadata."""

    def __init__(self) -> None:
        self._tools: dict[Callable[..., Any], ToolMetadata] = {}

    def register(self, tool: Callable[..., Any], metadata: ToolMetadata) -> None:
        self._tools[tool] = metadata

    def lookup(self, tool: Callable[..., Any]) -> ToolMetadata | None:
        return self._tools.get(tool)

    def is_registered(self, tool: Callable[..., Any]) -> bool:
        return tool in self._tools

    def __len__(self) -> int:
        return len(self._tools)


# Default global registry shared by the decorators and the runtime.
registry = RecoveryRegistry()
