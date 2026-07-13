from pathlib import Path

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


def test_link_auth_never_clobbers_real_file(tmp_path):
    real = tmp_path / "link"; real.write_text("do not delete")
    target = tmp_path / "auth"; target.write_text("auth")
    ai._link_auth(real, target)
    assert real.read_text() == "do not delete"


def test_prepare_isolated_home_never_raises_on_missing_auth(tmp_path):
    op = tmp_path / "operator"; op.mkdir()
    home = ai.prepare_isolated_home("claude", tmp_path / "s", operator_home=op)
    assert home.exists(), "missing operator auth is non-fatal — spawn still gets a HOME"
