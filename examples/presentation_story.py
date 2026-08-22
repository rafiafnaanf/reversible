"""Interactive presentation story: a cybersecurity incident response.

A SOC analyst agent responds to a suspected compromise. It takes several
effectful actions, then discovers it made a mistake and reverts down the
stack, verifying each reversal.

Run:

    uv run python examples/presentation_story.py

Each step pauses (press Enter) so you can narrate it live to your
supervisor. The story shows:
  - R actions (delete, restore via preimage)
  - K actions (compensation: unblock an IP)
  - LIFO rollback
  - Verification after each recovery
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

from reversible import (
    JournalSink,
    Runtime,
    compensable,
    configure_logging,
    read_journal,
    reversible,
)

configure_logging()

# When piped (not a TTY), don't pause - lets tests/CI run it non-interactively.
INTERACTIVE = sys.stdin.isatty()

WORKDIR = tempfile.mkdtemp(prefix="reversible-incident-")
JOURNAL = os.path.join(WORKDIR, "journal.jsonl")

# Simulated "system state" the agent acts on.
STATE = {
    "firewall_blocked_ips": [],   # list of blocked IPs
    "quarantine_dir": os.path.join(WORKDIR, "quarantine"),
}


def step(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")
    if INTERACTIVE:
        input("  [press Enter to continue] ")


def check(label: str, cond: bool) -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


# -- tools (the SOC agent's effectful actions) ------------------------------

def delete_file(path: str) -> None:
    if os.path.exists(path):
        os.remove(path)


def restore_file(path: str, preimage_path: str) -> None:
    if os.path.exists(preimage_path):
        shutil.copyfile(preimage_path, path)


def unblock_ip(ip: str) -> None:
    """Compensation for block_ip: remove the IP from the firewall."""
    if ip in STATE["firewall_blocked_ips"]:
        STATE["firewall_blocked_ips"].remove(ip)


# Custom recoveries must be registered by name so journal records can
# resolve them at rollback time (like the built-in delete_file etc.).
from reversible import registry as _registry

_registry.register_recovery("unblock_ip", unblock_ip)


@reversible(inverse=delete_file, inverse_args=("dest",))
def quarantine_file(path: str, dest: str) -> None:
    """R: copy a suspicious file into quarantine.

    The inverse deletes the QUARANTINED copy (dest), not the source.
    """
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copyfile(path, dest)


@reversible(inverse=restore_file, inverse_args=("path", "preimage_path"))
def edit_config(path: str, content: str, preimage_path: str = "") -> None:
    """R: edit a config file, keeping a preimage for restore."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


@compensable(compensation=unblock_ip, compensation_args=("ip",))
def block_ip(ip: str) -> None:
    """K: block an IP on the firewall (compensation = unblock)."""
    STATE["firewall_blocked_ips"].append(ip)


def main() -> None:
    print("=" * 60)
    print("  CYBERSECURITY INCIDENT RESPONSE - REVERSIBLE AGENT")
    print("=" * 60)
    print("\n  A SOC analyst agent responds to a suspected compromise.")
    print("  It takes effectful actions... then makes a mistake.")
    print("  Watch it revert down the stack, verifying each step.")

    runtime = Runtime(agent_id="soc-agent", session_id="incident-1",
                      sink=JournalSink(JOURNAL))

    # -- Setup: the suspicious file and config -----------------------------
    os.makedirs(STATE["quarantine_dir"], exist_ok=True)
    suspicious = os.path.join(WORKDIR, "suspicious.bin")
    with open(suspicious, "w", encoding="utf-8") as fh:
        fh.write("malware-bytes")
    config = os.path.join(WORKDIR, "app.conf")
    with open(config, "w", encoding="utf-8") as fh:
        fh.write("log_level=info\n")

    # -- Step 1: quarantine the suspicious file ----------------------------
    step("STEP 1: Agent quarantines a suspicious file (R action)")
    print("  quarantine_file(suspicious.bin -> quarantine/)")
    quarantined = os.path.join(STATE["quarantine_dir"], "suspicious.bin")
    runtime.call(quarantine_file, suspicious, quarantined)
    check("file copied to quarantine", os.path.exists(quarantined))
    print("  Recovery recorded: delete_file(quarantined)")

    # -- Step 2: block a suspicious IP -------------------------------------
    step("STEP 2: Agent blocks a suspicious IP (K action)")
    print("  block_ip('203.0.113.66')")
    runtime.call(block_ip, "203.0.113.66")
    check("IP blocked", "203.0.113.66" in STATE["firewall_blocked_ips"])
    print("  Compensation recorded: unblock_ip('203.0.113.66')")

    # -- Step 3: harden the config ----------------------------------------
    step("STEP 3: Agent hardens the config (R action, preimage)")
    print("  edit_config(app.conf, 'log_level=debug', preimage)")
    preimage = os.path.join(WORKDIR, "app.conf.preimage")
    shutil.copyfile(config, preimage)
    runtime.call(edit_config, config, "log_level=debug", preimage_path=preimage)
    check("config now debug", open(config).read() == "log_level=debug")
    print("  Recovery recorded: restore_file(app.conf, preimage)")

    # -- Show the stack ----------------------------------------------------
    step("THE ACTION STACK SO FAR")
    for r in read_journal(JOURNAL):
        print(f"  seq {r.seq}: {r.action_type} {r.tool} -> {r.recovery}")

    # -- Step 4: the mistake -----------------------------------------------
    step("STEP 4: THE MISTAKE - wrong IP blocked!")
    print("  The agent meant to block the suspicious IP, but analysts")
    print("  misidentified it: '198.51.100.10' is a LEGIT CUSTOMER.")
    print("  The agent's block_ip call was recorded as a K action.")
    runtime.call(block_ip, "198.51.100.10")  # the mistake, recorded
    check("customer IP wrongly blocked", "198.51.100.10" in STATE["firewall_blocked_ips"])
    print("  Recovery recorded: unblock_ip('198.51.100.10')")
    print("\n  The agent must revert to before the mistake.")

    # -- Step 5: revert down the stack ------------------------------------
    step("STEP 5: REVERT DOWN THE STACK (LIFO)")
    print("  Rolling back the last 4 actions, verifying each...\n")

    # Undo the wrong block (compensation: unblock customer)
    print("  [1/4] undo block_ip -> unblock_ip('198.51.100.10')")
    runtime.rollback_to(4)
    check("customer IP unblocked", "198.51.100.10" not in STATE["firewall_blocked_ips"])

    # Undo edit_config (restore preimage)
    print("  [2/4] undo edit_config -> restore_file(app.conf, preimage)")
    runtime.rollback_to(3)
    check("config restored to 'log_level=info'", open(config).read().strip() == "log_level=info")

    # Undo block_ip (compensation: unblock suspicious)
    print("  [3/4] undo block_ip -> unblock_ip('203.0.113.66')")
    runtime.rollback_to(2)
    check("suspicious IP unblocked", "203.0.113.66" not in STATE["firewall_blocked_ips"])

    # Undo quarantine (delete the quarantined copy)
    print("  [4/4] undo quarantine_file -> delete_file(quarantined)")
    runtime.rollback_to(1)
    check("quarantine removed", not os.path.exists(quarantined))

    # -- Step 6: final state ----------------------------------------------
    step("FINAL STATE - original state restored")
    check("stack empty", len(runtime) == 0)
    check("no quarantine copy remains", not os.path.exists(quarantined))
    check("config back to info", open(config).read().strip() == "log_level=info")
    check("no IPs blocked", STATE["firewall_blocked_ips"] == [])
    print("\n  The agent's actions were fully reversed, verified step by step.")
    print("  The 'wrong IP' mistake was rolled back with everything else.")

    shutil.rmtree(WORKDIR, ignore_errors=True)
    print("\n  Done.")


if __name__ == "__main__":
    main()
