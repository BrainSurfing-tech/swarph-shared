"""peer_registry — canonical peer-name resolution for the swarph-mesh.

The authoritative source of canonical peer names is the mesh-gateway
``/peers`` endpoint. This module is a thin TTL-cached client over that
endpoint plus a static drift-mapping (``KNOWN_ALIASES``) for observed
contagion-aliases (see §15.3 of the swarph-mesh PLAN.md).

Two distinct contagion vectors motivate this module:

* **Vector A — peer-onboarding chatter.** A new peer's first DMs introduce
  non-canonical references; receiving peers absorb. Auditable at peer-
  introduction. Canonical example: ``lab-claude → lab-ovh`` (gemini-
  researcher / workstation-lc onboarding 2026-05-04/05).
* **Vector B — human-prompt shorthand.** A human (typically commander)
  uses a friendly nickname for a peer in conversation with an AI peer;
  the AI absorbs without registry check. NOT auditable at peer-
  introduction; requires per-send registry verification. Canonical
  example: ``drop → droplet`` (lab session 2026-05-08, six DMs missed).

The mitigation is the same for both vectors: ``validate_node_name`` at
every adapter-boundary send. The Vector B case is load-bearing for why
the gate must be at every send, not just at peer-introduction.

Public surface
==============

* :data:`NAMING_CONVENTION_REGEX` — static format check.
* :data:`KNOWN_ALIASES` — static ``{alias: canonical}`` drift table.
* :func:`canonical_names` — TTL-cached gateway ``/peers`` query.
* :func:`validate_node_name` — full pipeline: regex → alias → registry.
* :func:`is_registered` — soft check, never raises.

Convention for ``KNOWN_ALIASES``: the dict resolves *FROM* an observed
contagion-alias *TO* the canonical registry name. Each entry has a
worked-example incident behind it; see PLAN.md §15.3.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Static state — regex + alias table
# ---------------------------------------------------------------------------

#: Regex enforcing canonical peer-name shape: lowercase, alphanumeric +
#: dash, must start and end with alphanumeric. Examples that pass:
#: ``lab-ovh``, ``droplet``, ``science-claude``. Examples that fail:
#: ``Lab-Ovh`` (uppercase), ``lab_ovh`` (underscore), ``-droplet`` /
#: ``droplet-`` (leading/trailing dash).
NAMING_CONVENTION_REGEX = re.compile(r"^[a-z][a-z0-9-]*[a-z0-9]$")

#: ``{alias: canonical}`` drift-mapping for OBSERVED contagion aliases.
#:
#: Worked examples (each maps to a real incident):
#:
#: * ``drop → droplet`` (Vector B, lab session 2026-05-08): commander's
#:   conversational nickname for droplet absorbed by lab; six DMs
#:   missed (#628 / #633 / #634 / #639 / #641 / #643).
#: * ``lab-claude → lab-ovh`` (Vector A, 2026-05-04/05): gemini-researcher
#:   / workstation-lc onboarding introduced "lab-claude"; drop absorbed;
#:   #560 / #562 / #564 sat unread.
#: * ``ws-lc → workstation-lc``: shorthand drift, lower-stakes.
#:
#: Convention: ALWAYS ``{alias: canonical}``. If you reverse the
#: direction, ``validate_node_name('drop')`` will return ``'drop'``
#: instead of ``'droplet'`` and the entire mitigation breaks. The
#: regression test ``test_known_aliases_direction_is_alias_to_canonical``
#: in ``tests/test_peer_registry.py`` enforces this invariant.
KNOWN_ALIASES: dict[str, str] = {
    "drop": "droplet",
    "lab-claude": "lab-ovh",
    "ws-lc": "workstation-lc",
}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_GATEWAY_URL = os.getenv("MESH_GATEWAY_URL", "http://localhost:8788")
GATEWAY_TOKEN_ENV = "MESH_GATEWAY_TOKEN"

#: Maximum age (seconds) of a stale cached canonical-names set that
#: ``canonical_names`` will return when the gateway is unreachable. One
#: hour matches "peer adds happen on the human-DM-handshake cadence
#: (minutes-to-hours)" rationale from PLAN.md §15.3.
CACHE_GRACE_SECONDS = 3600


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class GatewayUnreachableError(RuntimeError):
    """Raised by :func:`canonical_names` (and :func:`validate_node_name`
    in strict mode) when the gateway cannot be reached and there is no
    usable cache. Loud-on-down per PLAN.md §16.5; never silently
    degrades to "registry-not-checked" without explicit ``strict=False``."""


class NotInRegistry(ValueError):
    """Raised by :func:`validate_node_name` when a name passes regex +
    alias resolution but is not present in the gateway registry."""


# ---------------------------------------------------------------------------
# Cache (module-level, single-process)
# ---------------------------------------------------------------------------

_cache: dict = {"names": None, "fetched_at": 0.0}


def _clear_cache() -> None:
    """Test-only cache reset. Not part of the public API."""
    _cache["names"] = None
    _cache["fetched_at"] = 0.0


# ---------------------------------------------------------------------------
# Gateway query
# ---------------------------------------------------------------------------


def _fetch_canonical_names(
    gateway_url: str,
    token: Optional[str],
    timeout_seconds: float,
) -> frozenset[str]:
    """Single ``GET /peers`` and extract the set of canonical names."""
    url = gateway_url.rstrip("/") + "/peers"
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:  # nosec B310
        body = resp.read()
    payload = json.loads(body.decode("utf-8"))
    # Accept either {"peers": [...]} or a bare list — different gateway
    # versions have shipped both shapes.
    peers = payload.get("peers", payload) if isinstance(payload, dict) else payload
    names = []
    for entry in peers:
        if isinstance(entry, str):
            names.append(entry)
        elif isinstance(entry, dict):
            n = entry.get("node_name") or entry.get("name") or entry.get("id")
            if n:
                names.append(n)
    return frozenset(names)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def canonical_names(
    ttl_seconds: int = 300,
    gateway_url: Optional[str] = None,
    token: Optional[str] = None,
    timeout_seconds: float = 5.0,
) -> frozenset[str]:
    """Return the current canonical peer names from the gateway.

    Module-level TTL cache; cache is shared across calls in the same
    process.

    Parameters
    ----------
    ttl_seconds:
        Maximum age of a cached result before re-querying. ``0`` forces
        a fresh fetch on every call (test fixtures use this).
    gateway_url:
        Override the default ``MESH_GATEWAY_URL`` env / ``localhost:8788``.
    token:
        Override the default ``MESH_GATEWAY_TOKEN`` env.
    timeout_seconds:
        HTTP timeout for the ``/peers`` request.

    Returns
    -------
    frozenset[str]
        Canonical peer names known to the gateway.

    Raises
    ------
    GatewayUnreachableError
        When the gateway cannot be reached and no usable cached result
        is available within :data:`CACHE_GRACE_SECONDS`.

    Notes
    -----
    *Graceful degradation* — if the gateway is unreachable but a prior
    successful response is cached within ``CACHE_GRACE_SECONDS`` (1h
    default), the stale set is returned with a loud warning. If no
    usable cache exists, ``GatewayUnreachableError`` is raised. This
    matches drop's PR #650 review carry-forward #2 and the
    fail-loud-not-fail-silent shape from PLAN.md §16.5.
    """
    now = time.time()
    if (
        ttl_seconds > 0
        and _cache["names"] is not None
        and (now - _cache["fetched_at"]) < ttl_seconds
    ):
        return _cache["names"]

    gw = gateway_url or DEFAULT_GATEWAY_URL
    tok = token if token is not None else os.getenv(GATEWAY_TOKEN_ENV)

    try:
        names = _fetch_canonical_names(gw, tok, timeout_seconds)
        _cache["names"] = names
        _cache["fetched_at"] = now
        return names
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        # Graceful degradation: stale-cache fallback within grace window.
        if (
            _cache["names"] is not None
            and (now - _cache["fetched_at"]) < CACHE_GRACE_SECONDS
        ):
            logger.warning(
                "peer_registry: gateway %s unreachable (%s); "
                "using stale canonical_names from %.0fs ago",
                gw,
                exc,
                now - _cache["fetched_at"],
            )
            return _cache["names"]
        raise GatewayUnreachableError(
            f"mesh-gateway at {gw} unreachable and no usable cached "
            f"canonical_names (error: {exc!r})"
        ) from exc


def validate_node_name(
    name: str,
    ttl_seconds: int = 300,
    gateway_url: Optional[str] = None,
    token: Optional[str] = None,
    strict: bool = True,
) -> str:
    """Validate and canonicalize a peer name.

    Three-stage pipeline:

    1. **Regex check** against :data:`NAMING_CONVENTION_REGEX`.
    2. **Alias resolution** via :data:`KNOWN_ALIASES`. Logs a loud
       warning when an alias resolves so contagion is observable, not
       silent.
    3. **Registry check** against :func:`canonical_names`. Optional —
       skipped if ``strict=False`` and the gateway is unreachable.

    Parameters
    ----------
    name:
        The peer name to validate. Must be a string.
    ttl_seconds, gateway_url, token:
        Forwarded to :func:`canonical_names`.
    strict:
        When ``True`` (default), gateway-unreachable + no cache raises
        :class:`GatewayUnreachableError`. When ``False``, the registry
        check is skipped with a loud warning and the alias-resolved
        name is returned optimistically. Use ``strict=False`` only in
        environments where the gateway may legitimately be offline
        (test fixtures, offline dev).

    Returns
    -------
    str
        The canonical peer name.

    Raises
    ------
    ValueError
        If ``name`` is not a string or fails the regex check.
    NotInRegistry
        If the (alias-resolved) name is not in
        :func:`canonical_names`.
    GatewayUnreachableError
        If ``strict=True`` and the gateway is unreachable with no
        usable cache.
    """
    if not isinstance(name, str):
        raise ValueError(
            f"peer name must be str, got {type(name).__name__}"
        )
    if not NAMING_CONVENTION_REGEX.match(name):
        raise ValueError(
            f"peer name {name!r} does not match naming convention "
            f"(lowercase, alnum + dash, must start and end alnum)"
        )

    if name in KNOWN_ALIASES:
        canonical = KNOWN_ALIASES[name]
        logger.warning(
            "peer_registry: %r is an alias; canonical is %r "
            "(contagion alias resolved)",
            name,
            canonical,
        )
        name = canonical

    try:
        registry = canonical_names(
            ttl_seconds=ttl_seconds,
            gateway_url=gateway_url,
            token=token,
        )
    except GatewayUnreachableError:
        if strict:
            raise
        logger.warning(
            "peer_registry: gateway unreachable + no cache; "
            "skipping registry check on %r (strict=False)",
            name,
        )
        return name

    if name not in registry:
        raise NotInRegistry(
            f"peer name {name!r} is not in the gateway registry "
            f"(known canonical names: {sorted(registry)})"
        )
    return name


def is_registered(
    name: str,
    ttl_seconds: int = 300,
    gateway_url: Optional[str] = None,
    token: Optional[str] = None,
) -> bool:
    """Soft check — ``True`` iff ``name`` (post-alias-resolution) is in
    the gateway registry. Never raises.

    Returns ``False`` on regex miss, on
    :class:`NotInRegistry`, or when the gateway is unreachable. For
    code paths that want graceful behavior on every error class, not
    loud failures.
    """
    try:
        validate_node_name(
            name,
            ttl_seconds=ttl_seconds,
            gateway_url=gateway_url,
            token=token,
            strict=True,
        )
        return True
    except (ValueError, NotInRegistry, GatewayUnreachableError):
        return False
