"""Presentation: real pi hook -> journal -> CLI rollback -> verify.

This shows the ACTUAL pi integration: the pi extension records real pi tool
calls (write / edit / bash) into the journal, then the CLI rolls them back.

For a live demo:
  1. Run pi with the extension installed (REVERSIBLE_MODE=local in a project)
  2. Do some writes/edits
  3. Run this script to inspect + roll back + verify

This script simulates the journal entries the pi extension writes (same
format), then drives the CLI-equivalent rollback and verifies restoration.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

from reversible import JournalSink, Runtime, configure_logging, read_journal, reversible

configure_logging()

WORKDIR = tempfile.mkdtemp(prefix="reversible-pi-demo-")
JOURNAL = os.path.join(WORKDIR, "journal.jsonl")


def delete_file(path: str) -> None:
    if os.path.exists(path):
        os.remove(path)


@reversible(inverse=delete_file, inverse_args=("path",))
def write_file(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def main() -> None:
    print("=" * 64)
    print("  PI HOOK -> JOURNAL -> CLI ROLLBACK -> VERIFY")
    print("=" * 64)

    # The pi extension writes journal lines in this exact format.
    runtime = Runtime(agent_id="pi", session_id="live-demo", sink=JournalSink(JOURNAL))

    print("\n[1] PI DOES WORK (recorded by the extension)\n")
    files = [os.path.join(WORKDIR, f"f{i}.txt") for i in range(3)]
    for i, f in enumerate(files):
        runtime.call(write_file, f, f"content-{i}")

    print(f"  files exist: {[os.path.exists(f) for f in files]}")

    print("\n[2] JOURNAL (what the extension wrote)\n")
    for r in read_journal(JOURNAL):
        print(f"  seq {r.seq}: {r.action_type} {r.tool} -> {r.recovery}")

    print("\n[3] CLI ROLLBACK (reversible rollback)\n")
    # Drive the same logic as `reversible rollback --journal <path>`.
    result = subprocess.run(
        [sys.executable, "-m", "reversible.cli", "rollback", "--journal", JOURNAL],
        capture_output=True,
        text=True,
    )
    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr)

    print("\n[4] VERIFY REVERSAL\n")
    ok = all(not os.path.exists(f) for f in files)
    print(f"  [{'PASS' if ok else 'FAIL'}] all files deleted by rollback")
    print(f"  [{'PASS' if ok else 'FAIL'}] original state restored")

    shutil.rmtree(WORKDIR, ignore_errors=True)
    print("\nDone.")


if __name__ == "__main__":
    main()
