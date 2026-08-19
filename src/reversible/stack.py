"""LIFO action stack."""

from __future__ import annotations

from typing import Iterator

from .action import ActionRecord


class ActionStack:
    """A simple LIFO stack of action records.

    Guarantees::

        push(A); push(B); push(C)
        pop() -> C
        pop() -> B
        pop() -> A
    """

    def __init__(self) -> None:
        self._actions: list[ActionRecord] = []

    def push(self, action: ActionRecord) -> None:
        self._actions.append(action)

    def pop(self) -> ActionRecord:
        return self._actions.pop()

    def peek(self) -> ActionRecord:
        return self._actions[-1]

    def clear(self) -> None:
        self._actions.clear()

    def __len__(self) -> int:
        return len(self._actions)

    def __iter__(self) -> Iterator[ActionRecord]:
        return iter(self._actions)

    def __bool__(self) -> bool:
        return bool(self._actions)
