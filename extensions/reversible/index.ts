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
 * append and remove the lock. This serializes the read-max + append critical
 * section against the Python runtime and other writers.
 */
const LOCK_RETRIES = 1000;
const LOCK_DELAY_MS = 1;

function withLock<T>(lockPath: string, fn: () => T): T {
  // Synchronous sleep via Atomics.wait (no setTimeoutSync in Node).
  const sab = new SharedArrayBuffer(4);
  const int32 = new Int32Array(sab);
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
        // lock held by another writer - wait and retry
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
 */
const issuedSeqs = new Map<string, number>();

function assignSeqAtIssue(toolCallId: string, journalPath: string): number {
  // Take the cross-language lock so the shared seq counter is atomic
  // across processes (pi extension + Python runtime + MCP middleware).
  const seq = withLock(journalPath + ".lock", () => nextSeq(journalPath));
  issuedSeqs.set(toolCallId, seq);
  return seq;
}

function takeSeqAtCompletion(toolCallId: string, journalPath: string): number {
  const seq = issuedSeqs.get(toolCallId);
  issuedSeqs.delete(toolCallId);
  return seq ?? nextSeq(journalPath); // fallback: not issued (shouldn't happen)
}

function appendJournal(record: Record<string, unknown>, journalPath: string): void {
  try {
    fs.mkdirSync(path.dirname(journalPath), { recursive: true });
    fs.appendFileSync(journalPath, JSON.stringify(record) + "\n", "utf8");
  } catch (err) {
    console.error(`[reversible] journal append failed: ${err}`);
  }
}

function preimagePath(preimagesDir: string, toolCallId: string): string {
  return path.join(preimagesDir, `${toolCallId}.preimage`);
}

/** Snapshot a file's current bytes if it exists and is under the size cap. */
function capturePreimage(
  filePath: string,
  preimagesDir: string,
  toolCallId: string,
): string | undefined {
  try {
    const st = fs.statSync(filePath);
    if (st.size > MAX_PREIMAGE_BYTES) return undefined;
    fs.mkdirSync(preimagesDir, { recursive: true });
    const dest = preimagePath(preimagesDir, toolCallId);
    fs.copyFileSync(filePath, dest);
    return dest;
  } catch {
    return undefined; // file didn't exist → no preimage needed
  }
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

  // For write/edit, attach the preimage path if one was captured.
  let recoveryArgs = cls.recoveryArgs.map((name) => args[name]);
  if (cls.preimage && typeof args.path === "string") {
    const pre = preimagePath(paths.preimages, event.toolCallId);
    if (fs.existsSync(pre)) recoveryArgs = [args.path, pre];
    else recoveryArgs = ["delete_file", args.path]; // created new file → delete
  }

  appendJournal(
    {
      seq: takeSeqAtCompletion(event.toolCallId, paths.journal),
      agent_id: AGENT_ID,
      namespace: NAMESPACE,
      session_id: sessionId,
      tool: event.toolName,
      args,
      action_type: cls.actionType,
      recovery: cls.recovery,
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
// Extension
// ---------------------------------------------------------------------------

export default function (pi: ExtensionAPI) {
  // Issue: assign seq (program order) + preimage capture, BEFORE execution.
  // tool_call fires in assistant source order, so seq reflects intent.
  pi.on("tool_call", (event: ToolCallEvent, ctx) => {
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
  });

  // Recording: after execution, only if it succeeded.
  pi.on("tool_result", (event: ToolResultEvent, ctx) => {
    if (event.isError) return;
    const sessionId = ctx?.sessionManager?.getSessionId() ?? "";
    const paths = resolvePaths(ctx?.cwd ?? process.cwd());
    recordResult(event, sessionId, paths);
  });
}
