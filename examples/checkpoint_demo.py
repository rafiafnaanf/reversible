"""Reversal - checkpoints: roll back to a specific point.

Shows surgical rollback: set a checkpoint, do more work, then undo only
the work after the checkpoint, leaving earlier actions intact.

Run:

    uv run python examples/checkpoint_demo.py
"""

from __future__ import annotations

import os
import shutil
import tempfile

from reversible import Runtime, configure_logging, reversible

configure_logging()

WORKDIR = tempfile.mkdtemp(prefix="reversible-checkpoint-")


def delete_file(path: str) -> None:
    if os.path.exists(path):
        os.remove(path)


@reversible(inverse=delete_file, inverse_args=("path",))
def create_file(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def main() -> None:
    print("=== Checkpoints ===\n")
    runtime = Runtime(agent_id="demo", session_id="checkpoint-1")

    # Phase 1: setup (keep)
    runtime.call(create_file, os.path.join(WORKDIR, "config.txt"), "base config")
    runtime.call(create_file, os.path.join(WORKDIR, "setup.txt"), "setup done")

    checkpoint = runtime.checkpoint()
    print(f"checkpoint taken at seq {checkpoint}\n")

    # Phase 2: work that goes wrong (undo)
    runtime.call(create_file, os.path.join(WORKDIR, "temp1.txt"), "scratch")
    runtime.call(create_file, os.path.join(WORKDIR, "temp2.txt"), "more scratch")

    print("before rollback_to:")
    print(f"  config.txt: {os.path.exists(os.path.join(WORKDIR, 'config.txt'))}")
    print(f"  temp1.txt:  {os.path.exists(os.path.join(WORKDIR, 'temp1.txt'))}")

    print(f"\n=== rollback_to({checkpoint}) - undo only the post-checkpoint work ===\n")
    result = runtime.rollback_to(checkpoint)
    for seq in result.recovered:
        print(f"[UNDO] seq {seq} → OK")

    print("\n=== Verify ===\n")
    print(f"config.txt exists: {os.path.exists(os.path.join(WORKDIR, 'config.txt'))}  (should be True - kept)")
    print(f"setup.txt exists:  {os.path.exists(os.path.join(WORKDIR, 'setup.txt'))}  (should be True - kept)")
    print(f"temp1.txt exists:  {os.path.exists(os.path.join(WORKDIR, 'temp1.txt'))}  (should be False - undone)")
    print(f"temp2.txt exists:  {os.path.exists(os.path.join(WORKDIR, 'temp2.txt'))}  (should be False - undone)")
    print(f"stack size:        {len(runtime)}  (should be 2 - the kept setup)")

    shutil.rmtree(WORKDIR, ignore_errors=True)
    print("\nDone.")


if __name__ == "__main__":
    main()
