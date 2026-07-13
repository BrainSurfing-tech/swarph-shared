"""Disposable-HOME credential isolation for headless agent spawns (#2a).

Generalises grok's in-repo isolation (swarph-cli spawn.py) to any provider. A
spawned agent receives a HOME that carries ONLY its own CLI auth — never the
operator's ~/.config/gh, ~/.git-credentials, ~/.netrc, ~/.ssh, which are simply
never linked in. Pure helpers (top half) are unit-tested; prepare_isolated_home
is a best-effort seam that never crashes a spawn.

See swarph-cli/docs/superpowers/specs/2026-07-13-swairm-pattern-port-design.md.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from swarph_shared.subprocess_env import FORBIDDEN_KEYS_EXPLICIT, FORBIDDEN_SUFFIXES

#: Per-provider auth path(s), RELATIVE to HOME — so they compose with the
#: disposable HOME. Only these are linked in; nothing else on disk is reachable.
PROVIDER_AUTH: dict[str, tuple[str, ...]] = {
    "claude": (".claude/.credentials.json",),
    "codex": (".codex/auth.json",),
    "gemini": (".gemini/oauth_creds.json",),
    "grok": (".grok/auth.json",),
}

#: Namespace prefixes whose *_HOME / *_AUTH_PATH / *_AUTH_PROVIDER_COMMAND /
#: *_CONFIG_DIR keys would redirect a CLI off the forced HOME. Deny per provider.
_PROVIDER_PREFIXES: dict[str, tuple[str, ...]] = {
    "claude": ("CLAUDE_", "ANTHROPIC_"),
    "codex": ("CODEX_", "OPENAI_"),
    "gemini": ("GEMINI_", "GOOGLE_"),
    "grok": ("GROK_", "XAI_"),
}
_REDIRECT_SUFFIXES = ("_HOME", "_AUTH_PATH", "_AUTH_PROVIDER_COMMAND", "_CONFIG_DIR")


def scrub_provider_namespace(env: dict, provider: str) -> None:
    """In-place: drop redirect keys in ``provider``'s namespace (best-effort)."""
    prefixes = _PROVIDER_PREFIXES.get(provider, ())
    if not prefixes:
        return
    for key in list(env):
        if key.startswith(prefixes) and key.endswith(_REDIRECT_SUFFIXES):
            env.pop(key, None)


def build_isolated_env(source: Mapping[str, str], home: Path, provider: str) -> dict[str, str]:
    """Billing-scrubbed env with HOME forced to ``home`` and redirects scrubbed.

    Pure: ``source`` is never mutated. HOME is IMPOSED (never taken from
    ``source``) — that is what cuts the spawned agent's access to on-disk creds.
    """
    env = {
        k: v for k, v in source.items()
        if k not in FORBIDDEN_KEYS_EXPLICIT and not k.endswith(FORBIDDEN_SUFFIXES)
    }
    scrub_provider_namespace(env, provider)
    env["HOME"] = str(home)
    return env


_GITCONFIG = "[user]\n\tname = swarph drone\n\temail = drone@swarph.local\n[credential]\n\thelper =\n"


def _link_auth(link: Path, target: Path) -> None:
    """Idempotent best-effort symlink ``link`` -> ``target`` (mirrors _link_grok_auth)."""
    if not target.exists():
        return
    try:
        if link.is_symlink():
            if link.readlink() == target:
                return
            link.unlink()          # stale/foreign/dangling → replace
        elif link.exists():
            return                 # never clobber a real file
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
    except OSError:
        return                     # never crash a spawn


def prepare_isolated_home(provider: str, root: Path, *, operator_home: Path | None = None) -> Path:
    """Create root/.{provider}-drone-home carrying ONLY this provider's auth."""
    op = operator_home if operator_home is not None else Path.home()
    home = Path(root) / f".{provider}-drone-home"
    try:
        home.mkdir(parents=True, exist_ok=True)
        for rel in PROVIDER_AUTH.get(provider, ()):
            _link_auth(home / rel, Path(op) / rel)
        (home / ".gitconfig").write_text(_GITCONFIG, encoding="utf-8")
    except OSError:
        pass                       # best-effort; a partial home is still a valid HOME
    return home
