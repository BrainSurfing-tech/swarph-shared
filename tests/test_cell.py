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
    VALID_PROVIDERS_V0_7,
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


def test_valid_providers_v0_7_is_claude_only():
    assert VALID_PROVIDERS_V0_7 == frozenset({"claude"})


def test_peer_name_re_accepts_kebab_and_snake():
    # Regex requires 2+ chars (anchor `[a-z]` followed by `[a-z0-9_-]{1,63}`),
    # rejects 1-char names. That's intentional — peer names should be
    # discoverable + greppable, not bare-letter identifiers.
    for name in ("lab-ovh", "drop", "drop-on-meta-edge", "lab_ovh", "ab"):
        assert PEER_NAME_RE.match(name), f"expected match: {name!r}"


def test_peer_name_re_rejects_single_char():
    assert not PEER_NAME_RE.match("x")  # too short per pattern


def test_peer_name_re_rejects_uppercase_and_leading_special():
    for name in ("Lab-OVH", "-lab", "_lab", "1lab", "", "lab ovh"):
        assert not PEER_NAME_RE.match(name), f"expected reject: {name!r}"


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
    assert cell.lineage is None
    assert cell.source_path is None
    assert cell.extra == {}


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
    with pytest.raises(CellError, match="kebab/snake-case"):
        parse_cell_dict(_minimal_dict(name="UPPER_CASE"))


def test_parse_rejects_missing_name():
    raw = _minimal_dict()
    del raw["name"]
    with pytest.raises(CellError, match="kebab/snake-case"):
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


def test_parse_rejects_non_claude_provider_in_v0_7():
    with pytest.raises(CellError, match="v0.8"):
        parse_cell_dict(_minimal_dict(provider="gemini"))


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
