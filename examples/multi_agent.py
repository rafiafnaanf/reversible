"""Stage 2 demo — three agents, one shared durable journal.

Simulates the motivating scenario: mixed harness/system agents each record
effectful calls into a single JSONL journal, tagged with agent_id and
session_id. The CLI can then inspect (and later roll back) per agent.
"""

from __future__ import annotations

import os
import tempfile

from reversible import (
    JournalSink,
    Runtime,
    compensable,
    configure_logging,
    filter_records,
    read_journal,
    reversible,
)

configure_logging()

WORKDIR = tempfile.mkdtemp(prefix="reversible-multiagent-")
JOURNAL = os.path.join(WORKDIR, "journal.jsonl")


# -- tools ----------------------------------------------------------------

def delete_file(path: str) -> None:
    if os.path.isfile(path):
        os.remove(path)


@reversible(inverse=delete_file, inverse_args=("path",))
def create_file(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def cancel_notification(message: str) -> None:
    pass


@compensable(compensation=cancel_notification, compensation_args=("message",))
def send_notification(message: str) -> None:
    pass


# -- agents ---------------------------------------------------------------

def pi_agent() -> None:
    """Agent A — pi harness (session s-pi-1)."""
    runtime = Runtime(agent_id="pi", session_id="s-pi-1", sink=JournalSink(JOURNAL))
    runtime.call(create_file, os.path.join(WORKDIR, "notes.md"), "# notes")
    runtime.call(create_file, os.path.join(WORKDIR, "todo.md"), "- [ ] task")


def email_agent() -> None:
    """Agent B — system agent (session s-email-1)."""
    runtime = Runtime(agent_id="email-agent", session_id="s-email-1", sink=JournalSink(JOURNAL))
    runtime.call(send_notification, "Deployment finished")


def wazuh_agent() -> None:
    """Agent C — system agent (session s-wazuh-1)."""
    runtime = Runtime(agent_id="wazuh-agent", session_id="s-wazuh-1", sink=JournalSink(JOURNAL))
    runtime.call(create_file, os.path.join(WORKDIR, "alert.txt"), "high severity")
    runtime.call(send_notification, "Alert raised")


def main() -> None:
    print("=== Three agents record into ONE journal ===\n")

    pi_agent()
    email_agent()
    wazuh_agent()

    records = read_journal(JOURNAL)
    print(f"journal: {JOURNAL}")
    print(f"total records: {len(records)}\n")

    print(f"{'seq':>4}  {'agent':<14} {'session':<12} {'type':<4} {'tool':<20} recovery")
    print("-" * 78)
    for r in records:
        session = r.session_id[:12] if r.session_id else "-"
        print(
            f"{r.seq:>4}  {r.agent_id:<14} {session:<12} {r.action_type:<4} "
            f"{r.tool:<20} {r.recovery}"
        )

    print("\n=== Per-agent filtering (the motivating scenario) ===\n")
    for agent in ("pi", "email-agent", "wazuh-agent"):
        scoped = filter_records(records, agent_id=agent)
        print(f"{agent}: {len(scoped)} record(s) — " + ", ".join(r.tool for r in scoped))

    print("\n=== CLI equivalent ===\n")
    print("uv run reversible history --journal <path> --agent email-agent")

    import shutil
    shutil.rmtree(WORKDIR, ignore_errors=True)
    print("\nDemo complete.")


if __name__ == "__main__":
    main()
