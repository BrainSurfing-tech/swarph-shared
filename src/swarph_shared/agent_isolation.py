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
