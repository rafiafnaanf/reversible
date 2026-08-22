"""Tests for the @execute decorator (declared execution policy)."""

from reversible import Runtime, execute, registry


def test_execute_skip_not_recorded():
    calls = []

    @execute(policy="skip")
    def run_trusted(path: str) -> None:
        calls.append(path)

    runtime = Runtime()
    runtime.call(run_trusted, "/opt/trusted/analyzer")

    assert len(runtime) == 0  # skip → not recorded
    assert calls == ["/opt/trusted/analyzer"]


def test_execute_record_is_record_only_k():
    calls = []

    @execute(policy="record")
    def run_binary(path: str) -> None:
        calls.append(path)

    runtime = Runtime()
    runtime.call(run_binary, "/tmp/mystery.bin")

    assert len(runtime) == 1
    record = runtime.history()[0]
    assert record.action_type.value == "K"  # record-only
    assert record.recovery is not None  # noop


def test_execute_default_policy_system_path_skips():
    """A tool whose source references a system path defaults to skip."""

    @execute()
    def run_system_tool() -> None:
        return "/bin/ls"  # trusted path in source → default skip

    assert registry.execute_policy(run_system_tool) == "skip"


def test_execute_default_policy_unknown_path_sandboxes():
    @execute()
    def run_unknown_tool() -> None:
        return "/tmp/random.bin"  # not a system path → default sandbox

    assert registry.execute_policy(run_unknown_tool) == "sandbox"


def test_execute_explicit_policy_wins_over_default():
    @execute(policy="record")
    def run_system_tool() -> None:
        return "/bin/ls"  # would default to skip, but explicit record wins

    assert registry.execute_policy(run_system_tool) == "record"
