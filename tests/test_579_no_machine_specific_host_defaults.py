"""Guard: no machine-specific IP literal may ship inside swarph-shared.

Written against the PROPERTY, not the syntax. #546's finder was disarmed by its own
fix (the literal moved into a fallback argument, so the query went blind). This
sweeps for any CGNAT/RFC1918 address in shipped source. Loopback is allowed —
wrong on this fleet, harmless anywhere.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "swarph_shared"

_MACHINE_SPECIFIC = re.compile(
    r"""\b(
          100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}
        | 10\.\d{1,3}\.\d{1,3}\.\d{1,3}
        | 172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}
        | 192\.168\.\d{1,3}\.\d{1,3}
    )\b""",
    re.VERBOSE,
)

_TEXT_SUFFIXES = {".py", ".md", ".default", ".service", ".timer", ".sh", ".toml", ".json"}


def _sweep(root: Path) -> list[str]:
    offenders: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in _TEXT_SUFFIXES:
            continue
        if "__pycache__" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            found = _MACHINE_SPECIFIC.search(line)
            if found:
                offenders.append(f"{path.name}:{lineno}: {found.group(0)}  |  {line.strip()[:90]}")
    return offenders


def test_no_machine_specific_ip_ships_in_the_package() -> None:
    offenders = _sweep(SRC)
    assert not offenders, (
        "Machine-specific address(es) shipped in swarph-shared (#578/#579).\n\n"
        + "\n".join(offenders)
    )


def test_the_guard_actually_fires(tmp_path: Path) -> None:
    """CAN-FAIL: prove the sweep is not vacuously green."""
    (tmp_path / "bad.py").write_text('X = "http://100.107.' + '222.72:8788"\n')
    assert _sweep(tmp_path)


def test_loopback_is_deliberately_allowed(tmp_path: Path) -> None:
    (tmp_path / "ok.py").write_text('X = "http://127.0.0.1:8788"\n')
    assert not _sweep(tmp_path)


def test_unset_gateway_raises_the_SAME_error_as_an_unreachable_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The behaviour removing the literal is FOR.

    `canonical_names` is deliberately fail-soft. "Unconfigured" therefore
    degrades through the EXISTING unreachable path rather than inventing a second
    shape a caller would have to learn.
    """
    from swarph_shared import peer_registry as pr

    monkeypatch.delenv("MESH_GATEWAY_URL", raising=False)
    monkeypatch.setattr(pr, "DEFAULT_GATEWAY_URL", "")
    monkeypatch.setattr(pr, "_cache", {"names": None, "fetched_at": 0.0})
    with pytest.raises(pr.GatewayUnreachableError, match="MESH_GATEWAY_URL is not set"):
        pr.canonical_names(ttl_seconds=0)


def test_unset_gateway_still_serves_a_warm_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail-soft means fail-soft: a warm cache survives an unset gateway."""
    from swarph_shared import peer_registry as pr

    monkeypatch.delenv("MESH_GATEWAY_URL", raising=False)
    monkeypatch.setattr(pr, "DEFAULT_GATEWAY_URL", "")
    monkeypatch.setattr(pr, "_cache", {"names": {"lab-ovh"}, "fetched_at": time.time()})
    assert pr.canonical_names(ttl_seconds=0) == {"lab-ovh"}
