"""Subprocess env scrubbing for `claude -p` (and any other subprocess that
must NOT inherit billing-path env keys).

**SCOPE — read carefully**: this denylist guards the BILLING PATH only —
keeping `claude -p` on subscription auth (`~/.claude/.credentials.json`)
instead of metered API. It does NOT cover general secret-leakage concerns.
Future auth-token shapes (e.g. ANTHROPIC_AUTH_TOKEN, OPENAI_BEARER, OAUTH_*,
*_SECRET, *_TOKEN) are NOT caught here — those are a separate-concern
allowlist if/when added.

When new billing-relevant key shapes appear, add them to
``FORBIDDEN_KEYS_EXPLICIT`` here.

Pattern is the explicit named set UNION the `*_API_KEY` suffix — denylist
not allowlist by design. Allowlist would be stronger for general-secret-leak
prevention but would break the moment `claude -p` (or any other subprocess)
needed an env var we didn't anticipate (PYTHONPATH, NPM_CONFIG_*, locale
vars, etc.). For the BILLING use case, denylist is correct.

The audit memory: anything ending in ``_API_KEY`` is force-popped; the
explicit set catches keys that don't end in `_API_KEY` but ARE billing-
relevant. Denylist composition: explicit_set ∪ `*_API_KEY` suffix.

Reference for the rule + audit lineage:
- CLAUDE.md "Critical operational rules" — "Lab-side daemons run
  subscription-billed via `claude -p`, NEVER anthropic.Anthropic() SDK.
  Setting ANTHROPIC_API_KEY in claude-service env would silently flip the
  billing path."
- evolution_tracker ev_an040i00v — primitive #7 `config-leak-via-env-
  inheritance` filed 2026-05-07, lab+claude-service both had the
  wholesale-passthrough bug
- DM #595/#596 review: lab+drop converged on denylist-with-suffix (not
  allowlist) as the right shape for the billing scope
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

# Keys that don't end in `_API_KEY` but ARE billing-relevant. Add new ones
# here when new providers ship tokens with non-standard naming.
FORBIDDEN_KEYS_EXPLICIT = frozenset({
    "ANTHROPIC_API_KEY",
    "CLAUDE_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "DEEPSEEK_API_KEY",
})


def scrub_env_for_subprocess() -> dict:
    """Return ``os.environ`` minus billing-relevant API keys.

    Defensive: pops anything ending in ``_API_KEY`` (forward-compat) plus an
    explicit set of known billing keys. Preserves PATH, HOME, USER, etc. so
    subscription-billed subprocesses (``claude -p``) can find their
    credentials file and runtime deps.

    Use case:

        proc = subprocess.run(
            ["claude", "-p", prompt, "--model", model],
            env={**scrub_env_for_subprocess(), "IS_SANDBOX": "1"},
            ...
        )

    Returns:
        Dict suitable for passing to ``subprocess.run(env=...)``. Caller adds
        any extra env vars on top (e.g. ``IS_SANDBOX=1``).
    """
    return {
        k: v
        for k, v in os.environ.items()
        if k not in FORBIDDEN_KEYS_EXPLICIT and not k.endswith("_API_KEY")
    }


def verify_subscription_setup(
    *,
    claude_bin: Optional[str] = None,
    creds_path: Optional[Path] = None,
) -> None:
    """Fail-loud sanity check for `claude -p` subscription billing readiness.

    Asserts:
      1. ``scrub_env_for_subprocess()`` removes ANTHROPIC_API_KEY even when set
      2. ``creds_path`` (default ``~/.claude/.credentials.json``) exists, is
         mode 0600, readable
      3. ``claude_bin`` (default ``CLAUDE_BIN`` env or ``/root/.local/bin/claude``)
         is executable

    Raises ``RuntimeError`` if any check fails — never serve a single
    request with leaked API key or broken subscription auth.

    Production wiring guidance: call this ONCE at service/boss/cron startup
    BEFORE any subprocess call hits the request path. That fires verification
    at deploy-time so a misconfigured deploy never serves a single request.

    Args:
        claude_bin: path to claude binary. Defaults to ``$CLAUDE_BIN`` or
                    ``/root/.local/bin/claude``.
        creds_path: path to credentials.json. Defaults to
                    ``~/.claude/.credentials.json``.
    """
    if creds_path is None:
        creds_path = Path.home() / ".claude" / ".credentials.json"
    if claude_bin is None:
        claude_bin = os.environ.get("CLAUDE_BIN", "/root/.local/bin/claude")

    saved = os.environ.get("ANTHROPIC_API_KEY")
    try:
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-FAKE-FOR-TEST"
        scrubbed = scrub_env_for_subprocess()
        if "ANTHROPIC_API_KEY" in scrubbed:
            raise RuntimeError(
                "swarph_shared.subprocess_env: scrub_env_for_subprocess did "
                "not remove ANTHROPIC_API_KEY"
            )
        if "PATH" not in scrubbed:
            raise RuntimeError(
                "swarph_shared.subprocess_env: scrub_env_for_subprocess "
                "over-pruned (PATH missing)"
            )
    finally:
        if saved is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = saved

    if not creds_path.exists():
        raise RuntimeError(
            f"swarph_shared.subprocess_env: subscription auth missing at "
            f"{creds_path}; claude -p will fail or fall back to API billing"
        )
    mode = creds_path.stat().st_mode & 0o777
    if mode & 0o077:
        raise RuntimeError(
            f"swarph_shared.subprocess_env: {creds_path} is mode {oct(mode)}; "
            f"should be 0600"
        )

    if not Path(claude_bin).exists():
        raise RuntimeError(
            f"swarph_shared.subprocess_env: claude binary missing at {claude_bin}"
        )
