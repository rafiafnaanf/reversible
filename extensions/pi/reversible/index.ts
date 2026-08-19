/**
 * Reversible Agent Runtime — global pi hook (Stage 2).
 *
 * Records effectful pi tool calls (write / edit / bash) into the shared
 * durable journal as JSONL, tagged with the session id. Read-only tools
 * (read / grep / find / ls) are never recorded.
 *
 * Recording happens on `tool_result` (post-execution, `isError === false`),
 * so only effects that actually happened are logged. `tool_call` is used
 * only for preimage capture: the inverse of "overwrite a file" is "restore
 * the old bytes", which must be snapshotted before execution.
 *
 * The extension never invokes Python — it appends JSON lines directly to
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
const JOURNAL_DIR = path.join(os.homedir(), ".reversible");
const JOURNAL_PATH = path.join(JOURNAL_DIR, "journal.jsonl");
const PREIMAGE_DIR = path.join(JOURNAL_DIR, "preimages");
const MAX_PREIMAGE_BYTES = 5 * 1024 * 1024; // skip snapshotting huge files

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

function nextSeq(): number {
  // Best-eff: read tail of journal for the max seq, then increment.
  try {
    if (fs.existsSync(JOURNAL_PATH)) {
      const lines = fs.readFileSync(JOURNAL_PATH, "utf8").trim().split("\n").filter(Boolean);
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

function appendJournal(record: Record<string, unknown>): void {
  try {
    fs.mkdirSync(JOURNAL_DIR, { recursive: true });
    fs.appendFileSync(JOURNAL_PATH, JSON.stringify(record) + "\n", "utf8");
  } catch (err) {
    console.error(`[reversible] journal append failed: ${err}`);
  }
}

function preimagePath(toolCallId: string): string {
  return path.join(PREIMAGE_DIR, `${toolCallId}.preimage`);
}

/** Snapshot a file's current bytes if it exists and is under the size cap. */
function capturePreimage(filePath: string, toolCallId: string): string | undefined {
  try {
    const st = fs.statSync(filePath);
    if (st.size > MAX_PREIMAGE_BYTES) return undefined;
    fs.mkdirSync(PREIMAGE_DIR, { recursive: true });
    const dest = preimagePath(toolCallId);
    fs.copyFileSync(filePath, dest);
    return dest;
  } catch {
    return undefined; // file didn't exist → no preimage needed
  }
}

// ---------------------------------------------------------------------------
// Recording
// ---------------------------------------------------------------------------

function recordResult(event: ToolResultEvent, sessionId: string): void {
  const cls = TOOL_CLASSES[event.toolName];
  if (!cls) return; // read-only / unknown → not recorded

  const args = { ...event.input } as Record<string, unknown>;

  // For write/edit, attach the preimage path if one was captured.
  let recoveryArgs = cls.recoveryArgs.map((name) => args[name]);
  if (cls.preimage && typeof args.path === "string") {
    const pre = preimagePath(event.toolCallId);
    if (fs.existsSync(pre)) recoveryArgs = [args.path, pre];
    else recoveryArgs = ["delete_file", args.path]; // created new file → delete
  }

  appendJournal({
    seq: nextSeq(),
    agent_id: AGENT_ID,
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
  });
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
  // Preimage capture: snapshot files BEFORE write/edit overwrites them.
  pi.on("tool_call", (event: ToolCallEvent) => {
    if (event.toolName === "write" || event.toolName === "edit") {
      const input = event.input as { path?: string };
      if (typeof input.path === "string") {
        capturePreimage(input.path, event.toolCallId);
      }
    }
  });

  // Recording: after execution, only if it succeeded.
  pi.on("tool_result", (event: ToolResultEvent, ctx) => {
    if (event.isError) return;
    const sessionId = ctx?.sessionManager?.getSessionId() ?? "";
    recordResult(event, sessionId);
  });
}
