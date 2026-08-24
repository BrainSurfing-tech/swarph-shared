"""Card #548 — no loopback gateway default survives in swarph-shared.

The mesh-gateway binds the tailnet IP ONLY (100.107.222.72:8788); localhost
has never been bound. A loopback DEFAULT fails as a bare "Connection refused"
with no cause named — and is invisible to exactly the people who would review
it, because their shells carry MESH_GATEWAY_URL. swarph-cli #546 proved the
class; this sweep keeps the class out of THIS repo.

Run: python -m pytest tests/test_548_default_gateway.py -v
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _src_root() -> Path:
    return Path(__file__).resolve().parent.parent / "src" / "swarph_shared"


def test_no_loopback_gateway_default_anywhere_in_src():
    offenders = []
    for path in _src_root().rglob("*.py"):
        # encoding="utf-8" is load-bearing: without it Windows reads with the
        # locale codec and the sweep crashes on the first non-ASCII byte.
        for i, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            stripped = line.strip()
            # A COMMENT MAY NAME THE DEFECT IT DESCRIBES; docstrings that
            # document the fix may too. Only CODE lines are flagged.
            if stripped.startswith("#"):
                continue
            if "8788" not in line:
                continue
            if "localhost:8788" in line or "127.0.0.1:8788" in line:
                offenders.append(
                    f"{path.relative_to(_src_root())}:{i}: {line.strip()[:90]}"
                )
    assert not offenders, (
        "a loopback gateway default survives in src/ — the mesh-gateway binds "
        "the tailnet IP only, so this fails as a bare 'Connection refused' "
        "with no cause named:\n  " + "\n  ".join(offenders)
    )


def test_the_source_sweep_can_fail(tmp_path, monkeypatch):
    """>>> PROVE THE SWEEP FIRES. <<< A detector that has only ever seen a clean
    tree is indistinguishable from one that matches nothing."""
    fake = tmp_path / "swarph_shared"
    fake.mkdir()
    (fake / "bad.py").write_text(
        'DEFAULT_GATEWAY_URL = "http://localhost:8788"\n', encoding="utf-8"
    )
    monkeypatch.setattr(f"{__name__}._src_root", lambda: fake)
    with pytest.raises(AssertionError):
        test_no_loopback_gateway_default_anywhere_in_src()
