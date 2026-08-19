"""Tests for @compensable (K) recording."""

from reversible import ActionType, Runtime, compensable


def test_compensable_metadata():
    calls = []

    def cancel_notification(message: str) -> None:
        calls.append(("cancel", message))

    @compensable(compensation=cancel_notification, compensation_args=("message",))
    def send_notification(message: str) -> None:
        calls.append(("send", message))

    runtime = Runtime()
    result = runtime.call(send_notification, "Project created")

    assert result is None
    assert len(runtime) == 1

    record = runtime.history()[0]
    assert record.action_type == ActionType.COMPENSABLE
    assert record.recovery is cancel_notification
    assert record.recovery_args == ("Project created",)

    # Compensation must NOT have run during normal execution.
    assert calls == [("send", "Project created")]
