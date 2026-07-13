import subprocess

from swarph_shared import agent_isolation as ai


def test_isolated_spawn_cannot_read_operator_gh_creds(tmp_path):
    """Prove the negative across BOTH channels — the file path AND the env vars.

    The source env is seeded with the credential channels a real dev shell
    exports (GH_TOKEN, XDG_CONFIG_HOME pointing at the operator's config,
    SSH_AUTH_SOCK). A leak through any of them would let the spawn reach the
    operator's gh token; the isolated env must close all of them.
    """
    op = tmp_path / "operator"
    (op / ".claude").mkdir(parents=True)
    (op / ".claude" / ".credentials.json").write_text("CLAUDE-AUTH-OK")
    (op / ".config" / "gh").mkdir(parents=True)
    (op / ".config" / "gh" / "hosts.yml").write_text("GH-SECRET-TOKEN")

    home = ai.prepare_isolated_home("claude", tmp_path / "scratch", operator_home=op)
    source = {
        "PATH": "/usr/bin:/bin",
        "GH_TOKEN": "GH-SECRET-TOKEN",           # gh reads this before ~/.config/gh
        "XDG_CONFIG_HOME": str(op / ".config"),  # redirects gh config back to operator
        "SSH_AUTH_SOCK": "/tmp/fake-operator-agent.sock",
    }
    env = ai.build_isolated_env(source, home, "claude")

    # env-var channels are stripped before the spawn ever runs
    assert "GH_TOKEN" not in env and "SSH_AUTH_SOCK" not in env and "XDG_CONFIG_HOME" not in env

    # and from INSIDE a real subprocess honouring $HOME/$XDG_CONFIG_HOME/$GH_TOKEN:
    script = (
        'test -r "$HOME/.claude/.credentials.json" && echo AUTH_OK || echo AUTH_MISSING; '
        'test -n "$GH_TOKEN" && echo TOKEN_LEAK || echo TOKEN_ISOLATED; '
        'test -n "$SSH_AUTH_SOCK" && echo SSH_LEAK || echo SSH_ISOLATED; '
        'gh_cfg="${XDG_CONFIG_HOME:-$HOME/.config}/gh/hosts.yml"; '
        'test -r "$gh_cfg" && echo GH_LEAK || echo GH_ISOLATED'
    )
    proc = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=20)
    out = proc.stdout
    assert "AUTH_OK" in out, "the agent's OWN auth is reachable through the disposable HOME"
    assert "TOKEN_ISOLATED" in out and "TOKEN_LEAK" not in out, "GH_TOKEN env channel is closed"
    assert "SSH_ISOLATED" in out and "SSH_LEAK" not in out, "SSH_AUTH_SOCK agent channel is closed"
    assert "GH_ISOLATED" in out and "GH_LEAK" not in out, \
        "gh config resolves to the drone HOME, not the operator's ~/.config — the negative holds"
