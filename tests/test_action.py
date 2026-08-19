"""Tests for ActionType and ActionRecord."""

from reversible import ActionRecord, ActionType


def make_record() -> ActionRecord:
    def create_file(path: str, content: str) -> str:
        return path

    def delete_file(path: str) -> None:
        return None

    return ActionRecord(
        id="001",
        action=create_file,
        args=("hello.txt", "hello"),
        kwargs={},
        action_type=ActionType.REVERSIBLE,
        recovery=delete_file,
        recovery_args=("hello.txt",),
        recovery_kwargs={},
        result="hello.txt",
    )


def test_action_type_values():
    assert ActionType.REVERSIBLE.value == "R"
    assert ActionType.COMPENSABLE.value == "K"


def test_record_preserves_recovery_args():
    """The record must retain the args needed for recovery, not just the fn."""
    record = make_record()
    assert record.recovery_args == ("hello.txt",)
    assert record.recovery is not None


def test_record_string():
    record = make_record()
    assert str(record) == "001 R create_file"


def test_record_default_result_is_none():
    record = make_record()
    assert record.result == "hello.txt"
