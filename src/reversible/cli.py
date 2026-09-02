"""Command-line interface for the reversible runtime.

Commands::

    reversible history [--agent X] [--session Y] [--journal PATH]
    reversible rollback [--agent X] [--session Y] [--journal PATH]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .journal import filter_records, mark_rolled_back, read_journal, tombstoned_seqs
from .rollback import RollbackEngine

DEFAULT_JOURNAL = Path.home() / ".reversible" / "journal.jsonl"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reversible",
        description="Reversible Agent Runtime - record and inspect agent tool calls.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    hist = sub.add_parser("history", help="inspect the action journal")
    hist.add_argument("--agent", dest="agent_id", default=None, help="filter by agent id")
    hist.add_argument("--session", dest="session_id", default=None, help="filter by session id")
    hist.add_argument(
        "--journal",
        default=str(DEFAULT_JOURNAL),
        help=f"journal path (default: {DEFAULT_JOURNAL})",
    )
    hist.add_argument("--json", action="store_true", help="emit raw JSON lines")

    rb = sub.add_parser("rollback", help="undo recorded actions (LIFO)")
    rb.add_argument("--agent", dest="agent_id", default=None, help="scope to agent id")
    rb.add_argument("--session", dest="session_id", default=None, help="scope to session id")
    rb.add_argument(
        "--to", dest="checkpoint", type=int, default=None,
        help="only undo actions with seq >= this checkpoint",
    )
    rb.add_argument(
        "--continue-on-error", dest="continue_on_error", action="store_true",
        default=False, help="keep going past failures, undoing what it can",
    )
    rb.add_argument(
        "--journal",
        default=str(DEFAULT_JOURNAL),
        help=f"journal path (default: {DEFAULT_JOURNAL})",
    )

    return parser


def _cmd_history(args: argparse.Namespace) -> int:
    records = read_journal(args.journal)
    records = filter_records(records, agent_id=args.agent_id, session_id=args.session_id)

    if args.json:
        for r in records:
            print(json.dumps(r.to_dict()))
        return 0

    if not records:
        print(f"(no records in {args.journal})")
        return 0

    print(f"Journal: {args.journal}  ({len(records)} records)\n")
    print(f"{'seq':>4}  {'agent':<14} {'session':<12} {'type':<4} {'tool':<20} recovery")
    print("-" * 80)
    done = tombstoned_seqs(args.journal)
    for r in records:
        session = r.session_id[:12] if r.session_id else "-"
        undone = " [UNDONE]" if str(r.seq) in done else ""
        print(
            f"{r.seq:>4}  {r.agent_id:<14} {session:<12} {r.action_type:<4} "
            f"{r.tool:<20} {r.recovery}{undone}"
        )
    return 0


def _cmd_rollback(args: argparse.Namespace) -> int:
    records = read_journal(args.journal)
    records = filter_records(records, agent_id=args.agent_id, session_id=args.session_id)
    # Skip actions already undone by an earlier rollback (idempotent undo).
    done = tombstoned_seqs(args.journal)
    records = [r for r in records if str(r.seq) not in done]

    if args.checkpoint is not None:
        records = [r for r in records if r.seq >= args.checkpoint]

    if not records:
        print(f"(no records to undo in {args.journal})")
        return 0

    print(f"Rollback {len(records)} action(s) from {args.journal} (LIFO)\n")
    engine = RollbackEngine(records, continue_on_error=args.continue_on_error)

    # Mark each seq as it is undone (a killed run keeps its markers), then
    # record the failed set once at the end.
    result = engine.rollback(
        on_recovered=lambda seq: mark_rolled_back(args.journal, [seq], [])
    )
    mark_rolled_back(args.journal, [], [s for s, _ in result.failed])

    for seq in result.recovered:
        print(f"[UNDO] seq {seq} → OK")
    for seq, err in result.failed:
        print(f"[UNDO] seq {seq} → FAIL: {err}")

    if result.ok:
        print("\n[INFO] Rollback complete - environment restored.")
        return 0

    print("\n[ERROR] Rollback stopped - environment NOT fully restored.")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "history":
        return _cmd_history(args)
    if args.command == "rollback":
        return _cmd_rollback(args)

    # Unreachable with required subparsers; parser.error exits.
    parser.error(f"unknown command: {args.command}")

if __name__ == "__main__":
    sys.exit(main())
