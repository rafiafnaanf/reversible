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
    """

    action_type: ActionType
    recovery: Callable[..., Any]
    recovery_args: tuple[str, ...] = ()
    recovery_kwargs: dict[str, str] = field(default_factory=dict)


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
