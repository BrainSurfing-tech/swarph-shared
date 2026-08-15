"""Untrusted-repo preflight — SWAIRM Pattern #3 port (INERT: no consumer yet).

When an agent reads a repo/worktree it did NOT author, a poisoned ``.git/config``
turns a plain ``git diff``/``status`` into code execution: ``diff.external``,
``diff.*.textconv``/``.command``, ``core.fsmonitor``, ``core.hookspath``,
``filter.*.clean``/``smudge``, and ``alias.{diff,log,show,status}`` all run a
command the moment a read command touches the repo. ``git config --list`` executes
nothing — it is the safe probe. A poisoned key ⇒ abstain (no read).

This module ports swairm's Queen preflight into a reusable, consumer-agnostic helper:
  - ``git_config_is_poisoned(config_list_output)`` — pure; the offending key or None.
    Regex ported VERBATIM from swairm's ``_POISONED_GIT_RE`` (battle-tested, review R1/I2).
  - ``preflight(workdir, run_git, *, base=...)`` — runs the safe probe, raises
    ``PreflightError`` on poison; with ``base`` given, also enforces a non-empty
    diff-vs-base (swairm I3: a worktree rebuilt at base has an empty diff and would be
    "approved" unread). ``run_git`` is an injectable seam (default = real subprocess).
  - ``safe_reader_flags()`` — the ``claude -p`` flags for reading an untrusted repo
    (``--setting-sources user`` so a committed project ``.claude/settings.json`` can't
    re-authorize the reader; ``--disallowedTools`` write/net, which deny-prime over any
    additive re-grant).

**INERT by design.** Nothing calls this yet. Consumers (future, per the spec): a
``--untrusted`` mode on the spawn helper (#2a), and any future swarph reviewer/verify
flow. The helper is a no-op until a caller opts in — never silently wired in.

See swarph-cli/docs/superpowers/specs/2026-07-13-swairm-pattern-port-design.md §Pattern#3.
"""
from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

#: Keys whose mere presence makes a read command EXECUTE code (textconv/external/
#: fsmonitor/hooks/filters) or HIJACK a read alias. Ported VERBATIM from swairm's
#: ``_POISONED_GIT_RE`` (spawning_pool/queen.py; review R1/I2). Line-anchored +
#: MULTILINE so it matches ``key=…`` at the start of a ``git config --list`` line only.
_POISONED_GIT_RE = re.compile(
    r"^(diff\.external=|diff\.[^=]*\.textconv=|diff\.[^=]*\.command=|core\.fsmonitor=|"
    r"core\.hookspath=|filter\.[^=]*\.(clean|smudge)=|alias\.(diff|log|show|status)=)",
    re.IGNORECASE | re.MULTILINE,
)

#: Deny these for any ``claude -p`` reading an untrusted repo. ``--disallowedTools`` is
#: deny-priming: a committed project settings that re-grants Edit/Write can't override it.
_SAFE_READER_DISALLOWED = "Edit,Write,MultiEdit,NotebookEdit,WebFetch,WebSearch"


class PreflightError(RuntimeError):
    """The untrusted repo failed a safety gate — abstain rather than read it."""


def git_config_is_poisoned(config_list_output: str) -> str | None:
    """Return the offending config KEY if ``git config --list`` output is poisoned, else None.

    Pure. Match the poison regex against the ``key=value`` lines; on a hit, return the
    key (the matched ``key=`` minus the trailing ``=``) so callers can name it.
    """
    m = _POISONED_GIT_RE.search(config_list_output)
    if m is None:
        return None
    return m.group(1).rstrip("=")


def default_run_git(workdir: str | Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Real subprocess seam: ``git <args>`` in ``workdir``, captured, 30s cap.

    The injectable default for :func:`preflight`; tests pass a fake instead.
    """
    return subprocess.run(
        ["git", *args],
        cwd=str(workdir),
        capture_output=True,
        text=True,
        timeout=30,
    )


def preflight(
    workdir: str | Path,
    run_git: Callable[..., Any] | None = None,
    *,
    base: str | None = None,
) -> None:
    """Gate an untrusted checkout before any read; raise :class:`PreflightError` to abstain.

    1. Safe probe: ``git config --list`` (executes nothing). Non-zero exit ⇒ raise.
       A poisoned key ⇒ raise (naming the key).
    2. (Optional, when ``base`` given) non-empty diff vs ``base`` — swairm I3: a worktree
       rebuilt at base shows an empty diff and would be "approved" unread. ``--no-ext-diff``
       is belt-and-suspenders after the config probe. Empty/failed diff ⇒ raise.

    ``run_git(workdir, *git_args)`` is the injectable seam (returns an object with
    ``.returncode`` and ``.stdout``); defaults to :func:`default_run_git`.
    """
    if run_git is None:
        run_git = default_run_git

    probe = run_git(workdir, "config", "--list")
    if probe.returncode != 0:
        raise PreflightError(
            f"`git config --list` probe failed (exit {probe.returncode}) — cannot verify safety"
        )
    poisoned = git_config_is_poisoned(probe.stdout)
    if poisoned is not None:
        raise PreflightError(
            f"poisoned git config key {poisoned!r} "
            "(textconv/external/fsmonitor/hooks/filter/alias) — refusing to read"
        )

    if base is not None:
        diff = run_git(workdir, "diff", "--no-ext-diff", "--stat", f"{base}...HEAD")
        if diff.returncode != 0 or not diff.stdout.strip():
            raise PreflightError(
                f"no diff vs {base!r} — worktree not at the change under review (empty-diff trap)"
            )


def safe_reader_flags() -> list[str]:
    """``claude -p`` flags for reading an untrusted repo (project settings can't re-authorize)."""
    return ["--setting-sources", "user", "--disallowedTools", _SAFE_READER_DISALLOWED]
