"""Demo — command → stack.

Shows effectful (write/create/send) tool calls being recorded onto the
action stack, while read-only tools are not recorded. Recovery operations
are recorded but never executed here.
"""

from __future__ import annotations

import os
import shutil
import tempfile

from reversible import Runtime, compensable, configure_logging, reversible

configure_logging()

WORKDIR = tempfile.mkdtemp(prefix="reversible-demo-")


# -- tools ----------------------------------------------------------------

def delete_file(path: str) -> None:
    if os.path.isfile(path):
        os.remove(path)


def delete_directory(path: str) -> None:
    if os.path.isdir(path):
        shutil.rmtree(path)


@reversible(inverse=delete_directory, inverse_args=("path",))
def create_directory(path: str) -> None:
    os.makedirs(path, exist_ok=True)


@reversible(inverse=delete_file, inverse_args=("path",))
def create_file(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


@compensable(compensation=delete_file, compensation_args=("path",))
def write_file(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def read_file(path: str) -> str:
    """Read-only tool — must NOT be recorded."""
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# -- demo -----------------------------------------------------------------

def main() -> None:
    print("=== Execute ===\n")

    runtime = Runtime()

    project = os.path.join(WORKDIR, "project")
    main_py = os.path.join(project, "main.py")

    runtime.call(create_directory, project)
    runtime.call(create_file, main_py, "print('hello')")
    runtime.call(write_file, main_py, "print('hello, world')")
    runtime.call(read_file, main_py)  # not recorded

    print("\n=== History ===\n")
    for record in runtime.history():
        print(f"{record.id} {record.action_type.value} {record.action.__name__}")

    print("\n=== Verification ===\n")
    print(f"main.py exists: {os.path.isfile(main_py)}")
    print(f"stack size:     {len(runtime)}")
    print(f"read_file was NOT recorded: {all(r.action.__name__ != 'read_file' for r in runtime.history())}")

    shutil.rmtree(WORKDIR, ignore_errors=True)
    print("\nDemo complete.")


if __name__ == "__main__":
    main()
