"""Tests for ``swarph_shared.cell`` (v0.3.0 — substrate-doc R7 §11.1.5 (O5))."""

from __future__ import annotations

from pathlib import Path

import pytest

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


# ---------------------------------------------------------------------------
# Module surface — exports + constants
# ---------------------------------------------------------------------------


def test_schema_version_v1_is_only_supported_version():
    assert SCHEMA_VERSION_V1 == "v1"
    assert VALID_SCHEMA_VERSIONS == frozenset({"v1"})


def test_valid_providers_pins_the_set_AND_the_ORDERING_CONTRACT():
    """Pins the EXACT set, so adding a provider is a deliberate edit rather than
    a silent widening.

    >>> AND THE EDIT IS NOT SAFE ON ITS OWN. swarph-cli's spawn.py holds
    `VALID_PROVIDERS ⊆ MEMBRANES` and RAISES AT IMPORT if a name here has no
    membrane there — so adding a provider BREAKS `swarph spawn` for everyone on
    the new release until the membrane ships. MEMBRANE FIRST, THEN THIS SET. <<<

    Measured: 0.6.0 added `vibe` here first (board #247's title said "blocked on
    swarph-shared adding 'vibe' FIRST" — backwards), and every fresh
    `pip install swarph-cli` broke for ~5h because the CLI pins only >=0.4.0 with
    no upper bound. Reverted in 0.6.1.

    THIS PACKAGE CANNOT ASSERT THE INVARIANT — importing swarph-cli is circular —
    so whoever edits this line must carry it. That is what this docstring is for:
    the test cannot fail on the ordering, only a reader can."""
    assert VALID_PROVIDERS == frozenset(
        {"claude", "codex", "antigravity", "grok", "vibe"})


def test_provider_extensions_are_opt_in_for_the_matching_client_runtime():
    raw = {"schema_version": "v1", "name": "muse-1", "provider": "muse",
           "role": "worker", "cwd": "/tmp"}
    with pytest.raises(CellError, match="provider"):
        parse_cell_dict(raw)

    cell = parse_cell_dict(raw, allowed_providers=VALID_PROVIDERS | {"muse"})
    assert cell.provider == "muse"


def test_a_vibe_cell_yaml_VALIDATES_now_that_the_membrane_has_SHIPPED():
    """>>> 0.6.2: THE MEMBRANE HAS SHIPPED (swarph-cli 0.41.5 on PyPI), SO A VIBE
    CELL.YAML VALIDATES AGAIN — flipped WITH the membrane, exactly as 0.6.1's
    docstring instructed and 0.6.0 failed to do. <<<
    The full precondition, verified before this release rather than assumed:
    0.41.5 is on /simple/, it carries `class VibeMembrane`, it declares
    `swarph-shared>=0.4.0,!=0.6.0,<0.7` (so 0.6.2 satisfies it), and the lab-ovh
    editable tree that five live cells run off reports unmembraned = []."""
    cell = parse_cell_dict(
        {"schema_version": "v1", "name": "vibe-1", "provider": "vibe",
         "role": "worker", "cwd": "/tmp"},
        base_dir=None,
    )
    assert cell.provider == "vibe"


def test_an_UNKNOWN_provider_is_still_refused_and_the_error_NAMES_the_set():
    """The negative leg. Without it, `validate` could have been changed to accept
    anything and both tests above would still pass."""
    import pytest
    with pytest.raises(CellError) as e:
        parse_cell_dict(
            {"schema_version": "v1", "name": "vibe-neg", "provider": "not-a-provider",
             "role": "worker", "cwd": "/tmp"},
            base_dir=None,
        )
    # >>> ASSERT AGAINST A MEMBER OF THE SET, NOT A HARDCODED NAME. The first
    # version checked for "vibe" — which passed while vibe was supported and
    # broke the moment it was reverted, testing the ROSTER instead of the
    # PROPERTY. The property is: the refusal lists what IS supported. <<<
    msg = str(e.value)
    for supported in sorted(VALID_PROVIDERS):
        assert supported in msg, (
            f"the refusal omits the supported provider {supported!r}, so a caller "
            f"cannot learn the rule from it: {msg}")


def test_peer_name_re_accepts_kebab():
    # Regex requires 2+ chars (rejects 1-char names) — peer names should be
    # discoverable + greppable, not bare-letter identifiers.
    for name in ("lab-ovh", "drop", "drop-on-meta-edge", "ab"):
        assert PEER_NAME_RE.match(name), f"expected match: {name!r}"


def test_peer_name_re_rejects_single_char():
    assert not PEER_NAME_RE.match("x")  # too short per pattern


def test_peer_name_re_rejects_uppercase_and_leading_special():
    for name in ("Lab-OVH", "-lab", "_lab", "1lab", "", "lab ovh"):
        assert not PEER_NAME_RE.match(name), f"expected reject: {name!r}"


def test_peer_name_re_rejects_underscore_and_trailing_dash():
    # Underscores + trailing dash used to pass the cell check but FAIL the
    # mesh send-boundary (NAMING_CONVENTION_REGEX) — boot-but-unaddressable.
    # PEER_NAME_RE now IS the registry regex, so these are rejected at boot.
    for name in ("lab_ovh", "foo-", "foo_", "a" * 65):
        assert not PEER_NAME_RE.match(name), f"expected reject: {name!r}"


def test_peer_name_re_is_the_registry_regex():
    # Single source of truth: a cell that boots is guaranteed mesh-addressable.
    from swarph_shared.peer_registry import NAMING_CONVENTION_REGEX

    assert PEER_NAME_RE is NAMING_CONVENTION_REGEX


# ---------------------------------------------------------------------------
# validate_uuid_str
# ---------------------------------------------------------------------------


def test_validate_uuid_str_accepts_canonical():
    canonical = "550e8400-e29b-41d4-a716-446655440000"
    assert validate_uuid_str(canonical) == canonical


def test_validate_uuid_str_rejects_garbage():
    with pytest.raises(CellError, match="not a valid UUID"):
        validate_uuid_str("not-a-uuid")


def test_validate_uuid_str_rejects_none():
    with pytest.raises(CellError, match="not a valid UUID"):
        validate_uuid_str(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# parse_cell_dict — happy paths
# ---------------------------------------------------------------------------


def _minimal_dict(**overrides):
    base = {
        "schema_version": "v1",
        "name": "lab-ovh",
        "role": "lab",
        "cwd": "/tmp",
        "provider": "claude",
    }
    base.update(overrides)
    return base


def test_parse_minimal_required_fields():
    cell = parse_cell_dict(_minimal_dict())
    assert cell.name == "lab-ovh"
    assert cell.role == "lab"
    assert cell.cwd == Path("/tmp")
    assert cell.provider == "claude"
    assert cell.schema_version == "v1"
    assert cell.session_id is None
    assert cell.starter_prompt_path is None
    assert cell.sandbox is None
    assert cell.lineage is None
    assert cell.source_path is None
    assert cell.extra == {}


def test_parse_codex_provider():
    cell = parse_cell_dict(_minimal_dict(provider="codex"))
    assert cell.provider == "codex"


def test_parse_with_sandbox():
    cell = parse_cell_dict(_minimal_dict(provider="codex", sandbox="read-only"))
    assert cell.sandbox == "read-only"


def test_parse_strips_sandbox_whitespace():
    cell = parse_cell_dict(_minimal_dict(provider="codex", sandbox="  workspace-write  "))
    assert cell.sandbox == "workspace-write"


def test_parse_with_pinned_session_id():
    fixed = "550e8400-e29b-41d4-a716-446655440000"
    cell = parse_cell_dict(_minimal_dict(session_id=fixed))
    assert cell.session_id == fixed


def test_parse_with_lineage_block():
    cell = parse_cell_dict(_minimal_dict(identity={
        "lineage": {
            "parent_peer_id": "drop",
            "spawn_manifest_signature": None,
        }
    }))
    assert isinstance(cell.lineage, Lineage)
    assert cell.lineage.parent_peer_id == "drop"
    assert cell.lineage.spawn_manifest_signature is None


def test_parse_relative_cwd_resolved_against_base_dir(tmp_path):
    raw = _minimal_dict(cwd="subdir")
    sub = tmp_path / "subdir"
    sub.mkdir()
    cell = parse_cell_dict(raw, base_dir=tmp_path)
    assert cell.cwd == sub.resolve()


def test_parse_relative_starter_prompt_resolved_against_base_dir(tmp_path):
    raw = _minimal_dict(cwd=str(tmp_path), starter_prompt_path="starter.md")
    cell = parse_cell_dict(raw, base_dir=tmp_path)
    assert cell.starter_prompt_path == (tmp_path / "starter.md").resolve()


def test_parse_extra_keys_preserved_for_forward_compat():
    raw = _minimal_dict(mesh={"gateway": "http://x"}, custom="v")
    cell = parse_cell_dict(raw)
    assert cell.extra["mesh"] == {"gateway": "http://x"}
    assert cell.extra["custom"] == "v"


def test_parse_strips_role_whitespace():
    cell = parse_cell_dict(_minimal_dict(role="  lab  "))
    assert cell.role == "lab"


# ---------------------------------------------------------------------------
# parse_cell_dict — validation errors
# ---------------------------------------------------------------------------


def test_parse_top_level_must_be_dict():
    with pytest.raises(CellError, match="must be a mapping"):
        parse_cell_dict(["a", "b"])


def test_parse_rejects_invalid_peer_name():
    with pytest.raises(CellError, match="kebab-case"):
        parse_cell_dict(_minimal_dict(name="UPPER_CASE"))


def test_parse_rejects_missing_name():
    raw = _minimal_dict()
    del raw["name"]
    with pytest.raises(CellError, match="kebab-case"):
        parse_cell_dict(raw)


def test_parse_rejects_empty_role():
    with pytest.raises(CellError, match="'role' is required"):
        parse_cell_dict(_minimal_dict(role=""))


def test_parse_rejects_missing_role():
    raw = _minimal_dict()
    del raw["role"]
    with pytest.raises(CellError, match="'role' is required"):
        parse_cell_dict(raw)


def test_parse_rejects_empty_cwd():
    with pytest.raises(CellError, match="'cwd' is required"):
        parse_cell_dict(_minimal_dict(cwd=""))


def test_parse_rejects_invalid_session_id_type():
    with pytest.raises(CellError, match="must be a string UUID"):
        parse_cell_dict(_minimal_dict(session_id=123))


def test_parse_rejects_invalid_session_id_value():
    with pytest.raises(CellError, match="not a valid UUID"):
        parse_cell_dict(_minimal_dict(session_id="not-a-uuid"))


def test_parse_rejects_unsupported_schema_version():
    with pytest.raises(CellError, match="schema_version"):
        parse_cell_dict(_minimal_dict(schema_version="v999"))


def test_parse_rejects_unsupported_provider():
    with pytest.raises(CellError, match="Unsupported provider"):
        parse_cell_dict(_minimal_dict(provider="gemini"))


def test_parse_rejects_invalid_sandbox_type():
    with pytest.raises(CellError, match="sandbox"):
        parse_cell_dict(_minimal_dict(sandbox=12))


def test_parse_rejects_empty_sandbox():
    with pytest.raises(CellError, match="sandbox"):
        parse_cell_dict(_minimal_dict(sandbox=""))


def test_parse_rejects_invalid_starter_prompt_path_type():
    with pytest.raises(CellError, match="starter_prompt_path"):
        parse_cell_dict(_minimal_dict(starter_prompt_path=12))


def test_parse_rejects_non_dict_identity():
    with pytest.raises(CellError, match="'identity' must be a mapping"):
        parse_cell_dict(_minimal_dict(identity="not-a-dict"))


def test_parse_rejects_non_dict_lineage():
    with pytest.raises(CellError, match="'identity.lineage' must be a mapping"):
        parse_cell_dict(_minimal_dict(identity={"lineage": "not-a-dict"}))


# ---------------------------------------------------------------------------
# Schema-stability discipline (drop-mother review #890 (C2))
# ---------------------------------------------------------------------------


def test_v0_6_cell_yaml_shape_parses_unchanged():
    """v0.6 cell.yaml files (no schema_version field; default to v1) MUST
    keep working unchanged in v0.7+. Schema-stability commitment per
    drop-mother review #890 (C2)."""
    v0_6_shape = {
        "name": "lab-ovh",
        "role": "lab",
        "cwd": "/tmp",
        # no schema_version, no provider, no identity — minimal v0.6
    }
    cell = parse_cell_dict(v0_6_shape)
    assert cell.schema_version == "v1"  # default-applied
    assert cell.provider == "claude"  # default-applied
    assert cell.lineage is None  # absent


# ---------------------------------------------------------------------------
# assisted_memory
# ---------------------------------------------------------------------------


def test_parse_assisted_memory_enabled_valid():
    cell = parse_cell_dict(_minimal_dict(assisted_memory={"enabled": True, "repo": "test/repo", "interval_min": 10}))
    assert cell.assisted_memory is not None
    assert cell.assisted_memory["enabled"] is True
    assert cell.assisted_memory["repo"] == "test/repo"
    assert cell.assisted_memory["interval_min"] == 10


def test_parse_assisted_memory_enabled_without_repo_rejects():
    with pytest.raises(CellError, match="repo.*required"):
        parse_cell_dict(_minimal_dict(assisted_memory={"enabled": True}))


def test_parse_assisted_memory_enabled_empty_repo_rejects():
    with pytest.raises(CellError, match="repo.*required"):
        parse_cell_dict(_minimal_dict(assisted_memory={"enabled": True, "repo": "   "}))


def test_parse_assisted_memory_disabled_no_repo_valid():
    cell = parse_cell_dict(_minimal_dict(assisted_memory={"enabled": False}))
    assert cell.assisted_memory is not None
    assert cell.assisted_memory["enabled"] is False
    assert cell.assisted_memory.get("repo") is None
    assert cell.assisted_memory["interval_min"] == 15


def test_parse_assisted_memory_absent_valid():
    cell = parse_cell_dict(_minimal_dict())
    assert cell.assisted_memory is None


def test_parse_assisted_memory_invalid_type_rejects():
    with pytest.raises(CellError, match="must be a mapping"):
        parse_cell_dict(_minimal_dict(assisted_memory="not-a-dict"))


def test_parse_assisted_memory_invalid_enabled_type_rejects():
    with pytest.raises(CellError, match="enabled.*boolean"):
        parse_cell_dict(_minimal_dict(assisted_memory={"enabled": "true", "repo": "test/repo"}))


def test_parse_assisted_memory_invalid_interval_min_rejects():
    with pytest.raises(CellError, match="interval_min.*positive integer"):
        parse_cell_dict(_minimal_dict(assisted_memory={"enabled": True, "repo": "test", "interval_min": -5}))



# --- F4 fix: cursor_path / tmux_session typed fields + extra: flatten ---

_F4_BASE = {
    "schema_version": "v1",
    "name": "gpu-wsl",
    "role": "gpu-wsl",
    "cwd": "/home/darw007d",
    "provider": "claude",
}


def test_f4_nested_extra_block_no_longer_double_nests():
    # The documented `extra:` block shape must populate the typed fields
    # (regression: previously survived into Cell.extra['extra'] -> None).
    cell = parse_cell_dict(
        {**_F4_BASE, "extra": {"cursor_path": "/c.json", "tmux_session": "claude"}}
    )
    assert cell.cursor_path == "/c.json"
    assert cell.tmux_session == "claude"
    assert "extra" not in cell.extra  # flattened, not double-nested


def test_f4_flat_top_level_pins_populate_typed_fields():
    cell = parse_cell_dict(
        {**_F4_BASE, "cursor_path": "/c.json", "tmux_session": "claude"}
    )
    assert cell.cursor_path == "/c.json"
    assert cell.tmux_session == "claude"


def test_f4_pins_mirrored_into_extra_for_backcompat_readers():
    # graduate-to-typed-field preserves the extra-dict reading path
    cell = parse_cell_dict(
        {**_F4_BASE, "cursor_path": "/c.json", "tmux_session": "claude"}
    )
    assert cell.extra.get("cursor_path") == "/c.json"
    assert cell.extra.get("tmux_session") == "claude"


def test_f4_absent_pins_default_to_none():
    cell = parse_cell_dict(dict(_F4_BASE))
    assert cell.cursor_path is None
    assert cell.tmux_session is None


def test_f4_top_level_pin_wins_over_nested_extra():
    cell = parse_cell_dict(
        {
            **_F4_BASE,
            "cursor_path": "/top.json",
            "extra": {"cursor_path": "/nested.json"},
        }
    )
    assert cell.cursor_path == "/top.json"
    # Back-compat: extra dict must also reflect the winning (top-level) value.
    assert cell.extra.get("cursor_path") == "/top.json"


def test_f4_non_string_pin_raises_cellerror():
    with pytest.raises(CellError):
        parse_cell_dict({**_F4_BASE, "cursor_path": 123})


def test_f4_non_mapping_extra_raises_cellerror():
    with pytest.raises(CellError):
        parse_cell_dict({**_F4_BASE, "extra": "not-a-dict"})
