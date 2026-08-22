"""Reversal - sandboxed execution with Docker (coarse reversal).

For executing untrusted binaries (e.g. malware analysis) where per-action
inverses are impossible, the container is the TRANSACTION BOUNDARY: run the
work in a throwaway container, and reversal = `docker rm -f` (nuke).

Run:

    uv run python examples/sandbox_docker.py

Requires the `docker` CLI. If docker is unavailable, the example prints a
notice and exits (the library core has zero dependencies; docker is only
used by this example).
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid

from reversible import Runtime, execute, configure_logging

configure_logging()

IMAGE = "alpine:latest"


def _docker(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=False
    )


def docker_available() -> bool:
    return _docker(["version", "--format", "{{.Server.Version}}"]).returncode == 0


class Sandbox:
    """A throwaway container - the unit of reversal."""

    def __init__(self, image: str = IMAGE) -> None:
        self.name = f"reversible-sandbox-{uuid.uuid4().hex[:8]}"
        self.image = image
        self.container_id: str | None = None

    def start(self) -> None:
        r = _docker(
            ["run", "-d", "--name", self.name, self.image, "sleep", "infinity"]
        )
        if r.returncode != 0:
            raise RuntimeError(f"docker run failed: {r.stderr.strip()}")
        self.container_id = r.stdout.strip()

    def exec(self, command: str) -> str:
        r = _docker(["exec", self.name, "sh", "-c", command])
        return r.stdout.strip()

    def nuke(self) -> None:
        """Coarse reversal: destroy the container and everything in it."""
        _docker(["rm", "-f", self.name])
        self.container_id = None


@execute(policy="sandbox")
def run_in_sandbox(sandbox: Sandbox, command: str) -> str:
    """Execute an untrusted command inside the sandbox container."""
    return sandbox.exec(command)


def main() -> None:
    if not docker_available():
        print("[SKIP] docker CLI not available - run this example where docker is installed.")
        return

    print("=== Sandboxed execution (coarse reversal) ===\n")
    runtime = Runtime(agent_id="sandbox-demo", session_id="s1")

    sandbox = Sandbox()
    sandbox.start()
    print(f"container started: {sandbox.name}")

    # Untrusted work inside the sandbox - recorded via @execute(policy="sandbox").
    runtime.call(run_in_sandbox, sandbox, "echo hello > /tmp/marker.txt")
    runtime.call(run_in_sandbox, sandbox, "echo world >> /tmp/marker.txt")

    print(f"marker contents: {sandbox.exec('cat /tmp/marker.txt')!r}")

    print("\n=== History ===\n")
    for record in runtime.history():
        print(f"{record.id} {record.action_type.value} {record.action.__name__}")

    print("\n=== Coarse reversal: nuke the container ===\n")
    sandbox.nuke()
    print(f"container removed: {sandbox.name}")

    # Verify the container is gone (the whole sandbox was the transaction).
    gone = _docker(["inspect", sandbox.name]).returncode != 0
    print(f"container gone: {gone}  (should be True)")
    print("All sandbox effects (files, processes, state) destroyed with it.")

    print("\nDone.")


if __name__ == "__main__":
    main()
