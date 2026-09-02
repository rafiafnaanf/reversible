"""Split compound shell commands into ordered, independently logged parts.

One bash call like ``mkdir -p x && cd x && touch main.py`` is a sequence
of effects, but arrives as one opaque string. Splitting it at the top
level turns it into independent records ordered as they appear:

    >>> split_shell("mkdir -p x && cd x && touch main.py")
    [mkdir -p x, cd x, touch main.py]

``$()`` substitutions execute before their enclosing command (the shell
evaluates them first), so they are hoisted ahead of it in the result:
issue order = execution order. E.g. ``echo $(whoami)`` becomes
``[whoami (inline), echo $(whoami)]``.

Scope: lexical, not a full shell parser. Top-level ``&&`` / ``||`` / ``;``
only; quotes are respected; ``$()`` handled one scan deep (nesting and
escapes are out of scope); pipelines (``|``) stay inside their part (pipes
are a documented non-goal, see the plan docs).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ShellPart:
    """One independently recordable unit of a compound command."""

    command: str
    inline: bool = False  # True: a $() substitution that runs before its outer part


def split_shell(command: str) -> list[ShellPart]:
    """Split a compound shell command into ordered parts (execution order).

    Inline ``$()`` substitutions come first (the shell evaluates them
    before the surrounding command), then enclosing parts in appearance
    order. An inline-only part (``$(rm x)``) emits just the inline.
    """
    parts: list[ShellPart] = []
    for segment in _split_top_level(command):
        spans = _inline_spans(segment)
        for _, _, content in spans:
            parts.append(ShellPart(content, inline=True))
        remainder = _without_spans(segment, spans)
        if remainder:
            parts.append(ShellPart(segment))
    return parts


def _split_top_level(command: str) -> list[str]:
    """Split on ``&&`` / ``||`` / ``;`` outside quotes and parens."""
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    depth = 0
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if quote is not None:
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "(":
            depth += 1
            buf.append(ch)
            i += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
            i += 1
            continue
        if depth == 0:
            two = command[i : i + 2]
            if two in ("&&", "||") or ch == ";":
                i += 2 if two in ("&&", "||") else 1
                part = "".join(buf).strip()
                if part:
                    parts.append(part)
                buf = []
                continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def _inline_spans(segment: str) -> list[tuple[int, int, str]]:
    """Top-level ``$()`` spans as (start, end, contents), quote-aware."""
    spans: list[tuple[int, int, str]] = []
    i, n = 0, len(segment)
    quote: str | None = None
    while i < n:
        ch = segment[i]
        if quote is not None:
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch == "$" and i + 1 < n and segment[i + 1] == "(":
            depth, j, q = 1, i + 2, None
            while j < n and depth:
                c = segment[j]
                if q is not None:
                    if c == q:
                        q = None
                elif c in ("'", '"'):
                    q = c
                elif c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                j += 1
            if depth == 0:
                spans.append((i, j, segment[i + 2 : j - 1].strip()))
            i = j
            continue
        i += 1
    return spans


def _without_spans(segment: str, spans: list[tuple[int, int, str]]) -> str:
    """The segment text minus the ``$()`` spans, stripped."""
    out, prev = [], 0
    for start, end, _ in spans:
        out.append(segment[prev:start])
        prev = end
    out.append(segment[prev:])
    return "".join(out).strip()