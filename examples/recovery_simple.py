"""Reversal - simple recovery entries.

Shows the "simple stuff": trivial inverses that need no preimage, plus
append-write which needs the original size captured BEFORE the append.

Run:

    uv run python examples/recovery_simple.py
"""

from __future__ import annotations

import os
import shutil
import tempfile

from reversible import Runtime, configure_logging, reversible

configure_logging()

WORKDIR = tempfile.mkdtemp(prefix="reversible-simple-")


# -- empty file / empty dir delete (trivial inverses, no preimage) ----------

def delete_file(path: str) -> None:
    if os.path.exists(path):
        os.remove(path)


def delete_directory(path: str) -> None:
    if os.path.isdir(path):
        os.rmdir(path)


@reversible(inverse=delete_file, inverse_args=("path",))
def create_empty_file(path: str) -> None:
    with open(path, "w", encoding="utf-8"):
        pass


@reversible(inverse=delete_directory, inverse_args=("path",))
def create_empty_directory(path: str) -> None:
    os.makedirs(path, exist_ok=True)


# -- append write (needs original size captured BEFORE the append) -----------

def truncate_file(path: str, original_size: int) -> None:
    """Inverse of append_write: truncate back to the pre-append size.

    If the file didn't exist before the append (original_size == 0), the
    correct inverse is to delete it entirely.
    """
    if original_size == 0:
        if os.path.exists(path):
            os.remove(path)
        return
    with open(path, "r+b") as fh:
        fh.truncate(original_size)


@reversible(
    inverse=truncate_file,
    inverse_args=("path", "original_size"),
    verify=lambda path, original_size: (
        not os.path.exists(path) if original_size == 0
        else os.path.getsize(path) == original_size
    ),
)
def append_write(path: str, content: str, original_size: int = 0) -> None:
    """Append to a file.

    The recovery (truncate) needs the size BEFORE the append, so the tool
    captures it itself and passes it through as ``original_size`` - the
    preimage rule: recovery args must be captured pre-execution.
    """
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(content)


def append_write_capture(path: str, content: str) -> None:
    """Capture pre-state, then append (so recovery has the original size)."""
    original_size = os.path.getsize(path)
    append_write(path, content, original_size=original_size)


def main() -> None:
    print("=== Simple recovery entries ===\n")
    runtime = Runtime(agent_id="demo", session_id="simple-1")

    empty_file = os.path.join(WORKDIR, "empty.txt")
    empty_dir = os.path.join(WORKDIR, "emptydir")
    log_file = os.path.join(WORKDIR, "app.log")

    runtime.call(create_empty_file, empty_file)
    runtime.call(create_empty_directory, empty_dir)

    # Append-write: capture the size BEFORE, then record the append via the
    # decorated tool (so the runtime sees the recovery arg).
    open(log_file, "a", encoding="utf-8").close()  # ensure it exists
    original = os.path.getsize(log_file)
    runtime.call(append_write, log_file, "first line\n", original_size=original)
    original = os.path.getsize(log_file)
    runtime.call(append_write, log_file, "second line\n", original_size=original)

    print(f"before rollback: log size = {os.path.getsize(log_file)}")

    print("\n=== Rollback ===\n")
    result = runtime.rollback()
    for seq in result.recovered:
        print(f"[UNDO] seq {seq} → OK")

    print("\n=== Verify ===\n")
    print(f"empty file exists:  {os.path.exists(empty_file)}  (should be False)")
    print(f"empty dir exists:   {os.path.exists(empty_dir)}  (should be False)")
    print(f"log file exists:    {os.path.exists(log_file)}  (should be False)")
    print(f"rollback ok:        {result.ok}")

    shutil.rmtree(WORKDIR, ignore_errors=True)
    print("\nDone.")


if __name__ == "__main__":
    main()
