"""Single source of truth for the dotted-slug caller convention.

Used by every public surface that accepts caller tags for cross-billing-path
attribution joins (`token_usage` ⋈ `subscription_usage`). Locked across droplet
+ lab per DM thread #586/#590/#592/#593: dotted slug, role-prefix, lowercase,
mirrors `token_usage.role`. Cross-billing joins assume both tables follow this
shape.

Examples:
  council.judge.claude.r2
  council.defender.r1
  orchestrator.boss
  agent.zeta
  watchtower.graphrag.archivist
  cli.repl
"""

from __future__ import annotations

import re

# Dotted slug, role-prefix, lowercase. Each segment must start with a letter
# and contain only [a-z0-9_]; segments are separated by dots; at least one dot
# is required (so flat slugs like `cli` are rejected — every caller must name
# both the surface and its sub-role at minimum).
CALLER_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")


def validate_caller(caller: str) -> None:
    """Raise ValueError if caller doesn't match convention. Single SoT.

    Defense-in-depth: every producer that writes a caller tag should call this.
    If a second producer is added without re-validation, malformed tags break
    cross-table joins silently.

    Raises:
        ValueError: if caller is not a string OR doesn't match CALLER_PATTERN.
    """
    if not isinstance(caller, str) or not CALLER_PATTERN.match(caller):
        raise ValueError(
            f"caller {caller!r} does not match convention "
            f"`role.subrole.specific` (lowercase dotted slug)"
        )
