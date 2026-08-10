"""Release metadata must agree with the package's public version."""

from __future__ import annotations

import re
from pathlib import Path

import swarph_shared


def test_version_constant_matches_pyproject():
    root = Path(__file__).resolve().parents[1]
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match, "pyproject.toml must declare the package version"
    assert swarph_shared.__version__ == match.group(1)
