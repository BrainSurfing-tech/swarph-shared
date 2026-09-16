"""Subprocess env scrubbing for `claude -p` (and any other subprocess that
must NOT inherit billing-path env keys).

**SCOPE — read carefully**: this denylist guards the BILLING PATH only —
keeping `claude -p` on subscription auth (`~/.claude/.credentials.json`)
instead of metered API. It does NOT cover general secret-leakage concerns.
As of the 2026-06-01 adversarial-sweep CRIT, this now ALSO catches the
billing/auth-REDIRECT class — the ``*_AUTH_TOKEN`` and ``*_BASE_URL`` suffixes
plus explicit GCP/Vertex creds — which flip ``claude -p`` (and the other
subscription CLIs) from subscription auth to a metered/relay endpoint while
cost_usd still reports 0.0. Other secret shapes (OPENAI_BEARER, OAUTH_*,
*_SECRET) remain out of scope — this is the BILLING denylist, not a general
secret-leak allowlist.

When new billing-relevant key shapes appear, add them to
``FORBIDDEN_KEYS_EXPLICIT`` here.

Pattern is the explicit named set UNION the `*_API_KEY` suffix — denylist
not allowlist by design. Allowlist would be stronger for general-secret-leak
prevention but would break the moment `claude -p` (or any other subprocess)
needed an env var we didn't anticipate (PYTHONPATH, NPM_CONFIG_*, locale
vars, etc.). For the BILLING use case, denylist is correct.

Denylist composition has ONE deliberate hole: ``ALLOWED_KEYS_EXPLICIT``
(see the block beside it) re-admits keys that match a forbidden suffix but
are swarph-internal rather than provider credentials. Exemption wins.

The audit memory: anything ending in ``_API_KEY`` is force-popped UNLESS
explicitly exempted; the
explicit set catches keys that don't end in `_API_KEY` but ARE billing-
relevant. Composition: (explicit_set ∪ suffix_sweep) MINUS ALLOWED_KEYS_EXPLICIT.

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
    # Billing/auth REDIRECT vars — these flip `claude -p` (and the other
    # subscription CLIs) from subscription auth to a metered/relay endpoint
    # WITHOUT ending in `_API_KEY`, so the suffix rule alone missed them.
    # Adversarial-sweep CRIT 2026-06-01: ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN
    # set in the parent env (e.g. an identity-proxy / metered relay) silently
    # bills off-subscription while cost_usd still reports 0.0.
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "GEMINI_BASE_URL",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_PROJECT",
    "VERTEX_PROJECT",
    "VERTEX_LOCATION",
})

# ---------------------------------------------------------------------------
# EXEMPTIONS — keys that MATCH a forbidden suffix but must still be inherited.
#
# The denylist is a shape rule, so it cannot tell a PROVIDER credential from a
# swarph-internal one that happens to share the shape. Every entry here is a
# deliberate hole in the billing scrub and must name WHY the billing risk is
# accepted. Exemption wins over both the explicit set and the suffix sweep.
#
# SWARPH_SLM_API_KEY (#656/#424, 2026-09-16): the dreaming SLM client's key.
#   It names swarph's OWN small-language-model endpoint (SWARPH_SLM_ENDPOINT,
#   #578 — no host default), whose intended target is a local Ollama that needs
#   no key at all. Without this exemption a cell-hosted enrich fails SILENTLY
#   against any hosted OpenAI-compatible target: available() can read True off
#   an unauthenticated /v1/models, then every generate() 401s into the
#   per-session except and the run reports ZERO PROPOSALS rather than an error.
#   ACCEPTED RISK, stated plainly: if an operator points SWARPH_SLM_ENDPOINT at
#   a METERED provider, this key now reaches every spawned cell and that spend
#   is real. The endpoint is the thing that decides whether it costs money, and
#   the endpoint is operator-chosen. This exemption trades a silent failure for
#   a visible, operator-owned billing decision.
ALLOWED_KEYS_EXPLICIT = frozenset({
    "SWARPH_SLM_API_KEY",
})

# Suffix sweep beside the explicit set — catches provider-namespaced variants
# of the same billing/auth-redirect shapes (e.g. <X>_AUTH_TOKEN, <X>_BASE_URL).
FORBIDDEN_SUFFIXES = ("_API_KEY", "_AUTH_TOKEN", "_BASE_URL")


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
        if k in ALLOWED_KEYS_EXPLICIT
        or (k not in FORBIDDEN_KEYS_EXPLICIT and not k.endswith(FORBIDDEN_SUFFIXES))
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

    # The billing/auth-REDIRECT vars that MUST be scrubbed — not just the API
    # key but the AUTH_TOKEN/BASE_URL class that silently flips subscription
    # billing to a metered/relay endpoint (adversarial-sweep CRIT 2026-06-01).
    _MUST_SCRUB = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")
    _saved = {k: os.environ.get(k) for k in _MUST_SCRUB}
    try:
        for k in _MUST_SCRUB:
            os.environ[k] = "FAKE-FOR-TEST"
        scrubbed = scrub_env_for_subprocess()
        leaked = [k for k in _MUST_SCRUB if k in scrubbed]
        if leaked:
            raise RuntimeError(
                "swarph_shared.subprocess_env: scrub_env_for_subprocess did "
                f"not remove billing/auth-redirect var(s): {leaked}"
            )
        if "PATH" not in scrubbed:
            raise RuntimeError(
                "swarph_shared.subprocess_env: scrub_env_for_subprocess "
                "over-pruned (PATH missing)"
            )
    finally:
        for _k, _v in _saved.items():
            if _v is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _v

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
