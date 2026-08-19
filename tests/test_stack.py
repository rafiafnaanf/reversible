"""Tests for the LIFO ActionStack."""

import pytest

from reversible import ActionRecord, ActionStack, ActionType


def make_record(name: str, action_type: ActionType = ActionType.REVERSIBLE) -> ActionRecord:
    def action() -> None:
        return None

    return ActionRecord(
        id=name,
        action=action,
        args=(),
        kwargs={},
        action_type=action_type,
        recovery=action,
        recovery_args=(),
        recovery_kwargs={},
    )


def test_push_pop_lifo():
    """push A, B, C → pop C, B, A."""
    stack = ActionStack()
    a, b, c = (make_record("A"), make_record("B"), make_record("C"))
    stack.push(a)
    stack.push(b)
    stack.push(c)

    assert stack.pop() is c
    assert stack.pop() is b
    assert stack.pop() is a


def test_peek_does_not_remove():
    stack = ActionStack()
    a = make_record("A")
    stack.push(a)
    assert stack.peek() is a
    assert len(stack) == 1


def test_len_and_iter():
    stack = ActionStack()
    a, b = make_record("A"), make_record("B")
    stack.push(a)
    stack.push(b)
    assert len(stack) == 2
    assert list(stack) == [a, b]  # oldest first


def test_clear():
    stack = ActionStack()
    stack.push(make_record("A"))
    stack.clear()
    assert len(stack) == 0
    assert not stack


def test_empty_stack_raises_on_pop():
    stack = ActionStack()
    with pytest.raises(IndexError):
        stack.pop()
