"""Tests for the Runtime: recording, history, read-only skip."""

from reversible import Runtime, compensable, reversible


def test_undecorated_tool_is_not_recorded():
    """Thinking/reading tools execute but are not logged to the stack."""
    calls = []

    def read_file(path: str) -> str:
        calls.append(path)
        return "contents"

    runtime = Runtime()
    result = runtime.call(read_file, "notes.txt")

    assert result == "contents"
    assert len(runtime) == 0
    assert runtime.history() == []
    assert calls == ["notes.txt"]


def test_mixed_registered_and_unregistered():
    calls = []

    def fake_remove(path: str) -> None:
        calls.append(("delete", path))

    @reversible(inverse=fake_remove, inverse_args=("path",))
    def create_file(path: str, content: str) -> None:
        calls.append(("create", path, content))

    def read_file(path: str) -> str:
        return "data"

    runtime = Runtime()
    runtime.call(read_file, "a.txt")          # not recorded
    runtime.call(create_file, "b.txt", "x")  # recorded
    runtime.call(read_file, "c.txt")          # not recorded

    assert len(runtime) == 1
    record = runtime.history()[0]
    assert record.action.__name__ == "create_file"


def test_history_order_is_oldest_first():
    calls = []

    def fake_remove(path: str) -> None:
        calls.append(("delete", path))

    @reversible(inverse=fake_remove, inverse_args=("path",))
    def create_file(path: str, content: str) -> None:
        calls.append(("create", path, content))

    runtime = Runtime()
    runtime.call(create_file, "a.txt", "1")
    runtime.call(create_file, "b.txt", "2")
    runtime.call(create_file, "c.txt", "3")

    names = [r.action.__name__ for r in runtime.history()]
    ids = [r.id for r in runtime.history()]
    assert names == ["create_file", "create_file", "create_file"]
    assert ids == ["001", "002", "003"]


def test_call_returns_original_result():
    def delete_file(path: str) -> None:
        return None

    @reversible(inverse=delete_file, inverse_args=("path",))
    def create_file(path: str, content: str) -> str:
        return f"wrote {path}"

    runtime = Runtime()
    assert runtime.call(create_file, "x.txt", "hi") == "wrote x.txt"


def test_compensable_recorded_too():
    calls = []

    def cancel(message: str) -> None:
        calls.append(("cancel", message))

    @compensable(compensation=cancel, compensation_args=("message",))
    def send(message: str) -> None:
        calls.append(("send", message))

    runtime = Runtime()
    runtime.call(send, "hi")
    assert len(runtime) == 1
    assert runtime.history()[0].action_type.value == "K"
