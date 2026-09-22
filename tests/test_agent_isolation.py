import json
from pathlib import Path

import pytest

from swarph_shared import agent_isolation as ai


def test_build_isolated_env_forces_home():
    src = {"HOME": "/home/operator", "PATH": "/usr/bin", "FOO": "bar"}
    env = ai.build_isolated_env(src, Path("/tmp/drone-home"), "claude")
    assert env["HOME"] == "/tmp/drone-home", "HOME must be the disposable dir, never the source"
    assert env["PATH"] == "/usr/bin" and env["FOO"] == "bar", "benign vars pass through"


def test_build_isolated_env_scrubs_billing_and_redirect():
    src = {"HOME": "/home/operator", "ANTHROPIC_API_KEY": "sk-x",
           "ANTHROPIC_AUTH_TOKEN": "t", "CLAUDE_CONFIG_DIR": "/evil"}
    env = ai.build_isolated_env(src, Path("/tmp/h"), "claude")
    assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_AUTH_TOKEN" not in env
    assert "CLAUDE_CONFIG_DIR" not in env, "a namespace redirect that would bypass forced HOME is scrubbed"


def test_build_isolated_env_drops_credential_redirect_channels():
    """Forcing HOME is not enough — env-var channels reach operator creds too.

    Security-review finding 2026-07-13: SSH_AUTH_SOCK, GH_TOKEN/GITHUB_TOKEN,
    GH_CONFIG_DIR, XDG_CONFIG_HOME, GIT_CONFIG_GLOBAL etc. all leak past a forced
    HOME. They must be scrubbed.
    """
    src = {
        "HOME": "/home/op", "PATH": "/usr/bin",
        "SSH_AUTH_SOCK": "/tmp/ssh-agent.sock",
        "GH_TOKEN": "ghp_x", "GITHUB_TOKEN": "ghp_y",
        "GH_ENTERPRISE_TOKEN": "e1", "GITHUB_ENTERPRISE_TOKEN": "e2",
        "GH_CONFIG_DIR": "/home/op/.config/gh",
        "XDG_CONFIG_HOME": "/home/op/.config", "XDG_DATA_HOME": "/home/op/.local/share",
        "XDG_CACHE_HOME": "/home/op/.cache", "XDG_STATE_HOME": "/home/op/.local/state",
        "GIT_CONFIG_GLOBAL": "/home/op/.gitconfig", "GIT_CONFIG_SYSTEM": "/etc/gitconfig",
        "GNUPGHOME": "/home/op/.gnupg",
    }
    env = ai.build_isolated_env(src, Path("/tmp/drone"), "claude")
    for leaked in (
        "SSH_AUTH_SOCK", "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN", "GH_CONFIG_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
        "XDG_CACHE_HOME", "XDG_STATE_HOME", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
        "GNUPGHOME",
    ):
        assert leaked not in env, f"{leaked} must be scrubbed (credential-redirect bypass)"
    assert env["PATH"] == "/usr/bin", "benign vars still pass through"


def test_build_isolated_env_does_not_mutate_source():
    src = {"HOME": "/home/operator", "PATH": "/usr/bin"}
    ai.build_isolated_env(src, Path("/tmp/h"), "codex")
    assert src["HOME"] == "/home/operator", "source dict is never mutated"


def test_provider_auth_map_relative_paths():
    assert ai.PROVIDER_AUTH["claude"] == (".claude/.credentials.json",)
    assert ai.PROVIDER_AUTH["codex"] == (".codex/auth.json",)
    assert not any(p.startswith("/") for paths in ai.PROVIDER_AUTH.values() for p in paths)


def test_scrub_provider_namespace_denies_redirect_keeps_rest():
    env = {"GROK_HOME": "/x", "GROK_AUTH_PATH": "/y", "GROK_MODEL": "keep", "XAI_API_KEY": "z"}
    ai.scrub_provider_namespace(env, "grok")
    assert "GROK_HOME" not in env and "GROK_AUTH_PATH" not in env
    assert env.get("GROK_MODEL") == "keep", "non-redirect namespace vars are preserved"


def test_prepare_isolated_home_links_only_provider_auth(tmp_path):
    op = tmp_path / "operator"
    (op / ".claude").mkdir(parents=True)
    (op / ".claude" / ".credentials.json").write_text("SECRET-CLAUDE-AUTH")
    (op / ".config" / "gh").mkdir(parents=True)
    (op / ".config" / "gh" / "hosts.yml").write_text("GH-TOKEN")
    (op / ".git-credentials").write_text("https://x:tok@github.com")

    root = tmp_path / "scratch"
    home = ai.prepare_isolated_home("claude", root, operator_home=op)

    assert home == root / ".claude-drone-home"
    assert (home / ".claude" / ".credentials.json").read_text() == "SECRET-CLAUDE-AUTH"
    assert not (home / ".config" / "gh" / "hosts.yml").exists()
    assert not (home / ".git-credentials").exists()


def test_prepare_isolated_home_resets_git_credential_helper(tmp_path):
    op = tmp_path / "operator"; (op / ".claude").mkdir(parents=True)
    (op / ".claude" / ".credentials.json").write_text("x")
    home = ai.prepare_isolated_home("claude", tmp_path / "s", operator_home=op)
    assert "helper =" in (home / ".gitconfig").read_text(), "credential helper list reset (blocks system helpers)"


def test_link_auth_idempotent_and_replaces_stale(tmp_path):
    target = tmp_path / "real_auth"; target.write_text("auth")
    link = tmp_path / "link"
    ai._link_auth(link, target)
    assert link.resolve() == target
    ai._link_auth(link, target)
    assert link.resolve() == target
    link.unlink(); link.symlink_to(tmp_path / "gone")   # stale
    ai._link_auth(link, target)
    assert link.resolve() == target


# --- #923: a real file at the link path. The four cases, and the one refusal. ---
#
# REPLACES test_link_auth_never_clobbers_real_file, which asserted the defect.
# "Never clobber a real file" left a BLANKED claude credential in place for 44h
# across 251 spawns: the guard fired on every one of them. Inside a drone home
# this module creates and owns, the only writer of a PROVIDER_AUTH path is this
# module, so the file it was protecting cannot legitimately exist.

def _claude_cred(path, access="tok", expires=1000):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": access, "refreshToken": "r", "expiresAt": expires,
        "refreshTokenExpiresAt": expires + 9 * 10 ** 8}}))


def test_link_auth_replaces_a_BLANKED_real_file(tmp_path):
    """The #923 state exactly: tokens emptied by a failed refresh, then pinned."""
    link, target = tmp_path / "link", tmp_path / "auth"
    _claude_cred(link, access="", expires=0)          # what the CLI wrote back
    _claude_cred(target, access="live", expires=9000)
    ai._link_auth(link, target, "claude")
    assert link.is_symlink() and link.resolve() == target


def test_link_auth_replaces_a_STALE_but_usable_real_file(tmp_path):
    """Tonight's disk state: a hand-copy, older than or equal to the operator's."""
    link, target = tmp_path / "link", tmp_path / "auth"
    _claude_cred(link, access="copy", expires=1000)
    _claude_cred(target, access="live", expires=9000)
    ai._link_auth(link, target, "claude")
    assert link.is_symlink() and link.resolve() == target


def test_link_auth_REFUSES_when_the_real_file_is_NEWER(tmp_path):
    """The only lossy case — and the signature of a rename-based CLI."""
    link, target = tmp_path / "link", tmp_path / "auth"
    _claude_cred(link, access="refreshed-in-the-drone", expires=9000)
    _claude_cred(target, access="older", expires=1000)
    with pytest.raises(ai.CredentialConflict) as exc:
        ai._link_auth(link, target, "claude")
    assert not link.is_symlink(), "the newer credential is left exactly where it is"
    assert json.loads(link.read_text())["claudeAiOauth"]["accessToken"] == "refreshed-in-the-drone"
    # str(exc) reaches an HTTP 502 body via swarph_cli service/app.py, so it must
    # carry NO paths; the paths ride on attributes for the caller to log.
    assert str(link) not in str(exc.value) and str(target) not in str(exc.value), (
        "str(CredentialConflict) is put in an HTTP response body — no absolute paths")
    assert exc.value.link == link and exc.value.target == target
    assert str(link) in exc.value.detail and str(target) in exc.value.detail


def test_link_auth_REFUSES_when_the_OPERATOR_credential_is_the_broken_one(tmp_path, capsys):
    """#923's failure mode mirrored: blaming a rename for an operator-side blank."""
    link, target = tmp_path / "link", tmp_path / "auth"
    _claude_cred(link, access="working", expires=1000)
    _claude_cred(target, access="", expires=0)               # operator blanked
    with pytest.raises(ai.CredentialConflict) as exc:
        ai._link_auth(link, target, "claude")
    assert "operator" in str(exc.value).lower(), "the summary names the side that is broken"
    assert "rename" not in exc.value.detail.lower(), (
        "no rename is in play — a message must not assert a cause that is not the cause")
    assert "PARENT DIRECTORY" not in exc.value.detail, (
        "the directory bind cannot help: the parent of a blank file holds a blank file")
    assert "RE-AUTHENTICATE" in exc.value.detail
    assert json.loads(link.read_text())["claudeAiOauth"]["accessToken"] == "working"
    assert "blank or unparseable" in capsys.readouterr().err

    target.write_text("{{{ not json")                        # unparseable, same branch
    with pytest.raises(ai.CredentialConflict) as exc2:
        ai._link_auth(link, target, "claude")
    assert "operator" in str(exc2.value).lower()


def test_link_auth_EQUAL_rank_replaces(tmp_path):
    """The one state live on disk tonight: two byte-identical files, equal expiresAt.

    The comparison is `>` not `>=`, so equal falls through to replace. That is one
    character with no test on it until now.
    """
    link, target = tmp_path / "link", tmp_path / "auth"
    _claude_cred(link, access="same", expires=5000)
    _claude_cred(target, access="same", expires=5000)
    ai._link_auth(link, target, "claude")
    assert link.is_symlink() and link.resolve() == target, (
        "equal rank must REPLACE — a hand-copy of the operator's own file is not a conflict")


def test_link_auth_unparseable_provider_falls_back_to_mtime(tmp_path):
    """codex/gemini/grok have no parser here; mtime is the honest floor."""
    import os
    link, target = tmp_path / "link", tmp_path / "auth"
    target.write_text("newer"); link.write_text("older")
    os.utime(link, (1, 1))                            # link clearly older
    ai._link_auth(link, target, "codex")
    assert link.is_symlink() and link.resolve() == target

    link2 = tmp_path / "link2"; link2.write_text("newer than target")
    os.utime(target, (1, 1))
    with pytest.raises(ai.CredentialConflict):
        ai._link_auth(link2, target, "codex")


def test_link_auth_says_so_when_the_target_is_absent(tmp_path, capsys):
    """macOS Keychain / not-logged-in. Legitimate, but never silent."""
    ai._link_auth(tmp_path / "link", tmp_path / "nothing-here", "claude")
    assert "does not exist" in capsys.readouterr().err


def test_warn_if_expiring_fires_inside_the_window_and_not_outside(tmp_path, capsys):
    """#923's root cause was a hard expiry nobody was watching."""
    import time
    target = tmp_path / "auth"
    now = time.time() * 1000
    _claude_cred(target, expires=int(now))
    # refreshTokenExpiresAt is expires + 9e8 ms (~10.4 days) -> outside the window
    assert ai._warn_if_expiring(target, "claude") is None
    target.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "t", "expiresAt": now, "refreshTokenExpiresAt": now + 2 * 86400 * 1000}}))
    assert "hard-expires" in (ai._warn_if_expiring(target, "claude") or "")
    assert "hard-expires" in capsys.readouterr().err
    assert ai._warn_if_expiring(target, "codex") is None, "no parser, no guess"


def test_warn_if_expiring_says_it_ONCE_A_DAY_not_once_a_spawn(tmp_path, capsys):
    """Inside the window the condition is CONTINUOUS; per-spawn lines count duration."""
    import time
    target, stamp_dir = tmp_path / "auth", tmp_path / "home"
    stamp_dir.mkdir()
    now = time.time() * 1000
    target.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "t", "expiresAt": now, "refreshTokenExpiresAt": now + 2 * 86400 * 1000}}))
    assert ai._warn_if_expiring(target, "claude", stamp_dir=stamp_dir) is not None
    for _ in range(4):
        assert ai._warn_if_expiring(target, "claude", stamp_dir=stamp_dir) is None, (
            "the return value must distinguish SPOKE from SUPPRESSED — a caller doing "
            "`if warn(): notify()` would otherwise notify on every spawn")
    assert capsys.readouterr().err.count("hard-expires") == 1, "five spawns, one line"
    (stamp_dir / ".credential-expiry-warned").write_text(str(now - 86400001))   # 24h+ ago
    assert ai._warn_if_expiring(target, "claude", stamp_dir=stamp_dir) is not None
    assert "hard-expires" in capsys.readouterr().err, "a full day later is news again"


def test_warn_if_expiring_measures_ELAPSED_time_not_the_calendar(tmp_path, capsys):
    """"%Y-%m-%d" printed twice in three minutes across UTC midnight."""
    import time
    target, stamp_dir = tmp_path / "auth", tmp_path / "home"
    stamp_dir.mkdir()
    midnight = 1790035200000.0                       # 2026-09-22T00:00:00Z
    doc = lambda now: json.dumps({"claudeAiOauth": {
        "accessToken": "t", "expiresAt": now, "refreshTokenExpiresAt": now + 2 * 86400 * 1000}})
    target.write_text(doc(midnight))
    assert ai._warn_if_expiring(target, "claude", now_ms=midnight - 120000, stamp_dir=stamp_dir)
    assert ai._warn_if_expiring(target, "claude", now_ms=midnight + 60000, stamp_dir=stamp_dir) is None, (
        "crossing midnight is not 24 hours elapsed")
    assert capsys.readouterr().err.count("hard-expires") == 1


def test_link_auth_says_so_when_BOTH_sides_are_broken(tmp_path, capsys):
    """The link is right and the spawn still fails — that must not read as a repair."""
    link, target = tmp_path / "link", tmp_path / "auth"
    _claude_cred(link, access="", expires=0)
    _claude_cred(target, access="", expires=0)
    ai._link_auth(link, target, "claude")
    err = capsys.readouterr().err
    assert link.is_symlink(), "linking is still the correct action"
    assert "ALSO blank or unparseable" in err, "the operator side must be named too"
    assert "RE-AUTHENTICATE" in err
    assert "NO USABLE CLAUDE CREDENTIAL EXISTS ON THIS BOX" in err


def test_prepare_isolated_home_never_raises_on_missing_auth(tmp_path):
    op = tmp_path / "operator"; op.mkdir()
    home = ai.prepare_isolated_home("claude", tmp_path / "s", operator_home=op)
    assert home.exists(), "missing operator auth is non-fatal — spawn still gets a HOME"


def test_warn_if_expiring_a_FUTURE_stamp_does_not_silence_it(tmp_path, capsys):
    """A clock that moved backwards must fail OPEN, like every other stamp failure."""
    target, stamp_dir = tmp_path / "auth", tmp_path / "home"
    stamp_dir.mkdir()
    now = 1790035200000.0
    target.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "t", "expiresAt": now, "refreshTokenExpiresAt": now + 2 * 86400 * 1000}}))
    stamp = stamp_dir / ".credential-expiry-warned"

    stamp.write_text(str(now + 5 * 365 * 86400000))          # NTP step back / restored home
    assert ai._warn_if_expiring(target, "claude", now_ms=now, stamp_dir=stamp_dir) is not None, (
        "a stamp from the future must not suppress the warning — it fails CLOSED and silently")
    assert "hard-expires" in capsys.readouterr().err

    for junk in ("", "   ", "not-a-number"):                 # the cases that already failed open
        stamp.write_text(junk)
        assert ai._warn_if_expiring(target, "claude", now_ms=now, stamp_dir=stamp_dir) is not None


def test_remaining_validity_is_logged_at_every_spawn(tmp_path, capsys):
    """The deferral rate is a per-runner, at-spawn quantity — not a fixed-window sampler's."""
    target = tmp_path / "auth"
    now = 1790035200000.0
    target.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "t", "expiresAt": now + 90 * 60000, "refreshTokenExpiresAt": now + 9 * 10 ** 8}}))
    assert "90 min of validity left" in (ai._log_remaining_validity(target, "claude", now_ms=now) or "")
    assert "90 min" in capsys.readouterr().err
    # every spawn, not once a day: this one carries no stamp and must repeat
    ai._log_remaining_validity(target, "claude", now_ms=now)
    assert "90 min" in capsys.readouterr().err, "the at-spawn reading must NOT be deduplicated"
    assert ai._log_remaining_validity(target, "codex", now_ms=now) is None, "no parser, no guess"


def test_prepare_isolated_home_NEVER_raises_on_a_credential_conflict(tmp_path, capsys):
    """THREE live callers, one a uvicorn on 0.0.0.0:8789 — a refusal must not 500 a service.

    The contract is "best-effort; a partial home is still a valid HOME". _link_auth still
    raises for direct callers; prepare_isolated_home converts it to a loud stderr line and
    a home with no credential, so the spawn fails at AUTH with a reason instead of a stack
    trace in a request handler. (science-claude: the caller count was a FLOOR of 2; the
    whole-home sweep returned 3.)
    """
    op, root = tmp_path / "op", tmp_path / "drones"
    _claude_cred(op / ".claude" / ".credentials.json", access="older", expires=1000)
    link = root / ".claude-drone-home" / ".claude" / ".credentials.json"
    _claude_cred(link, access="newer-in-the-drone", expires=9000)

    home = ai.prepare_isolated_home("claude", root, operator_home=op)     # must NOT raise

    assert home.exists(), "a partial home is still a valid HOME"
    err = capsys.readouterr().err
    assert "REFUSED" in err and "left untouched" in err
    assert not link.is_symlink(), "the refusal holds — the newer credential is not clobbered"
    assert json.loads(link.read_text())["claudeAiOauth"]["accessToken"] == "newer-in-the-drone"
    with pytest.raises(ai.CredentialConflict):
        ai._link_auth(link, op / ".claude" / ".credentials.json", "claude")   # direct call still raises
