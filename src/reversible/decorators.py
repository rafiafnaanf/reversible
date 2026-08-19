"""Tool decorators that register recovery metadata.

Decorated tools are recorded by the runtime when called. The recovery
operation is only *recorded* here — it is never executed during normal
execution; it runs only when rollback is explicitly requested.
"""

from __future__ import annotations

from typing import Any, Callable

from .action import ActionType
from .registry import ToolMetadata, registry


def reversible(
    *,
    inverse: Callable[..., Any],
    inverse_args: tuple[str, ...] = (),
    inverse_kwargs: dict[str, str] | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Mark a tool as reversible (R): an exact inverse restores prior state.

    Args:
        inverse: the recovery callable that undoes the tool's effect.
        inverse_args: names of the original call's parameters to forward
            positionally to ``inverse``. If omitted, the original args and
            kwargs are forwarded unchanged.
        inverse_kwargs: mapping ``{inverse_parameter: original_parameter}``
            for recovery keyword arguments.

    Example::

        @reversible(inverse=delete_file, inverse_args=("path",))
        def create_file(path: str, content: str): ...
    """

    def decorate(tool: Callable[..., Any]) -> Callable[..., Any]:
        metadata = ToolMetadata(
            action_type=ActionType.REVERSIBLE,
            recovery=inverse,
            recovery_args=tuple(inverse_args),
            recovery_kwargs=dict(inverse_kwargs or {}),
        )
        registry.register(tool, metadata)
        return tool

    return decorate


def compensable(
    *,
    compensation: Callable[..., Any],
    compensation_args: tuple[str, ...] = (),
    compensation_kwargs: dict[str, str] | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Mark a tool as compensable (K): a compensation mitigates its effect.

    Compensation does not necessarily restore the exact original state, so
    it is never called an "inverse".

    Args: same shape as :func:`reversible`.

    Example::

        @compensable(compensation=cancel_notification)
        def send_notification(message: str): ...
    """

    def decorate(tool: Callable[..., Any]) -> Callable[..., Any]:
        metadata = ToolMetadata(
            action_type=ActionType.COMPENSABLE,
            recovery=compensation,
            recovery_args=tuple(compensation_args),
            recovery_kwargs=dict(compensation_kwargs or {}),
        )
        registry.register(tool, metadata)
        return tool

    return decorate
