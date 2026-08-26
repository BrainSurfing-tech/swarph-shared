"""Guard: no machine-specific IP literal may ship inside swarph-shared.

Written against the PROPERTY, not the syntax. #546's finder was disarmed by its own
fix (the literal moved into a fallback argument, so the query went blind). This
sweeps for any CGNAT/RFC1918 address in shipped source. Loopback is allowed —
wrong on this fleet, harmless anywhere.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
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


def _sweep(root: Path) -> tuple[list[str], list[str]]:
    """Offender lines under `root`, plus the paths the sweep could NOT cover.

    Two channels, because a sweep that cannot cover a path must SAY so or it
    asserts coverage it did not perform:

      - Decoding is NOT a read failure. errors="replace" keeps the line
        stream intact; IP literals are pure ASCII, so a replacement char can
        neither forge a hit nor hide one. The old form (strict decode +
        `except UnicodeDecodeError: continue`) silently dropped any non-UTF-8
        file — drop-on-meta-edge's Required 3 on PR #24, verified live by
        lab-ovh with a real cp1252 probe. swarph-cli #318 is the reference.
      - An OSError READING a file (permissions, locking) IS a coverage
        failure: the content is UNKNOWN, so the file is named in
        `unreadable` — mesh-gateway #633's coverage channel, ported here.
      - So is an OSError TRAVERSING or STAT-ing, and the stdlib hides both
        by default: pathlib's rglob suppresses scandir errors (a denied
        subtree vanishes) and Path.is_file() swallows OSError into False
        (an unstat-able file vanishes). gpt-ops's P2 on the merged sweep.
        os.walk's onerror names the directory that could not be entered,
        and an explicit os.stat names the file whose metadata could not be
        read — every eligible path is either scanned or named.
    """
    offenders: list[str] = []
    unreadable: list[str] = []

    def _walk_error(exc: OSError) -> None:
        unreadable.append(
            f"{exc.filename or '?'}: walk: {exc.__class__.__name__}: {exc}"
        )

    for dirpath, dirnames, filenames in os.walk(root, onerror=_walk_error):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            path = Path(dirpath) / name
            if path.suffix not in _TEXT_SUFFIXES:
                continue
            try:
                if not stat.S_ISREG(os.stat(path).st_mode):
                    continue  # fifo/socket/device: not sweepable content
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                unreadable.append(f"{path.name}: {exc.__class__.__name__}: {exc}")
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                found = _MACHINE_SPECIFIC.search(line)
                if found:
                    offenders.append(f"{path.name}:{lineno}: {found.group(0)}  |  {line.strip()[:90]}")
    return offenders, unreadable


def test_no_machine_specific_ip_ships_in_the_package() -> None:
    offenders, unreadable = _sweep(SRC)
    # Coverage BEFORE content: if the sweep could not read an eligible file,
    # the offenders list is a partial result and must not be read as complete.
    assert not unreadable, (
        "The sweep could not READ these eligible files, so its result is NOT "
        "full coverage (#633's channel). Fix the read — do not skip the file:\n"
        + "\n".join(unreadable)
    )
    assert not offenders, (
        "Machine-specific address(es) shipped in swarph-shared (#578/#579).\n\n"
        + "\n".join(offenders)
    )


def test_the_guard_actually_fires(tmp_path: Path) -> None:
    """CAN-FAIL: prove the sweep is not vacuously green."""
    (tmp_path / "bad.py").write_text('X = "http://100.107.' + '222.72:8788"\n')
    offenders, _ = _sweep(tmp_path)
    assert offenders


def test_loopback_is_deliberately_allowed(tmp_path: Path) -> None:
    (tmp_path / "ok.py").write_text('X = "http://127.0.0.1:8788"\n')
    offenders, _ = _sweep(tmp_path)
    assert not offenders


def test_a_non_utf8_file_is_SWEPT_not_skipped(tmp_path: Path) -> None:
    """CAN-FAIL for the decode arm: a genuinely non-UTF-8 file carrying an
    offender must still produce the hit.

    Writes REAL cp1252 bytes (0xE9 = 'é', a UnicodeDecodeError under strict
    UTF-8). Before errors="replace", this file was silently dropped — the
    exact shape lab-ovh's live probe proved against the old form.
    """
    target = tmp_path / "legacy.py"
    target.write_bytes('X = "http://100.64.189.91:8788"  # café\n'.encode("cp1252"))
    offenders, unreadable = _sweep(tmp_path)
    assert not unreadable
    assert any("100.64.189.91" in o for o in offenders), (
        f"cp1252 file carrying an offender produced no hit: {offenders}"
    )


def _deny_read(path: Path) -> None:
    if os.name == "nt":
        subprocess.run(
            ["icacls", str(path), "/deny", "*S-1-1-0:R"],
            check=True, capture_output=True,
        )
    else:
        path.chmod(0)


def test_an_unreadable_file_is_NAMED_not_dropped(tmp_path: Path) -> None:
    """CAN-FAIL for the OSError arm (#633's channel): a file the sweep cannot
    read must be reported, or the sweep asserts coverage it did not perform."""
    target = tmp_path / "locked.py"
    target.write_text('X = "http://100.64.189.91:8788"\n')
    _deny_read(target)
    try:
        try:
            target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
        else:
            pytest.skip("this box does not enforce read-denial (e.g. running as root)")
        offenders, unreadable = _sweep(tmp_path)
        assert not offenders, "unreadable content must not leak into the offender channel"
        assert any("locked.py" in u for u in unreadable), unreadable
    finally:
        # Deletion does not require read permission, and tmp_path cleanup
        # handles the rest — no ACL restore needed.
        target.unlink(missing_ok=True)


def test_a_read_failure_is_NAMED_on_every_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DETERMINISTIC twin of the chmod test above (gpt-ops's P2 on the merged
    sweep): the chmod proof SKIPS where the runner is privileged, so on those
    runners the reporting behaviour is unproven. The injected OSError fires
    everywhere. This pins the read arm the merged sweep already had."""
    target = tmp_path / "unreadable.py"
    target.write_text('X = "http://100.64.189.91:8788"\n')
    real_read = Path.read_text

    def _read_bomb(self, *a, **kw):
        if self.name == "unreadable.py":
            raise OSError("injected read failure")
        return real_read(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", _read_bomb)
    offenders, unreadable = _sweep(tmp_path)
    assert not offenders, "unreadable content must not leak into the offender channel"
    assert any("unreadable.py" in u for u in unreadable), unreadable


def test_a_stat_failure_is_NAMED_not_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INJECTED metadata failure: a file whose STAT fails must enter the
    coverage channel. Path.is_file() swallows OSError into False — under the
    rglob+is_file form this file was neither scanned nor named: absent
    rendering as good, one layer down. Deterministic on every runner."""
    target = tmp_path / "ghost.py"
    target.write_text('X = "http://100.64.189.91:8788"\n')
    real_stat = os.stat

    def _stat_bomb(p, *a, **kw):
        if str(p).endswith("ghost.py"):
            raise OSError("injected stat failure")
        return real_stat(p, *a, **kw)

    monkeypatch.setattr(os, "stat", _stat_bomb)
    offenders, unreadable = _sweep(tmp_path)
    assert not offenders, "unstat-able content must not leak into the offender channel"
    assert any("ghost.py" in u for u in unreadable), unreadable


def test_a_traversal_failure_is_NAMED_not_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INJECTED scandir failure: a directory the sweep cannot ENTER may hold
    eligible files, so the coverage claim is false unless the failure is
    named. pathlib's rglob suppresses scandir OSError (3.13+): the whole
    subtree vanished silently. Deterministic on every runner."""
    sub = tmp_path / "denied"
    sub.mkdir()
    (sub / "hidden.py").write_text('X = "http://100.64.189.91:8788"\n')
    real_scandir = os.scandir

    def _scandir_bomb(p):
        # pathlib's rglob passes directory paths with a trailing separator.
        # The 3-arg form sets .filename, as real scandir failures carry it.
        if str(p).rstrip("/\\").endswith("denied"):
            raise PermissionError(13, "injected traversal failure", str(p))
        return real_scandir(p)

    monkeypatch.setattr(os, "scandir", _scandir_bomb)
    offenders, unreadable = _sweep(tmp_path)
    assert not offenders, "an unentered subtree must not leak into the offender channel"
    assert any("denied" in u for u in unreadable), unreadable


def test_the_shipped_default_is_EMPTY() -> None:
    """The property, observed in a SUBPROCESS with the env removed.

    THREE EARLIER ATTEMPTS WERE EACH DISARMABLE, and the third was only caught by
    a reviewer (drop-on-meta-edge, seat-A, 17 variants on sys3):

      1. monkeypatch DEFAULT_GATEWAY_URL to "" then assert the refusal — SET THE
         VERY THING IT OBSERVED. Vacuous: re-introducing a literal left all green.
      2. importlib.reload() — observed the real value but replaced the module
         object and broke 6 unrelated tests.
      3. regex over the source for the os.getenv default argument — reads ONE
         argument, so it is blind to anything appended after it. Proven by drop:
             DEFAULT_GATEWAY_URL = os.getenv(..., "").strip() or "http://lab-ovh-1:8788"
         leaves this file 6/6 GREEN. A second assignment on the next line does too.

    A subprocess reads the value the package ACTUALLY SHIPS, whatever syntax
    produces it — or-append, second assignment, computed, or plain literal.
    """
    import subprocess
    import sys

    # PYTHONPATH pins the probe to THIS tree. Without it the subprocess imports
    # the INSTALLED swarph_shared and reports its default instead — which on this
    # box still reads "http://localhost:8788" from before #548, so the guard would
    # fail against a package it is not testing. Version is not a location.
    env = {k: v for k, v in os.environ.items() if k != "MESH_GATEWAY_URL"}
    env["PYTHONPATH"] = str(SRC.parent)
    out = subprocess.run(
        [sys.executable, "-c",
         "from swarph_shared import peer_registry as p; print(repr(p.DEFAULT_GATEWAY_URL))"],
        capture_output=True, text=True, env=env,
    )
    assert out.returncode == 0, f"probe failed: {out.stderr[-400:]}"
    shipped = out.stdout.strip()
    assert shipped in ("''", '""'), (
        f"swarph-shared ships a gateway host default: {shipped} (#578/#579). "
        "A default that names a machine has that machine's lifetime."
    )


def test_import_time_capture_does_not_outlive_an_env_unset() -> None:
    """#632 — THE guard for the import-time capture defect.

    gpt-ops's repro (DM #29321): import peer_registry WITH MESH_GATEWAY_URL
    set, unset it, then call canonical_names(ttl=0) cold. The import-time
    assignment keeps dialling the CAPTURED value; the call must instead
    fail soft with GatewayUnreachableError naming MESH_GATEWAY_URL.

    Runs in a SUBPROCESS with NOTHING patched: the two behavioural tests
    below pin `DEFAULT_GATEWAY_URL` — the very symbol whose staleness is
    the defect — so they are green against it BY CONSTRUCTION and are NOT
    the guard for it. A test that patches the symbol under test cannot
    fail on it. This one observes the package as shipped: env at import,
    env removed, call.
    """
    import subprocess
    import sys
    import textwrap

    env = dict(os.environ)
    env["MESH_GATEWAY_URL"] = "http://gateway.invalid:8788"  # set AT IMPORT
    env["PYTHONPATH"] = str(SRC.parent)
    code = textwrap.dedent(
        """
        import os
        from swarph_shared import peer_registry as p
        os.environ.pop("MESH_GATEWAY_URL")  # unset AFTER import
        try:
            p.canonical_names(ttl_seconds=0)
        except p.GatewayUnreachableError as e:
            print("RAISED:", str(e)[:300])
        else:
            print("NO-RAISE")
        """
    )
    out = subprocess.run([sys.executable, "-c", code],
                         capture_output=True, text=True, env=env, timeout=60)
    assert out.returncode == 0, f"probe failed: {out.stderr[-400:]}"
    assert "MESH_GATEWAY_URL is not set" in out.stdout, (
        "unset-after-import must read the CALL-TIME env and take the "
        "fail-soft 'not set' path; instead the call used the import-time "
        f"capture. probe said: {out.stdout.strip()[:300]}"
    )


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
    monkeypatch.setattr(pr, "_cache", {"names": None, "fetched_at": 0.0})
    with pytest.raises(pr.GatewayUnreachableError, match="MESH_GATEWAY_URL is not set"):
        pr.canonical_names(ttl_seconds=0)


def test_unset_gateway_still_serves_a_warm_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail-soft means fail-soft: a warm cache survives an unset gateway."""
    from swarph_shared import peer_registry as pr

    monkeypatch.delenv("MESH_GATEWAY_URL", raising=False)
    monkeypatch.setattr(pr, "_cache", {"names": {"lab-ovh"}, "fetched_at": time.time()})
    assert pr.canonical_names(ttl_seconds=0) == {"lab-ovh"}
