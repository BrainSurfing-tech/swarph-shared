# swarph-shared

Four small, dependency-free substrate primitives for building multi-LLM mesh tooling — the bottom of the [`swarph-mesh`](https://github.com/darw007d/swarph-mesh) dependency stack. Each solves one sharp problem that bites anyone wiring multiple LLM providers together:

- **`caller_convention`** — a single source of truth for the dotted-slug caller-id regex, so usage-attribution joins across billing paths never break silently when a second producer starts writing tagged rows
- **`subprocess_env`** — env scrubbing for `claude -p` (and any subprocess that must not inherit billing-path keys), so a stray `ANTHROPIC_API_KEY` can't silently flip a subscription call onto metered API billing
- **`json_mode`** — a JSON-parsing harness with prose-extraction fallback and a retry-callback contract, for vendor LLMs that drift from strict-JSON output
- **`peer_registry`** — canonical peer-name resolution against a mesh-gateway `/peers` endpoint, with TTL cache, alias drift-mapping, and graceful gateway-unreachable degradation, so a nickname for a peer can't silently fork into two identities

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
validate_caller("agent.judge.claude.r2")    # OK
validate_caller("orchestrator.main")        # OK
validate_caller("Agent.Judge")              # raises ValueError
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
canonical = validate_node_name("db")             # → "database-node" (warns: alias resolved)
canonical = validate_node_name("database-node")  # → "database-node"
canonical = validate_node_name("ghost-peer")     # raises NotInRegistry

# Offline-tolerant for test fixtures or air-gapped dev
canonical = validate_node_name("database-node", strict=False)   # skips registry check on gateway-down

# Soft check — never raises
from swarph_shared import is_registered
if is_registered("db"):
    send_dm(to="database-node", ...)   # the canonical resolution happens above
```

The convention for `KNOWN_ALIASES` is `{alias: canonical}` — the dict resolves FROM observed drift-aliases TO registry names. **Direction matters**: reversing an entry would silently propagate the drift instead of correcting it, so each entry is anti-regression-tested.

Two ways a peer-name nickname leaks in and forks an identity, both motivating the per-send check:

- **Onboarding chatter.** A new peer's first messages introduce a non-canonical name; receiving peers absorb it. Catchable at peer-introduction.
- **Human shorthand.** A person uses a friendly nickname for a peer while talking to an AI agent; the agent absorbs it without a registry check. NOT catchable at introduction — it **requires per-send verification** at the adapter boundary.

Gateway-unreachable handling is two-tier:

1. Cached `canonical_names` from a prior successful query within `CACHE_GRACE_SECONDS` (1h) — return the stale set with a loud warning.
2. No usable cache + gateway down — raise `GatewayUnreachableError`. Fail-loud, not fail-silent; never default to "registry-not-checked" without explicit `strict=False`.

## Why these four

Each primitive is here because the failure it prevents is silent — it doesn't throw, it just quietly produces wrong data or wrong billing until someone notices much later:

- **`caller_convention`** — usage-attribution joins break the moment a second producer writes caller-tagged rows in a slightly different shape. Lock the shape once; validate at every write.
- **`subprocess_env`** — a subprocess that inherits the parent's full env can silently flip `claude -p` from subscription auth onto metered API the instant an `ANTHROPIC_API_KEY` shows up in the process environment. Scrub the billing-path keys; fail loud if subscription auth is broken.
- **`json_mode.parse_json_with_retry`** — retry-on-bad-JSON is easy to get subtly wrong: concatenating the feedback onto the original prompt drifts multi-turn semantics. The harness enforces appending feedback as a *new* user turn instead.
- **`peer_registry`** — a peer addressed by two names is two identities as far as routing and audit are concerned. Resolve every name to canonical before it's used.

These four are the bottom of the swarph-mesh dependency stack — every higher-layer adapter (Claude / GPT / Gemini / DeepSeek / Grok) imports from here so substrate behavior stays consistent across providers.

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
