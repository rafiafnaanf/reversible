"""Presentation benchmark: run pi, do commands, revert down the stack, verify.

Simulates a pi session doing effectful work (write / edit / bash), records
it into the journal, then reverts down the stack step by step and verifies
each reversal actually restored state.

Run:

    uv run python examples/presentation_benchmark.py
"""

from __future__ import annotations

import os
import shutil
import tempfile

from reversible import (
    JournalSink,
    Runtime,
    configure_logging,
    read_journal,
    reversible,
)

configure_logging()
import logging as _logging
_logging.getLogger("reversible").setLevel(_logging.WARNING)  # quiet engine logs

WORKDIR = tempfile.mkdtemp(prefix="reversible-benchmark-")
JOURNAL = os.path.join(WORKDIR, "journal.jsonl")


# -- tools (simulate pi's write / edit / bash) ------------------------------

def delete_file(path: str) -> None:
    if os.path.exists(path):
        os.remove(path)


def restore_file(path: str, preimage_path: str) -> None:
    """Inverse of edit: restore the pre-captured bytes."""
    if os.path.exists(preimage_path):
        shutil.copyfile(preimage_path, path)


@reversible(inverse=delete_file, inverse_args=("path",))
def write_file(path: str, content: str) -> None:
    """Simulate pi's write tool: create/overwrite a file."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


@reversible(inverse=restore_file, inverse_args=("path", "preimage_path"))
def edit_file(path: str, content: str, preimage_path: str = "") -> None:
    """Simulate pi's edit tool: overwrite, keeping a preimage."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


# -- benchmark --------------------------------------------------------------

def check(label: str, cond: bool) -> None:
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}")


def main() -> None:
    print("=" * 64)
    print("  REVERSIBLE AGENT RUNTIME - PRESENTATION BENCHMARK")
    print("  run pi -> do commands -> revert down the stack -> verify")
    print("=" * 64)

    runtime = Runtime(agent_id="pi", session_id="demo-1", sink=JournalSink(JOURNAL))

    # -- Phase 1: pi does effectful work -----------------------------------
    print("\n[1] PI RUNS COMMANDS\n")
    a = os.path.join(WORKDIR, "a.txt")
    b = os.path.join(WORKDIR, "b.txt")
    c = os.path.join(WORKDIR, "c.txt")

    runtime.call(write_file, a, "alpha")
    runtime.call(write_file, b, "beta")
    runtime.call(write_file, c, "gamma")

    # Simulate an edit: capture preimage, then overwrite.
    preimage = os.path.join(WORKDIR, "c.preimage")
    shutil.copyfile(c, preimage)
    runtime.call(edit_file, c, "GAMMA EDITED", preimage_path=preimage)

    print(f"  files created: a.txt={os.path.exists(a)} b.txt={os.path.exists(b)} "
          f"c.txt={os.path.exists(c)}")
    print(f"  c.txt content: {open(c).read()!r}")

    # -- Phase 2: show the stack -------------------------------------------
    print("\n[2] ACTION STACK (journal)\n")
    for r in read_journal(JOURNAL):
        print(f"  seq {r.seq}: {r.action_type} {r.tool} -> {r.recovery}")

    # -- Phase 3: revert down the stack, verifying each ---------------------
    print("\n[3] REVERT DOWN THE STACK (LIFO)\n")

    # Step 1: undo the edit (restore c.txt from preimage)
    print("  step 1: undo edit_file(c.txt) -> restore_file")
    result = runtime.rollback_to(4)  # undo seq >= 4 (only the edit)
    check("c.txt restored to 'gamma'", open(c).read() == "gamma")
    check("edit undone (seq 4 gone)", len(runtime) == 3)

    # Step 2: undo write c.txt (delete)
    print("  step 2: undo write_file(c.txt) -> delete_file")
    result = runtime.rollback_to(3)  # undo seq >= 3 (the write of c)
    check("c.txt deleted", not os.path.exists(c))

    # Step 3: undo write b.txt (delete)
    print("  step 3: undo write_file(b.txt) -> delete_file")
    result = runtime.rollback_to(2)  # undo seq >= 2 (the write of b)
    check("b.txt deleted", not os.path.exists(b))

    # Step 4: undo write a.txt (delete)
    print("  step 4: undo write_file(a.txt) -> delete_file")
    result = runtime.rollback_to(1)  # undo seq >= 1 (the write of a)
    check("a.txt deleted", not os.path.exists(a))

    # -- Phase 4: final state ----------------------------------------------
    print("\n[4] FINAL STATE\n")
    check("stack empty (all reverted)", len(runtime) == 0)
    check("no files remain", not os.path.exists(a) and not os.path.exists(b)
          and not os.path.exists(c))
    print("\n  All reversals verified. Original state restored.")

    shutil.rmtree(WORKDIR, ignore_errors=True)
    print("\nDone.")


if __name__ == "__main__":
    main()
