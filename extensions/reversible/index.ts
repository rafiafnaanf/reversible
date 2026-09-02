/**
 * Reversible Agent Runtime - global pi hook.
 *
 * Records effectful pi tool calls (write / edit / bash) into a durable
 * JSONL journal, tagged with the session id. Read-only tools (read / grep /
 * find / ls) are never recorded.
 *
 * Two modes:
 *   - global (default): journal in ~/.reversible/journal.jsonl
 *   - local: journal in <project>/.reversible/journal.jsonl (per-project)
 * Set the mode with the REVERSIBLE_MODE env var ("global" | "local").
 *
 * Recording happens on `tool_result` (post-execution, `isError === false`),
 * so only effects that actually happened are logged. `tool_call` is used
 * only for preimage capture: the inverse of "overwrite a file" is "restore
 * the old bytes", which must be snapshotted before execution.
 *
 * The extension never invokes Python - it appends JSON lines directly to
 * the journal file. Python is used only for rollback/inspection (CLI).
 *
 * Install: place in ~/.pi/agent/extensions/reversible/index.ts (global)
 *          or .pi/extensions/reversible/index.ts (project-local).
 */
import type { ExtensionAPI, ToolResultEvent, ToolCallEvent } from "@earendil-works/pi-coding-agent";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { exec } from "node:child_process";

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

const AGENT_ID = "pi";
const NAMESPACE = "pi";
const MAX_PREIMAGE_BYTES = 5 * 1024 * 1024; // skip snapshotting huge files

/** "global" (default) or "local" (per-project). */
const MODE = (process.env.REVERSIBLE_MODE ?? "global").toLowerCase();

/** Resolve journal + preimage dirs for a given project cwd. */
function resolvePaths(cwd: string): { journal: string; preimages: string } {
  if (MODE === "local") {
    return {
      journal: path.join(cwd, ".reversible", "journal.jsonl"),
      preimages: path.join(cwd, ".reversible", "preimages"),
    };
  }
  const dir = path.join(os.homedir(), ".reversible");
  return {
    journal: path.join(dir, "journal.jsonl"),
    preimages: path.join(dir, "preimages"),
  };
}

/** Classification table: which pi tools are effectful, and how to recover. */
interface ToolClass {
  actionType: "R" | "K";
  recovery: string;
  /** names of original args forwarded to recovery (positional) */
  recoveryArgs: string[];
  /** snapshot the target file before execution (for write/edit on existing) */
  preimage?: boolean;
}

const TOOL_CLASSES: Record<string, ToolClass> = {
  write: { actionType: "R", recovery: "restore_file", recoveryArgs: ["path", "preimage_path"], preimage: true },
  edit: { actionType: "R", recovery: "restore_file", recoveryArgs: ["path", "preimage_path"], preimage: true },
  bash: { actionType: "K", recovery: "noop", recoveryArgs: [] },
  // read-only tools: intentionally absent → not recorded
};

// ---------------------------------------------------------------------------
// Journal helpers
// ---------------------------------------------------------------------------

let seqCounter = 0;

function nextSeq(journalPath: string): number {
  // Best-eff: read tail of journal for the max seq, then increment.
  // Rollback markers carry no seq; reserved lines do - both handled by
  // the tail scan (first line from the end with a finite seq wins).
  try {
    if (fs.existsSync(journalPath)) {
      const lines = fs.readFileSync(journalPath, "utf8").trim().split("\n").filter(Boolean);
      for (let i = lines.length - 1; i >= 0; i--) {
        try {
          const seq = Number(JSON.parse(lines[i]).seq);
          if (Number.isFinite(seq)) return seq + 1;
        } catch { continue; }
      }
    }
  } catch { /* ignore */ }
  return ++seqCounter;
}

/**
 * Cross-language advisory lock via O_EXCL lock-file creation.
 *
 * Node has no built-in flock, so we mirror the Python JournalLock: atomically
 * create ``<journal>.lock`` with O_CREAT|O_EXCL, retry until we win, then
 * work and remove the lock. This serializes the read-max + append critical
 * section against the Python runtime and other writers. A lock file older
 * than STALE_LOCK_SECONDS is taken over once - a crashed writer must not
 * wedge the journal forever.
 */
const LOCK_RETRIES = 1000;
const LOCK_DELAY_MS = 1;
const STALE_LOCK_MS = 5_000;

function withLock<T>(lockPath: string, fn: () => T): T {
  // Synchronous sleep via Atomics.wait (no setTimeoutSync in Node).
  const sab = new SharedArrayBuffer(4);
  const int32 = new Int32Array(sab);
  let tookOverStale = false;
  for (let i = 0; i < LOCK_RETRIES; i++) {
    try {
      const fd = fs.openSync(lockPath, "wx"); // O_CREAT|O_EXCL
      try {
        fs.writeSync(fd, String(process.pid));
        return fn();
      } finally {
        fs.closeSync(fd);
        try { fs.unlinkSync(lockPath); } catch { /* ignore */ }
      }
    } catch (err) {
      if ((err as NodeJS.ErrnoException).code === "EEXIST") {
        if (!tookOverStale) {
          try {
            const age = Date.now() - fs.statSync(lockPath).mtimeMs;
            if (age > STALE_LOCK_MS) {
              fs.unlinkSync(lockPath); // crashed writer: take over once
              tookOverStale = true;
              continue;
            }
          } catch { /* released between EEXIST and stat: retry */ }
        }
        Atomics.wait(int32, 0, 0, LOCK_DELAY_MS);
        continue;
      }
      throw err;
    }
  }
  throw new Error(`could not acquire journal lock: ${lockPath}`);
}

/**
 * seq assigned at ISSUE time (program order), keyed by toolCallId.
 *
 * This is the Reorder-Buffer rule: assign the sequence number when the
 * tool call is issued (tool_call, in assistant source order), NOT when it
 * completes (tool_result, arbitrary for parallel execution). Rollback then
 * retires in descending seq - deterministic regardless of completion order.
 *
 * The reservation is DURABLE: a ``{"type": "reserved"}`` line is appended
 * under the lock at issue time, so no other writer (or sibling tool call)
 * can be assigned the same seq while the full record is still in flight.
 */
const issuedSeqs = new Map<string, number>();

function assignSeqAtIssue(toolCallId: string, journalPath: string): number {
  let seq: number;
  withLock(journalPath + ".lock", () => {
    seq = nextSeq(journalPath);
    _appendLine(
      { type: "reserved", seq, toolCallId, ts: new Date().toISOString() },
      journalPath,
    );
  });
  issuedSeqs.set(toolCallId, seq!);
  return seq!;
}

function takeSeqAtCompletion(toolCallId: string, journalPath: string): number {
  const seq = issuedSeqs.get(toolCallId);
  issuedSeqs.delete(toolCallId);
  return seq ?? nextSeq(journalPath); // fallback: not issued (shouldn't happen)
}

/** Append one line. Caller must hold the journal lock. */
function _appendLine(record: Record<string, unknown>, journalPath: string): void {
  fs.mkdirSync(path.dirname(journalPath), { recursive: true });
  fs.appendFileSync(journalPath, JSON.stringify(record) + "\n", "utf8");
}

/** Append a record under the journal lock (the write is the critical section). */
function appendJournal(record: Record<string, unknown>, journalPath: string): void {
  withLock(journalPath + ".lock", () => {
    _appendLine(record, journalPath);
  });
}

function preimagePath(preimagesDir: string, toolCallId: string): string {
  return path.join(preimagesDir, `${toolCallId}.preimage`);
}

type PreimageStatus = "absent" | "captured" | "skipped";

/** Status of the last capture, keyed by toolCallId (same lifetime as issuedSeqs). */
const preimageStatus = new Map<string, PreimageStatus>();

/**
 * Snapshot a file's current bytes before an overwrite, and REMEMBER why.
 *
 * "absent"  - the file did not exist: the inverse is delete_file.
 * "captured" - preimage on disk: the inverse is restore_file.
 * "skipped"  - the file EXISTS but we could not snapshot it (too large,
 *              stat/copy error): there is NO safe inverse. Rollback must
 *              not delete a file that existed before we touched it.
 */
function capturePreimage(
  filePath: string,
  preimagesDir: string,
  toolCallId: string,
): void {
  let status: PreimageStatus = "skipped";
  try {
    const st = fs.statSync(filePath);
    if (st.size <= MAX_PREIMAGE_BYTES) {
      fs.mkdirSync(preimagesDir, { recursive: true });
      fs.copyFileSync(filePath, preimagePath(preimagesDir, toolCallId));
      status = "captured";
    }
  } catch (err) {
    status =
      (err as NodeJS.ErrnoException).code === "ENOENT" ? "absent" : "skipped";
  }
  preimageStatus.set(toolCallId, status);
}

// ---------------------------------------------------------------------------
// Recording
// ---------------------------------------------------------------------------

function recordResult(
  event: ToolResultEvent,
  sessionId: string,
  paths: { journal: string; preimages: string },
): void {
  const cls = TOOL_CLASSES[event.toolName];
  if (!cls) return; // read-only / unknown → not recorded

  const args = { ...event.input } as Record<string, unknown>;
  let actionType = cls.actionType;
  let recovery = cls.recovery;
  let recoveryArgs: unknown[] = cls.recoveryArgs.map((name) => args[name]);

  if (cls.preimage && typeof args.path === "string") {
    const pre = preimagePath(paths.preimages, event.toolCallId);
    // Unknown status (hook restarted mid-call) is treated as skipped:
    // when we cannot PROVE the file was absent, we must not delete it.
    const status: PreimageStatus =
      preimageStatus.get(event.toolCallId) ??
      (fs.existsSync(pre) ? "captured" : "skipped");
    if (status === "captured") {
      recovery = "restore_file";
      recoveryArgs = [args.path, pre];
    } else if (status === "absent") {
      // The write CREATED this file, so the exact inverse is delete.
      recovery = "delete_file";
      recoveryArgs = [args.path];
    } else {
      // Existing file, snapshot unavailable: no safe inverse. Record as
      // record-only (K/noop) - manual reversal, never a false "restored".
      actionType = "K";
      recovery = "noop";
      recoveryArgs = [];
    }
    preimageStatus.delete(event.toolCallId);
  }

  appendJournal(
    {
      seq: takeSeqAtCompletion(event.toolCallId, paths.journal),
      agent_id: AGENT_ID,
      namespace: NAMESPACE,
      session_id: sessionId,
      tool: event.toolName,
      args,
      action_type: actionType,
      recovery,
      recovery_args: recoveryArgs,
      recovery_kwargs: {},
      is_error: event.isError,
      result_summary: summarizeContent(event.content),
      ts: new Date().toISOString(),
    },
    paths.journal,
  );
}

function summarizeContent(content: unknown): string {
  try {
    if (Array.isArray(content)) {
      const text = content
        .filter((c) => c && (c as { type?: string }).type === "text")
        .map((c) => (c as { text?: string }).text ?? "")
        .join(" ")
        .trim();
      return text.slice(0, 200);
    }
    return "";
  } catch {
    return "";
  }
}

// ---------------------------------------------------------------------------
// Revert command (/revert): undo recorded actions via the Python CLI
// ---------------------------------------------------------------------------

interface JournalLine {
  seq?: number;
  type?: string;
  tool?: string;
  args?: Record<string, unknown>;
  session_id?: string;
  is_error?: boolean;
  recovered?: string[];
}

/** Pending (not yet undone) journal lines for one session, oldest first. */
function pendingLines(journalPath: string, sessionId: string): JournalLine[] {
  try {
    if (!fs.existsSync(journalPath)) return [];
    const lines = fs.readFileSync(journalPath, "utf8").split("\n").filter(Boolean);
    const parsed: JournalLine[] = [];
    for (const line of lines) {
      try { parsed.push(JSON.parse(line) as JournalLine); } catch { continue; }
    }
    const undone = new Set<string>();
    for (const marker of parsed.filter((l) => l.type === "rollback")) {
      for (const seq of marker.recovered ?? []) undone.add(String(seq));
    }
    return parsed.filter(
      (l) =>
        l.type !== "rollback" &&
        l.type !== "reserved" && // in-flight: no record to undo yet
        typeof l.seq === "number" &&
        !undone.has(String(l.seq)) &&
        l.session_id === sessionId,
    );
  } catch {
    return [];
  }
}

function describeLine(line: JournalLine): string {
  const target =
    typeof line.args?.path === "string"
      ? line.args.path
      : typeof line.args?.command === "string"
        ? String(line.args.command).slice(0, 60)
        : "";
  return `${line.tool ?? "?"}${target ? ` ${target}` : ""}`;
}

/** Run the Python rollback CLI and report the outcome. */
function runRevert(
  cliCommand: string,
  journalPath: string,
  sessionId: string,
  toSeq: number | undefined,
  cwd: string,
  notify: (msg: string, level?: "info" | "error") => void,
): void {
  const args = [
    "rollback", "--journal", journalPath,
    "--agent", "pi", "--session", sessionId,
  ];
  if (toSeq !== undefined) args.push("--to", String(toSeq));
  const cmd = `${cliCommand} ${args.map(quote).join(" ")}`;
  exec(cmd, { cwd, timeout: 120_000 }, (err, stdout, stderr) => {
    const output = `${stdout ?? ""}${stderr ? `\n${stderr}` : ""}`.trim();
    if (err) {
      const hint = err.message.includes("ENOENT")
        ? " (is the reversible CLI installed? set REVERSIBLE_CLI, e.g.\n  uv run --project /path/to/reversible python -m reversible.cli)"
        : "";
      notify(`[reversible] revert FAILED${hint}\n${output.slice(0, 800)}`, "error");
    } else {
      notify(`[reversible] reverted:\n${output.slice(0, 800)}`, "info");
    }
  });
}

function quote(s: string): string {
  return /^[A-Za-z0-9_@%+=:,./-]+$/.test(s) ? s : `"${s.replace(/(["$`\\])/g, "\\$1")}"`;
}

// ---------------------------------------------------------------------------
// Extension
// ---------------------------------------------------------------------------

export default function (pi: ExtensionAPI) {
  // Issue: assign seq (program order) + preimage capture, BEFORE execution.
  // tool_call fires in assistant source order, so seq reflects intent.
  // Recording is auxiliary: a failure here must never break tool dispatch.
  pi.on("tool_call", (event: ToolCallEvent, ctx) => {
    try {
      const paths = resolvePaths(ctx?.cwd ?? process.cwd());
      if (TOOL_CLASSES[event.toolName]) {
        assignSeqAtIssue(event.toolCallId, paths.journal);
      }
      if (event.toolName === "write" || event.toolName === "edit") {
        const input = event.input as { path?: string };
        if (typeof input.path === "string") {
          capturePreimage(input.path, paths.preimages, event.toolCallId);
        }
      }
    } catch (err) {
      console.error(`[reversible] issue-time recording failed: ${err}`);
    }
  });

  // Recording: after execution, only if it succeeded. Errored calls are
  // not recorded, and their issue-time state is cleaned up.
  pi.on("tool_result", (event: ToolResultEvent, ctx) => {
    if (event.isError) {
      issuedSeqs.delete(event.toolCallId);
      preimageStatus.delete(event.toolCallId);
      return;
    }
    try {
      const sessionId = ctx?.sessionManager?.getSessionId() ?? "";
      const paths = resolvePaths(ctx?.cwd ?? process.cwd());
      recordResult(event, sessionId, paths);
    } catch (err) {
      issuedSeqs.delete(event.toolCallId);
      preimageStatus.delete(event.toolCallId);
      console.error(`[reversible] recording failed: ${err}`);
    }
  });

  // /revert: undo recorded actions, all of them or back to a chosen state.
  // Reversal executes in Python (recovery implementations + verification
  // live there); the journal is the only bridge between the two sides.
  pi.registerCommand("revert", {
    description: "Reverse pi's file actions (reversible journal)",
    handler: async (_args, ctx) => {
      const cwd = ctx?.cwd ?? process.cwd();
      const paths = resolvePaths(cwd);
      const sessionId = ctx?.sessionManager?.getSessionId() ?? "";
      const pending = pendingLines(paths.journal, sessionId);
      if (pending.length === 0) {
        ctx.ui.notify("[reversible] nothing to revert", "info");
        return;
      }

      // Newest first: undoing the latest action is the common case.
      // Each option carries its target seq alongside the label - no
      // parsing the human-readable string.
      const newestFirst = [...pending].reverse();
      const options: Array<{ label: string; seq?: number }> = [
        { label: `Undo everything (${pending.length} action(s))` },
        ...newestFirst.map((l) => ({
          label: `seq ${l.seq} - ${describeLine(l)}   (this and everything after)`,
          seq: l.seq,
        })),
      ];
      const choice = await ctx.ui.select(
        "Revert to state:",
        options.map((o) => o.label),
      );
      if (choice === undefined) return;

      const chosen = options.find((o) => o.label === choice) ?? options[0];
      const toSeq = chosen.seq;
      const undoCount =
        toSeq === undefined
          ? pending.length
          : pending.length - pending.findIndex((l) => l.seq === toSeq);
      const ok = await ctx.ui.confirm(
        "Revert?",
        toSeq === undefined
          ? `Undo all ${pending.length} recorded action(s)?`
          : `Undo seq ${toSeq} and everything after (${undoCount} action(s))?`,
      );
      if (!ok) return;

      runRevert(
        process.env.REVERSIBLE_CLI ?? "python3 -m reversible.cli",
        paths.journal,
        sessionId,
        toSeq,
        cwd,
        (msg, level) => ctx.ui.notify(msg, level ?? "info"),
      );
    },
  });
}
