"""Tests for swarph_shared.json_mode.

Covers:
1. parse_json happy path + prose-extraction + balanced-braces fallback
2. parse_json_with_retry: success on retry, failure on retry, retry callback raises
3. build_retry_feedback_turn produces the canonical feedback string

The key invariant from PR #125 review: retry feedback goes as a NEW [USER] turn
in the harness's contract — the on_retry callback is responsible for that.
"""

import pytest

from swarph_shared import (
    build_retry_feedback_turn,
    parse_json,
    parse_json_with_retry,
)


# ============================================================================
# parse_json — direct + fallback
# ============================================================================

def test_parse_json_happy_path():
    parsed, err = parse_json('{"a": 1, "b": "two"}')
    assert parsed == {"a": 1, "b": "two"}
    assert err is None


def test_parse_json_extracts_from_prose():
    """Real claude/gemini output sometimes wraps JSON in prose."""
    text = (
        'Sure, here is the JSON:\n'
        '{"verdict": "TRADE", "confidence": 0.8}\n'
        'Let me know if you want me to elaborate.'
    )
    parsed, err = parse_json(text)
    assert parsed == {"verdict": "TRADE", "confidence": 0.8}
    assert err is None


def test_parse_json_extracts_from_markdown_fence():
    """Models sometimes emit ```json ... ``` even when told not to."""
    text = '```json\n{"action": "BUY"}\n```'
    parsed, err = parse_json(text)
    assert parsed == {"action": "BUY"}
    assert err is None


def test_parse_json_handles_nested_objects():
    """Balanced-brace walker must handle nested {} correctly."""
    text = 'Result: {"outer": {"inner": [1, 2]}, "n": 3}'
    parsed, err = parse_json(text)
    assert parsed == {"outer": {"inner": [1, 2]}, "n": 3}
    assert err is None


def test_parse_json_returns_error_on_garbage():
    parsed, err = parse_json("totally not json at all")
    assert parsed is None
    assert err is not None
    assert "no JSON object" in err


def test_parse_json_returns_error_on_unbalanced():
    parsed, err = parse_json("{unbalanced opening brace only")
    assert parsed is None
    assert err is not None


def test_parse_json_handles_empty():
    parsed, err = parse_json("")
    assert parsed is None
    assert err == "empty response"


def test_parse_json_handles_whitespace_only():
    parsed, err = parse_json("   \n\t  ")
    assert parsed is None
    assert err == "empty response"


# ============================================================================
# build_retry_feedback_turn
# ============================================================================

def test_build_retry_feedback_turn_includes_error():
    """Feedback string must include the error so the model can correct."""
    feedback = build_retry_feedback_turn("Expecting value: line 1 column 5")
    assert "Expecting value" in feedback
    assert "valid JSON" in feedback
    assert "No prose" in feedback


def test_build_retry_feedback_turn_is_string():
    """Returns plain str — caller appends as content of a new [USER] turn."""
    feedback = build_retry_feedback_turn("err")
    assert isinstance(feedback, str)


# ============================================================================
# parse_json_with_retry — the harness contract
# ============================================================================

def test_retry_succeeds_on_second_attempt():
    """Initial parse fails, retry callback returns valid JSON."""
    initial = "I'm not following the schema, sorry."

    retries_seen = []

    def on_retry(feedback: str) -> str:
        retries_seen.append(feedback)
        # Caller's job in real code: append feedback as new [USER] turn,
        # invoke LLM, return its new response. Here we simulate.
        return '{"verdict": "TRADE"}'

    parsed, err_class = parse_json_with_retry(initial, on_retry=on_retry)
    assert parsed == {"verdict": "TRADE"}
    assert err_class is None
    # Retry was invoked exactly once with feedback containing the error
    assert len(retries_seen) == 1
    assert "valid JSON" in retries_seen[0]


def test_retry_fails_returns_malformed_json():
    """Both initial parse AND retry parse fail → error_class='malformed_json'."""

    def on_retry(feedback: str) -> str:
        # Model is still confused
        return "Still no JSON, sorry."

    parsed, err_class = parse_json_with_retry("nothing here", on_retry=on_retry)
    assert parsed is None
    assert err_class == "malformed_json"


def test_retry_callback_raises_returns_retry_failed():
    """If on_retry callback itself raises, harness catches + reports clean."""

    def on_retry(feedback: str) -> str:
        raise RuntimeError("network error")

    parsed, err_class = parse_json_with_retry("not json", on_retry=on_retry)
    assert parsed is None
    assert err_class == "retry_failed"


def test_retry_not_invoked_on_success():
    """If initial parse succeeds, on_retry must NOT be called."""
    on_retry_called = []

    def on_retry(feedback: str) -> str:
        on_retry_called.append(feedback)
        return "{}"

    parsed, err_class = parse_json_with_retry(
        '{"first": "try"}',
        on_retry=on_retry,
    )
    assert parsed == {"first": "try"}
    assert err_class is None
    assert on_retry_called == []


def test_retry_invoked_only_once():
    """Even if retry response is also bad, no second retry attempted —
    the harness retries ONCE per the contract, never recursively."""
    call_count = [0]

    def on_retry(feedback: str) -> str:
        call_count[0] += 1
        return "still bad"

    parsed, err_class = parse_json_with_retry("garbage", on_retry=on_retry)
    assert parsed is None
    assert err_class == "malformed_json"
    assert call_count[0] == 1, "harness must retry exactly once, not recursively"
