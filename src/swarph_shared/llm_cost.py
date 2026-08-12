"""llm_cost — one price base, one cost function, for the whole mesh.

Card #426 (PROVEN project 14): omega_llm/estimate.py hand-maintains a price
tier table; swarph-cli/bench/prices.py auto-refreshes an equivalent table
from LiteLLM's ``model_prices_and_context_window.json``. Both exist, they
mostly agree, and the hand-maintained one is ALREADY WRONG: it has no field
for Anthropic's 1-hour prompt-cache tier
(``cache_creation_input_token_cost_above_1hr``), so on a mesh that runs
1-hour caches it understates cache-creation cost by ~60% (measured
2026-08-12: $6.25/Mtok charged vs $10.00/Mtok actual). Writing a SECOND cost
function into omega_llm and swarph-shared separately mints a THIRD divergent
pair on the day this one was found — while ``proven.packs.port_link`` exists
specifically to catch that class. So: this is the one place. Consumers
(omega_llm, swarph-cli/bench) should call into this rather than carry their
own tables.

THE SELF-CHECKING PROPERTY, which is why this module is worth having:
``claude -p --output-format json`` returns the vendor's OWN computed price
per call, in ``modelUsage[model].costUSD``. That is an independent second
source for the same tokens. ``reconcile()`` prices the same usage from the
table and compares — the two agree to within a few percent when the model
and the cache-TTL tier are both right, and diverge sharply when either is
wrong (a bad model guess, or ignoring the 1h/5m split). Every consumer of
this module gets that check for free by passing the vendor figure through.

THE TWO INPUT BLOCKS ARE COMPLEMENTARY, NOT REDUNDANT (the trap that will
bite any second implementation): a `claude -p` JSON response carries the
1h/5m cache split in ``usage.cache_creation`` — NOT keyed by model — and the
model identity + vendor cost in ``modelUsage[model]`` — whose
``cacheCreationInputTokens`` is a FLAT total with NO ttl split. Read only
the first and you cannot price it (no model to look up rates for); read
only the second and you get the tier wrong. ``usage_from_claude_p_json()``
reads both and refuses to guess a tier it was not given.

Public surface
===============
    fetch_price_base(...)      -> dict[model_id, PriceRow], TTL-cached, LiteLLM-sourced
    PriceRow                   -> per-token rates, incl. the above_1hr tier
    TokenUsage                 -> the six numbers a cost figure is built from
    compute_cost(usage, rates) -> CostResult (never estimates a rate it does not have)
    reconcile(usage, rates, vendor_cost_usd) -> ReconcileResult
    usage_from_claude_p_json(payload) -> (model_id, TokenUsage, vendor_cost_usd | None)
    classify_source(...)       -> "subscription" | "metered" | "unknown", from the ROUTE
                                   never from whether a price happened to be > 0 (that flag
                                   breaks the moment a subscription path starts reporting a
                                   real notional cost — see swarph-cli/claude-service issue #13)
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

LITELLM_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
CACHE_PATH = Path(
    os.environ.get("SWARPH_LLM_COST_CACHE", str(Path.home() / ".cache/swarph/llm_price_base.json"))
)
CACHE_TTL_SECONDS = 6 * 3600  # bench refreshes weekly; this is a per-process safety net, not the source of truth


class PriceFetchError(Exception):
    """Raised only when there is neither a live fetch nor any usable cache."""


@dataclass(frozen=True)
class PriceRow:
    """Per-token rates for one model, in USD/token (LiteLLM's own unit)."""

    input: float
    output: float
    cache_read: Optional[float] = None
    cache_creation: Optional[float] = None
    cache_creation_above_1hr: Optional[float] = None  # the field omega's table lacks


@dataclass(frozen=True)
class TokenUsage:
    """The six numbers a cost figure is built from. ``ephemeral_1h``/``ephemeral_5m``
    come from ``usage.cache_creation`` in a claude -p response; a flat
    ``cache_creation`` total with no split is NOT accepted here (see module
    docstring) — callers must resolve the split or use ``cache_creation_unsplit``
    explicitly, which prices at the 5m (cheaper, conservative) rate and is
    flagged in the result as an underestimate.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_1h_tokens: int = 0
    cache_creation_5m_tokens: int = 0
    cache_creation_unsplit_tokens: int = 0  # escape hatch; see class docstring


@dataclass(frozen=True)
class CostResult:
    cost_usd: float
    breakdown: dict = field(default_factory=dict)
    underestimated: bool = False  # True if cache_creation_unsplit_tokens was priced at the 5m rate
    missing_rates: tuple = ()  # rate fields the model's PriceRow did not have; those terms priced as 0


@dataclass(frozen=True)
class ReconcileResult:
    computed_usd: float
    vendor_usd: float
    ratio: float  # vendor / computed; ~1.0 means the table + model + tier are all right
    agrees: bool  # within tolerance


def _fetch_live() -> dict:
    req = urllib.request.Request(LITELLM_URL, headers={"User-Agent": "swarph-shared/llm_cost"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def _row_from_raw(raw: dict) -> PriceRow:
    return PriceRow(
        input=float(raw.get("input_cost_per_token") or 0.0),
        output=float(raw.get("output_cost_per_token") or 0.0),
        cache_read=raw.get("cache_read_input_token_cost"),
        cache_creation=raw.get("cache_creation_input_token_cost"),
        cache_creation_above_1hr=raw.get("cache_creation_input_token_cost_above_1hr"),
    )


def fetch_price_base(*, force_refresh: bool = False) -> dict:
    """model_id -> PriceRow. Live-fetches LiteLLM's base, TTL-caches to disk,
    falls back to a stale cache on any network failure (never raises for a
    transient outage — matches peer_registry's gateway-unreachable posture).
    Raises PriceFetchError only if there is no live fetch AND no cache at all.
    """
    if not force_refresh and CACHE_PATH.exists():
        age = time.time() - CACHE_PATH.stat().st_mtime
        if age < CACHE_TTL_SECONDS:
            try:
                raw = json.loads(CACHE_PATH.read_text())
                return {k: _row_from_raw(v) for k, v in raw.items() if isinstance(v, dict)}
            except Exception:
                pass  # corrupt cache: fall through to a live fetch

    try:
        raw = _fetch_live()
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        # ValueError catches json.JSONDecodeError too: a 200 with a malformed
        # body (rate-limit HTML page, truncated response) is a transient-outage
        # shape, not a code bug, and must degrade to the stale cache the same
        # way a connection failure does — a review finding on the first cut,
        # which caught this contradicting the docstring's own "never raises"
        # promise.
        if CACHE_PATH.exists():
            try:
                raw = json.loads(CACHE_PATH.read_text())
                return {k: _row_from_raw(v) for k, v in raw.items() if isinstance(v, dict)}
            except Exception:
                pass
        raise PriceFetchError("no live fetch and no usable cache — cannot price anything")

    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        # tempfile -> os.replace, matching this ecosystem's .env write discipline
        # elsewhere: two concurrent fetch_price_base() calls refreshing at once
        # must not be able to interleave writes into a corrupt cache file. A
        # partial write is self-healing today (the next read's broad `except
        # Exception: pass` falls through to a live refetch), but a torn write
        # under load is exactly the thundering-herd shape this avoids for free.
        tmp = CACHE_PATH.with_suffix(CACHE_PATH.suffix + f".tmp.{os.getpid()}")
        try:
            tmp.write_text(json.dumps(raw))
            os.replace(tmp, CACHE_PATH)
        finally:
            # If replace succeeded, tmp no longer exists at this path (renamed
            # onto CACHE_PATH) and this is a no-op. If write_text or replace
            # raised (rare: cross-device, permission change mid-run), clean up
            # the orphan rather than leave a .tmp.<pid> file on disk forever —
            # review finding, optional/non-blocking (pricing itself degrades
            # fine either way; this is disk hygiene, not correctness).
            tmp.unlink(missing_ok=True)
    except OSError:
        pass  # caching is an optimisation; a write failure must not break pricing
    return {k: _row_from_raw(v) for k, v in raw.items() if isinstance(v, dict)}


def compute_cost(usage: TokenUsage, rates: PriceRow) -> CostResult:
    """Pure function: never fetches, never estimates a rate it was not given.
    A term whose rate is absent from ``rates`` prices as 0 and is named in
    ``missing_rates`` — never silently dropped, never guessed.
    """
    breakdown: dict = {}
    missing: list = []

    def term(name: str, tokens: int, rate: Optional[float]) -> float:
        if tokens <= 0:
            return 0.0
        if rate is None:
            missing.append(name)
            return 0.0
        v = tokens * rate
        breakdown[name] = v
        return v

    total = 0.0
    total += term("input", usage.input_tokens, rates.input)
    total += term("output", usage.output_tokens, rates.output)
    total += term("cache_read", usage.cache_read_tokens, rates.cache_read)
    total += term("cache_creation_1h", usage.cache_creation_1h_tokens, rates.cache_creation_above_1hr)
    total += term("cache_creation_5m", usage.cache_creation_5m_tokens, rates.cache_creation)

    underestimated = False
    if usage.cache_creation_unsplit_tokens > 0:
        # No split given: price at the CHEAPER (5m) rate, so the result is a
        # documented UNDERESTIMATE rather than a guessed-expensive overestimate.
        # A caller that cares must resolve the split (it is available — see
        # module docstring) rather than rely on this fallback.
        total += term("cache_creation_unsplit_at_5m_rate", usage.cache_creation_unsplit_tokens, rates.cache_creation)
        underestimated = True

    return CostResult(cost_usd=total, breakdown=breakdown, underestimated=underestimated, missing_rates=tuple(missing))


def reconcile(usage: TokenUsage, rates: PriceRow, vendor_usd: float, *, tolerance: float = 0.05) -> ReconcileResult:
    """Compare a table-derived cost against the vendor's own figure for the
    same call (``modelUsage[model].costUSD`` from claude -p). This is the
    free oracle: agreement validates model + tier selection; disagreement
    names a defect (wrong model, unsplit cache treated as split, stale rates).
    """
    computed = compute_cost(usage, rates).cost_usd
    if computed <= 0:
        # A genuinely free call (both sides legitimately zero) agrees; a zero
        # table-derived cost against a NON-zero vendor figure is the real
        # disagreement (missing rates, or usage that should have priced but
        # didn't) and must still be flagged. Conflating the two — reported as
        # disagrees=True unconditionally on the review's first pass — hid the
        # one case where "no cost" is the correct, reconciled answer.
        agrees = vendor_usd == 0.0
        return ReconcileResult(computed_usd=computed, vendor_usd=vendor_usd,
                               ratio=(1.0 if agrees else float("inf")), agrees=agrees)
    ratio = vendor_usd / computed
    return ReconcileResult(computed_usd=computed, vendor_usd=vendor_usd, ratio=ratio, agrees=abs(ratio - 1.0) <= tolerance)


def usage_from_claude_p_json(payload: dict) -> tuple:
    """Extract (model_id, TokenUsage, vendor_cost_usd) from a
    ``claude -p --output-format json`` response. Reads BOTH ``usage`` (for
    the cache-TTL split) and ``modelUsage`` (for model identity + vendor
    cost) — see module docstring for why neither block alone suffices.
    Returns (None, None, None) if modelUsage is absent or empty (nothing to
    attribute the usage to) rather than guessing a model.
    """
    model_usage = payload.get("modelUsage") or {}
    if not model_usage:
        return None, None, None
    # First (only, in practice) model entry — claude -p reports one model per call today.
    model_id, mu = next(iter(model_usage.items()))

    u = payload.get("usage") or {}
    cc = u.get("cache_creation") or {}
    has_split = "ephemeral_1h_input_tokens" in cc or "ephemeral_5m_input_tokens" in cc

    if has_split:
        usage = TokenUsage(
            input_tokens=int(mu.get("inputTokens") or 0),
            output_tokens=int(mu.get("outputTokens") or 0),
            cache_read_tokens=int(mu.get("cacheReadInputTokens") or 0),
            cache_creation_1h_tokens=int(cc.get("ephemeral_1h_input_tokens") or 0),
            cache_creation_5m_tokens=int(cc.get("ephemeral_5m_input_tokens") or 0),
        )
    else:
        # No split visible (older CLI, or a non-claude-p caller): fall back to
        # modelUsage's flat total, priced conservatively — see compute_cost.
        usage = TokenUsage(
            input_tokens=int(mu.get("inputTokens") or 0),
            output_tokens=int(mu.get("outputTokens") or 0),
            cache_read_tokens=int(mu.get("cacheReadInputTokens") or 0),
            cache_creation_unsplit_tokens=int(mu.get("cacheCreationInputTokens") or 0),
        )

    vendor_cost = mu.get("costUSD")
    return model_id, usage, (float(vendor_cost) if vendor_cost is not None else None)


# Hosts/markers that identify the flat-rate subscription route (claude-service's
# OpenAI-compatible shim). Matched against whatever base URL is visible to the
# caller. SOURCE IS DERIVED FROM THE ROUTE, NEVER FROM PRICE — a call over the
# subscription route is flat-rate whether or not a notional price is attached;
# pricing it (via this module) is for COMPARISON, never for a spend column.
SUBSCRIPTION_MARKERS = (":8787", "claude-service")


def classify_source(*, api_base: str = "") -> str:
    """"subscription" | "metered" | "unknown" — from the ROUTE the call took.

    Never from whether a cost figure happened to be > 0. That rule broke the
    moment claude-service's shim was fixed to report a real notional cost
    (swarph-cli/claude-service #13): every subscription call would silently
    reclassify as metered, inflating the metered aggregate by exactly the
    €0 lane's volume. Reported as "unknown" rather than guessed when no
    route is visible — a determinate wrong label in a spend ledger is worse
    than an honest gap.
    """
    base = str(api_base or "")
    if any(m in base for m in SUBSCRIPTION_MARKERS):
        return "subscription"
    return "metered" if base else "unknown"
