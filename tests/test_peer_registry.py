"""Tests for swarph_shared.peer_registry.

Drop's PR #650 review carry-forwards (DM #650, 2026-05-08):

1. **Anti-regression on KNOWN_ALIASES direction** — explicit fixture
   asserts ``validate_node_name('drop')`` returns ``'droplet'``. If
   the dict is ever reversed, this fires loud.
2. **Gateway-unreachable graceful degradation** — simulate gateway
   503 / URLError and assert the stale-cache fallback + the strict-
   vs-non-strict mode both behave per spec.
"""

from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from swarph_shared import peer_registry
from swarph_shared.peer_registry import (
    KNOWN_ALIASES,
    NAMING_CONVENTION_REGEX,
    GatewayUnreachableError,
    NotInRegistry,
    canonical_names,
    is_registered,
    validate_node_name,
    _clear_cache,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_cache():
    """Each test starts with an empty cache so prior tests don't leak."""
    _clear_cache()
    yield
    _clear_cache()


def _mock_response(names: list[str]) -> MagicMock:
    """Build a context-manager mock that mimics urlopen returning a
    JSON body of ``{"peers": [{"node_name": ...}, ...]}``."""
    body = json.dumps({"peers": [{"node_name": n} for n in names]}).encode()
    mock = MagicMock()
    mock.read.return_value = body
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=False)
    return mock


# ===========================================================================
# KNOWN_ALIASES direction — drop → droplet anti-regression (PR #650 #1)
# ===========================================================================


def test_known_aliases_direction_is_alias_to_canonical():
    """Convention is ``{alias: canonical}``. If the dict is ever
    rewritten in the wrong direction (canonical → alias), this fires
    loud and blocks the regression at CI before any user runs into
    the contagion class again. Drop's explicit anti-regression
    fixture from DM #650."""
    assert KNOWN_ALIASES["drop"] == "droplet"
    assert KNOWN_ALIASES["lab-claude"] == "lab-ovh"
    assert KNOWN_ALIASES["ws-lc"] == "workstation-lc"


def test_validate_node_name_resolves_drop_to_droplet():
    """Concrete end-to-end fixture: passing the alias `drop` resolves
    to canonical `droplet` after gateway registry confirms `droplet`
    is registered."""
    with patch(
        "urllib.request.urlopen",
        return_value=_mock_response(["droplet", "lab-ovh"]),
    ):
        result = validate_node_name("drop")
    assert result == "droplet"


def test_validate_node_name_resolves_lab_claude_to_lab_ovh():
    """Vector A worked example."""
    with patch(
        "urllib.request.urlopen",
        return_value=_mock_response(["lab-ovh"]),
    ):
        result = validate_node_name("lab-claude")
    assert result == "lab-ovh"


def test_validate_node_name_passes_canonical_through():
    """Already-canonical names skip alias resolution and pass through."""
    with patch(
        "urllib.request.urlopen",
        return_value=_mock_response(["droplet"]),
    ):
        assert validate_node_name("droplet") == "droplet"


# ===========================================================================
# Regex + format checks
# ===========================================================================


def test_regex_accepts_canonical_names():
    for name in [
        "lab-ovh",
        "droplet",
        "science-claude",
        "gpu-wsl",
        "razorpeter",
        "workstation-lc",
        "gemini-researcher",
    ]:
        assert NAMING_CONVENTION_REGEX.match(name), f"{name!r} should match"


def test_regex_rejects_uppercase():
    assert not NAMING_CONVENTION_REGEX.match("Lab-Ovh")


def test_regex_rejects_underscore():
    assert not NAMING_CONVENTION_REGEX.match("lab_ovh")


def test_regex_rejects_leading_dash():
    assert not NAMING_CONVENTION_REGEX.match("-droplet")


def test_regex_rejects_trailing_dash():
    assert not NAMING_CONVENTION_REGEX.match("droplet-")


def test_regex_rejects_leading_digit():
    assert not NAMING_CONVENTION_REGEX.match("1peer")


def test_validate_node_name_rejects_uppercase():
    with pytest.raises(ValueError, match="naming convention"):
        validate_node_name("Lab-Ovh")


def test_validate_node_name_rejects_non_string():
    with pytest.raises(ValueError, match="must be str"):
        validate_node_name(123)  # type: ignore[arg-type]


# ===========================================================================
# canonical_names + TTL cache
# ===========================================================================


def test_canonical_names_returns_frozenset():
    with patch(
        "urllib.request.urlopen",
        return_value=_mock_response(["lab-ovh", "droplet"]),
    ):
        result = canonical_names()
    assert result == frozenset({"lab-ovh", "droplet"})
    assert isinstance(result, frozenset)


def test_canonical_names_cached_within_ttl():
    """Second call inside TTL returns cached result without hitting gateway."""
    mock = _mock_response(["a", "b"])
    with patch("urllib.request.urlopen", return_value=mock) as mock_open:
        canonical_names(ttl_seconds=300)
        canonical_names(ttl_seconds=300)
    assert mock_open.call_count == 1


def test_canonical_names_ttl_zero_bypasses_cache():
    """``ttl_seconds=0`` forces a fresh fetch on every call."""
    mock = _mock_response(["a"])
    with patch("urllib.request.urlopen", return_value=mock) as mock_open:
        canonical_names()
        canonical_names(ttl_seconds=0)
    assert mock_open.call_count == 2


def test_canonical_names_handles_bare_list_payload():
    """Some gateway versions return a bare list, not ``{"peers": [...]}``."""
    body = json.dumps([{"node_name": "a"}, {"node_name": "b"}]).encode()
    mock = MagicMock()
    mock.read.return_value = body
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=False)
    with patch("urllib.request.urlopen", return_value=mock):
        result = canonical_names()
    assert result == frozenset({"a", "b"})


# ===========================================================================
# Gateway-unreachable graceful degradation (PR #650 carry-forward #2)
# ===========================================================================


def test_gateway_unreachable_no_cache_raises_loud():
    """No cache + gateway down → :class:`GatewayUnreachableError`. The
    fail-loud-not-fail-silent shape from PLAN.md §16.5 — silence on a
    down dependency would be exactly the bug class drop spotted."""
    err = urllib.error.URLError("connection refused")
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(GatewayUnreachableError, match="unreachable"):
            canonical_names()


def test_gateway_unreachable_within_grace_uses_stale_cache():
    """Cache exists, gateway goes down, returns cached canonical_names
    with a loud warning (not silent)."""
    # First successful fetch populates cache.
    with patch(
        "urllib.request.urlopen",
        return_value=_mock_response(["a", "b"]),
    ):
        first = canonical_names()
    assert first == frozenset({"a", "b"})

    # Gateway goes down. ttl_seconds=0 forces a fresh fetch attempt
    # which fails — should fall back to cache.
    err = urllib.error.URLError("connection refused")
    with patch("urllib.request.urlopen", side_effect=err):
        result = canonical_names(ttl_seconds=0)
    assert result == frozenset({"a", "b"})


def test_gateway_503_within_grace_uses_stale_cache():
    """HTTPError 503 also triggers the stale-cache fallback."""
    with patch(
        "urllib.request.urlopen",
        return_value=_mock_response(["x"]),
    ):
        canonical_names()

    err = urllib.error.HTTPError(
        url="x", code=503, msg="Service Unavailable", hdrs=None, fp=io.BytesIO(b"")
    )
    with patch("urllib.request.urlopen", side_effect=err):
        result = canonical_names(ttl_seconds=0)
    assert result == frozenset({"x"})


def test_validate_node_name_strict_false_skips_registry_when_offline():
    """``strict=False`` allows operation when the gateway is down +
    no cache; the alias-resolved name is returned optimistically."""
    err = urllib.error.URLError("offline")
    with patch("urllib.request.urlopen", side_effect=err):
        result = validate_node_name("droplet", strict=False)
    assert result == "droplet"


def test_validate_node_name_strict_false_still_resolves_alias_when_offline():
    """Alias resolution is local (regex + KNOWN_ALIASES) and must still
    work when the gateway is down + strict=False."""
    err = urllib.error.URLError("offline")
    with patch("urllib.request.urlopen", side_effect=err):
        result = validate_node_name("drop", strict=False)
    assert result == "droplet"


def test_validate_node_name_strict_true_raises_when_offline_no_cache():
    """Default strict=True propagates GatewayUnreachableError. The
    intent is: in production, never silently bypass the registry
    check; require explicit opt-in to degrade."""
    err = urllib.error.URLError("offline")
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(GatewayUnreachableError):
            validate_node_name("droplet")


# ===========================================================================
# is_registered — soft check, never raises
# ===========================================================================


def test_is_registered_true_for_canonical():
    with patch(
        "urllib.request.urlopen",
        return_value=_mock_response(["droplet"]),
    ):
        assert is_registered("droplet")


def test_is_registered_resolves_alias():
    with patch(
        "urllib.request.urlopen",
        return_value=_mock_response(["droplet"]),
    ):
        assert is_registered("drop")


def test_is_registered_false_on_unknown_name():
    with patch(
        "urllib.request.urlopen",
        return_value=_mock_response(["droplet"]),
    ):
        assert not is_registered("ghost-peer")


def test_is_registered_false_on_regex_miss():
    """No gateway call needed — regex fails first."""
    assert not is_registered("Bad-Name")


def test_is_registered_false_on_gateway_offline():
    err = urllib.error.URLError("offline")
    with patch("urllib.request.urlopen", side_effect=err):
        assert not is_registered("droplet")


# ===========================================================================
# NotInRegistry path
# ===========================================================================


def test_not_in_registry_raises_with_known_set():
    """The error message includes the sorted known set so a confused
    caller can see what they should have used."""
    with patch(
        "urllib.request.urlopen",
        return_value=_mock_response(["droplet", "lab-ovh"]),
    ):
        with pytest.raises(NotInRegistry, match="not in the gateway registry"):
            validate_node_name("ghost-peer")


def test_not_in_registry_after_alias_resolution():
    """If the alias resolves to a name still not in the registry, we
    raise NotInRegistry with the canonical name (not the alias)."""
    # Empty registry — even canonical droplet is not present
    with patch(
        "urllib.request.urlopen",
        return_value=_mock_response([]),
    ):
        with pytest.raises(NotInRegistry):
            validate_node_name("drop")
