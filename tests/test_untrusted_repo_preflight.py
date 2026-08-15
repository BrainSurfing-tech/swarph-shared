"""Tests for untrusted_repo_preflight (SWAIRM Pattern #3 port).

Pure poison-detection is exhaustively tested against the swairm _POISONED_GIT_RE
key set; preflight uses an injected run_git seam (no real subprocess).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from swarph_shared import untrusted_repo_preflight as urp


# ---------------------------------------------------------------- git_config_is_poisoned

def test_clean_config_is_not_poisoned():
    cfg = "user.name=Pierre\nuser.email=p@x.com\ncore.editor=vim\ndiff.tool=vimdiff\n"
    assert urp.git_config_is_poisoned(cfg) is None


@pytest.mark.parametrize("line, key", [
    ("diff.external=/tmp/evil.sh", "diff.external"),
    ("diff.mytool.textconv=/tmp/evil.sh", "diff.mytool.textconv"),
    ("diff.mytool.command=/tmp/evil.sh", "diff.mytool.command"),
    ("core.fsmonitor=/tmp/evil.sh", "core.fsmonitor"),
    ("core.hookspath=/tmp/hooks", "core.hookspath"),
    ("filter.lfs.clean=/tmp/evil.sh", "filter.lfs.clean"),
    ("filter.lfs.smudge=/tmp/evil.sh", "filter.lfs.smudge"),
    ("alias.diff=!/tmp/evil.sh", "alias.diff"),
    ("alias.log=!/tmp/evil.sh", "alias.log"),
    ("alias.show=!/tmp/evil.sh", "alias.show"),
    ("alias.status=!/tmp/evil.sh", "alias.status"),
])
def test_each_poison_key_is_detected_and_returned(line, key):
    cfg = f"user.name=Pierre\n{line}\ncore.editor=vim\n"
    assert urp.git_config_is_poisoned(cfg) == key


def test_detection_is_case_insensitive():
    assert urp.git_config_is_poisoned("DIFF.EXTERNAL=/evil") == "DIFF.EXTERNAL"


@pytest.mark.parametrize("benign", [
    "diff.tool=vimdiff",           # diff.tool is NOT execution
    "core.editor=vim",
    "alias.co=checkout",           # not diff/log/show/status
    "alias.ci=commit",
    "filter.lfs.required=true",    # filter.*.required is not clean/smudge
    "diff.color=auto",
    "core.autocrlf=input",
])
def test_benign_lookalike_keys_are_not_poison(benign):
    cfg = f"user.name=P\n{benign}\nuser.email=x@y.z\n"
    assert urp.git_config_is_poisoned(cfg) is None


def test_poison_must_be_line_anchored_not_mid_value():
    # a benign key whose VALUE contains a poison-looking substring must not match
    cfg = "user.email=diff.external=spoof@x.com\nremote.origin.url=https://x/diff.external=y\n"
    assert urp.git_config_is_poisoned(cfg) is None


def test_poison_found_among_many_benign_lines():
    cfg = (
        "user.name=Pierre\nuser.email=p@x.com\ncore.editor=vim\n"
        "core.hookspath=/tmp/steal\n"
        "remote.origin.url=https://github.com/x/y.git\n"
    )
    assert urp.git_config_is_poisoned(cfg) == "core.hookspath"


# ---------------------------------------------------------------- preflight (injected seam)

def _run_git(config_stdout="user.name=P\n", config_rc=0, diff_stdout=" f | 1 +\n", diff_rc=0):
    """Build a fake run_git(workdir, *args) driven by canned outputs."""
    def fake(workdir, *args):
        if args[:2] == ("config", "--list"):
            return SimpleNamespace(returncode=config_rc, stdout=config_stdout)
        if args and args[0] == "diff":
            return SimpleNamespace(returncode=diff_rc, stdout=diff_stdout)
        raise AssertionError(f"unexpected git args: {args}")
    return fake


def test_preflight_passes_clean_config_and_nonempty_diff():
    # must not raise
    urp.preflight("/work", _run_git(), base="main")


def test_preflight_raises_on_poisoned_config():
    poisoned = _run_git(config_stdout="user.name=P\ncore.hookspath=/tmp/steal\n")
    with pytest.raises(urp.PreflightError) as ei:
        urp.preflight("/work", poisoned, base="main")
    assert "core.hookspath" in str(ei.value)


def test_preflight_raises_when_probe_fails():
    with pytest.raises(urp.PreflightError):
        urp.preflight("/work", _run_git(config_rc=128), base="main")


def test_preflight_raises_on_empty_diff_vs_base():
    # I3: a rebuilt worktree at base has an empty diff -> would be approved unread
    with pytest.raises(urp.PreflightError):
        urp.preflight("/work", _run_git(diff_stdout="   \n"), base="main")


def test_preflight_skips_diff_check_when_no_base():
    # base=None -> the (optional) diff guard is skipped; empty diff must NOT raise
    urp.preflight("/work", _run_git(diff_stdout=""), base=None)


def test_preflight_probes_config_list_exactly():
    seen = []

    def spy(workdir, *args):
        seen.append(args)
        if args[:2] == ("config", "--list"):
            return SimpleNamespace(returncode=0, stdout="user.name=P\n")
        return SimpleNamespace(returncode=0, stdout=" f | 1 +\n")

    urp.preflight("/work", spy, base="main")
    assert ("config", "--list") in seen, "must run the safe `git config --list` probe"


# ---------------------------------------------------------------- safe_reader_flags

def test_safe_reader_flags_exact():
    assert urp.safe_reader_flags() == [
        "--setting-sources", "user",
        "--disallowedTools", "Edit,Write,MultiEdit,NotebookEdit,WebFetch,WebSearch",
    ]
