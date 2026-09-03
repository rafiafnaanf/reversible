"""Tool decorators that register recovery metadata.

Decorated tools are recorded by the runtime when called. The recovery
operation is only *recorded* here - it is never executed during normal
execution; it runs only when rollback is explicitly requested.
"""

from __future__ import annotations

from typing import Any, Callable

from .action import ActionType
from .logging import get_logger
from .registry import ToolMetadata, registry

log = get_logger()


def _register_callable(kind: str, fn: Callable[..., Any], namespace: str) -> None:
    """Register an inverse/compensation/verify by name for journal resolution.

    Lambdas have no stable name ("<lambda>") - they work in-memory but can
    never resolve from a journal, so registering them would only pollute
    the namespace. Named functions are required for journal-backed undo.
    """
    name = getattr(fn, "__name__", "")
    if not name or name == "<lambda>":
        log.warning(
            "[REG] %s is a lambda - journal-backed rollback cannot resolve it; "
            "use a named function",
            kind,
        )
        return
    if kind == "verify":
        registry.register_verify(name, fn, namespace=namespace)
    else:
        registry.register_recovery(name, fn, namespace=namespace)

# Default execution policies for @execute.
EXECUTE_POLICY_SKIP = "skip"        # trusted → not logged
EXECUTE_POLICY_RECORD = "record"    # audit-only, manual reversal
EXECUTE_POLICY_SANDBOX = "sandbox"  # untrusted → run in docker, nuke on reversal

# Paths treated as trusted system binaries (default policy = skip).
_TRUSTED_PREFIXES = ("/bin/", "/usr/bin/", "/sbin/", "/usr/sbin/")


def reversible(
    *,
    inverse: Callable[..., Any],
    inverse_args: tuple[str, ...] = (),
    inverse_kwargs: dict[str, str] | None = None,
    verify: Callable[..., Any] | None = None,
    namespace: str = "",
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Mark a tool as reversible (R): an exact inverse restores prior state.

    Args:
        inverse: the recovery callable that undoes the tool's effect.
        inverse_args: names of the original call's parameters to forward
            positionally to ``inverse``. If omitted, the original args and
            kwargs are forwarded unchanged.
        inverse_kwargs: mapping ``{inverse_parameter: original_parameter}``
            for recovery keyword arguments.
        verify: optional post-condition. A callable that runs *after*
            recovery and asserts the observable state matches expectation
            (e.g. read back the value). It is called with the recovery's
            args/kwargs (the same values the recovery received). Raises if
            the state was not restored.
        namespace: optional scope for this tool's recovery, so same-named
            recoveries from different modules/agents don't collide.

    Example::

        @reversible(inverse=delete_file, inverse_args=("path",),
                    verify=lambda path: not os.path.exists(path))
        def create_file(path: str, content: str): ...
    """

    def decorate(tool: Callable[..., Any]) -> Callable[..., Any]:
        metadata = ToolMetadata(
            action_type=ActionType.REVERSIBLE,
            recovery=inverse,
            recovery_args=tuple(inverse_args),
            recovery_kwargs=dict(inverse_kwargs or {}),
            verify=verify,
            namespace=namespace or None,
        )
        registry.register(tool, metadata)
        # Auto-register the inverse (and verify) by name in the namespace, so
        # journal records (storing the function name) resolve at rollback time.
        _register_callable("inverse", inverse, namespace)
        if verify is not None:
            _register_callable("verify", verify, namespace)
        return tool

    return decorate


def compensable(
    *,
    compensation: Callable[..., Any],
    compensation_args: tuple[str, ...] = (),
    compensation_kwargs: dict[str, str] | None = None,
    verify: Callable[..., Any] | None = None,
    namespace: str = "",
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Mark a tool as compensable (K): a compensation mitigates its effect.

    Compensation does not necessarily restore the exact original state, so
    it is never called an "inverse".

    Args: same shape as :func:`reversible`, including ``verify`` and
    ``namespace``.

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
            verify=verify,
            namespace=namespace or None,
        )
        registry.register(tool, metadata)
        _register_callable("compensation", compensation, namespace)
        if verify is not None:
            _register_callable("verify", verify, namespace)
        return tool

    return decorate


def execute(
    *,
    policy: str | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Mark a tool as binary execution with a declared policy.

    Executing an arbitrary binary is the X (unknown) case - its effects
    can't be introspected. So execution is *declared*, not auto-detected:

    * ``policy="skip"`` - trusted binary, not logged (e.g. a trusted
      executable).
    * ``policy="record"`` - audit-only K, manual reversal.
    * ``policy="sandbox"`` - untrusted, run in a sandbox (e.g. docker);
      reversal is coarse (nuke the sandbox).

    Default policy is path-based: a binary under ``/bin`` / ``/usr/bin``
    is trusted (``skip``); anywhere else defaults to ``sandbox``. An
    explicit ``policy`` always wins.

    Example::

        @execute(policy="skip")
        def run_trusted(path: str, args: list[str]): ...

        @execute()  # defaults to sandbox for non-system paths
        def run_binary(path: str, args: list[str]): ...
    """

    def decorate(tool: Callable[..., Any]) -> Callable[..., Any]:
        resolved = policy or _default_policy(tool)
        registry.register_execute(tool, resolved)
        return tool

    return decorate


def _default_policy(tool: Callable[..., Any]) -> str:
    """Path-based default: trusted system paths → skip, else sandbox."""
    import inspect

    try:
        src = inspect.getsource(tool)
    except (OSError, TypeError):
        return EXECUTE_POLICY_SANDBOX
    for prefix in _TRUSTED_PREFIXES:
        if prefix in src:
            return EXECUTE_POLICY_SKIP
    return EXECUTE_POLICY_SANDBOX
