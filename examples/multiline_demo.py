"""Compound commands: split, execute, rollback in appearance order.

One bash string like ``touch a && touch b && touch c`` arrives as a
single opaque call. We split it into independent parts, execute each
through the runtime (so each gets its own seq in appearance order), and
rollback undoes them in reverse appearance order. Inline ``$()``
substitutions are hoisted first: the shell evaluates them before the
surrounding command, so they take the earlier seqs.
"""

import tempfile
from pathlib import Path

from reversible import Runtime, reversible
from reversible.recovery_builtin import delete_file
from reversible.shellsplit import split_shell


@reversible(inverse=delete_file, inverse_args=("path",))
def touch(path: str) -> None:
    Path(path).write_text("x")


def main() -> None:
    command = "touch a.txt && touch b.txt && touch c.txt"
    print(f"command: {command}")
    print("split into:")
    for i, part in enumerate(split_shell(command), 1):
        print(f"  {i}. {part.command}")

    with tempfile.TemporaryDirectory() as tmp:
        rt = Runtime()
        for part in split_shell(command):
            _, _, arg = part.command.partition(" ")
            rt.call(touch, f"{tmp}/{arg}")

        print("history (issue order):")
        for r in rt.history():
            print(f"  seq {r.seq}: {r.recovery.__name__} {r.recovery_args}")

        result = rt.rollback()
        print(f"rollback recovered seqs: {result.recovered} (reverse appearance order)")
        print(f"remaining files: {list(Path(tmp).iterdir()) or 'none'}")

    # Inline $() hoisting: the shell runs whoami before the echo.
    inline = "echo $(whoami) && pwd"
    print(f"\ninline example: {inline}")
    print("execution order:")
    for i, part in enumerate(split_shell(inline), 1):
        tag = " (inline, hoisted)" if part.inline else ""
        print(f"  {i}. {part.command}{tag}")


if __name__ == "__main__":
    main()
