"""Tests for swarph_shared.caller_convention.

Mirrors the existing tests/test_opus_subscription.py caller-validation coverage
(positive + negative cases) so the extraction is byte-equivalent in behavior.
"""

import pytest

from swarph_shared import CALLER_PATTERN, validate_caller


def test_validate_caller_accepts_dotted_slugs():
    """All canonical examples from the convention doc must validate."""
    for caller in (
        "council.judge.claude.r2",
        "council.defender.r1",
        "orchestrator.boss",
        "agent.zeta",
        "watchtower.graphrag.archivist",
        "cli.repl",
        "cli.repl.user",
    ):
        validate_caller(caller)  # no raise


def test_validate_caller_rejects_malformed():
    """Negative cases — every shape that violates the regex must raise."""
    bad = [
        "",
        "single",            # no dots — flat slug rejected
        "Council.Judge",     # uppercase
        "council..judge",    # double dot
        ".council.judge",    # leading dot
        "council.judge.",    # trailing dot
        "council judge.r2",  # space
        "1council.judge",    # leading digit
        "council.1judge",    # leading digit on subsegment
        "council.judge!",    # special char
    ]
    for caller in bad:
        with pytest.raises(ValueError):
            validate_caller(caller)


def test_validate_caller_rejects_non_strings():
    """Defense-in-depth: non-str inputs must raise, not silently pass."""
    for not_str in [None, 42, ["council", "judge"], {"role": "council"}, b"council.judge"]:
        with pytest.raises(ValueError):
            validate_caller(not_str)


def test_caller_pattern_is_compiled_regex():
    """CALLER_PATTERN should be the compiled regex for callers that want to
    inspect it directly (e.g. registry validation at peer-registration)."""
    import re
    assert isinstance(CALLER_PATTERN, re.Pattern)
    # Spot-check the compiled pattern matches what caller_convention.py uses
    assert CALLER_PATTERN.match("council.judge.claude.r2") is not None
    assert CALLER_PATTERN.match("Council.Judge") is None
