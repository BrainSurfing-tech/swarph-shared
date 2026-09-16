"""Tests for swarph_shared.subprocess_env.

Mirrors the writer-revalidation patterns from tests/test_opus_subscription.py
so the extraction maintains the conservative-denylist + verify_subscription_setup
behavior. Drop's PR #125 review flag #2 explicitly asked for this.
"""

import os
from pathlib import Path
import pytest

from swarph_shared import (
    ALLOWED_KEYS_EXPLICIT,
    FORBIDDEN_KEYS_EXPLICIT,
    scrub_env_for_subprocess,
    verify_subscription_setup,
)


def test_scrub_removes_anthropic_api_key(monkeypatch):
    """The CRITICAL case — billing-flip prevention. Must catch ANTHROPIC_API_KEY."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-FAKE-FOR-TEST")
    env = scrub_env_for_subprocess()
    assert "ANTHROPIC_API_KEY" not in env, (
        "subscription billing flips to API-metered if this leaks"
    )


def test_scrub_removes_all_explicit_billing_keys(monkeypatch):
    """All keys in FORBIDDEN_KEYS_EXPLICIT must be stripped."""
    for k in FORBIDDEN_KEYS_EXPLICIT:
        monkeypatch.setenv(k, f"FAKE-{k}")
    env = scrub_env_for_subprocess()
    for k in FORBIDDEN_KEYS_EXPLICIT:
        assert k not in env, f"explicit denylist key {k!r} leaked through"


def test_scrub_forward_compat_api_key_suffix(monkeypatch):
    """Future provider keys ending in `_API_KEY` must be caught by suffix rule."""
    monkeypatch.setenv("FUTURE_PROVIDER_API_KEY", "FAKE-FUTURE")
    monkeypatch.setenv("ANTHROPIC_ADMIN_API_KEY", "FAKE-ADMIN")
    monkeypatch.setenv("MASSIVE_API_KEY", "FAKE-MASSIVE")
    env = scrub_env_for_subprocess()
    assert "FUTURE_PROVIDER_API_KEY" not in env
    assert "ANTHROPIC_ADMIN_API_KEY" not in env
    assert "MASSIVE_API_KEY" not in env


def test_scrub_preserves_path_home_user():
    """Over-prune detection — `claude -p` needs PATH/HOME to find creds + binary."""
    env = scrub_env_for_subprocess()
    assert "PATH" in env, "PATH must pass through for claude -p to find runtime"
    assert "HOME" in env, "HOME must pass through for credentials.json discovery"


def test_scrub_does_not_touch_os_environ(monkeypatch):
    """Defense: scrubbing must NOT mutate os.environ — only return a copy.

    Subprocess invocation pattern is `env={**scrub_env_for_subprocess(), ...}`
    which creates a new dict each call; if scrub itself mutated os.environ,
    subsequent calls in same process would see the previous scrub's removals.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-FAKE")
    assert "ANTHROPIC_API_KEY" in os.environ
    _ = scrub_env_for_subprocess()
    # os.environ untouched
    assert "ANTHROPIC_API_KEY" in os.environ


def test_verify_subscription_setup_raises_on_missing_creds(tmp_path, monkeypatch):
    """If credentials.json doesn't exist, fail loud."""
    fake_creds = tmp_path / "nonexistent" / ".credentials.json"
    fake_bin = tmp_path / "fake-claude-bin"
    fake_bin.write_text("#!/bin/sh\n")
    fake_bin.chmod(0o755)
    with pytest.raises(RuntimeError, match="subscription auth missing"):
        verify_subscription_setup(claude_bin=str(fake_bin), creds_path=fake_creds)


def test_verify_subscription_setup_raises_on_loose_creds_perms(tmp_path):
    """credentials.json with permissive mode (other-readable) must raise."""
    creds = tmp_path / ".credentials.json"
    creds.write_text('{"fake": "creds"}')
    creds.chmod(0o644)  # group + other can read
    fake_bin = tmp_path / "fake-claude-bin"
    fake_bin.write_text("#!/bin/sh\n")
    fake_bin.chmod(0o755)
    with pytest.raises(RuntimeError, match="should be 0600"):
        verify_subscription_setup(claude_bin=str(fake_bin), creds_path=creds)


def test_verify_subscription_setup_raises_on_missing_binary(tmp_path):
    """If claude binary doesn't exist, fail loud."""
    creds = tmp_path / ".credentials.json"
    creds.write_text('{"fake": "creds"}')
    creds.chmod(0o600)
    nonexistent_bin = tmp_path / "nope" / "claude"
    with pytest.raises(RuntimeError, match="claude binary missing"):
        verify_subscription_setup(claude_bin=str(nonexistent_bin), creds_path=creds)


def test_verify_subscription_setup_passes_with_valid_setup(tmp_path):
    """Happy path — all three checks pass."""
    creds = tmp_path / ".credentials.json"
    creds.write_text('{"fake": "creds"}')
    creds.chmod(0o600)
    fake_bin = tmp_path / "fake-claude"
    fake_bin.write_text("#!/bin/sh\n")
    fake_bin.chmod(0o755)
    # Should not raise
    verify_subscription_setup(claude_bin=str(fake_bin), creds_path=creds)


def test_verify_subscription_setup_restores_env_on_pass(tmp_path, monkeypatch):
    """Pre-set ANTHROPIC_API_KEY must survive verify_subscription_setup unchanged."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-PRE-EXISTING")
    creds = tmp_path / ".credentials.json"
    creds.write_text('{"fake": "creds"}')
    creds.chmod(0o600)
    fake_bin = tmp_path / "fake-claude"
    fake_bin.write_text("#!/bin/sh\n")
    fake_bin.chmod(0o755)
    verify_subscription_setup(claude_bin=str(fake_bin), creds_path=creds)
    # Env restored after the test-injection inside verify
    assert os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-PRE-EXISTING"


# --- ALLOWED_KEYS_EXPLICIT: the deliberate hole in the suffix sweep (#656/#424) ---


def test_exempt_slm_key_survives_the_suffix_sweep(monkeypatch):
    """SWARPH_SLM_API_KEY ends in _API_KEY but must reach the subprocess.

    If the exemption is removed this reads "not in env" — the silent-failure
    mode the exemption exists to prevent (available() True off an
    unauthenticated /v1/models, then every generate() 401s into zero proposals).
    """
    monkeypatch.setenv("SWARPH_SLM_API_KEY", "slm-FAKE-FOR-TEST")
    env = scrub_env_for_subprocess()
    assert env.get("SWARPH_SLM_API_KEY") == "slm-FAKE-FOR-TEST"


def test_exemption_is_exact_not_a_prefix_hole(monkeypatch):
    """The hole is ONE key, not the SWARPH_ namespace.

    A sibling swarph-named key with the same shape is still scrubbed — this is
    what fails if someone widens the exemption to a prefix or a pattern.
    """
    monkeypatch.setenv("SWARPH_OTHER_API_KEY", "should-not-survive")
    monkeypatch.setenv("SWARPH_SLM_AUTH_TOKEN", "should-not-survive")
    env = scrub_env_for_subprocess()
    assert "SWARPH_OTHER_API_KEY" not in env
    assert "SWARPH_SLM_AUTH_TOKEN" not in env


def test_exemption_can_never_re_admit_a_named_billing_key():
    """Structural guard on the SET, not on one call.

    Adding e.g. ANTHROPIC_API_KEY to ALLOWED_KEYS_EXPLICIT would flip billing
    silently and every behavioural test above would still pass, because they
    each assert on their own key. This is the only check that sees the overlap.
    """
    assert not (ALLOWED_KEYS_EXPLICIT & FORBIDDEN_KEYS_EXPLICIT), (
        "an exemption naming a known billing key re-opens the billing flip"
    )


def test_slm_siblings_are_not_billing_shaped(monkeypatch):
    """ENDPOINT / MODEL / TIMEOUT carry no credential and must pass through.

    Pinned so a future suffix added to FORBIDDEN_SUFFIXES cannot strip the
    config half of the SLM client and leave only the key.
    """
    for name in ("SWARPH_SLM_ENDPOINT", "SWARPH_SLM_MODEL", "SWARPH_SLM_TIMEOUT"):
        monkeypatch.setenv(name, "x")
    env = scrub_env_for_subprocess()
    for name in ("SWARPH_SLM_ENDPOINT", "SWARPH_SLM_MODEL", "SWARPH_SLM_TIMEOUT"):
        assert name in env, name
