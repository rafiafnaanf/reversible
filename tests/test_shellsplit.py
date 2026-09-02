"""Tests for compound shell command splitting."""

from reversible.shellsplit import ShellPart, split_shell


def test_split_plain_chain():
    parts = split_shell("mkdir -p x && cd x && touch main.py")
    assert [p.command for p in parts] == ["mkdir -p x", "cd x", "touch main.py"]
    assert all(not p.inline for p in parts)


def test_split_or_and_semicolon():
    parts = split_shell("a || b ; c")
    assert [p.command for p in parts] == ["a", "b", "c"]


def test_quotes_do_not_split():
    parts = split_shell('echo "a && b" && pwd')
    assert [p.command for p in parts] == ['echo "a && b"', "pwd"]


def test_inline_hoisted_first():
    """$() executes before its enclosing command: hoisted ahead of it."""
    parts = split_shell("echo $(whoami) && pwd")
    assert [p.command for p in parts] == ["whoami", "echo $(whoami)", "pwd"]
    assert [p.inline for p in parts] == [True, False, False]


def test_multiple_inlines_in_order():
    parts = split_shell("echo $(a) $(b)")
    assert [p.command for p in parts] == ["a", "b", "echo $(a) $(b)"]
    assert [p.inline for p in parts] == [True, True, False]


def test_standalone_inline_skips_outer():
    parts = split_shell("$(rm x) && pwd")
    assert [p.command for p in parts] == ["rm x", "pwd"]


def test_pipeline_stays_whole():
    parts = split_shell("cat f | grep x | wc -l && pwd")
    assert [p.command for p in parts] == ["cat f | grep x | wc -l", "pwd"]


def test_inline_contents_not_split():
    """&& inside $() belongs to the inline, not the outer split."""
    parts = split_shell("echo $(test 1 && 2) && pwd")
    assert [p.command for p in parts] == [
        "test 1 && 2",
        "echo $(test 1 && 2)",
        "pwd",
    ]


def test_inline_with_quotes():
    parts = split_shell('echo $(echo ")") && pwd')
    assert parts[0].command == 'echo ")"'
    assert parts[0].inline
    assert parts[2].command == "pwd"


def test_empty_and_whitespace():
    assert split_shell("") == []
    assert split_shell("   ") == []
    assert split_shell("a && && b") == [ShellPart("a"), ShellPart("b")]


def test_single_command_no_split():
    assert [p.command for p in split_shell("ls -la")] == ["ls -la"]
