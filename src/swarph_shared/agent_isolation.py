"""Disposable-HOME credential isolation for headless agent spawns (#2a).

Generalises grok's in-repo isolation (swarph-cli spawn.py) to any provider. A
spawned agent receives a HOME that carries ONLY its own CLI auth — never the
operator's ~/.config/gh, ~/.git-credentials, ~/.netrc, ~/.ssh, which are simply
never linked in. Pure helpers (top half) are unit-tested; prepare_isolated_home
is a best-effort seam that never crashes a spawn.

See swarph-cli/docs/superpowers/specs/2026-07-13-swairm-pattern-port-design.md.
"""
from __future__ import annotations

import json
import sys
import time
from collections.abc import Mapping
from pathlib import Path

from swarph_shared.subprocess_env import FORBIDDEN_KEYS_EXPLICIT, FORBIDDEN_SUFFIXES

#: Per-provider auth path(s), RELATIVE to HOME — so they compose with the
#: disposable HOME. Only these are linked in; nothing else on disk is reachable.
PROVIDER_AUTH: dict[str, tuple[str, ...]] = {
    "claude": (".claude/.credentials.json",),
    "codex": (".codex/auth.json",),
    "gemini": (".gemini/oauth_creds.json",),
    "grok": (".grok/auth.json",),
}

#: Namespace prefixes whose *_HOME / *_AUTH_PATH / *_AUTH_PROVIDER_COMMAND /
#: *_CONFIG_DIR keys would redirect a CLI off the forced HOME. Deny per provider.
_PROVIDER_PREFIXES: dict[str, tuple[str, ...]] = {
    "claude": ("CLAUDE_", "ANTHROPIC_"),
    "codex": ("CODEX_", "OPENAI_"),
    "gemini": ("GEMINI_", "GOOGLE_"),
    "grok": ("GROK_", "XAI_"),
}
_REDIRECT_SUFFIXES = ("_HOME", "_AUTH_PATH", "_AUTH_PROVIDER_COMMAND", "_CONFIG_DIR")

#: Provider-INDEPENDENT credential channels that survive a forced HOME and would
#: hand the spawned agent the operator's real credentials WITHOUT touching any
#: file this module links. Forcing HOME alone does NOT close these — each is an
#: env var read before, or instead of, the HOME-anchored path:
#:   * SSH_AUTH_SOCK        — live ssh-agent socket → auth as operator over SSH,
#:                            no ~/.ssh access needed.
#:   * GH_*/GITHUB_* tokens — gh/git read these BEFORE ~/.config/gh.
#:   * GH_CONFIG_DIR / XDG_* — redirect a tool's config dir back into operator
#:                            space despite the forced HOME (gh: GH_CONFIG_DIR →
#:                            $XDG_CONFIG_HOME/gh → $HOME/.config/gh).
#:   * GIT_CONFIG_GLOBAL/SYSTEM — re-point git past the drone's own .gitconfig.
#:   * GNUPGHOME            — gpg home redirect.
#: (Security-review finding, 2026-07-13 — the negative must hold across env
#: channels, not just the file path.)
_CREDENTIAL_REDIRECT_KEYS = frozenset({
    "SSH_AUTH_SOCK",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "GH_CONFIG_DIR",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "XDG_STATE_HOME",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GNUPGHOME",
})


def scrub_provider_namespace(env: dict, provider: str) -> None:
    """In-place: drop redirect keys in ``provider``'s namespace (best-effort)."""
    prefixes = _PROVIDER_PREFIXES.get(provider, ())
    if not prefixes:
        return
    for key in list(env):
        if key.startswith(prefixes) and key.endswith(_REDIRECT_SUFFIXES):
            env.pop(key, None)


def build_isolated_env(source: Mapping[str, str], home: Path, provider: str) -> dict[str, str]:
    """Billing-scrubbed env with HOME forced to ``home`` and redirects scrubbed.

    Pure: ``source`` is never mutated. HOME is IMPOSED (never taken from
    ``source``) — that is what cuts the spawned agent's access to on-disk creds.
    """
    env = {
        k: v for k, v in source.items()
        if k not in FORBIDDEN_KEYS_EXPLICIT
        and k not in _CREDENTIAL_REDIRECT_KEYS
        and not k.endswith(FORBIDDEN_SUFFIXES)
    }
    scrub_provider_namespace(env, provider)
    env["HOME"] = str(home)
    return env


_GITCONFIG = "[user]\n\tname = swarph drone\n\temail = drone@swarph.local\n[credential]\n\thelper =\n"


class CredentialConflict(RuntimeError):
    """Linking would destroy a usable credential. Refused, for a human to resolve.

    ``str(exc)`` IS DELIBERATELY SHORT AND CARRIES NO PATHS. swarph-cli's
    service/app.py puts ``str(exc)`` of any RuntimeError straight into an HTTP 502
    body, in the same block that truncates CLI stderr to 200 chars precisely so
    credentials and environment never leave the box. The paths live on ``.link``
    and ``.target`` for the caller to LOG; the full sentence goes to stderr here.
    (science-claude, review of PR #27, finding 3.)
    """

    def __init__(self, summary: str, link: Path, target: Path, detail: str) -> None:
        super().__init__(summary)
        self.link = link
        self.target = target
        self.detail = detail


def _stderr(msg: str) -> None:
    """Every previously-silent branch of this module ends here.

    #923: a drone credential sat blanked for 44h across 251 spawns and NOTHING
    printed. A seam that is allowed to do nothing must still be allowed to say so.
    """
    sys.stderr.write(f"swarph_shared.agent_isolation: {msg}\n")


def _freshness(path: Path, provider: str | None) -> tuple[bool, float]:
    """-> (usable, rank). Rank is comparable only BETWEEN FILES OF ONE PROVIDER.

    For claude the rank is the OAuth ``expiresAt``, which is what the two files
    actually disagree about; for every other provider it is mtime, because this
    module has no parser for their formats and mtime is the honest floor.
    """
    if provider == "claude":
        try:
            obj = json.loads(path.read_text(encoding="utf-8")).get("claudeAiOauth")
        except (OSError, ValueError, AttributeError):
            return (False, -1.0)
        if not isinstance(obj, dict):
            return (False, -1.0)
        exp = obj.get("expiresAt")
        return (bool((obj.get("accessToken") or "").strip()),
                float(exp) if isinstance(exp, (int, float)) else -1.0)
    try:
        st = path.stat()
    except OSError:
        return (False, -1.0)
    return (st.st_size > 0, float(st.st_mtime))


def _link_auth(link: Path, target: Path, provider: str | None = None) -> None:
    """Idempotent best-effort symlink ``link`` -> ``target`` (mirrors _link_grok_auth).

    ONE INODE, TWO PATHS is the whole design: a refresh on either side is
    instantly visible to the other, so there is no window in which the drone and
    the operator hold different tokens and no copy to keep in step.

    #923 replaced the old ``elif link.exists(): return  # never clobber a real
    file`` guard. That guard was defending against a case that cannot arise --
    inside a drone home THIS function creates and owns, the only writer of a
    PROVIDER_AUTH path is this function -- and it cost 44h of silent failure: a
    regular file appeared at the claude link path holding a credential whose
    refresh token had hard-expired, and every subsequent spawn hit the guard and
    left it there. A regular file at this path is now REPLACED when doing so
    loses nothing, and raises ``CredentialConflict`` when it would.
    """
    if not target.exists():
        # macOS keeps some CLI credentials in the Keychain, not on disk; a
        # provider may simply not be logged in. Both are legitimate and neither
        # should be silent -- the drone gets a HOME with no auth either way.
        _stderr(f"{target} does not exist — the spawn will have no {provider or 'provider'} auth")
        return
    try:
        if link.is_symlink():
            if link.readlink() == target:
                return             # already the intended binding
            link.unlink()          # stale/foreign/dangling → replace
        elif link.exists():
            here_ok, here = _freshness(link, provider)
            there_ok, there = _freshness(target, provider)
            # TWO conditions refuse, and they have DIFFERENT causes and different
            # remedies. Ranking them together made an unusable operator credential
            # raise "the signature of a CLI that rewrites by rename" -- a diagnosis
            # of a mechanism not in play, prescribing a directory bind that cannot
            # help, because the parent of a blank file holds a blank file.
            # (science-claude, review of PR #27, finding 1.)
            if here_ok and not there_ok:
                detail = (
                    f"the OPERATOR credential at {target} is blank or unparseable, while "
                    f"{link} holds a usable one. Linking now would replace a working "
                    f"credential with a broken one. RE-AUTHENTICATE ON THIS BOX; nothing "
                    f"about the drone home needs changing. This is #923's failure mode on "
                    f"the operator side: a refresh that could not complete, written back "
                    f"with empty tokens.")
                _stderr(detail)
                raise CredentialConflict(
                    f"operator {provider or 'provider'} credential is blank or unparseable",
                    link, target, detail)
            if here_ok and there_ok and here > there:
                detail = (
                    f"{link} is a real file holding a credential NEWER than {target} "
                    f"({here} > {there}). Relinking would discard it. This is the "
                    f"signature of a CLI that rewrites its credential by rename: it "
                    f"replaced the symlink, then refreshed. Reconcile by hand, or bind "
                    f"the credential's PARENT DIRECTORY instead of the file.")
                _stderr(detail)
                raise CredentialConflict(
                    f"drone {provider or 'provider'} credential is newer than the operator's",
                    link, target, detail)
            _stderr(
                f"{link} was a real file, not a link to {target} "
                f"({'stale' if here_ok else 'UNUSABLE — blank or unparseable'}); "
                f"replacing it with the intended symlink")
            link.unlink()
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
    except OSError as exc:
        # Windows refuses symlinks without Developer Mode or SeCreateSymbolicLink.
        # The spawn continues with no auth rather than dying, but it says so:
        # THIS FILE-LINKING DESIGN DOES NOT GENERALISE OFF POSIX.
        _stderr(f"could not link {link} -> {target}: {exc}")
        return


#: How long before a refresh token's HARD expiry to start saying so. #923: the
#: drone's own login hard-expired with no warning at all and the next spawn
#: turned that into a blanked credential.
EXPIRY_WARN_SECONDS = 3 * 86400


def _warn_if_expiring(target: Path, provider: str, now_ms: float | None = None,
                      stamp_dir: Path | None = None) -> str | None:
    """Print (and return) a line when the OPERATOR credential is about to die.

    Only claude exposes a hard expiry this module can read; the others return
    None rather than a guess.

    ONCE A DAY, NOT ONCE A SPAWN. Inside the window this condition is CONTINUOUS,
    and at lab-ovh's measured rate (251 spawns in 44h) printing per spawn would
    emit ~400 identical lines over the final three days: a count that measures how
    long the condition lasted, not how many times anything happened. A dated stamp
    in ``stamp_dir`` turns it back into news. Without a stamp_dir it prints every
    call, which is what the unit tests want.
    (science-claude, review of PR #27, finding 5.)
    """
    if provider != "claude":
        return None
    try:
        obj = json.loads(target.read_text(encoding="utf-8")).get("claudeAiOauth")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(obj, dict):
        return None
    exp = obj.get("refreshTokenExpiresAt")
    if not isinstance(exp, (int, float)):
        return None
    now = time.time() * 1000 if now_ms is None else now_ms
    if exp - now >= EXPIRY_WARN_SECONDS * 1000:
        return None
    line = (f"{target}: refresh token hard-expires in ~{max(0, int((exp - now) / 3600000))}h. "
            f"When it does, the next spawn cannot refresh and the CLI writes the "
            f"credential back BLANKED. Re-authenticate before then.")
    if stamp_dir is not None:
        stamp = stamp_dir / ".credential-expiry-warned"
        today = time.strftime("%Y-%m-%d", time.gmtime((now or 0) / 1000))
        try:
            if stamp.read_text(encoding="utf-8").strip() == today:
                return line                      # already said so today
        except OSError:
            pass
        try:
            stamp.write_text(today, encoding="utf-8")
        except OSError:
            pass                                 # a missed stamp only costs a repeat
    _stderr(line)
    return line

def prepare_isolated_home(provider: str, root: Path, *, operator_home: Path | None = None) -> Path:
    """Create root/.{provider}-drone-home carrying ONLY this provider's auth."""
    op = operator_home if operator_home is not None else Path.home()
    home = Path(root) / f".{provider}-drone-home"
    try:
        home.mkdir(parents=True, exist_ok=True)
        for rel in PROVIDER_AUTH.get(provider, ()):
            _link_auth(home / rel, Path(op) / rel, provider)
            _warn_if_expiring(Path(op) / rel, provider, stamp_dir=home)
        (home / ".gitconfig").write_text(_GITCONFIG, encoding="utf-8")
    except OSError:
        pass                       # best-effort; a partial home is still a valid HOME
    return home
