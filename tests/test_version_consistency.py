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


def test_publish_workflow_refuses_tags_outside_main_history():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github" / "workflows" / "pypi-publish.yml").read_text(
        encoding="utf-8"
    )
    assert "fetch-depth: 0" in workflow
    assert 'git merge-base --is-ancestor "$GITHUB_SHA" origin/main' in workflow
