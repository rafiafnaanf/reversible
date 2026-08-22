"""Reversal — the full record → rollback cycle.

The canonical library entry: define effectful tools, record them, then
roll back and verify restoration. Run:

    uv run python examples/reversal_basic.py
"""

from __future__ import annotations

import os
import shutil
import tempfile

from reversible import Runtime, compensable, configure_logging, reversible

configure_logging()

WORKDIR = tempfile.mkdtemp(prefix="reversible-reversal-")


def delete_file(path: str) -> None:
    if os.path.exists(path):
        os.remove(path)


def delete_directory(path: str) -> None:
    if os.path.isdir(path):
        os.rmdir(path)


@reversible(inverse=delete_directory, inverse_args=("path",))
def create_directory(path: str) -> None:
    os.makedirs(path, exist_ok=True)


@reversible(
    inverse=delete_file,
    inverse_args=("path",),
    verify=lambda path: not os.path.exists(path),
)
def create_file(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def cancel_notification(message: str) -> None:
    print(f"[mock] cancelled: {message!r}")


@compensable(compensation=cancel_notification, compensation_args=("message",))
def send_notification(message: str) -> None:
    print(f"[mock] sent: {message!r}")


def main() -> None:
    print("=== Record ===\n")
    runtime = Runtime(agent_id="demo", session_id="reversal-1")

    project = os.path.join(WORKDIR, "project")
    main_py = os.path.join(project, "main.py")

    runtime.call(create_directory, project)
    runtime.call(create_file, main_py, "print('hello')")
    runtime.call(send_notification, "Project created")

    print("\n=== History ===\n")
    for record in runtime.history():
        print(f"{record.id} {record.action_type.value} {record.action.__name__}")

    print("\n=== Rollback (LIFO) ===\n")
    result = runtime.rollback()

    for seq in result.recovered:
        print(f"[UNDO] seq {seq} → OK")
    for seq, err in result.failed:
        print(f"[UNDO] seq {seq} → FAIL: {err}")

    print("\n=== Verify ===\n")
    print(f"rollback ok:      {result.ok}")
    print(f"project exists:   {os.path.exists(project)}  (should be False)")
    print(f"main.py exists:   {os.path.exists(main_py)}  (should be False)")
    print(f"stack empty:      {len(runtime) == 0}")

    shutil.rmtree(WORKDIR, ignore_errors=True)
    print("\nDone.")


if __name__ == "__main__":
    main()
