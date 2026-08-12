"""Tests for swarph_shared.llm_cost.

Card #426: the arithmetic and field mapping were fully measured before this
was written (real claude -p calls on lab-ovh, 2026-08-12), so the anchor
tests below use FROZEN REAL FIXTURES rather than invented numbers — the
reconciliation test in particular is the actual call that first exposed the
36% understatement in omega_llm's hand-maintained table, reproduced here as
a permanent regression check with a real vendor cost to compare against.
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from swarph_shared.llm_cost import (
    PriceRow,
    TokenUsage,
    classify_source,
    compute_cost,
    fetch_price_base,
    reconcile,
    usage_from_claude_p_json,
)

# claude-opus-5's real LiteLLM rates as of 2026-08-12 (USD/token). Frozen here
# so these tests never depend on network access or a moving price table.
OPUS_5_RATES = PriceRow(
    input=5e-06,
    output=2.5e-05,
    cache_read=5e-07,
    cache_creation=6.25e-06,
    cache_creation_above_1hr=1e-05,
)


# ---------------------------------------------------------------------------
# compute_cost: never estimates a rate it was not given
# ---------------------------------------------------------------------------

def test_compute_cost_basic_terms():
    u = TokenUsage(input_tokens=1000, output_tokens=200, cache_read_tokens=500)
    r = compute_cost(u, OPUS_5_RATES)
    expected = 1000 * 5e-06 + 200 * 2.5e-05 + 500 * 5e-07
    assert r.cost_usd == pytest.approx(expected)
    assert r.missing_rates == ()
    assert not r.underestimated


def test_compute_cost_missing_rate_is_named_not_guessed():
    """A model whose PriceRow lacks cache_read must report the gap by name,
    never price the term as free and never raise."""
    bare = PriceRow(input=1e-06, output=2e-06)  # no cache fields at all
    u = TokenUsage(input_tokens=10, cache_read_tokens=999)
    r = compute_cost(u, bare)
    assert "cache_read" in r.missing_rates
    assert r.cost_usd == pytest.approx(10 * 1e-06)  # cache_read priced as 0, not dropped silently from the total either way


def test_compute_cost_unsplit_cache_creation_is_flagged_underestimate():
    """The escape hatch for a flat cache_creation total (no 1h/5m split
    visible) must price at the CHEAPER rate and say so — never guess expensive."""
    u = TokenUsage(cache_creation_unsplit_tokens=1000)
    r = compute_cost(u, OPUS_5_RATES)
    assert r.underestimated is True
    assert r.cost_usd == pytest.approx(1000 * OPUS_5_RATES.cache_creation)  # the 5m (cheap) rate, not above_1hr


def test_compute_cost_zero_usage_is_zero_not_an_error():
    r = compute_cost(TokenUsage(), OPUS_5_RATES)
    assert r.cost_usd == 0.0
    assert r.missing_rates == ()


# ---------------------------------------------------------------------------
# THE RECONCILIATION — the free oracle, exercised against a REAL frozen call
# ---------------------------------------------------------------------------

def test_reconcile_real_call_agrees_when_tier_is_correct():
    """The actual call that found the 36% gap: claude -p, model claude-opus-5,
    lab-ovh, 2026-08-12. Vendor-reported costUSD = 0.41508. Pricing with the
    correct 1h-cache tier must reconcile close to 1.00x."""
    usage = TokenUsage(
        input_tokens=2,
        output_tokens=6,
        cache_read_tokens=23720,
        cache_creation_1h_tokens=40306,
        cache_creation_5m_tokens=0,
    )
    result = reconcile(usage, OPUS_5_RATES, vendor_usd=0.41508, tolerance=0.02)
    assert result.agrees, f"ratio was {result.ratio:.3f}, expected ~1.0"
    assert result.computed_usd == pytest.approx(0.41508, rel=0.02)


def test_reconcile_flags_disagreement_when_1hr_tier_is_ignored():
    """THE REGRESSION THIS MODULE EXISTS TO PREVENT: pricing the same real
    call's cache-creation tokens at the STANDARD (5m) rate instead of the
    above_1hr rate — which is exactly what omega_llm's hand-maintained table
    does today, because it has no above_1hr field at all. Must NOT reconcile."""
    usage_wrongly_unsplit = TokenUsage(
        input_tokens=2,
        output_tokens=6,
        cache_read_tokens=23720,
        cache_creation_5m_tokens=40306,  # the bug: 1h-tier tokens priced at the 5m rate
    )
    result = reconcile(usage_wrongly_unsplit, OPUS_5_RATES, vendor_usd=0.41508, tolerance=0.02)
    assert not result.agrees
    assert result.ratio > 1.3  # vendor charged materially more than the mispriced total


def test_reconcile_computed_zero_never_divides_by_zero():
    result = reconcile(TokenUsage(), OPUS_5_RATES, vendor_usd=0.5)
    assert result.agrees is False
    assert result.computed_usd == 0.0


def test_reconcile_both_sides_genuinely_zero_agrees():
    """Review finding #4: a legitimately free call (nothing priced, vendor
    also reports 0.0) must reconcile as agreeing — the prior version marked
    EVERY zero-computed call as disagreeing regardless of the vendor figure,
    which hid the one case where 'no cost' is the correct answer."""
    result = reconcile(TokenUsage(), OPUS_5_RATES, vendor_usd=0.0)
    assert result.agrees is True
    assert result.ratio == 1.0


def test_reconcile_zero_computed_nonzero_vendor_still_disagrees():
    """The real defect finding #4 must not accidentally paper over: zero
    table-derived cost against a NON-zero vendor figure (missing rates, or
    usage that should have priced) is a genuine disagreement."""
    result = reconcile(TokenUsage(), OPUS_5_RATES, vendor_usd=0.5)
    assert result.agrees is False


# ---------------------------------------------------------------------------
# usage_from_claude_p_json: the two-block extraction, and its refusal shape
# ---------------------------------------------------------------------------

# The literal response shape observed from `claude -p --output-format json`
# on lab-ovh, 2026-08-12 (values redacted-equivalent, structure real).
REAL_CLAUDE_P_PAYLOAD = {
    "usage": {
        "cache_creation": {"ephemeral_1h_input_tokens": 40306, "ephemeral_5m_input_tokens": 0},
    },
    "modelUsage": {
        "claude-opus-5[1m]": {
            "inputTokens": 2,
            "outputTokens": 6,
            "cacheReadInputTokens": 23720,
            "cacheCreationInputTokens": 40306,
            "costUSD": 0.41508,
            "canonicalModel": "claude-opus-5",
            "provider": "firstParty",
        }
    },
}


def test_usage_from_claude_p_json_reads_both_blocks():
    model_id, usage, vendor_cost = usage_from_claude_p_json(REAL_CLAUDE_P_PAYLOAD)
    assert model_id == "claude-opus-5[1m]"
    assert usage.cache_creation_1h_tokens == 40306
    assert usage.cache_creation_unsplit_tokens == 0  # split WAS available; unsplit path unused
    assert vendor_cost == pytest.approx(0.41508)


def test_usage_from_claude_p_json_falls_back_when_split_absent():
    """An older CLI or non-claude-p caller might report modelUsage with no
    cache_creation split. Must fall back to the flat/unsplit field rather
    than crash or silently drop the tokens."""
    payload = {
        "usage": {},  # no cache_creation block at all
        "modelUsage": {"claude-sonnet-4-6": {"inputTokens": 5, "cacheCreationInputTokens": 100, "costUSD": 0.01}},
    }
    model_id, usage, vendor_cost = usage_from_claude_p_json(payload)
    assert model_id == "claude-sonnet-4-6"
    assert usage.cache_creation_unsplit_tokens == 100
    assert usage.cache_creation_1h_tokens == 0


def test_usage_from_claude_p_json_refuses_to_guess_a_model():
    """No modelUsage block -> (None, None, None), never an invented model."""
    model_id, usage, vendor_cost = usage_from_claude_p_json({"usage": {}, "modelUsage": {}})
    assert model_id is None and usage is None and vendor_cost is None


# ---------------------------------------------------------------------------
# classify_source: route, never price
# ---------------------------------------------------------------------------

def test_classify_source_subscription_route():
    assert classify_source(api_base="http://127.0.0.1:8787/v1") == "subscription"
    assert classify_source(api_base="https://claude-service.internal/v1") == "subscription"


def test_classify_source_metered_route():
    assert classify_source(api_base="https://api.anthropic.com") == "metered"


def test_classify_source_unknown_when_no_route_visible():
    """No base URL at all -> 'unknown', never a guessed label. This is the
    fix for the #13 regression: source must never be inferred from whether a
    cost figure is > 0, because a fixed shim will start reporting real costs
    on the subscription route too."""
    assert classify_source() == "unknown"
    assert classify_source(api_base="") == "unknown"


def test_classify_source_is_not_fooled_by_a_priced_subscription_call():
    """The actual #13 regression, expressed as a contract: pass the
    subscription route with an arbitrarily large notional price attached —
    classify_source must not even look at price, because it doesn't take one."""
    # classify_source's signature has no price parameter at all — this test
    # documents that as intentional, not an oversight.
    import inspect
    sig = inspect.signature(classify_source)
    assert "cost" not in sig.parameters and "price" not in sig.parameters


# ---------------------------------------------------------------------------
# fetch_price_base: TTL cache + graceful degradation (network mocked)
# ---------------------------------------------------------------------------

def _mock_response(payload: dict) -> MagicMock:
    body = json.dumps(payload).encode()
    mock = MagicMock()
    mock.read.return_value = body
    mock.__enter__.return_value = mock
    mock.__exit__.return_value = False
    return mock


def test_fetch_price_base_parses_live_payload(tmp_path, monkeypatch):
    monkeypatch.setattr("swarph_shared.llm_cost.CACHE_PATH", tmp_path / "prices.json")
    payload = {
        "claude-opus-5": {
            "input_cost_per_token": 5e-06,
            "output_cost_per_token": 2.5e-05,
            "cache_read_input_token_cost": 5e-07,
            "cache_creation_input_token_cost": 6.25e-06,
            "cache_creation_input_token_cost_above_1hr": 1e-05,
        },
        "some-other-field-that-is-not-a-model": "ignored",
    }
    with patch("swarph_shared.llm_cost._fetch_live", return_value=payload):
        base = fetch_price_base(force_refresh=True)
    assert base["claude-opus-5"].cache_creation_above_1hr == 1e-05


def test_fetch_price_base_falls_back_to_stale_cache_on_network_failure(tmp_path, monkeypatch):
    cache_file = tmp_path / "prices.json"
    cache_file.write_text(json.dumps({"claude-opus-5": {"input_cost_per_token": 1e-06, "output_cost_per_token": 1e-06}}))
    monkeypatch.setattr("swarph_shared.llm_cost.CACHE_PATH", cache_file)
    with patch("swarph_shared.llm_cost._fetch_live", side_effect=OSError("network down")):
        base = fetch_price_base(force_refresh=True)
    assert "claude-opus-5" in base  # degraded, not raised


def test_fetch_price_base_raises_only_when_no_fetch_and_no_cache(tmp_path, monkeypatch):
    monkeypatch.setattr("swarph_shared.llm_cost.CACHE_PATH", tmp_path / "does_not_exist.json")
    from swarph_shared.llm_cost import PriceFetchError
    with patch("swarph_shared.llm_cost._fetch_live", side_effect=OSError("network down")):
        with pytest.raises(PriceFetchError):
            fetch_price_base(force_refresh=True)


def test_fetch_price_base_falls_back_to_stale_cache_on_malformed_200(tmp_path, monkeypatch):
    """Review finding #1: a live fetch that returns 200 with a non-JSON body
    (rate-limit HTML page, truncated response) raised json.JSONDecodeError
    uncaught in the first version, even with a usable stale cache sitting
    right next to it — contradicting the module's own 'never raises for a
    transient outage' promise. json.JSONDecodeError IS a ValueError; the
    except clause now catches it the same way a connection failure is caught."""
    cache_file = tmp_path / "prices.json"
    cache_file.write_text(json.dumps({"claude-opus-5": {"input_cost_per_token": 1e-06, "output_cost_per_token": 1e-06}}))
    monkeypatch.setattr("swarph_shared.llm_cost.CACHE_PATH", cache_file)
    with patch("swarph_shared.llm_cost._fetch_live", side_effect=json.JSONDecodeError("bad", "doc", 0)):
        base = fetch_price_base(force_refresh=True)
    assert "claude-opus-5" in base  # degraded to stale cache, not raised


def test_fetch_price_base_serves_a_fresh_cache_without_a_live_fetch(tmp_path, monkeypatch):
    """Review finding #2: the TTL-hit path (`not force_refresh and CACHE_PATH.exists()
    and age < CACHE_TTL_SECONDS`) was never exercised — all prior tests forced
    a refresh, skipping it entirely. This proves a cache written just now is
    served WITHOUT touching the network at all."""
    cache_file = tmp_path / "prices.json"
    cache_file.write_text(json.dumps({"claude-opus-5": {"input_cost_per_token": 9e-06, "output_cost_per_token": 9e-05}}))
    monkeypatch.setattr("swarph_shared.llm_cost.CACHE_PATH", cache_file)
    with patch("swarph_shared.llm_cost._fetch_live", side_effect=AssertionError("must not fetch live on a fresh cache")):
        base = fetch_price_base()  # force_refresh defaults to False
    assert base["claude-opus-5"].input == 9e-06


def test_fetch_price_base_refetches_when_cache_is_expired(tmp_path, monkeypatch):
    """The other half of finding #2: an expired (past-TTL) cache must NOT be
    served silently — it must trigger a live refetch through the normal,
    non-forced call path."""
    import os as _os
    cache_file = tmp_path / "prices.json"
    cache_file.write_text(json.dumps({"claude-opus-5": {"input_cost_per_token": 1e-06, "output_cost_per_token": 1e-06}}))
    old = time.time() - 999999
    _os.utime(cache_file, (old, old))
    monkeypatch.setattr("swarph_shared.llm_cost.CACHE_PATH", cache_file)
    fresh_payload = {"claude-opus-5": {"input_cost_per_token": 7e-06, "output_cost_per_token": 7e-05}}
    with patch("swarph_shared.llm_cost._fetch_live", return_value=fresh_payload) as mock_fetch:
        base = fetch_price_base()  # force_refresh=False, but cache is expired
    mock_fetch.assert_called_once()
    assert base["claude-opus-5"].input == 7e-06  # got the FRESH value, not the stale one


def test_fetch_price_base_write_goes_through_tempfile_and_replace(tmp_path, monkeypatch):
    """Review finding #3: the cache write must go through a tempfile +
    os.replace rather than a direct write_text.

    HONEST ABOUT WHAT THIS PROVES: a single-threaded, non-interrupted test
    cannot demonstrate atomicity under a crash or a race — that needs fault
    injection or concurrent threads, disproportionate for a finding the
    review itself called self-healing/non-blocking. What THIS asserts is
    that os.replace is actually invoked (not that write_text is used
    directly) and that no stray tempfile survives a successful run — the
    mechanism is present, not that it was stress-tested.
    """
    cache_file = tmp_path / "prices.json"
    monkeypatch.setattr("swarph_shared.llm_cost.CACHE_PATH", cache_file)
    with patch("swarph_shared.llm_cost._fetch_live", return_value={"m": {"input_cost_per_token": 1e-06, "output_cost_per_token": 1e-06}}), \
         patch("swarph_shared.llm_cost.os.replace", wraps=__import__("os").replace) as mock_replace:
        fetch_price_base(force_refresh=True)
    mock_replace.assert_called_once()  # proves the tempfile->replace PATH executed, not just write_text
    assert cache_file.exists()
    json.loads(cache_file.read_text())  # the file left behind parses cleanly
    leftover_tmp_files = list(tmp_path.glob("*.tmp.*"))
    assert leftover_tmp_files == []  # os.replace consumed the tempfile, nothing orphaned
