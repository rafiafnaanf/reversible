"""Tests for the verification predicate (volatile state, e.g. ASLR).

The pattern: record a post-condition (verify) that runs AFTER recovery and
asserts the observable state matches expectation. For volatile state like
ASLR there's no file to snapshot — the "snapshot" is the recorded old value,
and verification is a read-back check, not a claim.
"""

import pytest

from reversible import Runtime, reversible


def test_verify_predicate_passes_on_restored_state():
    """Simulate ASLR: a volatile kernel parameter, no file to snapshot."""
    aslr = {"value": 2}  # current kernel value

    def set_aslr(value: int) -> None:
        aslr["value"] = value

    def read_aslr() -> int:
        return aslr["value"]

    old_value = read_aslr()  # snapshot BEFORE the change

    @reversible(
        inverse=set_aslr,
        inverse_args=("value",),
        # verify reads back the CURRENT value and compares to the OLD value
        # (captured at record time), not to the action's argument.
        verify=lambda value: read_aslr() == old_value,
    )
    def set_aslr_tool(value: int) -> None:
        set_aslr(value)

    runtime = Runtime()
    runtime.call(set_aslr_tool, 0)  # disable ASLR

    assert read_aslr() == 0  # effect happened

    record = runtime.history()[0]
    assert record.verify is not None

    # Simulate recovery: restore the old value, then verify.
    set_aslr(old_value)
    record.verify_recovery()  # must not raise — state restored


def test_verify_predicate_raises_when_state_not_restored():
    """If recovery silently fails, verification catches it."""
    aslr = {"value": 2}

    def set_aslr(value: int) -> None:
        aslr["value"] = value

    def read_aslr() -> int:
        return aslr["value"]

    old_value = read_aslr()

    @reversible(
        inverse=set_aslr,
        inverse_args=("value",),
        verify=lambda value: read_aslr() == old_value,
    )
    def set_aslr_tool(value: int) -> None:
        set_aslr(value)

    runtime = Runtime()
    runtime.call(set_aslr_tool, 0)

    record = runtime.history()[0]

    # Recovery "runs" but fails silently — value stays at 0, expected 2.
    # (set_aslr never called back; state not restored.)
    with pytest.raises(AssertionError):
        record.verify_recovery()


def test_verify_optional_no_predicate():
    """Tools without a verify predicate are unaffected."""
    calls = []

    def delete_file(path: str) -> None:
        calls.append(("delete", path))

    @reversible(inverse=delete_file, inverse_args=("path",))
    def create_file(path: str, content: str) -> None:
        calls.append(("create", path, content))

    runtime = Runtime()
    runtime.call(create_file, "a.txt", "hi")

    record = runtime.history()[0]
    assert record.verify is None
    record.verify_recovery()  # no-op, must not raise
