import subprocess

from swarph_shared import agent_isolation as ai


def test_isolated_spawn_cannot_read_operator_gh_creds(tmp_path):
    op = tmp_path / "operator"
    (op / ".claude").mkdir(parents=True)
    (op / ".claude" / ".credentials.json").write_text("CLAUDE-AUTH-OK")
    (op / ".config" / "gh").mkdir(parents=True)
    (op / ".config" / "gh" / "hosts.yml").write_text("GH-SECRET-TOKEN")

    home = ai.prepare_isolated_home("claude", tmp_path / "scratch", operator_home=op)
    env = ai.build_isolated_env({"PATH": "/usr/bin:/bin"}, home, "claude")

    script = (
        'test -r "$HOME/.claude/.credentials.json" && echo AUTH_OK || echo AUTH_MISSING; '
        'test -r "$HOME/.config/gh/hosts.yml" && echo GH_LEAK || echo GH_ISOLATED'
    )
    proc = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=20)
    assert "AUTH_OK" in proc.stdout, "the agent's OWN auth is reachable through the disposable HOME"
    assert "GH_ISOLATED" in proc.stdout and "GH_LEAK" not in proc.stdout, \
        "the operator's gh token is UNREACHABLE from the isolated spawn — the credential negative"
