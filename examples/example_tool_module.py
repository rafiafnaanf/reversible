"""
Example custom tool module - a template for developers.

This module shows how to write your OWN effectful tools for the Reversible
Agent Runtime, the same way pi's example extensions show how to write your
own pi extensions.

Copy this file into your project, rename it, and adapt the tools to your
domain. The pattern is always the same:

    1. Write the tool function (the forward effect).
    2. Write the recovery function (inverse for R, compensation for K).
    3. Decorate the tool with @reversible / @compensable, telling the
       runtime which recovery to use and which arguments to forward.

    4. (Recommended) Pass a namespace so your recoveries don't collide
       with same-named recoveries from other modules/agents.

Run it:

    uv run python examples/example_tool_module.py

Expected output:

    [EXEC] create_file('hello.txt', 'Hello world')
    [LOG ] R → delete_file('hello.txt')

    History:
    001 R create_file        -> delete_file
"""

from __future__ import annotations

import os

from reversible import (
    JournalSink,
    Runtime,
    compensable,
    configure_logging,
    reversible,
)

configure_logging()

# ---------------------------------------------------------------------------
# 1. Custom tool module - your tools
# ---------------------------------------------------------------------------

def delete_file(path: str) -> None:
    """Recovery (inverse) for create_file."""
    if os.path.exists(path):
        os.remove(path)


@reversible(inverse=delete_file, inverse_args=("path",), namespace="example")
def create_file(path: str, content: str) -> None:
    """Forward effect: create a file.

    Decorator declaration:
      - inverse=delete_file       -> recovery operation
      - inverse_args=("path",)    -> which original args to forward
      - namespace="example"       -> scope for this recovery, so a
        same-named recovery in another module/agent doesn't collide
    """
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


# A compensable (K) tool: recovery mitigates the effect but doesn't
# necessarily restore the exact prior state.
def cancel_notification(message: str) -> None:
    """Compensation for send_notification."""
    print(f"[mock] cancelled notification: {message!r}")


@compensable(compensation=cancel_notification, compensation_args=("message",),
              namespace="example")
def send_notification(message: str) -> None:
    """Forward effect: send (here: mock) a notification."""
    print(f"[mock] sent notification: {message!r}")


# ---------------------------------------------------------------------------
# 2. Example: read-only tools are NOT recorded
# ---------------------------------------------------------------------------

def read_file(path: str) -> str:
    """Read-only tool: intentionally NOT decorated -> not recorded."""
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# 3. Usage - record into the action stack (and optionally the journal)
# ---------------------------------------------------------------------------

def main() -> None:
    import tempfile

    workdir = tempfile.mkdtemp(prefix="reversible-example-")

    # In-memory stack only:
    runtime = Runtime(agent_id="example", session_id="demo-1",
                    namespace="example")

    # To also persist to the durable journal, pass a sink:
    #   runtime = Runtime(agent_id="example", session_id="demo-1",
    #                     namespace="example",
    #                     sink=JournalSink("~/.reversible/journal.jsonl"))

    path = os.path.join(workdir, "hello.txt")

    runtime.call(create_file, path, "Hello world")   # recorded (R)
    runtime.call(read_file, path)                    # NOT recorded
    runtime.call(send_notification, "Hello world")   # recorded (K)

    print("\nHistory:")
    for record in runtime.history():
        recovery = (
            record.recovery.__name__ if callable(record.recovery) else record.recovery
        )
        print(f"{record.id} {record.action_type.value} {record.action.__name__:<15} -> {recovery}")

    print(f"\nStack size: {len(runtime)} (read_file was NOT recorded)")
    print(f"Journal sink enabled: {runtime._sink is not None}")

    import shutil
    shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()