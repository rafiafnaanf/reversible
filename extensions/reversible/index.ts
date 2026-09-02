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
import * as crypto from "node:crypto";
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
// Shell command splitting (mirror of reversible/shellsplit.py - keep in sync;
// the Python tests are the executable spec for this logic)
// ---------------------------------------------------------------------------

interface ShellPart {
  command: string;
  inline: boolean; // a $() substitution: executes BEFORE its outer part
}

/** Split on && / || / ; outside quotes and parens. */
function splitTopLevel(command: string): string[] {
  const parts: string[] = [];
  const buf: string[] = [];
  let quote: string | null = null;
  let depth = 0;
  let i = 0;
  while (i < command.length) {
    const ch = command[i];
    if (quote !== null) {
      buf.push(ch);
      if (ch === quote) quote = null;
      i++;
      continue;
    }
    if (ch === '"' || ch === "'") {
      quote = ch;
      buf.push(ch);
      i++;
      continue;
    }
    if (ch === "(") {
      depth++;
      buf.push(ch);
      i++;
      continue;
    }
    if (ch === ")") {
      depth = Math.max(0, depth - 1);
      buf.push(ch);
      i++;
      continue;
    }
    if (depth === 0) {
      const two = command.slice(i, i + 2);
      if (two === "&&" || two === "||" || ch === ";") {
        i += two === "&&" || two === "||" ? 2 : 1;
        const part = buf.join("").trim();
        if (part) parts.push(part);
        buf.length = 0;
        continue;
      }
    }
    buf.push(ch);
    i++;
  }
  const tail = buf.join("").trim();
  if (tail) parts.push(tail);
  return parts;
}

/** Top-level $() spans as [start, end, contents], quote-aware. */
function inlineSpans(segment: string): Array<[number, number, string]> {
  const spans: Array<[number, number, string]> = [];
  let i = 0;
  let quote: string | null = null;
  while (i < segment.length) {
    const ch = segment[i];
    if (quote !== null) {
      if (ch === quote) quote = null;
      i++;
      continue;
    }
    if (ch === '"' || ch === "'") {
      quote = ch;
      i++;
      continue;
    }
    if (ch === "$" && i + 1 < segment.length && segment[i + 1] === "(") {
      let depth = 1;
      let j = i + 2;
      let q: string | null = null;
      while (j < segment.length && depth) {
        const c = segment[j];
        if (q !== null) {
          if (c === q) q = null;
        } else if (c === '"' || c === "'") {
          q = c;
        } else if (c === "(") {
          depth++;
        } else if (c === ")") {
          depth--;
        }
        j++;
      }
      if (depth === 0) spans.push([i, j, segment.slice(i + 2, j - 1).trim()]);
      i = j;
      continue;
    }
    i++;
  }
  return spans;
}

/**
 * Split a compound shell command into ordered parts (execution order):
 * inline $() substitutions first (the shell evaluates them before the
 * surrounding command), then enclosing parts in appearance order.
 * Pipelines stay whole; an inline-only part emits just the inline.
 */
export function splitShell(command: string): ShellPart[] {
  const parts: ShellPart[] = [];
  for (const segment of splitTopLevel(command)) {
    const spans = inlineSpans(segment);
    for (const [, , content] of spans) {
      parts.push({ command: content, inline: true });
    }
    let remainder = "";
    let prev = 0;
    for (const [start, end] of spans) {
      remainder += segment.slice(prev, start);
      prev = end;
    }
    remainder += segment.slice(prev);
    if (remainder.trim()) parts.push({ command: segment, inline: false });
  }
  return parts;
}

// ---------------------------------------------------------------------------
// Script handler fingerprints: every script execution is checked against
// registered handlers; a match records R with that recovery, a miss logs
// K/noop as usual. Config: <journal-dir>/script-handlers.json
//   {"handlers": [{"glob": "deploy/*.sh", "recovery": "rollback_deploy"},
//                 {"hash": "sha256:...", "recovery": "rollback_migrate"}]}
// Handler signature: recovery(script_path) - resolves at rollback via the
// Python registry; unresolvable names fail closed (RollbackError).
// ---------------------------------------------------------------------------

interface ScriptHandler {
  glob?: string;
  hash?: string; // sha256 hex, optional "sha256:" prefix
  recovery: string;
}

function loadScriptHandlers(journalPath: string): ScriptHandler[] {
  const cfg = path.join(path.dirname(journalPath), "script-handlers.json");
  try {
    const parsed = JSON.parse(fs.readFileSync(cfg, "utf8"));
    return Array.isArray(parsed.handlers) ? parsed.handlers : [];
  } catch {
    return []; // no config: every script logs as K/noop
  }
}

function globToRegex(glob: string): RegExp {
  const esc = glob
    .replace(/[.+^${}()|[\]\\]/g, "\\$&")
    .replace(/\*\*/g, "\u0000")
    .replace(/\*/g, "[^/]*")
    .replace(/\u0000/g, ".*")
    .replace(/\?/g, ".");
  return new RegExp(`^${esc}$`);
}

function sha256File(absPath: string): string | undefined {
  try {
    return crypto.createHash("sha256").update(fs.readFileSync(absPath)).digest("hex");
  } catch {
    return undefined; // not a file / unreadable
  }
}

/** First handler whose glob or content hash matches a token in the part. */
export function matchScriptHandler(
  part: string,
  cwd: string,
  handlers: ScriptHandler[],
): { recovery: string; args: unknown[] } | undefined {
  for (const token of part.split(/\s+/)) {
    const p = token.replace(/^["']+|["']+$/g, "");
    if (!p || p.startsWith("-")) continue;
    for (const h of handlers) {
      if (h.glob && globToRegex(h.glob).test(p)) {
        return { recovery: h.recovery, args: [p] };
      }
      if (h.hash) {
        const abs = path.isAbsolute(p) ? p : path.join(cwd, p);
        const digest = sha256File(abs);
        if (digest && digest === h.hash.replace(/^sha256:/, "")) {
          return { recovery: h.recovery, args: [p] };
        }
      }
    }
  }
  return undefined;
}

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
 * seqs assigned at ISSUE time (program order), keyed by toolCallId.
 *
 * This is the Reorder-Buffer rule: assign the sequence numbers when the
 * tool call is issued (tool_call, in assistant source order), NOT when it
 * completes (tool_result, arbitrary for parallel execution). Rollback then
 * retires in descending seq - deterministic regardless of completion order.
 *
 * A compound bash command reserves ONE seq PER SPLIT PART (see splitShell):
 * the parts are logged as independent records in execution order, so
 * rollback undoes them in reverse appearance order. Reservations are
 * DURABLE: ``{"type": "reserved"}`` lines are appended under one lock hold
 * at issue time, so no other writer (or sibling tool call) can be assigned
 * these seqs while the full records are still in flight.
 */
const issuedSeqs = new Map<string, number[]>();
/** Split parts for the in-flight bash call, in appearance order. */
interface BashPart {
  command: string;
  inline: boolean;
  handler?: { recovery: string; args: unknown[] }; // fingerprint match
}
const bashParts = new Map<string, BashPart[]>();

function issueSeqs(toolCallId: string, journalPath: string, count: number): number[] {
  const seqs: number[] = [];
  withLock(journalPath + ".lock", () => {
    for (let k = 0; k < count; k++) {
      const seq = nextSeq(journalPath);
      _appendLine(
        { type: "reserved", seq, toolCallId, ts: new Date().toISOString() },
        journalPath,
      );
      seqs.push(seq);
    }
  });
  return seqs;
}

function takeSeqsAtCompletion(toolCallId: string, journalPath: string): number[] {
  const seqs = issuedSeqs.get(toolCallId);
  issuedSeqs.delete(toolCallId);
  return seqs ?? [nextSeq(journalPath)]; // fallback: not issued (shouldn't happen)
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
  const summary = summarizeContent(event.content);
  const seqs = takeSeqsAtCompletion(event.toolCallId, paths.journal);

  // Compound bash command: one record per split part, in execution order
  // (the seqs were reserved per-part at issue). Rollback then undoes the
  // parts in reverse appearance order. Bash is record-only (K/noop):
  // shell effects are not statically classifiable.
  if (event.toolName === "bash" && typeof args.command === "string") {
    const parts = bashParts.get(event.toolCallId) ?? [
      { command: args.command, inline: false },
    ];
    bashParts.delete(event.toolCallId);
    parts.forEach((part, i) => {
      appendJournal(
        {
          seq: seqs[i] ?? seqs[0],
          agent_id: AGENT_ID,
          namespace: NAMESPACE,
          session_id: sessionId,
          tool: "bash",
          args: { command: part.command },
          action_type: part.handler ? "R" : "K",
          recovery: part.handler ? part.handler.recovery : "noop",
          recovery_args: part.handler ? part.handler.args : [],
          recovery_kwargs: {},
          is_error: event.isError,
          result_summary: i === 0 ? summary : "",
          ts: new Date().toISOString(),
        },
        paths.journal,
      );
    });
    return;
  }

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
      seq: seqs[0],
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
      result_summary: summary,
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
  // Issue: assign seqs (program order) + preimage capture, BEFORE execution.
  // tool_call fires in assistant source order, so seq reflects intent.
  // Recording is auxiliary: a failure here must never break tool dispatch.
  pi.on("tool_call", (event: ToolCallEvent, ctx) => {
    try {
      const paths = resolvePaths(ctx?.cwd ?? process.cwd());
      if (TOOL_CLASSES[event.toolName]) {
        let partCount = 1;
        if (event.toolName === "bash") {
          const cmd = (event.input as { command?: string }).command ?? "";
          const cwd = ctx?.cwd ?? process.cwd();
          const handlers = loadScriptHandlers(paths.journal);
          const parts = splitShell(cmd);
          const safe = parts.length > 0 ? parts : [{ command: cmd, inline: false }];
          // Check every script execution against the handler fingerprints:
          // a match records R with that handler's recovery, a miss logs K/noop.
          bashParts.set(
            event.toolCallId,
            safe.map((p) => ({ ...p, handler: matchScriptHandler(p.command, cwd, handlers) })),
          );
          partCount = safe.length;
        }
        issuedSeqs.set(
          event.toolCallId,
          issueSeqs(event.toolCallId, paths.journal, partCount),
        );
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
      bashParts.delete(event.toolCallId);
      preimageStatus.delete(event.toolCallId);
      return;
    }
    try {
      const sessionId = ctx?.sessionManager?.getSessionId() ?? "";
      const paths = resolvePaths(ctx?.cwd ?? process.cwd());
      recordResult(event, sessionId, paths);
    } catch (err) {
      issuedSeqs.delete(event.toolCallId);
      bashParts.delete(event.toolCallId);
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
