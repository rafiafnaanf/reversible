<div align="center">

# Reversible Agent Runtime

**Record effectful agent tool calls, and take them back.**

A small, framework-independent Python library that sits between an AI agent
and its tools. It records every effectful call (write, delete, create, send,
...) onto an action stack together with its **recovery operation**, then
restores prior state through reverse execution, scoped per agent/session,
verified after each recovery, or rolled back to a checkpoint.

</div>

---

## Why

Agents act on the world. When an agent deletes a file, sends an email, or
creates a resource, that effect is real, and often irreversible by accident.
This library operationalizes the *Revisable by Design* assumption: **actions
have known recovery operations**, and a generic execution layer can record
them so prior state can be restored.

It does **not** try to automatically classify actions as reversible,
compensable, or irreversible. That is future work.

## How it works

```text
Agent
  │  tool call
  ▼
Runtime
  ├── execute tool
  └── record action + recovery   →  Action Stack
                                    ↓ (optional)
                               Durable JSONL journal
```

* **Intercept**: the runtime sits between the agent and its tools.
* **Execute**: the tool runs normally; the result is returned unchanged.
* **Record**: the action, its arguments, and its recovery operation (with
  its own arguments) are pushed onto a LIFO stack.
* **Never auto-undo**: recovery runs only when rollback is explicitly
  requested.

## Features

* **Zero dependencies**: pure Python standard library.
* **Two action types**: `R` (reversible: exact inverse) and `K`
  (compensable: mitigation, not exact restore).
* **Recovery metadata**: `@reversible(inverse=...)` and
  `@compensable(compensation=...)` decorators.
* **Recovery args preserved**: the record keeps the arguments needed for
  the recovery call, not just the function.
* **Verification**: an optional `verify` post-condition on any tool that
  runs after recovery and asserts the observable state was actually
  restored (read back the value, don't trust the claim).
* **Concurrency-safe journal**: cross-language lock around seq assignment
  plus append, so concurrent writers never collide on sequence numbers.
* **Deterministic ordering**: `seq` is assigned at *issue* time (program
  order), so parallel execution can't scramble rollback order.
* **Rollback**: LIFO undo/compensation, scoped per agent/session, verified
  after each recovery.
* **Checkpoints**: roll back to a specific point, leaving earlier actions
  intact.
* **Durable journal**: append-only JSONL, the cross-language contract
  between any harness and the engine.
* **Multi-agent identity**: every record carries `agent_id`, `session_id`,
  and a global `seq`, so one shared journal can be filtered and rolled back
  per agent/session.
* **Global pi hook**: a TypeScript extension that records effectful pi
  tool calls into the journal automatically.
* **CLI**: `reversible history` / `reversible rollback`.

## Install

```bash
uv sync
```

Requires Python >= 3.12 and [uv](https://docs.astral.sh/uv/).

## Quick start

```python
from reversible import Runtime, compensable, reversible


def delete_file(path: str) -> None:
    ...


@reversible(inverse=delete_file, inverse_args=("path",))
def create_file(path: str, content: str) -> None:
    ...


@compensable(compensation=cancel_notification, compensation_args=("message",))
def send_notification(message: str) -> None:
    ...


runtime = Runtime()

runtime.call(create_file, "hello.txt", "Hello world")
runtime.call(send_notification, "Project created")

for record in runtime.history():
    print(record)          # e.g. "001 R create_file"
```

### Recording rule

A tool call is logged **only if** the tool is decorated with
`@reversible` / `@compensable`. Undecorated tools (thinking, reading,
pure computation) execute normally but are **not** recorded.

### Recovery argument selectors

By default the recovery callable receives the original args/kwargs
unchanged. Use `inverse_args` / `inverse_kwargs` (or the `compensation_*`
equivalents) to forward only the named parameters:

```python
@reversible(inverse=delete_file, inverse_args=("path",))
def create_file(path: str, content: str) -> None:
    ...   # recovery becomes delete_file(path)
```

### Verification

Every tool can declare an optional `verify` predicate, a post-condition
that runs *after* recovery and asserts the observable state was actually
restored. This is how restoration is *proven*, not assumed:

```python
@reversible(
    inverse=set_aslr,
    inverse_args=("value",),
    verify=lambda value: read_aslr() == old_value,   # read back, don't trust
)
def set_aslr_tool(value: int) -> None: ...
```

The predicate receives the recovery's arguments and returns truthy when the
state is restored. If it returns falsy, the runtime raises `AssertionError`
and catches silent recovery failure.

## Durable journal

Pass a `JournalSink` to write each recorded action through to an
append-only JSONL journal, the cross-language contract between any
harness and the Python engine.

```python
from reversible import JournalSink, Runtime

runtime = Runtime(
    agent_id="pi",
    session_id="sess-abc",
    sink=JournalSink("~/.reversible/journal.jsonl"),
)
runtime.call(create_file, "hello.txt", "Hello world")
```

Every record carries identity fields for multi-agent scoping:

```json
{"seq": 1, "agent_id": "pi", "session_id": "sess-abc",
 "tool": "create_file", "args": {"path": "hello.txt", "content": "Hello world"},
 "action_type": "R", "recovery": "delete_file",
 "recovery_args": ["hello.txt"], "recovery_kwargs": {}, "is_error": false}
```

* `agent_id` / `session_id`: filter and roll back one agent's stack.
* `seq`: global monotonic sequence for cross-agent LIFO order.

### Inspect the journal

```bash
uv run reversible history                       # all records
uv run reversible history --agent pi            # one agent
uv run reversible history --agent pi --session sess-abc   # one session
uv run reversible history --json                # raw JSON lines
```

## Rollback

`Runtime.rollback()` undoes recorded actions in LIFO order, optionally
scoped by `agent_id` / `session_id`, and verifies each recovery:

```python
result = runtime.rollback()
# result.ok - True if all recoveries succeeded and verified
```

Recovery names in the journal resolve to callables via a name-keyed
registry. Built-in recoveries (`delete_file`, `delete_directory`,
`truncate_file`, `restore_file`, `noop`) are registered automatically.

### Checkpoints

`Runtime.checkpoint()` returns a marker for the current stack position;
`Runtime.rollback_to(checkpoint)` undoes only the work after that point,
leaving earlier actions intact:

```python
checkpoint = runtime.checkpoint()
# ... more work ...
result = runtime.rollback_to(checkpoint)  # undo only post-checkpoint work
```

```text
A, B, CHECKPOINT, C, D

rollback_to(checkpoint) -> D^-1, C^-1   (A, B remain)
```

### `@execute` - declared execution policy

Executing an arbitrary binary is the X (unknown) case. Its effects can't
be introspected, so execution is *declared*, not auto-detected:

```python
@execute(policy="skip")      # trusted executable -> not logged
@execute(policy="record")    # audit-only K, manual reversal
@execute(policy="sandbox")   # untrusted -> run in docker, nuke on reversal
```

Default policy is path-based: `/bin` / `/usr/bin` -> `skip`, else
`sandbox`. An explicit `policy` wins.

### CLI rollback

```bash
uv run reversible rollback [--agent X] [--session Y] [--journal PATH]
uv run reversible rollback --to <checkpoint>        # undo only post-checkpoint
```

## Hooking agents

The library is designed to be hooked at any agent's tool-dispatch boundary.
Each harness gets a thin adapter; every adapter writes the **same** journal
format, and the Python engine is the single authority that reads it.

| Harness | Hook |
| ------- | ---- |
| **pi** (TypeScript) | global extension: `extensions/pi/reversible/index.ts` |
| **MCP servers** | `ServerMiddleware` (planned) |
| **LangChain** | `wrap_tool_call` middleware (planned) |
| **Pure Python** | `Runtime.call()` directly |

**Design principle for hooks:** assign `seq` at *issue* time, not
*completion* time. A harness fires a tool call in program order (issue) but
may complete it out of order (parallel execution). The hook must tag each
record with a `seq` captured when the call is *issued* (e.g. pi's
`tool_call`), so rollback retires in deterministic descending `seq`
regardless of completion order. This is the reorder-buffer rule.

### Global pi hook

A TypeScript extension records effectful pi tool calls (`write`, `edit`,
`bash`) into `~/.reversible/journal.jsonl`, tagged with the session id.
Read-only tools (`read`, `grep`, `find`, `ls`) are never recorded.

Install globally (hooks every pi session):

```bash
mkdir -p ~/.pi/agent/extensions/reversible
cp extensions/pi/reversible/index.ts ~/.pi/agent/extensions/reversible/
```

## Demos

```bash
uv run python examples/example_tool_module.py   # HOW TO: write your own tools
uv run python examples/basic.py                  # command -> stack
uv run python examples/multi_agent.py            # 3 agents, 1 journal
uv run python examples/reversal_basic.py         # record -> rollback -> verify
uv run python examples/recovery_simple.py        # empty file/dir, append-write
uv run python examples/checkpoint_demo.py        # roll back to a checkpoint
uv run python examples/sandbox_docker.py        # sandboxed exec, coarse reversal
```

### Write your own effectful tools

`examples/example_tool_module.py` is a ready-to-copy template, the analog
of pi's example extensions. It shows the full pattern:

1. Write the tool function (the forward effect).
2. Write the recovery function (inverse for R, compensation for K).
3. Decorate the tool with `@reversible(inverse=..., inverse_args=...)` or
   `@compensable(compensation=..., compensation_args=...)`.
4. Execute via `Runtime.call()` (recorded). Read-only tools that aren't
   decorated are automatically skipped.

## Tests

```bash
uv run pytest
```

## Project layout

```text
src/reversible/
├── action.py            # ActionType (R/K), ActionRecord (+ identity, verify, to_journal)
├── decorators.py        # @reversible, @compensable (+ verify), @execute
├── registry.py          # recovery metadata registry (+ by_name, execute policies)
├── stack.py             # LIFO ActionStack
├── runtime.py           # Runtime.call / history / rollback / checkpoint
├── journal.py           # JournalRecord, JournalSink, JSONL reader, cross-language lock
├── rollback.py          # RollbackEngine, RollbackResult (LIFO, scoped, verified)
├── recovery_builtin.py  # delete_file, delete_directory, truncate_file, restore_file, noop
├── cli.py               # reversible history / rollback
├── logging.py           # stdlib logging
└── exceptions.py

extensions/pi/reversible/index.ts   # global pi hook (TypeScript)
```

## License

[MIT](LICENSE)
