"""swarph-shared — shared substrate primitives for the swarph-mesh ecosystem.

Five small, single-purpose modules consumed by every swarph-mesh component
(omega-boss, opus_subscription, swarph-mesh, swarph-cli) so the substrate
patterns stay consistent across producers:

  - caller_convention   — single source of truth for the dotted-slug caller
                          regex used in token_usage / subscription_usage
                          attribution joins
  - subprocess_env      — env scrubbing for `claude -p` (and any other
                          subprocess that must not inherit billing-path env
                          keys); paired with subscription-setup verification
  - json_mode           — JSON parsing harness with prose-extraction fallback
                          and retry-callback contract for vendor LLMs that
                          drift from strict-JSON output
  - peer_registry       — canonical peer-name resolution against the
                          mesh-gateway /peers endpoint; static KNOWN_ALIASES
                          drift table for observed contagion-aliases. Closes
                          the framing-contagion class observed in lab-claude
                          (Vector A) and drop (Vector B) incidents.
  - cell                — universal cell.yaml schema (substrate-doc R7
                          §11.1.5 (O5) cell.yaml universal-genome). Pure
                          dict→Cell parser + dataclasses; file I/O is
                          swarph-cli's concern at the operator-tooling
                          layer. Schema FROZEN at "v1" per drop-mother
                          review #890 (C2) discipline; v0.6 cell.yaml
                          files keep working unchanged in v0.7+.
  - llm_cost             — one price base (LiteLLM-sourced), one cost
                          function, for the whole mesh (card #426).
                          omega_llm and swarph-cli/bench each held a
                          separate price table; the hand-maintained one
                          silently lacked Anthropic's 1-hour cache tier and
                          understated cost by ~60% on that component.
                          Self-checking: reconcile() compares a computed
                          cost against claude -p's own reported costUSD for
                          the same call.

The package is **MIT-licensed** and **pure stdlib** — zero runtime deps. Same
pattern as phawkes / fisherrao / tailcor / diebold-yilmaz / hodgex
(Pierre Samson + Claude Opus authorship lineage).

For the rationale + integration patterns see README.md.
"""

from __future__ import annotations

from swarph_shared.caller_convention import (
    CALLER_PATTERN,
    validate_caller,
)
from swarph_shared.subprocess_env import (
    FORBIDDEN_KEYS_EXPLICIT,
    scrub_env_for_subprocess,
    verify_subscription_setup,
)
from swarph_shared.json_mode import (
    parse_json,
    parse_json_with_retry,
    build_retry_feedback_turn,
)
from swarph_shared.peer_registry import (
    KNOWN_ALIASES,
    NAMING_CONVENTION_REGEX,
    GatewayUnreachableError,
    MalformedPeerListError,
    NotInRegistry,
    canonical_names,
    is_registered,
    validate_node_name,
)
from swarph_shared.cell import (
    Cell,
    CellError,
    Lineage,
    PEER_NAME_RE,
    SCHEMA_VERSION_V1,
    VALID_PROVIDERS,
    VALID_SCHEMA_VERSIONS,
    parse_cell_dict,
    validate_uuid_str,
)
from swarph_shared.agent_isolation import (
    PROVIDER_AUTH,
    build_isolated_env,
    prepare_isolated_home,
)
from swarph_shared.untrusted_repo_preflight import (
    PreflightError,
    default_run_git,
    git_config_is_poisoned,
    preflight,
    safe_reader_flags,
)
from swarph_shared.llm_cost import (
    CostResult,
    PriceFetchError,
    PriceRow,
    ReconcileResult,
    TokenUsage,
    classify_source,
    compute_cost,
    fetch_price_base,
    reconcile,
    usage_from_claude_p_json,
)

__version__ = "0.8.1"

__all__ = [
    "__version__",
    # caller_convention
    "CALLER_PATTERN",
    "validate_caller",
    # subprocess_env
    "FORBIDDEN_KEYS_EXPLICIT",
    "scrub_env_for_subprocess",
    "verify_subscription_setup",
    # agent_isolation
    "PROVIDER_AUTH",
    "build_isolated_env",
    "prepare_isolated_home",
    # json_mode
    "parse_json",
    "parse_json_with_retry",
    "build_retry_feedback_turn",
    # peer_registry
    "KNOWN_ALIASES",
    "NAMING_CONVENTION_REGEX",
    "GatewayUnreachableError",
    "MalformedPeerListError",
    "NotInRegistry",
    "canonical_names",
    "is_registered",
    "validate_node_name",
    # cell (v0.3.0 — substrate-doc R7 §11.1.5 (O5) relocation)
    "Cell",
    "CellError",
    "Lineage",
    "PEER_NAME_RE",
    "SCHEMA_VERSION_V1",
    "VALID_PROVIDERS",
    "VALID_SCHEMA_VERSIONS",
    "parse_cell_dict",
    "validate_uuid_str",
    # untrusted_repo_preflight (v0.5.0 — SWAIRM Pattern #3 port; INERT)
    "PreflightError",
    "default_run_git",
    "git_config_is_poisoned",
    "preflight",
    "safe_reader_flags",
    # llm_cost (v0.8.0 — card #426, one price base + one cost function)
    "CostResult",
    "PriceFetchError",
    "PriceRow",
    "ReconcileResult",
    "TokenUsage",
    "classify_source",
    "compute_cost",
    "fetch_price_base",
    "reconcile",
    "usage_from_claude_p_json",
]
