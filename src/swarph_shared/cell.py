"""``swarph_shared.cell`` — universal cell.yaml schema (substrate-doc R7 §11.1.5 (O5)).

Defines the cell.yaml v1 schema as the canonical mesh-citizenship boot
config: identity (name, role), execution (cwd, provider, session_id),
context (starter_prompt_path), and v2-deferred lineage (parent_peer_id,
spawn_manifest_signature). Lives in ``swarph-shared`` so any swarph-mesh
component (swarph-cli, future LLM-side bootstrappers, federation handshake
implementations) can parse cell.yaml without depending on swarph-cli's
file-I/O surface.

This module is **pure-stdlib** + zero runtime deps:
* No PyYAML — caller passes a parsed dict (file I/O is swarph-cli's concern)
* No file system access — caller resolves paths
* No network — caller fetches mesh-gateway URL bodies if applicable

The split: swarph-shared owns DATA SHAPES + SCHEMA VALIDATION;
swarph-cli owns FILE DISCOVERY + I/O + SIDECAR PERSISTENCE + SLOT
ALLOCATION (operator-tooling layer per substrate-doc R7 §11.1.7).

Migration history per substrate-doc R7 §11.1.5 (O5) cell.yaml universal-
genome open question: shipped first inside swarph-cli v0.6 (PR #8) to
validate the schema in production use; relocated here in swarph-shared
v0.3.0 / swarph-cli v0.7 PR-D as symbol-only refactor (no field changes,
no behavior changes — schema FROZEN at ``schema_version: "v1"``).
swarph-cli re-exports for back-compat so v0.6 cell.yaml files keep
working unchanged in v0.7+.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


SCHEMA_VERSION_V1 = "v1"
"""Frozen cell.yaml schema version. Per drop-mother review #890 (C2) +
``feedback_swarph_paper_rev_bar``, v1 is the stable baseline. v0.6
files keep working unchanged in v0.7+. Future schema changes require
explicit ``schema_version: "v2"`` bump + parallel-supported-version
window per ``swarph-mesh`` DEPRECATIONS discipline."""

VALID_SCHEMA_VERSIONS = frozenset({SCHEMA_VERSION_V1})

# Conservative peer-name pattern, mirrors swarph_shared.peer_registry
# discipline — kebab/snake-case, no spaces, no leading hyphen.
PEER_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")

# Provider whitelist for the spawn path. Per-version gates live in the
# docstring, not the symbol name (beta #1010 review observation):
# additive-optional new providers extend this frozenset without breaking
# existing imports. Schema_version on Cell carries version semantics;
# constants don't need redundant version tagging.
VALID_PROVIDERS = frozenset({"claude", "codex"})


class CellError(ValueError):
    """Raised on cell.yaml validation failure.

    Subclass of ``ValueError`` so callers using broad-except catches
    on data-validation issues still funnel cleanly. Distinct from
    file-I/O errors (which stay as ``OSError`` subclasses raised by
    the caller's I/O layer).
    """


@dataclass
class Lineage:
    """Optional lineage block — alpha #891 (D1) reserved shape.

    v0.6+v0.7 accepts presence + parses; semantic validation (signature
    verification) graduates with the v2 cryptographic-lineage tier per
    substrate-doc R6 §11.1.2 candidate primitive S-B.
    """

    parent_peer_id: Optional[str] = None
    spawn_manifest_signature: Optional[str] = None


@dataclass
class Cell:
    """Parsed cell.yaml — v0.7+ schema (``schema_version: "v1"``).

    Same shape as swarph-cli v0.6's local Cell dataclass — symbol
    relocation only per PR-D. v0.6 cell.yaml files keep working unchanged.

    Fields:
      name              kebab/snake-case peer name (PEER_NAME_RE-validated)
      role              claude --name display value; sibling slots get
                        ``<role>-N`` suffix at the swarph-cli operator-
                        tooling layer (slot allocation is NOT
                        swarph-shared's concern)
      cwd               absolute working-dir path the spawn lands in;
                        callers MUST resolve relative paths before
                        constructing Cell (swarph-shared doesn't touch
                        the filesystem)
      schema_version    frozen at "v1" in v0.7+
      session_id        optional pinned UUID; sidecar persistence is
                        swarph-cli's concern at the tooling layer
      starter_prompt_path  optional absolute path; readability is the
                        caller's concern (swarph-cli's load_cell + Cell
                        wrapper does the file read at runtime)
      provider          spawn membrane provider ("claude" or "codex")
      sandbox           optional provider-specific sandbox policy. Shared
                        schema validates only string shape; swarph-cli
                        membranes validate provider-specific values.
      lineage           optional v2-tier reserved shape
      source_path       optional Path metadata, set by swarph-cli's
                        load_cell when reading from disk; left None when
                        the Cell came from a non-file source (e.g., S-G
                        spawn-context endpoint at v0.7+)
      extra             unparsed top-level keys — preserved for forward-
                        compat (v0.7+ may attach meaning to ``mesh:``,
                        ``capabilities:``, ``memory_mirror:`` etc.)
    """

    name: str
    role: str
    cwd: Path
    schema_version: str = SCHEMA_VERSION_V1
    session_id: Optional[str] = None
    starter_prompt_path: Optional[Path] = None
    provider: str = "claude"
    sandbox: Optional[str] = None
    lineage: Optional[Lineage] = None
    source_path: Optional[Path] = None
    extra: dict[str, Any] = field(default_factory=dict)


def validate_uuid_str(value: str) -> str:
    """Validate-and-normalise a UUID string; raise CellError on failure.

    ``claude --session-id`` rejects non-UUIDs at the harness layer;
    catching it here gives a substrate-shaped error path instead of a
    bare claude-cli traceback.
    """
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise CellError(
            f"cell.yaml: session_id is not a valid UUID: {value!r}"
        ) from exc


def parse_cell_dict(
    raw: Any,
    *,
    source: str = "<dict>",
    base_dir: Optional[Path] = None,
) -> Cell:
    """Parse + validate a cell.yaml-shaped dict into a Cell instance.

    Pure function — no file I/O, no path resolution beyond the
    optional ``base_dir`` argument (used to resolve relative paths
    in cwd / starter_prompt_path against the cell.yaml's parent dir).

    Args:
      raw: The parsed YAML/JSON dict. Must be a mapping at top level.
      source: Identifier for error messages (caller usually passes the
        absolute path to the cell.yaml file or "<dict>" / "<URL>" for
        non-file sources).
      base_dir: Optional directory used to resolve relative paths in
        ``cwd`` and ``starter_prompt_path``. swarph-cli passes the
        cell.yaml's parent directory; pass None when no relative-path
        resolution is needed (e.g., when cell came from a URL body).

    Raises:
      CellError on any schema violation. swarph-shared raises CellError
      consistently across schema-validation paths so callers can broad-
      catch without provider-specific adapters.

    Returns:
      A populated Cell instance with all schema-required fields validated.
    """
    if not isinstance(raw, dict):
        raise CellError(
            f"cell.yaml top-level must be a mapping ({source}); "
            f"got {type(raw).__name__}"
        )

    raw = dict(raw)  # shallow copy; pop will mutate
    schema_version = raw.pop("schema_version", SCHEMA_VERSION_V1)
    name = raw.pop("name", None)
    role = raw.pop("role", None)
    cwd_raw = raw.pop("cwd", None)
    session_id = raw.pop("session_id", None)
    starter_prompt_raw = raw.pop("starter_prompt_path", None)
    provider = raw.pop("provider", "claude")
    sandbox = raw.pop("sandbox", None)
    identity = raw.pop("identity", None)

    if schema_version not in VALID_SCHEMA_VERSIONS:
        raise CellError(
            f"cell.yaml: schema_version {schema_version!r} is not "
            f"supported by this swarph-shared build. "
            f"Supported: {sorted(VALID_SCHEMA_VERSIONS)}."
        )

    if not isinstance(name, str) or not PEER_NAME_RE.match(name):
        raise CellError(
            f"cell.yaml: 'name' must be a kebab/snake-case peer name "
            f"matching {PEER_NAME_RE.pattern}; got {name!r}"
        )
    if not isinstance(role, str) or not role.strip():
        raise CellError(
            "cell.yaml: 'role' is required and must be a non-empty string"
        )
    if not isinstance(cwd_raw, str) or not cwd_raw.strip():
        raise CellError(
            "cell.yaml: 'cwd' is required and must be a non-empty string"
        )

    cwd = Path(cwd_raw).expanduser()
    if not cwd.is_absolute():
        # Resolve relative to base_dir for ergonomic author-from-anywhere
        # config files. If no base_dir provided, leave the path as-is and
        # let the caller (swarph-cli) decide how to handle.
        if base_dir is not None:
            cwd = (base_dir / cwd).resolve()

    # NOTE: swarph-shared does NOT verify cwd.is_dir() — that's a runtime
    # concern owned by swarph-cli (it's also the layer that knows about
    # ephemeral path scenarios like containerised spawn). swarph-shared
    # only validates SHAPE, not RUNTIME REACHABILITY.

    if session_id is not None:
        if not isinstance(session_id, str):
            raise CellError(
                f"cell.yaml: 'session_id' must be a string UUID, got "
                f"{type(session_id).__name__}"
            )
        session_id = validate_uuid_str(session_id)

    starter_path: Optional[Path] = None
    if starter_prompt_raw is not None:
        if not isinstance(starter_prompt_raw, str) or not starter_prompt_raw.strip():
            raise CellError(
                "cell.yaml: 'starter_prompt_path' must be a non-empty string"
            )
        starter_path = Path(starter_prompt_raw).expanduser()
        if not starter_path.is_absolute() and base_dir is not None:
            starter_path = (base_dir / starter_path).resolve()

    if provider not in VALID_PROVIDERS:
        raise CellError(
            f"cell.yaml: provider {provider!r} is not in the supported "
            f"provider set (valid: {sorted(VALID_PROVIDERS)}). "
            "Unsupported provider spawn is queued for a future release."
        )

    if sandbox is not None:
        if not isinstance(sandbox, str) or not sandbox.strip():
            raise CellError(
                "cell.yaml: 'sandbox' must be a non-empty string"
            )
        sandbox = sandbox.strip()

    lineage_obj: Optional[Lineage] = None
    if identity is not None:
        if not isinstance(identity, dict):
            raise CellError(
                f"cell.yaml: 'identity' must be a mapping; got "
                f"{type(identity).__name__}"
            )
        lineage_raw = identity.get("lineage")
        if lineage_raw is not None:
            if not isinstance(lineage_raw, dict):
                raise CellError(
                    "cell.yaml: 'identity.lineage' must be a mapping"
                )
            lineage_obj = Lineage(
                parent_peer_id=lineage_raw.get("parent_peer_id"),
                spawn_manifest_signature=lineage_raw.get(
                    "spawn_manifest_signature"
                ),
            )

    return Cell(
        name=name,
        role=role.strip(),
        cwd=cwd,
        schema_version=schema_version,
        session_id=session_id,
        starter_prompt_path=starter_path,
        provider=provider,
        sandbox=sandbox,
        lineage=lineage_obj,
        source_path=None,  # Caller (swarph-cli) sets this when reading from disk
        extra=raw,  # whatever's left — preserved for forward-compat
    )
