"""Recovery metadata registry.

The decorators register a tool's recovery metadata here. The runtime looks
up the metadata when a tool is called, so it knows whether to record the
call and what recovery operation to attach to the record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .action import ActionType
from .logging import get_logger

log = get_logger()


@dataclass(frozen=True)
class ToolMetadata:
    """Recovery metadata attached to a registered tool.

    ``recovery_args`` / ``recovery_kwargs`` are *selectors*: names of the
    original call's parameters whose values are forwarded to the recovery
    operation. If both are empty, the recovery is called with the original
    args/kwargs unchanged.

    ``verify`` is an optional post-condition: a callable that runs *after*
    recovery and asserts the observable state matches expectation. It is
    how volatile state (e.g. ASLR) is verified - read back the value, don't
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

    * ``by_function`` - decorators register tools here (identity-keyed).
    * ``by_name`` - recovery operations registered by name, so journal
      records (which store ``recovery`` as a string) can resolve to
      callables at rollback time. Built-in recoveries (``delete_file``,
      ``restore_file``, …) live here.

    Plus an ``execute_policies`` map for ``@execute``-decorated tools.
    """

    def __init__(self) -> None:
        self._tools: dict[Callable[..., Any], ToolMetadata] = {}
        self._by_name: dict[str, dict[str, Callable[..., Any]]] = {}
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

    def register_recovery(
        self, name: str, fn: Callable[..., Any], namespace: str = ""
    ) -> None:
        """Register a recovery operation by name, scoped to a namespace.

        ``namespace`` separates same-named recoveries from different
        modules/agents (e.g. ``coding-agent`` vs ``ghidra-mcp``), so two
        modules with a recovery named ``X`` never overwrite each other.
        The empty namespace holds global built-ins (``delete_file``, …).

        Warns if a same-named recovery already exists in the namespace and
        is being replaced (a likely namespace collision) - so the silent
        overwrite becomes visible instead of causing a wrong rollback later.
        """
        ns = self._by_name.setdefault(namespace, {})
        existing = ns.get(name)
        if existing is not None and existing is not fn:
            log.warning(
                "[REG] overwriting recovery %r in namespace %r "
                "(previous: %r) - possible namespace collision",
                name, namespace, existing,
            )
        ns[name] = fn

    def lookup_by_name(
        self, name: str, namespace: str = ""
    ) -> Callable[..., Any] | None:
        """Resolve a recovery name within a namespace, then global."""
        ns = self._by_name.get(namespace)
        if ns and name in ns:
            return ns[name]
        # Fall back to the global (empty) namespace for built-ins.
        g = self._by_name.get("")
        return g.get(name) if g else None

    def __len__(self) -> int:
        return len(self._tools) + sum(len(v) for v in self._by_name.values())

    def snapshot(self) -> dict[str, dict]:
        """Capture the full registry state (for test isolation / restore)."""
        return {
            "tools": dict(self._tools),
            "by_name": {ns: dict(fns) for ns, fns in self._by_name.items()},
            "policies": dict(self._execute_policies),
        }

    def restore(self, snap: dict[str, dict]) -> None:
        """Restore a state captured by :meth:`snapshot`."""
        self._tools.clear()
        self._tools.update(snap["tools"])
        self._by_name.clear()
        self._by_name.update(snap["by_name"])
        self._execute_policies.clear()
        self._execute_policies.update(snap["policies"])


# Default global registry shared by the decorators and the runtime.
registry = RecoveryRegistry()
