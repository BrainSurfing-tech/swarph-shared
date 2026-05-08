# swarph-shared

Shared substrate primitives for the [swarph-mesh](https://github.com/darw007d/hedge-fund-mcp/blob/main/research/swarph_cli/PLAN.md) ecosystem. Four small, single-purpose modules consumed by every swarph-mesh component:

- **`caller_convention`** — single source of truth for the dotted-slug caller regex used in cross-billing-path attribution joins (`token_usage` ⋈ `subscription_usage`)
- **`subprocess_env`** — env scrubbing for `claude -p` (and any subprocess that must not inherit billing-path env keys); paired with subscription-setup verification
- **`json_mode`** — JSON parsing harness with prose-extraction fallback and retry-callback contract for vendor LLMs that drift from strict-JSON output
- **`peer_registry`** — canonical peer-name resolution against the mesh-gateway `/peers` endpoint, with TTL cache, static drift-mapping (`KNOWN_ALIASES`), and graceful gateway-unreachable degradation. Closes the framing-contagion class observed in `lab-claude → lab-ovh` (peer-onboarding chatter, Vector A) and `drop → droplet` (human-prompt shorthand, Vector B) incidents.

MIT-licensed, **pure stdlib** (zero runtime deps), Python 3.10+. Same authorship lineage as `phawkes` / `fisherrao` / `tailcor` / `diebold-yilmaz` / `hodgex` — Pierre Samson + Claude Opus.

## Install

```bash
pip install swarph-shared
```

## Usage

### caller_convention — validate caller tags before attribution writes

```python
from swarph_shared import validate_caller

# Locked convention: dotted slug, role-prefix, lowercase
validate_caller("council.judge.claude.r2")  # OK
validate_caller("orchestrator.boss")        # OK
validate_caller("Council.Judge")            # raises ValueError
validate_caller("flat_slug")                # raises ValueError (no dot)
```

Use this at every public surface that accepts a `caller` parameter. Defense-in-depth — every producer should validate, not trust upstream.

### subprocess_env — keep `claude -p` on subscription billing

```python
import subprocess
from swarph_shared import scrub_env_for_subprocess, verify_subscription_setup

# ONCE at boot — fail loud if subscription auth is broken
verify_subscription_setup()

# Each subprocess invocation
proc = subprocess.run(
    ["claude", "-p", "explain X", "--model", "claude-opus-4-7"],
    env={**scrub_env_for_subprocess(), "IS_SANDBOX": "1"},
    capture_output=True,
    text=True,
    timeout=120,
)
```

**Scope (READ THIS)**: the denylist guards the BILLING PATH only — keeping `claude -p` on subscription auth (`~/.claude/.credentials.json`) instead of metered API. It does NOT cover general secret-leakage. Future auth-token shapes (`*_TOKEN`, `*_SECRET`, `OAUTH_*`, etc.) are NOT caught here — those are a separate-concern allowlist if/when added.

When new billing-relevant key shapes appear, add them to `FORBIDDEN_KEYS_EXPLICIT` in `subprocess_env.py`.

### json_mode — handle vendor-drift in JSON-mode responses

```python
from swarph_shared import parse_json, parse_json_with_retry

# Best-effort parse with prose-extraction fallback
parsed, err = parse_json('Sure, here is: {"a": 1} thanks!')
# → ({"a": 1}, None)

# Retry-on-failure harness — caller plugs in their LLM via callback
def on_retry(feedback_str: str) -> str:
    """Append feedback as a NEW [USER] turn, re-call LLM, return new text."""
    messages.append({"role": "user", "content": feedback_str})
    response = llm.invoke(messages)
    return response.text

parsed, err_class = parse_json_with_retry(initial_text, on_retry=on_retry)
# err_class is one of: None | "malformed_json" | "retry_failed"
```

**The [USER]-turn invariant**: the `on_retry` callback receives the feedback string and is responsible for appending it as a NEW user turn (not concatenating to the previous prompt). This multi-turn-preserving pattern is the canonical retry shape — concatenation drifts model context, new turn preserves the actual conversation.

### peer_registry — validate canonical peer names before mesh DMs

```python
from swarph_shared import validate_node_name, NotInRegistry, GatewayUnreachableError

# Resolve aliases + verify against gateway registry
canonical = validate_node_name("drop")           # → "droplet" (warns: alias resolved)
canonical = validate_node_name("droplet")        # → "droplet"
canonical = validate_node_name("ghost-peer")     # raises NotInRegistry

# Offline-tolerant for test fixtures or air-gapped dev
canonical = validate_node_name("droplet", strict=False)   # skips registry check on gateway-down

# Soft check — never raises
from swarph_shared import is_registered
if is_registered("drop"):
    send_dm(to="droplet", ...)   # the canonical resolution happens above
```

The convention for `KNOWN_ALIASES` is `{alias: canonical}` — the dict resolves FROM observed contagion-aliases TO registry names. **Direction matters**: writing `{"droplet": "drop"}` would silently propagate the contagion. There's a unit test (`test_known_aliases_direction_is_alias_to_canonical`) that anti-regression-pins this.

Two contagion vectors motivate the per-send registry check:

- **Vector A — peer-onboarding chatter.** A new peer's first DMs introduce non-canonical references; receiving peers absorb. Auditable at peer-introduction. Example: `lab-claude → lab-ovh`.
- **Vector B — human-prompt shorthand.** A human uses a friendly nickname for a peer in conversation with an AI peer; the AI absorbs without registry check. NOT auditable at peer-introduction; **requires per-send registry verification** at the adapter boundary. Example: `drop → droplet`.

Gateway-unreachable handling is two-tier per drop's PR #650 review:

1. Cached `canonical_names` from prior successful query within `CACHE_GRACE_SECONDS` (1h) — return stale set with loud warning.
2. No usable cache + gateway down — raise `GatewayUnreachableError`. Fail-loud-not-fail-silent; never default to "registry-not-checked" without explicit `strict=False`.

## Audit-memory rationale

This package extracts patterns that emerged in cross-Claude review of the OMEGA hedge-fund-mcp codebase (lab-OVH + droplet, 2026-04 → 2026-05). The patterns matter beyond the OMEGA scope:

- **`caller_convention`** keeps cross-billing-path attribution joins from breaking silently when a second producer adds caller-tagged rows. Originally locked in DM thread #586/#590/#592/#593 (lab+drop convergence).
- **`subprocess_env`** prevents the silent billing-flip that the `evolution_tracker.jsonl ev_an040i00v` immune-catch documented (lab-orchestrator/orchestrator.py:354 had the wholesale-passthrough bug that would silently flip `claude -p` from subscription to API metered the moment `ANTHROPIC_API_KEY` entered the daemon process env).
- **`json_mode.parse_json_with_retry`** locks the [USER]-turn retry pattern from PR #125 nit (concatenating retry feedback to the prompt drifts multi-turn semantics; appending as a new user turn preserves them).
- **`peer_registry`** captures the framing-contagion class surfaced in two distinct incidents on 2026-05-04/05 (lab-claude, Vector A) and 2026-05-08 (drop, Vector B). The KNOWN_ALIASES dict's worked examples are documentation AND code — each entry is anti-regression-tested so the direction can never be reversed silently. The TTL-cached gateway query + graceful-degradation fallback are drop's PR #650 review carry-forwards.

These four primitives are the bottom of the swarph-mesh dependency stack — every higher-layer adapter (Gemini / DeepSeek / Claude / OpenAI / Grok) imports from here so the substrate behavior stays consistent across providers.

## Versioning + dev

```bash
git clone https://github.com/darw007d/swarph-shared
cd swarph-shared
python -m venv venv && source venv/bin/activate
pip install -e ".[dev]"
pytest
```

Versioning follows the same shape as the other Samson+Claude libraries: `0.1.0` initial, semver from there. Breaking changes in any of the three modules require a major bump because consumer adapters depend on the API shape.

## License

MIT. Pierre Samson + Claude Opus, 2026.
