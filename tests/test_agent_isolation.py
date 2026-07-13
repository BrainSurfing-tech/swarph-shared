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
