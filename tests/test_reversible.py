"""Tests for @reversible (R) recording."""

from reversible import ActionType, Runtime, reversible


def test_reversible_metadata():
    calls = []

    def delete_file(path: str) -> None:
        calls.append(("delete", path))

    @reversible(inverse=delete_file, inverse_args=("path",))
    def create_file(path: str, content: str) -> str:
        calls.append(("create", path, content))
        return path

    assert create_file.__name__ == "create_file"  # decorator returns the tool unchanged

    runtime = Runtime()
    result = runtime.call(create_file, "hello.txt", "hello")

    assert result == "hello.txt"
    assert len(runtime) == 1

    record = runtime.history()[0]
    assert record.action_type == ActionType.REVERSIBLE
    assert record.recovery is delete_file
    assert record.recovery_args == ("hello.txt",)
    assert record.args == ("hello.txt", "hello")
    assert record.result == "hello.txt"

    # Recovery must NOT have run during normal execution.
    assert calls == [("create", "hello.txt", "hello")]


def test_reversible_without_arg_selectors_forwards_original_args():
    calls = []

    def delete_file(path: str) -> None:
        calls.append(("delete", path))

    @reversible(inverse=delete_file)
    def create_file(path: str, content: str) -> None:
        calls.append(("create", path, content))

    runtime = Runtime()
    runtime.call(create_file, "a.txt", "x")

    record = runtime.history()[0]
    assert record.recovery_args == ("a.txt", "x")
    assert calls == [("create", "a.txt", "x")]


def test_reversible_inverse_kwargs_selector():
    calls = []

    def delete_file(path: str) -> None:
        calls.append(("delete", path))

    @reversible(inverse=delete_file, inverse_kwargs={"path": "path"})
    def create_file(path: str, content: str) -> None:
        calls.append(("create", path, content))

    runtime = Runtime()
    runtime.call(create_file, "b.txt", "y")

    record = runtime.history()[0]
    assert record.recovery_args == ()
    assert record.recovery_kwargs == {"path": "b.txt"}
