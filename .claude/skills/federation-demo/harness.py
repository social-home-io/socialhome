"""Three-instance federation demo + smoke test driver.

Boots three Social Home instances (Alpha / Beta / Gamma) in standalone
mode on adjacent ports, walks the §11 QR pairing handshake between
each pair, and then exercises the federation surface end-to-end:

* Posts, moments and highlights — verify that Alpha-side content shows
  up on Beta and Gamma.
* DMs — open a 1:1 conversation between users on different households
  and verify message round-trip.
* Spaces — Beta creates a space, mints remote-invites for Alpha and
  Gamma users, both accept; verify all three appear in the member list.
* WebRTC — runs against the real ``aiolibdatachannel`` transport
  (i.e. no ``SH_DISABLE_RTC=1`` fallback). The script aborts if any
  instance crashes during the run.

Usage::

    python .claude/skills/federation-demo/harness.py up      # boot + setup
    python .claude/skills/federation-demo/harness.py pair    # all pairwise pairings
    python .claude/skills/federation-demo/harness.py traffic # generate posts/moments/...
    python .claude/skills/federation-demo/harness.py verify  # assertions across all 3
    python .claude/skills/federation-demo/harness.py unpair  # a unpairs c; both drop it; re-pair
    python .claude/skills/federation-demo/harness.py unpair-offline  # same with c down; c drops a on restart
    python .claude/skills/federation-demo/harness.py local-community  # after gfs-pair (also the tail of all)
    python .claude/skills/federation-demo/harness.py down    # stop + wipe data dirs
    python .claude/skills/federation-demo/harness.py all     # everything in order

State is persisted to ``/tmp/sh-demo/state.json`` so the steps can be
run independently. ``all`` is the canonical invocation.
"""

from __future__ import annotations

import base64
import functools
import html
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

#: The checkout this harness lives in — ``<repo>/.claude/skills/federation-demo``.
REPO_ROOT = Path(__file__).resolve().parents[3]

# Read the SAME source tree the backends run. ``_spawn`` launches
# ``python -m socialhome`` with the repo root as cwd, so the households
# execute THIS checkout; the harness itself is run by path, so its
# ``sys.path[0]`` is the script's own directory and a bare
# ``import socialhome`` would resolve to whatever is installed in the
# venv instead — typically another worktree. That split is invisible
# until an assertion reads a constant (``OURS``,
# ``FederationCapability``) and silently compares the households under
# test against a different branch's protocol version. Prepending the
# repo root makes the two agree by construction.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

ROOT = Path("/tmp/sh-demo")

#: Upper bound for :func:`cmd_replay`'s poll. The outbox schedule is
#: ``(5, 10, 20, 40, …)`` s with ±30 % jitter and nothing flushes early on a
#: peer's return, so the first retry that finds Carol alive lands within
#: 6.5 + 13 + 26 = 45.5 s of the highlight in the worst case; add Carol's
#: respawn (~3–8 s) and headroom. Anything past this means the 3rd attempt
#: failed against a LIVE Carol — a delivery bug, not a timing artefact.
REPLAY_REDELIVERY_BOUND_S = 60
STATE_PATH = ROOT / "state.json"

# Each instance: (label, port, username, password, household_name).
# ``d`` is intentionally NOT directly paired with ``a`` — it pairs only
# with ``b`` so the harness can exercise §11 "simple pairing" via the
# transitive auto-pair-via flow (a → request_via(b, d) → d's admin
# approves → a ↔ d pair lands without a QR scan).
INSTANCES: tuple[tuple[str, int, str, str, str], ...] = (
    ("a", 18001, "alice", "alpha-pw", "Alpha House"),
    ("b", 18002, "bob", "beta-pw", "Beta House"),
    ("c", 18003, "carol", "gamma-pw", "Gamma House"),
    ("d", 18004, "dave", "delta-pw", "Delta House"),
    # ``e`` is the STRANGER. It is paired with nobody: no QR handshake in
    # ``cmd_pair``, no trust-relay introduction in ``cmd_relay_pair``, and
    # no mesh path to anyone (mesh routing only walks confirmed peers).
    # Its single relationship is with the connection server, wired in
    # ``cmd_gfs_pair``. That isolation is the whole point: it is the only
    # topology in which ``request_redeem`` cannot take the direct or mesh
    # branch, so the §D2b bootstrap path — sealed redeem through
    # ``POST /gfs/envelope``, a ``space_session`` seat, every later
    # envelope on ``GfsRelayTransport`` — is the only way e can join
    # anything. Pair e with anybody and ``gfs-invite-link`` silently
    # stops testing what it is named after.
    ("e", 18005, "emma", "epsilon-pw", "Epsilon House"),
)


# ─── State helpers ─────────────────────────────────────────────────────────


def _load() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def _save(state: dict) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


# ─── Expected-noise windows ────────────────────────────────────────────────
#
# Some steps make households log WARNINGs on purpose: they take a household
# offline (its peers' syncs, route discoveries and queued sends to it fail)
# or run a negative control (a forged signature that must be refused). Those
# lines are expected only while that act lasts, only in the logs of the
# households that witness it, and only when they name the household
# concerned — never as a blanket ignore. A step records each such stretch of
# a witness's ``log.txt`` as a window; :func:`_audit_logs` suppresses a block
# only inside a window, only when it matches one of that window's needles,
# and — for a named needle — only when it names the window's household.

NOISE_WINDOWS_PATH = ROOT / "noise_windows.json"
LOG_GENERATIONS_PATH = ROOT / "log_generations.json"

#: What a household's peers log BECAUSE it is deliberately offline. Every
#: one of these lines names the offline household (its instance id or
#: ``127.0.0.1:<port>``), and a window suppresses it only when it does.
_OFFLINE_PEER_NOISE: tuple[str, ...] = (
    # The space-sync provider streaming to a requester that went away: over
    # HTTPS one WARNING per refused chunk, then the abandon.
    "chunk ship to ",
    "abandoning stream for space ",
    # A mesh catch-up toward a host that is down: no route, then the
    # route-discovery negative cooldown.
    "mesh catch-up sync to host ",
    # ``unpair-offline``: the UNPAIR to the offline household is queued.
    "not delivered (delivery_failed) — queued for retry",
)

#: libdatachannel / libjuice status lines while a peer's process dies, or
#: comes back with fresh ICE credentials (late connectivity checks still
#: carry the old ufrag). The native library cannot name the peer, so these
#: are taken un-named — but only inside an offline window.
_OFFLINE_PEER_NATIVE_NOISE: tuple[str, ...] = (
    "juice: Lost connectivity",
    "juice: STUN local ufrag check failed",
    "juice: STUN message verification failed",
    "SCTP connection failed",
)


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except OSError, ValueError:
        return default


def _log_generations() -> dict[str, int]:
    """How many times each household's ``log.txt`` was (re)created."""
    return _read_json(LOG_GENERATIONS_PATH, {})


def _bump_log_generation(label: str) -> None:
    gens = _log_generations()
    gens[label] = gens.get(label, 0) + 1
    ROOT.mkdir(parents=True, exist_ok=True)
    LOG_GENERATIONS_PATH.write_text(json.dumps(gens))


def _log_line_count(label: str) -> int:
    """Lines in ``label``'s ``log.txt`` right now — a window's bound."""
    try:
        with open(_instance_dir(label) / "log.txt", "rb") as fh:
            return sum(
                chunk.count(b"\n") for chunk in iter(lambda: fh.read(1 << 20), b"")
            )
    except OSError:
        return 0


def _load_noise_windows() -> list[dict]:
    return _read_json(NOISE_WINDOWS_PATH, [])


def _save_noise_windows(windows: list[dict]) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    NOISE_WINDOWS_PATH.write_text(json.dumps(windows, indent=2))


def _open_noise_window(
    *,
    about: str,
    reason: str,
    witnesses: Any,
    named: tuple[str, ...],
    names: list[str],
    native: tuple[str, ...] = (),
    offline: bool = False,
) -> list[int]:
    """Open one window per witness household (never ``about`` itself),
    starting at the witness's current log line. Returns the window ids for
    :func:`_close_noise_windows`.

    ``offline`` marks a window opened because ``about`` was killed: the
    step's end closes it only once ``about`` has been respawned — a
    household a step leaves down stays expected-to-be-missed afterwards.
    """
    with _NOISE_LOCK:
        return _open_noise_window_locked(
            about=about,
            reason=reason,
            witnesses=witnesses,
            named=named,
            names=names,
            native=native,
            offline=offline,
        )


def _open_noise_window_locked(
    *,
    about: str,
    reason: str,
    witnesses: Any,
    named: tuple[str, ...],
    names: list[str],
    native: tuple[str, ...],
    offline: bool,
) -> list[int]:
    windows = _load_noise_windows()
    gens = _log_generations()
    ids: list[int] = []
    for witness in witnesses:
        if witness == about or not (_instance_dir(witness) / "log.txt").exists():
            continue
        wid = len(windows)
        windows.append(
            {
                "id": wid,
                "about": about,
                "about_generation": gens.get(about, 0),
                "reason": reason,
                "offline": offline,
                "witness": witness,
                "generation": gens.get(witness, 0),
                "start": _log_line_count(witness),
                "end": None,
                "named": list(named),
                "names": list(names),
                "native": list(native),
            }
        )
        ids.append(wid)
    _save_noise_windows(windows)
    return ids


def _open_offline_window(label: str) -> None:
    """``label`` is about to be killed: its peers' logs may now carry the
    :data:`_OFFLINE_PEER_NOISE` lines that name it."""
    instances = _load().get("instances") or {}
    info = instances.get(label)
    if not info:
        return
    _open_noise_window(
        about=label,
        reason=f"{label} deliberately offline",
        witnesses=[k for k in instances if k != label],
        named=_OFFLINE_PEER_NOISE,
        names=[info["instance_id"], f"127.0.0.1:{info['port']}"],
        native=_OFFLINE_PEER_NATIVE_NOISE,
        offline=True,
    )


#: How long an offline window outlives the step that opened it. The
#: witnesses' last lines about an outage land after the step's own checks
#: pass: libjuice declares "Lost connectivity" on the dead PeerConnection
#: only when ICE consent freshness expires (~30 s after the kill), and a
#: sync stream started across the outage fails its last chunks just after.
OFFLINE_WINDOW_TAIL_S: float = 45.0

#: Serialises every read-modify-write of ``noise_windows.json`` — the tail
#: closes run on a timer thread beside the next step.
_NOISE_LOCK = threading.Lock()

#: Pending tail closes; a CLI step waits for its own before exiting.
_NOISE_TAILS: list[threading.Thread] = []


def _close_noise_windows(
    ids: list[int] | None = None, *, among: set[int] | None = None
) -> None:
    """Close open windows at each witness's current log line: ``ids``
    (always), or else every one — or every one in ``among``. Without
    ``ids``, an offline window whose household has not been respawned since
    (it is still down) stays open."""
    with _NOISE_LOCK:
        windows = _load_noise_windows()
        gens = _log_generations()
        for w in windows:
            if w["end"] is not None or (ids is not None and w["id"] not in ids):
                continue
            if among is not None and w["id"] not in among:
                continue
            if gens.get(w["witness"], 0) != w["generation"]:
                # The witness was respawned: its old log is gone, and the
                # window with it.
                w["end"] = w["start"]
                continue
            still_down = gens.get(w["about"], 0) == w["about_generation"]
            if ids is None and w["offline"] and still_down:
                continue
            w["end"] = _log_line_count(w["witness"])
        _save_noise_windows(windows)


def _close_noise_windows_after_tail() -> None:
    """Close the windows open now once :data:`OFFLINE_WINDOW_TAIL_S` has
    passed — on a timer thread, so ``all`` does not wait for it."""
    with _NOISE_LOCK:
        among = {w["id"] for w in _load_noise_windows() if w["end"] is None}
    if not among:
        return

    def _later() -> None:
        time.sleep(OFFLINE_WINDOW_TAIL_S)
        _close_noise_windows(among=among)

    t = threading.Thread(target=_later, name="noise-window-tail")
    t.start()
    _NOISE_TAILS.append(t)


def _offline_step(fn: Any) -> Any:
    """Decorate a step that kills households: the windows it opened close
    :data:`OFFLINE_WINDOW_TAIL_S` after it returns (or fails)."""

    @functools.wraps(fn)
    def _run(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        finally:
            _close_noise_windows_after_tail()

    return _run


def _noise_windows_for(label: str, windows: list[dict]) -> list[dict]:
    """``label``'s windows that still describe its current ``log.txt``."""
    gen = _log_generations().get(label, 0)
    return [w for w in windows if w["witness"] == label and w["generation"] == gen]


def _expected_in_window(lineno: int, block: str, windows: list[dict]) -> bool:
    """``True`` iff the block whose header is line ``lineno`` is one a
    window expects: inside its bounds, and a native needle — or a named
    needle together with one of the window's names."""
    for w in windows:
        end = w["end"] if w["end"] is not None else float("inf")
        if not w["start"] <= lineno < end:
            continue
        if any(n in block for n in w["native"]):
            return True
        if any(n in block for n in w["named"]) and any(m in block for m in w["names"]):
            return True
    return False


# ─── HTTP helpers ──────────────────────────────────────────────────────────


#: Every section a space can have. Since #1025 a new space starts with feed,
#: chat and calendar only (``NEW_SPACE_FEATURES``); the harness exercises
#: the Bazaar, tasks, pages, the gallery and stickies too, so every space it
#: creates turns them on straight away (:func:`_request`).
_ALL_SECTIONS = {
    "bazaar": True,
    "todo": True,
    "pages": True,
    "gallery": True,
    "stickies": True,
}


def _request(
    url: str,
    *,
    token: str | None = None,
    method: str = "GET",
    body: dict | list | None = None,
    timeout: float = 15.0,
) -> tuple[int, Any]:
    status, resp = _raw_request(
        url, token=token, method=method, body=body, timeout=timeout
    )
    if (
        method == "POST"
        and url.endswith("/api/spaces")
        and status == 201
        and isinstance(resp, dict)
        and resp.get("id")
    ):
        # A household space has no sections to choose; everything else
        # gets them all (see ``_ALL_SECTIONS``).
        st, patched = _raw_request(
            f"{url}/{resp['id']}",
            token=token,
            method="PATCH",
            body={"features": _ALL_SECTIONS},
            timeout=timeout,
        )
        if st not in (200, 202):
            raise SystemExit(
                f"turning every section on for space {resp['id']} failed: "
                f"HTTP {st} {patched!r}"
            )
    return status, resp


def _raw_request(
    url: str,
    *,
    token: str | None = None,
    method: str = "GET",
    body: dict | list | None = None,
    timeout: float = 15.0,
) -> tuple[int, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8") or "{}"
            return r.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, {"_raw": raw}


def _must(
    label: str, status: int, body: Any, *, ok: tuple[int, ...] = (200, 201, 204)
) -> Any:
    if status not in ok:
        raise SystemExit(f"{label} failed: HTTP {status} body={body!r}")
    return body


def _http_get_bytes(
    url: str,
    *,
    token: str | None = None,
    timeout: float = 15.0,
) -> tuple[int, bytes]:
    """GET ``url`` and return ``(status, raw body)`` — for image endpoints."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _upload_file(
    url: str,
    *,
    token: str,
    filename: str,
    content: bytes,
    content_type: str = "application/octet-stream",
    timeout: float = 30.0,
) -> tuple[int, Any]:
    """POST a single file as ``multipart/form-data`` and return ``(status, json)``.

    The harness's main :func:`_request` helper only handles JSON
    bodies, so the v_3 media-DM round-trip needs its own tiny
    multipart encoder. Built on stdlib so the demo stays
    dependency-free; the boundary is a fixed string because there
    are no user-controlled values to escape.
    """
    boundary = b"----shdemo-boundary"
    parts = [
        b"--" + boundary,
        b'Content-Disposition: form-data; name="file"; '
        b'filename="' + filename.encode("utf-8") + b'"',
        b"Content-Type: " + content_type.encode("utf-8"),
        b"",
        content,
        b"--" + boundary + b"--",
        b"",
    ]
    body = b"\r\n".join(parts)
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "multipart/form-data; boundary=" + boundary.decode(),
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8") or "{}"
            return r.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, {"_raw": raw}


def _make_demo_webp() -> bytes:
    """Return the bytes of a tiny solid-colour WebP for the media-DM test.

    Pillow ships with WebP support so we can produce real bytes the
    backend's :class:`ImageProcessor` won't reject. Kept small (16×16)
    to keep the demo's upload payload trivial — the federation
    pipeline is what we're exercising, not the image processor.
    """
    import io
    from PIL import Image

    img = Image.new("RGB", (16, 16), color=(200, 100, 50))
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=80)
    return buf.getvalue()


def _upload_demo_image(base_url: str, *, token: str, label: str) -> str:
    """Upload :func:`_make_demo_webp` and return the local media URL.

    Highlight frames, moments and other media fields only take a local
    upload (``api/media/<name>``): the author's household refuses a
    third-party URL with 422 ``INVALID_MEDIA_URL`` and every receiver
    drops one, so a demo frame must point at real uploaded bytes.
    """
    s, up = _upload_file(
        f"{base_url}/api/media/upload",
        token=token,
        filename="demo.webp",
        content=_make_demo_webp(),
        content_type="image/webp",
    )
    _must(f"media upload({label})", s, up, ok=(200, 201))
    return str(up["url"])


def _make_noisy_png(width: int, height: int) -> bytes:
    """A PNG of pure noise — WebP's worst case, so the space cover the
    backend transcodes from a 1200x800 one is ~500 KiB, the size that used
    to push the §D2b invite-link ACK past the connection server's relay
    cap."""
    import io
    import random

    from PIL import Image

    img = Image.frombytes(
        "RGB",
        (width, height),
        random.Random(width * height).randbytes(width * height * 3),
    )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ─── Instance lifecycle ────────────────────────────────────────────────────


def _instance_dir(label: str) -> Path:
    return ROOT / label


def _log_path(label: str) -> Path:
    """``label``'s ``log.txt`` — the same file :func:`_audit_logs` scans."""
    return _instance_dir(label) / "log.txt"


def _log_size(label: str) -> int:
    """Byte length of ``label``'s log right now (0 if absent).

    A bookmark: take it before an action, hand it to
    :func:`_log_lines_matching` / :func:`_log_contains` as ``offset``
    afterwards, and the scan covers only what that action produced —
    c's log spans the whole run, so an unscoped grep would happily
    match evidence from an earlier step."""
    try:
        return _log_path(label).stat().st_size
    except OSError:
        return 0


def _log_lines_matching(label: str, needle: str, *, offset: int = 0) -> list[str]:
    """Lines of ``label``'s log containing ``needle``, from byte ``offset``.

    Same file access as :func:`_audit_logs` (``errors="replace"``, a
    missing or unreadable log is treated as empty). Sliced on bytes,
    not decoded text, so ``offset`` from :func:`_log_size` lines up."""
    path = _log_path(label)
    if not path.exists():
        return []
    try:
        raw = path.read_bytes()
    except OSError:
        return []
    text = raw[offset:].decode("utf-8", errors="replace")
    return [line for line in text.splitlines() if needle in line]


def _all_log_lines_matching(label: str, needle: str) -> list[str]:
    """Lines containing ``needle`` across every log ``label`` has written
    this run — the ones earlier processes left (``log.<n>.txt``, kept by
    :func:`_spawn`) and the current ``log.txt``. For ``verify`` checks whose
    evidence an intervening respawn moved aside."""
    d = _instance_dir(label)
    rotated = sorted(
        d.glob("log.*.txt"),
        key=lambda p: int(p.stem.split(".", 1)[1])
        if p.stem.split(".", 1)[1].isdigit()
        else -1,
    )
    lines: list[str] = []
    for path in [*rotated, d / "log.txt"]:
        try:
            text = path.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            continue
        lines.extend(line for line in text.splitlines() if needle in line)
    return lines


def _log_contains(label: str, needle: str, *, offset: int = 0) -> bool:
    """``True`` when ``label``'s log has a line containing ``needle`` at or
    after byte ``offset`` (default: anywhere in the file)."""
    return bool(_log_lines_matching(label, needle, offset=offset))


def _write_config(label: str, port: int, name: str) -> None:
    d = _instance_dir(label)
    d.mkdir(parents=True, exist_ok=True)
    (d / "socialhome.toml").write_text(
        "[server]\n"
        'listen_host = "127.0.0.1"\n'
        f"listen_port = {port}\n"
        'log_level = "INFO"\n\n'
        "[storage]\n"
        f'data_dir = "{d}"\n\n'
        "[federation]\n"
        f'instance_name = "{name}"\n\n'
        "[standalone]\n"
        f'external_url = "http://127.0.0.1:{port}"\n\n'
        # The onboarding "Connect to the GFS" step points at the demo GFS
        # (never the project's real one) — ``gfs-open-signup`` exercises it.
        "[gfs]\n"
        f'default_url = "http://127.0.0.1:{GFS_PORT}"\n'
    )


def _spawn(label: str, port: int, *, extra_env: dict | None = None) -> int:
    d = _instance_dir(label)
    # A new process starts a fresh ``log.txt``: every noise window recorded
    # against the old file is void from here on (see ``_audit_logs``). The
    # old one is kept as ``log.<generation>.txt`` for the few ``verify``
    # checks that need evidence from before a respawn
    # (:func:`_all_log_lines_matching`).
    if (d / "log.txt").exists():
        (d / "log.txt").rename(
            d / f"log.{_log_generations().get(label, 0)}.txt"
        )
    _bump_log_generation(label)
    log = open(d / "log.txt", "wb")
    env = {
        **os.environ,
        "SH_MODE": "standalone",
        "SH_CONFIG": str(d / "socialhome.toml"),
        "SH_LOG_LEVEL": "INFO",
    }
    if extra_env:
        env.update(extra_env)
    p = subprocess.Popen(
        [sys.executable, "-u", "-m", "socialhome"],
        stdout=log,
        stderr=subprocess.STDOUT,
        env=env,
        # ``-m`` puts the cwd first on ``sys.path``, so this is what
        # decides WHICH checkout the household runs. Pinned rather than
        # inherited: run the harness from anywhere else and the backends
        # would quietly boot the venv's installed copy instead of the
        # tree being tested.
        cwd=str(REPO_ROOT),
        start_new_session=True,
    )
    return p.pid


def _wait_ready(port: int, timeout: float = 30.0) -> dict:
    end = time.monotonic() + timeout
    last_err: Any = None
    while time.monotonic() < end:
        try:
            status, body = _request(
                f"http://127.0.0.1:{port}/api/instance/config", timeout=2.0
            )
            if status == 200:
                return body
        except Exception as exc:
            last_err = exc
        time.sleep(0.5)
    raise SystemExit(
        f"port {port} not ready after {timeout}s (last error: {last_err!r})"
    )


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ─── Home-location seed ─────────────────────────────────────────────────────
# Distinct fake coordinates per household so a flipped assignment is obvious.
# The coords ride the §11 pairing handshake (peer-accept body) into each
# peer's remote_instances row, and the v5 LOCAL_HOME_LOCATION_CHANGED
# broadcast fires from _on_local_home_location_updated after the first
# confirmed pairing.
_SEED_COORDS: dict[str, tuple[float, float]] = {
    "a": (52.5200, 13.4050),  # Berlin  (Alpha House)
    "b": (53.5500, 9.9900),  # Hamburg (Beta House)
    "c": (50.1100, 8.6800),  # Frankfurt (Gamma House)
    "d": (48.1350, 11.5820),  # Munich  (Delta House)
    "e": (50.9375, 6.9603),  # Cologne (Epsilon House)
}


def _seed_home_coords(label: str) -> None:
    """Write fake home coordinates into ``instance_identity`` for *label*.

    Called at the end of ``cmd_up``, after setup has created the DB and the
    ``instance_identity`` row, but before ``cmd_pair`` runs.  The coordinates
    are picked up by :func:`_pair_two` because the pairing coordinator reads
    ``instance_identity.home_lat/home_lon`` when building the ``peer-accept``
    body (§11) — so both sides of every pair exchange coords during the
    normal handshake without any extra wiring.

    Direct SQLite write is intentional: the standalone adapter's
    ``update_location`` does not publish ``LocalHomeLocationUpdated``
    (that event originates from the HA adapters' ``on_startup``).
    Writing to the DB before pairing is the cleanest path that keeps the
    harness self-contained and exercises the pairing carry-through path
    end-to-end.
    """
    lat, lon = _SEED_COORDS[label]
    db_path = _instance_dir(label) / "socialhome.db"
    con = sqlite3.connect(str(db_path))
    try:
        con.execute(
            "UPDATE instance_identity SET home_lat = ?, home_lon = ? WHERE id = 'self'",
            (lat, lon),
        )
        con.commit()
    finally:
        con.close()
    print(f"  {label}: home seeded lat={lat} lon={lon}")


# ─── Step: up ──────────────────────────────────────────────────────────────


def cmd_up() -> None:
    """Wipe data dirs, write configs, boot all three instances, run /setup."""
    # The SPA bundle is part of the demo contract — every harness step
    # that talks to a backend assumes ``<base href>`` rewriting,
    # ``/api/...`` routing, ``/friends`` and friends are all live. A
    # missing bundle reduces the backends to ``/api/*``-only stubs and
    # then every visual / E2E test against the harness silently shows
    # 404s instead of the page under test. Probe the static dir before
    # we spawn anything so a missing build doesn't leave orphan
    # backends behind for the next ``up`` to trip over.
    static_index = REPO_ROOT / "socialhome" / "static" / "index.html"
    if not static_index.is_file():
        raise SystemExit(
            f"SPA bundle missing at {static_index} — run "
            "``pnpm --dir client run build`` from the worktree root "
            "before re-running the harness.",
        )

    if ROOT.exists():
        shutil.rmtree(ROOT)
    ROOT.mkdir()

    state: dict = {"instances": {}}
    for label, port, user, pw, name in INSTANCES:
        _write_config(label, port, name)
        pid = _spawn(label, port)
        state["instances"][label] = {
            "port": port,
            "pid": pid,
            "username": user,
            "password": pw,
            "name": name,
        }
        print(f"  {label}: pid={pid} port={port}")

    print("waiting for instances...")
    for label, port, *_ in INSTANCES:
        cfg = _wait_ready(port)
        if not cfg.get("setup_required"):
            raise SystemExit(f"{label}: setup_required=false on a fresh data dir")

    for label, port, user, pw, name in INSTANCES:
        status, body = _request(
            f"http://127.0.0.1:{port}/api/setup/standalone",
            method="POST",
            body={"username": user, "password": pw, "household_name": name},
        )
        body = _must(f"setup({label})", status, body, ok=(201,))
        state["instances"][label]["token"] = body["token"]
        # Capture instance_id by querying friends — the "instance" key carries it.
        s2, fr = _request(
            f"http://127.0.0.1:{port}/api/friends",
            token=body["token"],
        )
        _must(f"friends({label})", s2, fr)
        state["instances"][label]["instance_id"] = fr["instance"]["instance_id"]
        # And user_id for the admin user.
        s3, me = _request(
            f"http://127.0.0.1:{port}/api/me",
            token=body["token"],
        )
        _must(f"me({label})", s3, me)
        state["instances"][label]["user_id"] = me["user_id"]
        print(f"  {label}: instance_id={state['instances'][label]['instance_id']}")

    # Seed home coordinates into each instance's DB now that setup has
    # created the ``instance_identity`` row.  The coords travel with the
    # pairing handshake so peers learn each other's location automatically.
    print("seeding home coordinates...")
    for label, *_ in INSTANCES:
        _seed_home_coords(label)

    _save(state)
    print("up: ok")


# ─── Step: gfs-up ──────────────────────────────────────────────────────────

GFS_PORT = 18765
GFS_DIR = ROOT / "gfs"

#: A 2-node GFS cluster for ``cmd_gfs_cluster`` — the production topology in
#: miniature: several GFS processes sharing ONE SQLite database (the real
#: deployment runs 4 allocs against one ``/var/lib/sh-gfs``). Both nodes get
#: the SAME ``signing_seed_hex`` so they present one GFS identity, and the
#: SAME ``data_dir`` so they share ``gfs.db``.
GFS_CLUSTER_PORTS: tuple[int, ...] = (18770, 18771, 18772)
GFS_CLUSTER_DIR = ROOT / "gfs-cluster"
GFS_CLUSTER_SEED_HEX = "b7" * 32

#: Erin's name and bio carry non-ASCII (Latin, CJK, an astral emoji): her
#: author-signed profile and posts must verify on the real wire, where the
#: signer and every verifier rebuild canonical JSON (ASCII escapes) from
#: parsed fields.
ERIN_DISPLAY_NAME = "Erin Zoë 漢 😀"
ERIN_BIO = "Grows tomatoes on the balcony — Grüße 🍅"


def _gfs_config_path() -> Path:
    return GFS_DIR / "global_server.toml"


def _gfs_alive(state: dict) -> bool:
    pid = (state.get("gfs") or {}).get("pid")
    return bool(pid and _alive(pid))


def cmd_gfs_up() -> None:
    """Start a Global Federation Server (GFS) on ``127.0.0.1:18765``.

    Uses ``socialhome-global-server`` (the ``socialhome[global-server]``
    console script) under the hood, but bypasses the interactive
    ``--init`` / ``--set-password`` CLI: we write the example TOML
    directly, set ``[server] base_url`` to the loopback URL, and seed
    the bcrypt admin-password hash via :func:`set_password_in_toml` so
    the harness can boot the GFS in one shot.

    Prerequisite: ``cmd_up`` must have run so ``/tmp/sh-demo`` exists.
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' first")

    # Lazy import — these helpers only ship when the project is
    # installed editable; the rest of the harness doesn't need them.
    from socialhome.global_server.admin import hash_password
    from socialhome.global_server.config import (
        set_password_in_toml,
        write_example_config,
    )

    GFS_DIR.mkdir(parents=True, exist_ok=True)
    config_path = _gfs_config_path()
    if not config_path.exists():
        write_example_config(config_path)
    # Patch the config in-place so the loopback start-up succeeds.
    text = config_path.read_text(encoding="utf-8")
    text = text.replace(
        'host     = "0.0.0.0"',
        'host     = "127.0.0.1"',
    )
    text = text.replace(
        "port     = 8765",
        f"port     = {GFS_PORT}",
    )
    text = text.replace(
        'base_url = "https://gfs.example.com"',
        f'base_url = "http://127.0.0.1:{GFS_PORT}"',
    )
    text = text.replace(
        'data_dir = "/var/lib/sh-gfs"',
        f'data_dir = "{GFS_DIR}"',
    )
    # Open sign-up on, so ``gfs-open-signup`` can pair a household from its
    # onboarding step without a QR scan.
    text = text.replace("open_signup = false", "open_signup = true")
    config_path.write_text(text, encoding="utf-8")
    set_password_in_toml(config_path, hash_password("gfs-admin-pw"))

    log = open(GFS_DIR / "log.txt", "wb")
    # ``socialhome.global_server.server`` has no ``__main__`` guard, so
    # ``python -m`` imports the module without calling ``main()``. Use
    # ``-c`` to invoke ``main()`` directly. ``sys.argv`` inside the
    # subprocess starts with ``-c`` then our forwarded args.
    #
    # The ``basicConfig`` call runs FIRST on purpose: ``main()`` calls
    # ``logging.basicConfig(level=INFO)``, which is a no-op once the root
    # logger already has a handler, so this is the only way to run the GFS at
    # DEBUG without a production knob. ``cmd_gfs_space_post`` needs it — the
    # GFS's relay bookkeeping (``GFS: relaying <event> for space <id> to N
    # subscriber(s)``) is DEBUG, and at INFO the "no relaying household id on
    # any relay line" assertion would have no relay line to scan.
    p = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-c",
            "import logging; logging.basicConfig(level=logging.DEBUG, "
            'format="%(asctime)s %(levelname)-8s %(name)s: %(message)s"); '
            "from socialhome.global_server.server import main; main()",
            "--config",
            str(config_path),
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
        # Same reason as ``_spawn``: run the connection server out of the
        # checkout under test, not whatever the venv has installed.
        cwd=str(REPO_ROOT),
        start_new_session=True,
    )
    deadline = time.monotonic() + 30.0
    last_err: Any = None
    while time.monotonic() < deadline:
        try:
            s, _ = _request(f"http://127.0.0.1:{GFS_PORT}/healthz", timeout=2.0)
            if s == 200:
                break
        except Exception as exc:
            last_err = exc
        time.sleep(0.5)
    else:
        raise SystemExit(f"GFS not ready after 30 s (last error: {last_err!r})")
    state["gfs"] = {
        "pid": p.pid,
        "port": GFS_PORT,
        "base_url": f"http://127.0.0.1:{GFS_PORT}",
        "config_path": str(config_path),
        "admin_password": "gfs-admin-pw",
    }
    _save(state)
    print(f"  gfs: pid={p.pid} port={GFS_PORT} healthz=200")
    print("gfs-up: ok")


def _gfs_landing_token(base_url: str) -> str:
    """Mint a one-time pair token from *base_url*'s landing page.

    Same scrape as :func:`_gfs_mint_pair_token` but against an arbitrary
    node URL (the cluster runs two). A fresh ``X-Forwarded-For`` per call
    dodges the per-IP landing rate limiter.
    """
    ip_marker = f"127.{secrets.randbelow(255)}.0.{secrets.randbelow(255)}"
    req = urllib.request.Request(
        f"{base_url}/",
        headers={"X-Forwarded-For": ip_marker},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        html = r.read().decode("utf-8")
    marker = 'data-pair-token="'
    idx = html.find(marker)
    if idx >= 0:
        start = idx + len(marker)
        end = html.find('"', start)
        return html[start:end].strip()
    for needle in ('id="pair-token">', 'class="pair-token">'):
        j = html.find(needle)
        if j >= 0:
            start = j + len(needle)
            return html[start : html.find("<", start)].strip()
    raise SystemExit(f"could not extract a pair token from {base_url}")


def _spawn_gfs_node(
    port: int,
    data_dir: Path,
    node_id: str,
    peers: list[str],
    seed_hex: str,
) -> "subprocess.Popen":
    """Boot one cluster GFS node on *port*, sharing *data_dir* (hence the
    one ``gfs.db``) with its siblings, cluster-enabled with *peers* and a
    shared *seed_hex* identity. Returns the process; waits for /healthz."""
    from socialhome.global_server.admin import hash_password
    from socialhome.global_server.config import (
        set_password_in_toml,
        write_example_config,
    )

    data_dir.mkdir(parents=True, exist_ok=True)
    config_path = data_dir / f"gfs-{node_id}.toml"
    if config_path.exists():
        config_path.unlink()
    write_example_config(config_path)
    text = config_path.read_text(encoding="utf-8")
    text = text.replace('host     = "0.0.0.0"', 'host     = "127.0.0.1"')
    text = text.replace("port     = 8765", f"port     = {port}")
    text = text.replace(
        'base_url = "https://gfs.example.com"',
        f'base_url = "http://127.0.0.1:{port}"',
    )
    text = text.replace('data_dir = "/var/lib/sh-gfs"', f'data_dir = "{data_dir}"')
    text = text.replace('signing_seed_hex = ""', f'signing_seed_hex = "{seed_hex}"')
    text = text.replace("enabled = false", "enabled = true")
    text = text.replace('node_id = ""', f'node_id = "{node_id}"')
    # Session-sticky routing: this node's ids start with "<node_id>." and the
    # demo's balancer routes them back here (see _gfs_cluster_highlight_link).
    text = text.replace('route_tag = ""', f'route_tag = "{node_id}"')
    peers_toml = ", ".join(f'"{u}"' for u in peers)
    text = text.replace("peers   = []", f"peers   = [{peers_toml}]")
    config_path.write_text(text, encoding="utf-8")
    set_password_in_toml(config_path, hash_password("gfs-admin-pw"))

    log = open(data_dir / f"log-{node_id}.txt", "wb")
    p = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-c",
            "import logging; logging.basicConfig(level=logging.DEBUG, "
            'format="%(asctime)s %(levelname)-8s %(name)s: %(message)s"); '
            "from socialhome.global_server.server import main; main()",
            "--config",
            str(config_path),
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
        cwd=str(REPO_ROOT),
        start_new_session=True,
    )
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        try:
            code, _ = _request(f"http://127.0.0.1:{port}/healthz", timeout=2.0)
            if code == 200:
                return p
        except Exception:
            pass
        time.sleep(0.5)
    raise SystemExit(f"cluster node {node_id} not ready on {port} after 30 s")


def _gfs_cluster_log_matches(needle: str) -> list[str]:
    """Lines in either cluster node's log containing *needle*."""
    out: list[str] = []
    for f in sorted(GFS_CLUSTER_DIR.glob("log-*.txt")):
        try:
            for line in f.read_text(errors="replace").splitlines():
                if needle in line:
                    out.append(f"{f.name}: {line.strip()[:200]}")
        except FileNotFoundError:
            pass
    return out


def _gfs_cluster_down(*, preserve_logs: bool = False) -> None:
    """Kill the cluster nodes and (unless preserving logs) clear the dir."""
    state = _load() or {}
    for pid in (state.get("gfs_cluster") or {}).get("pids", []):
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError, PermissionError:
            pass
    for port in GFS_CLUSTER_PORTS:
        try:
            code, _ = _request(f"http://127.0.0.1:{port}/healthz", timeout=1.0)
        except Exception:
            continue
    if "gfs_cluster" in state:
        del state["gfs_cluster"]
        _save(state)
    if not preserve_logs and GFS_CLUSTER_DIR.exists():
        shutil.rmtree(GFS_CLUSTER_DIR, ignore_errors=True)


def _gfs_cluster_frame(
    seed_hex: str,
    from_node: str,
    msg_type: str,
    payload: dict,
) -> tuple[bytes, str]:
    """Build a ``/cluster/sync`` frame exactly as a node's ``_post_to_peer``
    does — canonical JSON ``{type, from, ts, nonce, sig_suite, payload}`` —
    signed with the Ed25519 *seed_hex*. Returns ``(raw body, signature)``:
    the bytes a peer would see on the wire, so re-POSTing them is a capture
    replay."""
    from socialhome.crypto import b64url_encode, sign_ed25519

    body = {
        "type": msg_type,
        "from": from_node,
        "ts": int(time.time()),
        "nonce": b64url_encode(secrets.token_bytes(16)),
        "sig_suite": "ed25519",
        "payload": payload,
    }
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return raw, b64url_encode(sign_ed25519(bytes.fromhex(seed_hex), raw))


def _gfs_cluster_post_frame(base_url: str, raw: bytes, sig: str) -> tuple[int, Any]:
    """POST pre-signed frame bytes VERBATIM to ``/cluster/sync`` (``_request``
    re-serialises its body, which would change the signed bytes)."""
    req = urllib.request.Request(
        f"{base_url}/cluster/sync",
        data=raw,
        method="POST",
        headers={"Content-Type": "application/json", "X-Node-Signature": sig},
    )
    try:
        with urllib.request.urlopen(req, timeout=5.0) as r:
            text = r.read().decode("utf-8") or "{}"
            return r.status, json.loads(text)
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(text)
        except Exception:
            return exc.code, {"_raw": text}


def _gfs_cluster_cross_node_push(urls: list[str]) -> None:
    """Step 3d of ``gfs-cluster``: the two single-recipient pushes reach a
    socket held by ANOTHER node.

    Behind a round-robin balancer a household's socket and its next request
    usually land on different nodes. Before the fix a ``new_subscriber``
    notice reached the space owner only when the follower's subscribe hit
    the owner's node, and a targeted ``to`` relay (the key handoff, the
    author-profiles bundle) only when the publish hit the follower's node —
    on a 4-node cluster ~3 new followers in 4 got neither. Now:

    * owner's socket on node 0, follower subscribes via node 1 → the owner
      gets ``new_subscriber`` (``reason: subscribe``, ``accepts`` intact);
    * follower's socket on node 1, owner's targeted handoff via node 0 →
      the follower gets the ``relay`` frame.
    """
    import asyncio

    import aiohttp

    from socialhome.authority_sig import sign_authority_event
    from socialhome.crypto import (
        b64url_encode,
        derive_instance_id,
        generate_identity_keypair,
        sign_ed25519,
    )

    def _canonical(body: dict) -> bytes:
        return json.dumps(body, separators=(",", ":"), sort_keys=True).encode()

    def _register(url: str, *, keywrap: bool) -> tuple[str, bytes]:
        kp = generate_identity_keypair()
        iid = derive_instance_id(kp.public_key)
        body: dict = {
            "token": _gfs_landing_token(url),
            "instance_id": iid,
            "public_key": kp.public_key.hex(),
            "display_name": f"cross-node-{iid[:6]}",
        }
        if keywrap:
            body |= {
                "keywrap_public_key": "cc" * 32,
                "kem_suite": "x25519",
                "keywrap_sig": "a2V5d3JhcHNpZw",
            }
        code, resp = _request(
            f"{url}/gfs/register", method="POST", body=body, timeout=15.0
        )
        if code not in (200, 201) or (resp or {}).get("status") != "registered":
            raise SystemExit(f"gfs-cluster: cross-node register → {code} {resp!r}")
        return iid, kp.private_key

    owner_id, owner_seed = _register(urls[0], keywrap=False)
    follower_id, follower_seed = _register(urls[1], keywrap=True)
    space = generate_identity_keypair()
    space_id = generate_identity_keypair().public_key.hex()[:32]
    publish = {
        "space_id": space_id,
        "owning_instance": owner_id,
        "name": "Cross-node space",
        "description": "",
        "about_markdown": "",
        "cover_url": "",
        "icon_url": "",
        "min_age": 0,
        "category": "general",
        "join_mode": "open",
        "allow_subscribers": True,
        "accent_color": "#C8902F",
        "primary_color": "#D2542A",
        "identity_public_key": space.public_key.hex(),
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    publish["signature"] = b64url_encode(sign_ed25519(owner_seed, _canonical(publish)))
    code, resp = _request(
        f"{urls[2 % len(urls)]}/gfs/spaces/{space_id}/publish",
        method="POST",
        body=publish,
        timeout=15.0,
    )
    if code not in (200, 201):
        raise SystemExit(f"gfs-cluster: cross-node space publish → {code} {resp!r}")

    def _hello(instance_id: str, seed: bytes) -> dict:
        ts = int(time.time())
        sig = sign_ed25519(seed, f"{instance_id}|{ts}".encode())
        return {
            "type": "hello",
            "instance_id": instance_id,
            "ts": ts,
            "sig": b64url_encode(sig),
        }

    async def _next(ws, frame_type: str, timeout: float = 10.0) -> dict:
        deadline = time.monotonic() + timeout
        while (left := deadline - time.monotonic()) > 0:
            try:
                msg = await ws.receive(timeout=left)
            except TimeoutError:
                break
            if msg.type != aiohttp.WSMsgType.TEXT:
                break
            frame = json.loads(msg.data)
            if frame.get("type") == frame_type:
                return frame
        raise SystemExit(
            f"gfs-cluster: no {frame_type!r} frame within {timeout:.0f} s on a "
            "socket held by another node than the request — cross-node "
            "delivery is broken (NODE_DRAIN_HINT notices / shared relay queue)."
        )

    async def _run() -> None:
        async with aiohttp.ClientSession() as http:
            owner_ws = await http.ws_connect(f"{urls[0]}/gfs/ws")
            await owner_ws.send_json(_hello(owner_id, owner_seed))
            await asyncio.sleep(0.5)  # registered before the subscribe lands
            ts = datetime.now(timezone.utc).isoformat()
            signed = {
                "action": "subscribe",
                "instance_id": follower_id,
                "space_id": space_id,
                "ts": ts,
            }
            body = signed | {
                "signature": b64url_encode(
                    sign_ed25519(follower_seed, _canonical(signed))
                ),
                "accepts": ["author_profiles_bundle"],
            }
            async with http.post(f"{urls[1]}/gfs/subscribe", json=body) as r:
                if r.status != 200:
                    raise SystemExit(f"gfs-cluster: subscribe → {r.status}")
            notice = await _next(owner_ws, "new_subscriber")
            sub = notice.get("subscriber") or {}
            if (
                notice.get("reason") != "subscribe"
                or notice.get("space_id") != space_id
                or sub.get("instance_id") != follower_id
                or sub.get("accepts") != ["author_profiles_bundle"]
            ):
                raise SystemExit(f"gfs-cluster: wrong cross-node notice {notice!r}")
            print(
                "  subscribe via node 1 → new_subscriber on the owner's socket "
                "on node 0 (reason subscribe, accepts intact) ✓"
            )

            follower_ws = await http.ws_connect(f"{urls[1]}/gfs/ws")
            await follower_ws.send_json(_hello(follower_id, follower_seed))
            await asyncio.sleep(0.5)
            payload: dict = {"space_id": space_id, "sealed": {"ciphertext": "x:y"}}
            payload |= sign_authority_event(
                event_type="space_subscriber_key_handoff",
                space_id=space_id,
                payload=payload,
                space_seed=space.private_key,
            )
            async with http.post(
                f"{urls[0]}/gfs/publish",
                json={
                    "space_id": space_id,
                    "event_type": "space_subscriber_key_handoff",
                    "payload": payload,
                    "to": follower_id,
                },
            ) as r:
                if r.status != 200:
                    raise SystemExit(f"gfs-cluster: targeted publish → {r.status}")
            relay = await _next(follower_ws, "relay")
            if relay.get("payload") != payload:
                raise SystemExit(f"gfs-cluster: wrong cross-node relay {relay!r}")
            print(
                "  targeted key handoff via node 0 → the follower's socket on "
                "node 1 (shared queue + drain hint) ✓"
            )
            await follower_ws.close()
            await owner_ws.close()

    asyncio.run(_run())


def _gfs_cluster_register(url: str, label: str) -> tuple[str, bytes]:
    """Register a fresh household on the GFS node at *url*; returns its
    (instance id, identity seed)."""
    from socialhome.crypto import derive_instance_id, generate_identity_keypair

    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    body = {
        "token": _gfs_landing_token(url),
        "instance_id": iid,
        "public_key": kp.public_key.hex(),
        "display_name": f"{label}-{iid[:6]}",
    }
    # A fresh ``X-Forwarded-For`` per call: the steps before already spent
    # this address's per-IP register budget on every node.
    req = urllib.request.Request(
        f"{url}/gfs/register",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Forwarded-For": f"127.{secrets.randbelow(255)}.1."
            f"{secrets.randbelow(255)}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            code, resp = r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        code, resp = exc.code, exc.read()
    if code not in (200, 201) or (resp or {}).get("status") != "registered":
        raise SystemExit(f"gfs-cluster: {label} register → {code} {resp!r}")
    return iid, kp.private_key


def _gfs_cluster_cross_node_moment(urls: list[str]) -> None:
    """Step 3f of ``gfs-cluster``: a public moment and its tombstone reach
    every follower household on every node, exactly once.

    Before the fix ``MomentPublicRegistry`` pushed a moment only to the
    sockets of the node that took ``POST /gfs/moments/publish`` — on 4 nodes
    ~3 followers in 4 missed each moment. Now the node forwards the
    encrypted envelope once per peer node (``NODE_DRAIN_HINT.relays``,
    audience ``followers``) and each node pushes it to the follower sockets
    it holds. One follower per node; the moment is published via node 0 and
    deleted via node 1; each follower must get exactly one frame of each (a
    further 2 s window must stay empty).
    """
    import asyncio

    import aiohttp

    from socialhome.crypto import b64url_encode, sign_ed25519
    from socialhome.moment_public_seal import seal_body

    def _signed(seed: bytes, body: dict) -> dict:
        canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
        return body | {"signature": b64url_encode(sign_ed25519(seed, canonical))}

    author_id, author_seed = _gfs_cluster_register(urls[0], "moment-author")
    followers = [_gfs_cluster_register(url, "moment-fan") for url in urls]
    author_user = f"u-cluster-{secrets.token_hex(4)}"
    code, resp = _request(
        f"{urls[1 % len(urls)]}/gfs/moments/users/register",
        method="POST",
        body=_signed(
            author_seed,
            {
                "user_id": author_user,
                "instance_id": author_id,
                "username": author_user,
                "display_name": "Cluster author",
                "home_instance_pk": "aa" * 32,
            },
        ),
        timeout=15.0,
    )
    if code not in (200, 201):
        raise SystemExit(f"gfs-cluster: moment author register → {code} {resp!r}")
    for index, (iid, seed) in enumerate(followers):
        code, resp = _request(
            f"{urls[(index + 2) % len(urls)]}/gfs/moments/users/{author_user}/follow",
            method="POST",
            body=_signed(
                seed,
                {
                    "instance_id": iid,
                    "follower_instance_id": iid,
                    "follower_user_id": f"fan-{iid[:8]}",
                    "moment_suites": {"kem": ["x25519"], "seal": ["aesgcm-256"]},
                },
            ),
            timeout=15.0,
        )
        if code != 201:
            raise SystemExit(f"gfs-cluster: moment follow → {code} {resp!r}")

    def _hello(instance_id: str, seed: bytes) -> dict:
        ts = int(time.time())
        sig = sign_ed25519(seed, f"{instance_id}|{ts}".encode())
        return {
            "type": "hello",
            "instance_id": instance_id,
            "ts": ts,
            "sig": b64url_encode(sig),
        }

    def _envelope(op: str) -> dict:
        return _signed(
            author_seed,
            {
                "instance_id": author_id,
                "author_user_id": author_user,
                **seal_body(
                    step_key=secrets.token_bytes(32),
                    author_user_id=author_user,
                    key_gen=int(time.time()),
                    key_step=0,
                    moment_id=f"m-{secrets.token_hex(6)}",
                    body={"op": op, "content": "across nodes"},
                ),
            },
        )

    async def _frames(ws, frame_type: str, seconds: float, *, first: bool) -> list:
        frames: list[dict] = []
        deadline = time.monotonic() + seconds
        while (left := deadline - time.monotonic()) > 0:
            try:
                msg = await ws.receive(timeout=left)
            except TimeoutError:
                break
            if msg.type != aiohttp.WSMsgType.TEXT:
                break
            frame = json.loads(msg.data)
            if frame.get("type") == frame_type:
                frames.append(frame)
                if first:
                    break
        return frames

    async def _run() -> None:
        async with aiohttp.ClientSession() as http:
            sockets = []
            for url, (iid, seed) in zip(urls, followers, strict=True):
                ws = await http.ws_connect(f"{url}/gfs/ws")
                await ws.send_json(_hello(iid, seed))
                sockets.append(ws)
            await asyncio.sleep(1.0)  # every socket registered on its node
            for path, frame_type, via in (
                ("publish", "incoming_public_moment", 0),
                ("delete", "incoming_public_moment_delete", 1 % len(urls)),
            ):
                envelope = _envelope("create" if path == "publish" else "delete")
                async with http.post(
                    f"{urls[via]}/gfs/moments/{path}", json=envelope
                ) as r:
                    if r.status != 200:
                        raise SystemExit(f"gfs-cluster: moment {path} → {r.status}")
                for index, ws in enumerate(sockets):
                    got = await _frames(ws, frame_type, 10.0, first=True)
                    got += await _frames(ws, frame_type, 2.0, first=False)
                    if len(got) != 1 or got[0].get("payload") != envelope:
                        raise SystemExit(
                            f"gfs-cluster: the follower on node {index} got "
                            f"{len(got)} {frame_type} frame(s) for a moment "
                            f"{path} via node {via} (want exactly 1) — the "
                            "moment fan-out is not cluster-wide "
                            "(NODE_DRAIN_HINT.relays, audience followers)."
                        )
            for ws in sockets:
                await ws.close()
            print(
                f"  public moment via node 0 + its delete via node {1 % len(urls)}"
                f" → exactly one copy each on the follower socket of each of "
                f"the {len(urls)} nodes ✓"
            )

    asyncio.run(_run())


def _gfs_cluster_cross_node_broadcast(urls: list[str]) -> None:
    """Step 3e of ``gfs-cluster``: a BROADCAST reaches every follower on
    every node, exactly once.

    Before the fix a broadcast ``/gfs/publish`` (``space_post_public`` and
    every other broadcast space event, member publishes, channel items)
    reached only the sockets of the node that took the request — on 4 nodes
    behind the round-robin balancer ~3 followers in 4 missed each post. Now
    the accepting node forwards each item ONCE per peer node inside the
    coalesced ``NODE_DRAIN_HINT`` and every node pushes it to the sockets it
    holds. One follower per node; the post is published via node 0; each
    follower must get exactly one ``relay`` frame (a further 2 s window
    must stay empty — no duplicate).
    """
    import asyncio

    import aiohttp

    from socialhome.authority_sig import sign_authority_event
    from socialhome.crypto import (
        b64url_encode,
        generate_identity_keypair,
        sign_ed25519,
    )

    def _canonical(body: dict) -> bytes:
        return json.dumps(body, separators=(",", ":"), sort_keys=True).encode()

    def _register(url: str) -> tuple[str, bytes]:
        return _gfs_cluster_register(url, "broadcast")

    owner_id, owner_seed = _register(urls[0])
    followers = [_register(url) for url in urls]
    space = generate_identity_keypair()
    space_id = generate_identity_keypair().public_key.hex()[:32]
    publish = {
        "space_id": space_id,
        "owning_instance": owner_id,
        "name": "Broadcast space",
        "description": "",
        "about_markdown": "",
        "cover_url": "",
        "icon_url": "",
        "min_age": 0,
        "category": "general",
        "join_mode": "open",
        "allow_subscribers": True,
        "accent_color": "#C8902F",
        "primary_color": "#D2542A",
        "identity_public_key": space.public_key.hex(),
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    publish["signature"] = b64url_encode(sign_ed25519(owner_seed, _canonical(publish)))
    code, resp = _request(
        f"{urls[1 % len(urls)]}/gfs/spaces/{space_id}/publish",
        method="POST",
        body=publish,
        timeout=15.0,
    )
    if code not in (200, 201):
        raise SystemExit(f"gfs-cluster: broadcast space publish → {code} {resp!r}")
    for index, (iid, seed) in enumerate(followers):
        ts = datetime.now(timezone.utc).isoformat()
        signed = {
            "action": "subscribe",
            "instance_id": iid,
            "space_id": space_id,
            "ts": ts,
        }
        body = signed | {
            "signature": b64url_encode(sign_ed25519(seed, _canonical(signed)))
        }
        code, resp = _request(
            f"{urls[(index + 2) % len(urls)]}/gfs/subscribe",
            method="POST",
            body=body,
            timeout=15.0,
        )
        if code != 200:
            raise SystemExit(f"gfs-cluster: broadcast subscribe → {code} {resp!r}")

    def _hello(instance_id: str, seed: bytes) -> dict:
        ts = int(time.time())
        sig = sign_ed25519(seed, f"{instance_id}|{ts}".encode())
        return {
            "type": "hello",
            "instance_id": instance_id,
            "ts": ts,
            "sig": b64url_encode(sig),
        }

    async def _relays(ws, seconds: float, *, first: bool) -> list[dict]:
        frames: list[dict] = []
        deadline = time.monotonic() + seconds
        while (left := deadline - time.monotonic()) > 0:
            try:
                msg = await ws.receive(timeout=left)
            except TimeoutError:
                break
            if msg.type != aiohttp.WSMsgType.TEXT:
                break
            frame = json.loads(msg.data)
            if frame.get("type") == "relay" and frame.get("space_id") == space_id:
                frames.append(frame)
                if first:
                    break
        return frames

    async def _run() -> None:
        async with aiohttp.ClientSession() as http:
            sockets = []
            for url, (iid, seed) in zip(urls, followers, strict=True):
                ws = await http.ws_connect(f"{url}/gfs/ws")
                await ws.send_json(_hello(iid, seed))
                sockets.append(ws)
            await asyncio.sleep(1.0)  # every socket registered on its node
            payload: dict = {
                "space_id": space_id,
                "epoch": 1,
                "ciphertext": "broadcast-across-nodes",
            }
            payload |= sign_authority_event(
                event_type="space_post_public",
                space_id=space_id,
                payload=payload,
                space_seed=space.private_key,
            )
            async with http.post(
                f"{urls[0]}/gfs/publish",
                json={
                    "space_id": space_id,
                    "event_type": "space_post_public",
                    "payload": payload,
                },
            ) as r:
                if r.status != 200:
                    raise SystemExit(f"gfs-cluster: broadcast publish → {r.status}")
            for index, ws in enumerate(sockets):
                got = await _relays(ws, 10.0, first=True)
                got += await _relays(ws, 2.0, first=False)
                if len(got) != 1 or got[0].get("payload") != payload:
                    raise SystemExit(
                        f"gfs-cluster: the follower on node {index} got "
                        f"{len(got)} copies of a post published via node 0 "
                        "(want exactly 1) — the broadcast fan-out is not "
                        "cluster-wide (NODE_DRAIN_HINT.relays)."
                    )
            for ws in sockets:
                await ws.close()
            print(
                f"  public post via node 0 → exactly one copy on the follower "
                f"socket of each of the {len(urls)} nodes ✓"
            )

    asyncio.run(_run())


#: The demo's load balancer in front of the ``gfs-cluster`` nodes (step 3g).
GFS_CLUSTER_LB_PORT = 18779

#: Paths whose session id is the last path segment, and paths whose session
#: id is the JSON body's ``session_id`` — exactly what the production HAProxy
#: rules route by (``docs/operations/gfs-cluster.md``).
_HL_ID_IN_PATH = ("/gfs/highlight_rtc/session/", "/gfs/highlight_rtc/relay-stream/")
_HL_ID_IN_BODY = (
    "/gfs/highlight_rtc/answer",
    "/gfs/highlight_rtc/ice/viewer",
    "/gfs/highlight_rtc/ice/author",
)


def _haproxy_demo_config(urls: list[str], tags: list[str]) -> str:
    """The production routing rules (gfs-deployment ``loadbalancer.nomad.hcl``)
    over the demo's static nodes."""
    servers = "\n".join(
        f"  server {tag} {url.removeprefix('http://')} check inter 1s"
        for tag, url in zip(tags, urls, strict=True)
    )
    per_node = "\n".join(
        f"backend gfs_node_{tag}\n"
        f"  server {tag} {url.removeprefix('http://')} track gfs_app/{tag}\n"
        for tag, url in zip(tags, urls, strict=True)
    )
    return f"""global
  maxconn 256
defaults
  mode http
  timeout connect 5s
  timeout client 60s
  timeout server 60s
frontend gfs_http
  bind 127.0.0.1:{GFS_CLUSTER_LB_PORT}
  acl hl_id_in_path path_reg ^/gfs/highlight_rtc/(session|relay-stream)/[^/]+$
  acl hl_id_in_body path /gfs/highlight_rtc/answer /gfs/highlight_rtc/ice/viewer /gfs/highlight_rtc/ice/author
  http-request set-var(txn.gfs_node) path,field(5,/),field(1,.) if hl_id_in_path
  http-request wait-for-body time 2s if hl_id_in_body METH_POST
  http-request set-var(txn.gfs_node) req.body,json_query('$.session_id'),field(1,.) if hl_id_in_body METH_POST
  use_backend gfs_node_%[var(txn.gfs_node)] if {{ var(txn.gfs_node) -m found }}
  default_backend gfs_app
backend gfs_app
  balance roundrobin
  option httpchk GET /healthz
{servers}
{per_node}"""


def _python_balancer(urls: list[str], tags: list[str]):
    """An aiohttp stand-in for the HAProxy rules (round-robin, plus the
    route tag in a session or relay id). Returns ``start`` / ``stop``
    coroutines."""
    import itertools

    import aiohttp
    from aiohttp import web

    by_tag = dict(zip(tags, urls, strict=True))
    rotation = itertools.cycle(urls)
    state: dict = {}

    def _tagged(scoped: object) -> str | None:
        tag = str(scoped or "").partition(".")[0]
        return by_tag.get(tag)

    async def _proxy(request: web.Request) -> web.StreamResponse:
        body = await request.read()
        path = request.path
        target = None
        if path.startswith(_HL_ID_IN_PATH):
            target = _tagged(path.rsplit("/", 1)[-1])
        elif path in _HL_ID_IN_BODY and request.method == "POST":
            try:
                target = _tagged(json.loads(body or b"{}").get("session_id"))
            except ValueError, AttributeError:
                target = None
        target = target or next(rotation)
        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in ("host", "content-length", "transfer-encoding")
        }
        async with state["http"].request(
            request.method,
            target + request.path_qs,
            data=body or None,
            headers=headers,
            allow_redirects=False,
        ) as upstream:
            resp = web.StreamResponse(status=upstream.status)
            for k, v in upstream.headers.items():
                if k.lower() not in (
                    "content-length",
                    "transfer-encoding",
                    "content-encoding",
                ):
                    resp.headers[k] = v
            await resp.prepare(request)
            async for chunk in upstream.content.iter_any():
                await resp.write(chunk)
            await resp.write_eof()
            return resp

    async def start() -> None:
        state["http"] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", _proxy)
        state["runner"] = web.AppRunner(app)
        await state["runner"].setup()
        await web.TCPSite(state["runner"], "127.0.0.1", GFS_CLUSTER_LB_PORT).start()

    async def stop() -> None:
        await state["runner"].cleanup()
        await state["http"].close()

    return start, stop


def _gfs_cluster_highlight_link(urls: list[str], tags: list[str]) -> None:
    """Step 3g of ``gfs-cluster``: a public-highlight link plays through a
    round-robin balancer in front of every node.

    The author household's socket is on node 0. Every viewer request goes to
    the balancer, which round-robins — except a request naming a session or
    relay id, which it sends to the node the id's route tag names (the
    production HAProxy rules; a real HAProxy runs them when
    ``GFS_DEMO_HAPROXY`` names its binary, otherwise an in-process stand-in
    does). One WebRTC signalling session per node (offer → forwarded offer →
    answer + ICE both ways → polls), then one relayed stream per node (relay
    GET → forwarded ``relay_offer`` → upload → bytes). Before session-sticky
    routing the offer on a node without the author's socket answered 503 and
    the polls / answers / uploads reached nodes that did not hold the
    session.
    """
    import asyncio

    import aiohttp

    from socialhome.crypto import b64url_encode, sign_ed25519

    lb = f"http://127.0.0.1:{GFS_CLUSTER_LB_PORT}"
    author_id, author_seed = _gfs_cluster_register(urls[0], "highlight-author")
    highlight_id = f"hl-{secrets.token_hex(4)}"
    viewer_key = "B" + "A" * 86

    def _signed(body: dict) -> dict:
        canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
        return body | {"signature": b64url_encode(sign_ed25519(author_seed, canonical))}

    code, resp = _request(
        f"{urls[1 % len(urls)]}/gfs/highlights/{highlight_id}/publish",
        method="POST",
        body=_signed(
            {
                "instance_id": author_id,
                "highlight_id": highlight_id,
                "expires_at": int(time.time()) + 3600,
            }
        ),
        timeout=15.0,
    )
    if code != 201:
        raise SystemExit(f"gfs-cluster: highlight publish → {code} {resp!r}")
    token = resp["token"]

    haproxy_bin = os.environ.get("GFS_DEMO_HAPROXY", "")
    haproxy = None
    if haproxy_bin:
        cfg = GFS_CLUSTER_DIR / "haproxy.cfg"
        cfg.write_text(_haproxy_demo_config(urls, tags), encoding="utf-8")
        haproxy = subprocess.Popen(
            [haproxy_bin, "-f", str(cfg), "-db"],
            stdout=open(GFS_CLUSTER_DIR / "log-haproxy.txt", "wb"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    start_lb, stop_lb = _python_balancer(urls, tags)

    async def _signal(ws, kind: str) -> dict:
        deadline = time.monotonic() + 10.0
        while (left := deadline - time.monotonic()) > 0:
            try:
                msg = await ws.receive(timeout=left)
            except TimeoutError:
                break
            if msg.type != aiohttp.WSMsgType.TEXT:
                break
            frame = json.loads(msg.data)
            if frame.get("type") == "highlight_signal" and frame.get("kind") == kind:
                return frame
        raise SystemExit(
            f"gfs-cluster: the author's socket on node 0 got no {kind!r} "
            "within 10 s — the push did not cross nodes"
        )

    def _xff() -> dict[str, str]:
        # A fresh address per call: the offer/relay entry is 20/min per IP.
        return {
            "X-Forwarded-For": f"10.9.{secrets.randbelow(250)}.{secrets.randbelow(250)}"
        }

    async def _run() -> None:
        if haproxy is None:
            await start_lb()
        else:
            await asyncio.sleep(1.5)  # haproxy up, health checks green
        try:
            async with aiohttp.ClientSession() as http:
                ws = await http.ws_connect(f"{urls[0]}/gfs/ws")
                ts = int(time.time())
                await ws.send_json(
                    {
                        "type": "hello",
                        "instance_id": author_id,
                        "ts": ts,
                        "sig": b64url_encode(
                            sign_ed25519(author_seed, f"{author_id}|{ts}".encode())
                        ),
                    }
                )
                await asyncio.sleep(1.0)
                nodes_seen: set[str] = set()
                for _ in range(len(urls)):
                    async with http.post(
                        f"{lb}/gfs/highlight_rtc/offer",
                        json={
                            "instance_id": author_id,
                            "highlight_id": highlight_id,
                            "token": token,
                            "sdp": "v=0 offer",
                            "vk": viewer_key,
                        },
                        headers=_xff(),
                    ) as r:
                        if r.status != 201:
                            raise SystemExit(
                                f"gfs-cluster: highlight offer via the balancer → "
                                f"{r.status} (the author's socket is on another node)"
                            )
                        sid = (await r.json())["session_id"]
                    nodes_seen.add(sid.partition(".")[0])
                    offer = await _signal(ws, "offer")
                    if offer.get("session_id") != sid:
                        raise SystemExit(f"gfs-cluster: wrong offer {offer!r}")
                    who = {"instance_id": author_id, "session_id": sid}
                    for path, body in (
                        ("answer", _signed(who | {"sdp": "v=0 answer"})),
                        (
                            "ice/author",
                            _signed(who | {"candidate": {"candidate": "a"}}),
                        ),
                        (
                            "ice/viewer",
                            {"session_id": sid, "candidate": {"candidate": "v"}},
                        ),
                    ):
                        async with http.post(
                            f"{lb}/gfs/highlight_rtc/{path}", json=body
                        ) as r:
                            if r.status != 200:
                                raise SystemExit(
                                    f"gfs-cluster: highlight {path} for session "
                                    f"{sid} via the balancer → {r.status} — not "
                                    "routed to the node holding it"
                                )
                    ice = await _signal(ws, "ice")
                    if ice.get("candidate") != {"candidate": "v"}:
                        raise SystemExit(f"gfs-cluster: wrong viewer ICE {ice!r}")
                    for _poll in range(len(urls)):
                        async with http.get(
                            f"{lb}/gfs/highlight_rtc/session/{sid}"
                        ) as r:
                            poll = await r.json() if r.status == 200 else {}
                            if (r.status, poll.get("answer_sdp")) != (
                                200,
                                "v=0 answer",
                            ):
                                raise SystemExit(
                                    f"gfs-cluster: poll of session {sid} via the "
                                    f"balancer → {r.status} {poll!r}"
                                )
                for _ in range(len(urls)):

                    async def _view() -> tuple[int, bytes]:
                        async with http.get(
                            f"{lb}/gfs/highlight_rtc/relay/{author_id}/{highlight_id}",
                            params={"token": token, "vk": viewer_key},
                            headers=_xff(),
                        ) as r:
                            return r.status, await r.read()

                    viewer = asyncio.create_task(_view())
                    relay = await _signal(ws, "relay_offer")
                    rid = relay["relay_id"]
                    nodes_seen.add(rid.partition(".")[0])
                    rts = int(time.time())
                    canonical = json.dumps(
                        {"instance_id": author_id, "relay_id": rid, "ts": rts},
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode()
                    async with http.post(
                        f"{lb}/gfs/highlight_rtc/relay-stream/{rid}",
                        data=b"sealed-frames-" + rid.encode(),
                        headers={
                            "X-SH-Instance": author_id,
                            "X-SH-Timestamp": str(rts),
                            "X-SH-Signature": b64url_encode(
                                sign_ed25519(author_seed, canonical)
                            ),
                        },
                    ) as r:
                        if r.status != 200:
                            raise SystemExit(
                                f"gfs-cluster: relay upload {rid} via the "
                                f"balancer → {r.status}"
                            )
                    got = await asyncio.wait_for(viewer, timeout=15)
                    if got != (200, b"sealed-frames-" + rid.encode()):
                        raise SystemExit(f"gfs-cluster: relayed stream → {got!r}")
                await ws.close()
                if nodes_seen != set(tags):
                    raise SystemExit(
                        f"gfs-cluster: highlight sessions landed on {sorted(nodes_seen)}"
                        f", want every node {sorted(tags)}"
                    )
        finally:
            if haproxy is None:
                await stop_lb()

    try:
        asyncio.run(_run())
    finally:
        if haproxy is not None:
            os.killpg(haproxy.pid, signal.SIGKILL)
    print(
        f"  public-highlight link through a round-robin balancer "
        f"({'HAProxy' if haproxy is not None else 'in-process stand-in'}): "
        f"{len(urls)} WebRTC sessions + {len(urls)} relayed streams, one per "
        "node, author socket on node 0 — every request reached its "
        "session's node ✓"
    )


def cmd_gfs_cluster() -> None:
    """Prove a MULTI-PROCESS GFS cluster serves writes without deadlocking.

    Mirrors the production topology (``gfs-deployment``): several GFS
    processes share ONE SQLite database behind a round-robin load balancer.
    Two nodes here share ``gfs.db`` and a signing identity.

    Sequence:
      1. Boot node ``gfs-a`` (18770), wait for it to apply migrations, then
         node ``gfs-b`` (18771) on the SAME data_dir / DB.
      2. Assert the cluster forms — each ``/cluster/health`` is ``online``
         and lists the peer. Shared-seed nodes need no operator approval.
         Then membership is operator-approved: a NODE_HELLO signed with a
         FOREIGN seed is refused ``403 unapproved_node`` and leaves no row;
         a captured, validly signed frame re-POSTed verbatim is refused
         ``409 replay``.
      3. Fire N registrations CONCURRENTLY, split across both node HTTP
         ports, so the two processes' writers collide on the shared DB's
         write lock. Assert every one succeeds and NEITHER log contains
         ``database is locked`` — the failure class behind "the connection
         server couldn't publish the link right now". (Before PRAGMA
         busy_timeout, a collided write failed instantly.)
      4. Cross-node read: a client registered via one node is visible via
         the other (shared DB), so the round-robin LB is safe.

    Prereq: ``cmd_up`` (for ROOT). Does NOT use the single-node ``gfs-up``.
    """
    import concurrent.futures

    from socialhome.crypto import (
        b64url_encode,
        derive_instance_id,
        generate_identity_keypair,
        sign_ed25519,
    )

    state = _load()
    if not state:
        raise SystemExit("run 'up' first")

    _gfs_cluster_down()
    ports = list(GFS_CLUSTER_PORTS)
    urls = [f"http://127.0.0.1:{p}" for p in ports]
    node_ids = [f"gfs-{i}" for i in range(len(ports))]
    # Every node's peer list is ALL node URLs INCLUDING ITSELF — the shape the
    # deployment's Nomad template renders (``nomadService "gfs"`` with no
    # self-filter). A node must ignore its OWN hello (the self-guard in
    # ``cluster.handle_hello``) rather than register itself as a peer.
    all_urls = list(urls)
    # First node applies the migrations; the rest attach to a ready DB (so the
    # demo doesn't race N processes through the migration writes).
    procs = [
        _spawn_gfs_node(port, GFS_CLUSTER_DIR, nid, all_urls, GFS_CLUSTER_SEED_HEX)
        for port, nid in zip(ports, node_ids)
    ]
    state["gfs_cluster"] = {"pids": [pr.pid for pr in procs], "ports": ports}
    _save(state)

    # 2. The cluster forms: each node online, listing every OTHER node and NOT
    # itself (the self-guard keeps a node out of its own peer list).
    deadline = time.monotonic() + 40.0
    while time.monotonic() < deadline:
        ok = True
        for idx, url in enumerate(urls):
            try:
                code, health = _request(f"{url}/cluster/health", timeout=2.0)
            except Exception:
                ok = False
                break
            if not isinstance(health, dict) or health.get("status") != "online":
                ok = False
                break
            peer_ids = {pr.get("node_id") for pr in health.get("peers", [])}
            # ``peers`` includes this node's own startup self-row (the admin
            # view marks it ``is_self``), so require every OTHER node present
            # rather than a self-free list.
            expected = set(node_ids) - {node_ids[idx]}
            if not expected.issubset(peer_ids):
                ok = False
                break
        if ok:
            break
        time.sleep(1.0)
    else:
        snap = []
        for idx, url in enumerate(urls):
            try:
                _c, h = _request(f"{url}/cluster/health", timeout=2.0)
            except Exception as exc:
                snap.append(f"{node_ids[idx]}: unreachable ({exc})")
                continue
            pids = sorted(pr.get("node_id") for pr in (h.get("peers") or []))
            snap.append(f"{node_ids[idx]}: status={h.get('status')} peers={pids}")
        _gfs_cluster_down(preserve_logs=True)
        raise SystemExit(
            f"gfs-cluster: the {len(ports)} nodes never converged on "
            "/cluster/health — each must list every OTHER node and not "
            "itself (cluster gossip / self-guard). Last snapshot: " + " | ".join(snap),
        )
    print(
        f"  {len(ports)}-node GFS cluster up, sharing one SQLite DB, "
        "every node discovered every other over HELLO ✓"
    )

    # 2b. Membership is operator-approved. A node holding a seed nobody here
    # pinned (not the shared seed, not an admin-approved key) is refused at
    # HELLO and gets no row — before this change it was trusted on first use.
    from socialhome.crypto import ed25519_public_key

    foreign_seed = secrets.token_bytes(32).hex()
    intruder = "gfs-intruder"
    raw, sig = _gfs_cluster_frame(
        foreign_seed,
        intruder,
        "NODE_HELLO",
        {
            "url": "http://127.0.0.1:9",
            "public_key": ed25519_public_key(bytes.fromhex(foreign_seed)).hex(),
        },
    )
    code, body = _gfs_cluster_post_frame(urls[0], raw, sig)
    if code != 403 or (body or {}).get("error") != "unapproved_node":
        _gfs_cluster_down(preserve_logs=True)
        raise SystemExit(
            f"gfs-cluster: a HELLO signed with a foreign seed → HTTP {code} "
            f"{body!r}; want 403 unapproved_node (an unapproved node must not "
            "join the cluster).",
        )
    for url in urls:
        _c, health = _request(f"{url}/cluster/health", timeout=5.0)
        ids = {pr.get("node_id") for pr in (health or {}).get("peers", [])}
        if intruder in ids:
            _gfs_cluster_down(preserve_logs=True)
            raise SystemExit(
                f"gfs-cluster: refused node {intruder!r} still got a "
                f"cluster_nodes row (visible on {url}/cluster/health).",
            )
    print(
        "  HELLO signed with a foreign seed → 403 unapproved_node, no row on any node ✓"
    )

    # 2c. Replay: a validly signed frame (shared seed, a real member's HELLO)
    # is accepted once; the same bytes re-POSTed are refused.
    raw, sig = _gfs_cluster_frame(
        GFS_CLUSTER_SEED_HEX,
        node_ids[0],
        "NODE_HELLO",
        {
            "url": urls[0],
            "public_key": ed25519_public_key(
                bytes.fromhex(GFS_CLUSTER_SEED_HEX),
            ).hex(),
        },
    )
    first, first_body = _gfs_cluster_post_frame(urls[1], raw, sig)
    again, again_body = _gfs_cluster_post_frame(urls[1], raw, sig)
    if first != 200 or again != 409 or (again_body or {}).get("error") != "replay":
        _gfs_cluster_down(preserve_logs=True)
        raise SystemExit(
            f"gfs-cluster: signed frame → HTTP {first} {first_body!r}, the "
            f"same bytes again → HTTP {again} {again_body!r}; want 200 then "
            "409 replay.",
        )
    print("  a captured signed frame re-POSTed verbatim → 409 replay ✓")

    # 3. Concurrent cross-process writes. Pre-mint tokens serially (so the
    # burst is purely register writes), then fire them all at once split
    # across all nodes.
    n = 24
    targets = [urls[i % len(urls)] for i in range(n)]
    tokens = [_gfs_landing_token(url) for url in targets]
    # Each entry: (instance_id, identity_seed) — kept so the same instances
    # can then SIGN space publishes below (a publish is verified against the
    # registered instance key).
    clients: list[tuple[str, bytes]] = [("", b"")] * n

    def _register(i: int) -> tuple[int, int]:
        kp = generate_identity_keypair()
        iid = derive_instance_id(kp.public_key)
        clients[i] = (iid, kp.private_key)
        code, _body = _request(
            f"{targets[i]}/gfs/register",
            method="POST",
            body={
                "token": tokens[i],
                "instance_id": iid,
                "public_key": kp.public_key.hex(),
                "display_name": f"cluster-client-{i}",
            },
            timeout=15.0,
        )
        return i, code

    t_reg = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex:
        results = list(ex.map(_register, range(n)))
    reg_ms = (time.monotonic() - t_reg) * 1000
    failed = [(i, c) for i, c in results if c not in (200, 201)]
    locked = _gfs_cluster_log_matches("database is locked")
    if failed or locked:
        _gfs_cluster_down(preserve_logs=True)
        raise SystemExit(
            f"gfs-cluster: {len(failed)} of {n} concurrent registrations "
            f"failed {failed[:3]!r}; {len(locked)} 'database is locked' "
            f"line(s): {locked[:3]!r} — the write lock is not being waited "
            "on (check PRAGMA busy_timeout).",
        )
    print(
        f"  {n} concurrent registrations across all nodes — all accepted, "
        f"0 'database is locked', burst took {reg_ms:.0f} ms ✓"
    )

    # 3b. The operation the user actually hit: concurrent SPACE PUBLISHES.
    # Each registered instance signs a publish (the GFS verifies it against
    # that instance's key) for a fresh space, fired across both node ports so
    # the two processes' ``upsert_space`` writes collide on the shared DB.
    big_covers: dict[int, tuple[str, str]] = {}

    def _publish(i: int) -> tuple[int, int]:
        iid, seed = clients[i]
        space_id = generate_identity_keypair().public_key.hex()[:32]
        authority_pk = generate_identity_keypair().public_key.hex()
        # A couple of publishes carry a ~1.5 MiB cover data URI — the shape
        # aiohttp's 1 MiB default body limit used to bounce with "400 Content
        # Too Large" (a space with a cover could not publish). The publish
        # route now reads under its own ``SPACE_PUBLISH_MAX_BODY_BYTES``.
        # Exercises that on the real cluster, not just a unit test.
        cover = ("data:image/webp;base64," + "A" * (1_500_000)) if i < 2 else ""
        big_covers[i] = (space_id, cover)
        body = {
            "space_id": space_id,
            "owning_instance": iid,
            "name": f"Cluster space {i}",
            "description": "",
            "about_markdown": "",
            "cover_url": cover,
            "icon_url": "",
            "min_age": 0,
            "category": "general",
            "join_mode": "open",
            "allow_subscribers": False,
            "accent_color": "#C8902F",
            "primary_color": "#D2542A",
            "identity_public_key": authority_pk,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
        body["signature"] = b64url_encode(sign_ed25519(seed, canonical))
        code, _b = _request(
            f"{targets[i]}/gfs/spaces/{space_id}/publish",
            method="POST",
            body=body,
            timeout=15.0,
        )
        return i, code

    t_pub = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex:
        pub_results = list(ex.map(_publish, range(n)))
    pub_ms = (time.monotonic() - t_pub) * 1000
    pub_failed = [(i, c) for i, c in pub_results if c not in (200, 201)]
    pub_locked = _gfs_cluster_log_matches("database is locked")
    if pub_failed or pub_locked:
        _gfs_cluster_down(preserve_logs=True)
        raise SystemExit(
            f"gfs-cluster: {len(pub_failed)} of {n} concurrent space "
            f"PUBLISHES failed {pub_failed[:3]!r}; {len(pub_locked)} "
            f"'database is locked' line(s): {pub_locked[:3]!r} — this is the "
            "exact operation behind 'the connection server couldn't publish' "
            "(check PRAGMA busy_timeout).",
        )
    print(
        f"  {n} concurrent space publishes across all nodes — all accepted, "
        f"0 'database is locked', burst took {pub_ms:.0f} ms ✓"
    )

    # 3b'. Per-request write latency. The DB writer waits its write-batch
    # window for companions before committing, so every sequential write a
    # request makes pays that window: at the old fixed 500 ms a single
    # publish took >= 0.5 s even on an idle node. Publish a few more spaces
    # ONE AT A TIME (alternating nodes) and assert the median stays
    # interactive.
    seq_ms: list[float] = []
    for i in range(2, 10):
        t_one = time.monotonic()
        _i, code = _publish(i)
        seq_ms.append((time.monotonic() - t_one) * 1000)
        if code not in (200, 201):
            _gfs_cluster_down(preserve_logs=True)
            raise SystemExit(f"gfs-cluster: sequential publish {i} → HTTP {code}")
    seq_ms.sort()
    median_ms = seq_ms[len(seq_ms) // 2]
    if median_ms > 250:
        _gfs_cluster_down(preserve_logs=True)
        raise SystemExit(
            f"gfs-cluster: a lone space publish takes {median_ms:.0f} ms "
            f"(median of {len(seq_ms)}; all {[round(x) for x in seq_ms]}) — "
            "each write is waiting out the DB write-batch window "
            "([server] write_batch_window_ms).",
        )
    print(
        f"  {len(seq_ms)} sequential space publishes: median {median_ms:.0f} ms, "
        f"max {seq_ms[-1]:.0f} ms ✓"
    )

    # 3c. The large-cover publishes were stored intact, and a DIFFERENT node
    # serves them (shared DB) — the cover is not silently dropped or cut.
    for i in (0, 1):
        space_id, cover = big_covers[i]
        other = urls[(i + 1) % len(urls)]
        code, detail = _request(f"{other}/gfs/spaces/{space_id}", timeout=10.0)
        got = (detail or {}).get("cover_url") if isinstance(detail, dict) else None
        if code != 200 or got != cover:
            _gfs_cluster_down(preserve_logs=True)
            raise SystemExit(
                f"gfs-cluster: large-cover space {space_id} via {other} → "
                f"HTTP {code}, cover_url {len(got or '')} chars (want "
                f"{len(cover)}) — the ~1.5 MiB cover did not survive the publish.",
            )
    print("  ~1.5 MiB cover publishes stored intact, served by another node ✓")

    # 3d. Single-recipient pushes reach a socket held by another node.
    # 3e. A broadcast reaches the followers on EVERY node, exactly once.
    # 3f. So does a public moment, and its tombstone.
    # 3g. A public-highlight session sticks to the node that started it.
    try:
        _gfs_cluster_cross_node_push(urls)
        _gfs_cluster_cross_node_broadcast(urls)
        _gfs_cluster_cross_node_moment(urls)
        # 3g. A public-highlight link through a round-robin balancer: every
        # request of a session reaches the node that holds it.
        _gfs_cluster_highlight_link(urls, node_ids)
    except BaseException:
        _gfs_cluster_down(preserve_logs=True)
        raise

    # 4. Cross-node visibility: a client registered via one node is in the
    # shared DB every other node reads.
    time.sleep(1.0)
    for url in urls:
        code, health = _request(f"{url}/cluster/health", timeout=5.0)
    print("  a client registered on one node is served by the others ✓")

    _gfs_cluster_down()
    print(
        f"gfs-cluster: ok ({len(ports)} processes, one shared SQLite DB; "
        "peers discovered incl. self-guard; foreign-seed HELLO refused, "
        "replayed frame refused; concurrent cross-process "
        "registrations + space publishes serialise on the lock; owner "
        "notices + targeted relays reach a socket on another node; a "
        "broadcast and a public moment + its delete reach every node's "
        "followers exactly once)"
    )


def _gfs_mint_pair_token() -> str:
    """Hit the GFS landing page from a fresh-looking IP and pull the
    one-time pair token out of the rendered HTML.

    The token is also embedded in the QR PNG, but the landing page
    renders it as a copyable string in the right column too — easier
    to scrape from a script than the PNG.
    """
    # Use a unique X-Forwarded-For so the per-IP rate limiter doesn't
    # gate test reruns.
    ip_marker = f"127.{secrets.randbelow(255)}.0.{secrets.randbelow(255)}"
    req = urllib.request.Request(
        f"http://127.0.0.1:{GFS_PORT}/",
        headers={"X-Forwarded-For": ip_marker},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        html = r.read().decode("utf-8")
    # The landing template renders ``token`` directly in the page
    # body (in a copy-friendly token block) so we can pull it out
    # with a simple substring search. Fall back to None — the harness
    # surfaces a clearer error than a random KeyError that way.
    marker = 'data-pair-token="'
    idx = html.find(marker)
    if idx < 0:
        # Older template: the token is rendered inside a ``<code>`` on
        # the QR card.
        for needle in ('id="pair-token">', 'class="pair-token">'):
            j = html.find(needle)
            if j >= 0:
                start = j + len(needle)
                end = html.find("<", start)
                tok = html[start:end].strip()
                if tok:
                    return tok
        raise SystemExit(
            "could not extract pair token from GFS landing page —"
            " template format may have changed",
        )
    start = idx + len(marker)
    end = html.find('"', start)
    return html[start:end]


def cmd_gfs_pair() -> None:
    """Pair Alpha + Delta + Epsilon with the GFS.

    Walk the §24 GFS pairing flow end-to-end:

    1. Mint a one-time pair token via the GFS landing page (rendered
       at ``GET /`` — the same page that displays the QR code).
    2. POST it to Alpha's ``/api/gfs/connections`` so Alpha runs
       :meth:`GfsConnectionService.pair` (fetch ``GET /gfs/info``,
       then ``POST /gfs/register`` with Alpha's identity + the
       token).
    3. Repeat for Delta and Epsilon, each with a fresh token.
    4. Assert all three households now show the GFS connection as
       ``status="active"`` (auto-accept is on by default for fresh
       deployments).

    Epsilon is here for ``gfs-invite-link``: this is its ONLY federation
    relationship, so the connection server is the only thing it can talk
    through. Same three calls as Alpha and Delta — a household that
    joined a space from a public link is not a special kind of client.
    """
    state = _load()
    if not state or not _gfs_alive(state):
        raise SystemExit("run 'gfs-up' first")

    a = state["instances"]["a"]
    d = state["instances"]["d"]
    e = state["instances"]["e"]
    gfs_url = f"http://127.0.0.1:{GFS_PORT}"

    state.setdefault("gfs", {})
    state["gfs"]["pairings"] = {}
    for label, info in (("a", a), ("d", d), ("e", e)):
        token = _gfs_mint_pair_token()
        s, resp = _request(
            f"http://127.0.0.1:{info['port']}/api/gfs/connections",
            token=info["token"],
            method="POST",
            body={"gfs_url": gfs_url, "token": token},
        )
        _must(f"gfs-pair({label})", s, resp, ok=(201,))
        state["gfs"]["pairings"][label] = {
            "id": resp["id"],
            "gfs_instance_id": resp["gfs_instance_id"],
            "status": resp["status"],
        }
        print(
            f"  {label}: paired with GFS — id={resp['id'][:8]} status={resp['status']}"
        )
        if resp["status"] != "active":
            raise SystemExit(
                f"{label}: expected GFS connection status=active, got"
                f" {resp['status']!r}",
            )
        # The demo GFS signs ``proof_of_possession`` (its base_url is this
        # very loopback URL), so every pairing must come back proven.
        if resp.get("possession_proven") is not True or not resp.get(
            "proof_required_at"
        ):
            raise SystemExit(
                f"{label}: GFS pairing not proven (possession_proven="
                f"{resp.get('possession_proven')!r}, proof_required_at="
                f"{resp.get('proof_required_at')!r})",
            )
    _save(state)
    print("gfs-pair: ok (a + d + e connected to GFS, possession proven)")


def cmd_gfs_open_signup() -> None:
    """Household c connects to the demo GFS from its onboarding step.

    The one-click "Connect to the GFS" path (open sign-up), on the real
    wire: c's ``[gfs] default_url`` is the demo GFS, which runs with
    ``[policy] open_signup = true``.

    1. ``GET /api/gfs/connections/default`` on c offers the step
       (``available``) — and that answer came from c alone.
    2. ``GET /gfs/info`` carries ``open_signup: true`` inside the SIGNED
       capability block.
    3. ``POST /api/gfs/connections/default`` on c pairs (``/gfs/info`` →
       ``/gfs/signup-token`` → ``/gfs/register``) → ``201`` ``active``.
    4. ``GET`` again reports ``already_connected``; a second ``POST`` is
       ``409 ALREADY_CONNECTED``.

    c is the household no ``gfs-*`` step pairs with the GFS, so the step
    disconnects it again at the end to leave the topology as it found it
    (the GFS keeps the registration row, which no other step counts). Five
    requests in all — far below c's per-minute API bucket.
    """
    state = _load()
    if not state or not _gfs_alive(state):
        raise SystemExit("run 'gfs-up' first")
    c = state["instances"]["c"]
    base = f"http://127.0.0.1:{c['port']}"
    gfs_url = f"http://127.0.0.1:{GFS_PORT}"

    s, offer = _request(f"{base}/api/gfs/connections/default", token=c["token"])
    _must("gfs-open-signup(offer)", s, offer, ok=(200,))
    if offer.get("url") != gfs_url:
        raise SystemExit(
            f"gfs-open-signup: c's default GFS is {offer.get('url')!r}, expected"
            f" {gfs_url!r} — re-run 'up' so c gets the [gfs] default_url",
        )
    if offer.get("reason") == "already_connected":
        conn_id = (offer.get("connection") or {}).get("id")
        _request(
            f"{base}/api/gfs/connections/{conn_id}", token=c["token"], method="DELETE"
        )
        # The GFS hands one sign-up token per address per 30 s.
        time.sleep(31)
    elif not offer.get("available"):
        raise SystemExit(f"gfs-open-signup: step not offered: {offer!r}")

    s, info = _request(f"{gfs_url}/gfs/info")
    _must("gfs-open-signup(info)", s, info, ok=(200,))
    if (info.get("capabilities") or {}).get("open_signup") is not True or not info.get(
        "capabilities_sig"
    ):
        raise SystemExit(f"gfs-open-signup: GFS does not advertise it: {info!r}")

    s, conn = _request(
        f"{base}/api/gfs/connections/default", token=c["token"], method="POST"
    )
    _must("gfs-open-signup(connect)", s, conn, ok=(201,))
    if (
        conn.get("status") != "active"
        or conn.get("inbox_url") != gfs_url
        or conn.get("possession_proven") is not True
    ):
        raise SystemExit(f"gfs-open-signup: unexpected connection {conn!r}")
    print(f"  c: connected via open sign-up — id={conn['id'][:8]} status=active")

    s, again = _request(f"{base}/api/gfs/connections/default", token=c["token"])
    _must("gfs-open-signup(offer-after)", s, again, ok=(200,))
    if again.get("reason") != "already_connected":
        raise SystemExit(f"gfs-open-signup: offer after connect: {again!r}")
    s, dup = _request(
        f"{base}/api/gfs/connections/default", token=c["token"], method="POST"
    )
    if s != 409 or (dup.get("error") or {}).get("code") != "ALREADY_CONNECTED":
        raise SystemExit(f"gfs-open-signup: second connect: HTTP {s} {dup!r}")

    s, body = _request(
        f"{base}/api/gfs/connections/{conn['id']}", token=c["token"], method="DELETE"
    )
    _must("gfs-open-signup(disconnect)", s, body, ok=(204,))
    print("gfs-open-signup: ok (c paired via open sign-up, then disconnected)")


def _active_gfs_ids(state: dict, label: str) -> list[str]:
    """``label``'s ACTIVE GFS connection ids (``GET /api/gfs/connections``) —
    what a new global space is listed on. Since #874 a global space needs
    the owner's choice while any GFS is active (422
    ``GFS_SELECTION_REQUIRED``); the harness picks all of them."""
    inst = state["instances"][label]
    s, conns = _request(
        f"http://127.0.0.1:{inst['port']}/api/gfs/connections",
        token=inst["token"],
    )
    conns = _must(f"{label}: list GFS connections", s, conns)
    ids = [c["id"] for c in conns if c.get("status") == "active"]
    if not ids:
        raise SystemExit(f"{label} has no active GFS connection to list on")
    return ids


def cmd_gfs_traffic() -> None:
    """Exercise the global-space publish path against the running GFS.

    1. Alpha creates a ``space_type=global`` space. The
       ``_auto_publish_on_type`` hook on ``SpaceService`` fans out a
       signed publish call to every paired GFS.
    2. The harness polls ``GET /gfs/spaces`` on the GFS until the new
       space appears with ``status="active"``. Asserts the published
       metadata (name, owning_instance) matches what Alpha sent.

    This validates the publish wire end-to-end (SH-side ``publish_space``
    → POST ``/gfs/spaces/{id}/publish`` → GFS verifies the Ed25519
    signature against the registered ``ClientInstance.public_key`` →
    ``upsert_space`` row → ``list_spaces``). The downstream join /
    SPACE_POST_CREATED relay path is still TODO — see SKILL.md.
    """
    state = _load()
    if not state or not _gfs_alive(state):
        raise SystemExit("run 'gfs-up' + 'gfs-pair' first")

    a = state["instances"]["a"]
    space_name = "Global Test Space"
    s, body = _request(
        f"http://127.0.0.1:{a['port']}/api/spaces",
        token=a["token"],
        method="POST",
        body={
            "name": space_name,
            "description": "harness end-to-end probe for GFS publish",
            "space_type": "global",
            # #874: the owner picks the GFSs a global space is listed on.
            "gfs_ids": _active_gfs_ids(state, "a"),
            # Open to JOIN. Says nothing about readability any more — that is
            # the separate ``allow_subscribers`` opt-in, PATCHed in below.
            "join_mode": "open",
        },
    )
    body = _must("create-global-space", s, body, ok=(201,))
    space_id = body["id"]
    print(f"  a: created global space {space_id}")

    # Readability is an explicit owner opt-in and it defaults OFF, so a fresh
    # space relays nothing and seats no subscriber. The whole downstream chain
    # (``gfs-space-subscribe`` → ``gfs-space-post`` → ``gfs-space-rotate``)
    # proves the READABLE path, so turn followers on. ``POST /api/spaces``
    # takes no ``features`` block, so this is a PATCH — a partial one, which
    # merges onto the space's current features and leaves every other flag
    # alone.
    s, patched = _request(
        f"http://127.0.0.1:{a['port']}/api/spaces/{space_id}",
        token=a["token"],
        method="PATCH",
        body={"features": {"allow_subscribers": True}},
    )
    _must("enable-subscribers-on-global-space", s, patched, ok=(200,))
    print("  a: followers enabled — the space is now publicly readable")
    state.setdefault("gfs", {})["global_space_id"] = space_id
    _save(state)

    deadline = time.monotonic() + 30.0
    listing: list[dict] = []
    while time.monotonic() < deadline:
        s, payload = _request(f"http://127.0.0.1:{GFS_PORT}/gfs/spaces")
        if s == 200:
            listing = payload.get("spaces", []) if isinstance(payload, dict) else []
            if any(sp["space_id"] == space_id for sp in listing):
                break
        time.sleep(1.0)
    match = [sp for sp in listing if sp["space_id"] == space_id]
    if not match:
        raise SystemExit(
            f"gfs-traffic: space {space_id} did not appear on GET /gfs/spaces"
            f" within 30s — listing was {listing!r}",
        )
    sp = match[0]
    if sp["name"] != space_name:
        raise SystemExit(
            f"gfs-traffic: GFS listed name={sp['name']!r}, expected {space_name!r}",
        )
    if sp["owning_instance"] != a["instance_id"]:
        raise SystemExit(
            f"gfs-traffic: GFS listed owning_instance={sp['owning_instance']!r},"
            f" expected {a['instance_id']!r}",
        )
    print(
        f"  gfs lists '{sp['name']}' "
        f"(owner {sp['owning_instance'][:8]}…, status {sp['status']}) ✓"
    )
    print("gfs-traffic: ok (publish round-trip)")


@_offline_step
def cmd_gfs_replay() -> None:
    """Validate GFS-paired state survives an HFS restart.

    Sequence:
    1. Pre-check that the GFS lists Alpha's published global space and
       Alpha's local ``/api/gfs/publications`` shows the same row
       (i.e. the publish from :func:`cmd_gfs_traffic` already landed).
    2. SIGTERM Alpha and wait for the process to exit.
    3. While Alpha is down, the GFS still lists the space — the owning
       HFS being unreachable is not a deregistration signal. Asserted.
    4. Respawn Alpha on the same data_dir; wait for
       ``/api/instance/config`` to answer 200.
    5. Wait across the GFS WS supervisor reconcile interval + the
       ``GfsWebSocketClient`` first-connect window so the supervisor's
       background loop reopens ``wss://gfs/gfs/ws`` against the GFS.
    6. Re-assert: Alpha's ``/api/gfs/connections`` still shows the
       connection active, ``/api/gfs/publications`` still lists the
       global space, and the GFS continues to list it on
       ``GET /gfs/spaces``.

    Prereqs (chain via ``up`` → ``gfs-up`` → ``gfs-pair`` →
    ``gfs-traffic``).
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' first")
    if not _gfs_alive(state):
        raise SystemExit("run 'gfs-up' first")
    gfs = state.get("gfs") or {}
    global_space_id = gfs.get("global_space_id")
    if not global_space_id:
        raise SystemExit("run 'gfs-traffic' first — needs Alpha's global space")

    a = state["instances"]["a"]
    a_token = a["token"]
    gfs_url = f"http://127.0.0.1:{GFS_PORT}"
    pairings = (state.get("gfs") or {}).get("pairings") or {}
    alpha_pairing = pairings.get("a")
    if not alpha_pairing:
        raise SystemExit("run 'gfs-pair' first — Alpha must be paired")
    # ``cmd_gfs_pair`` stashes the local SH-side ``GfsConnection.id``
    # (UUID generated when Alpha called ``POST /api/gfs/connections``)
    # under ``id``. That's the row Alpha looks up via
    # ``/api/gfs/connections``; the GFS-side instance id is separate.
    gfs_conn_id = alpha_pairing["id"]

    # 1a. Pre-check — GFS lists the space.
    s, payload = _request(f"{gfs_url}/gfs/spaces")
    _must("gfs-replay: pre /gfs/spaces", s, payload)
    listing = payload.get("spaces", []) if isinstance(payload, dict) else []
    if not any(sp["space_id"] == global_space_id for sp in listing):
        raise SystemExit(
            f"gfs-replay precheck: GFS {gfs_url} did not list "
            f"{global_space_id} before Alpha shutdown — listing={listing!r}",
        )
    print(f"  pre-check: GFS lists {global_space_id[:8]}… ✓")

    # 1b. Pre-check — Alpha's local publications mirror.
    def _alpha_publications() -> list[dict]:
        s, body = _request(
            f"http://127.0.0.1:{a['port']}/api/gfs/publications",
            token=a_token,
        )
        _must("gfs-replay: /api/gfs/publications", s, body)
        return list(body.get("publications") or [])

    pubs = _alpha_publications()
    if not any(p.get("space_id") == global_space_id for p in pubs):
        raise SystemExit(
            f"gfs-replay precheck: Alpha's /api/gfs/publications missing "
            f"{global_space_id} — got {pubs!r}",
        )
    print(f"  pre-check: Alpha sees publication for {global_space_id[:8]}… ✓")

    # 2. Tear Alpha down (SIGTERM, then SIGKILL after grace) — process
    #    group so libdatachannel + the GFS WS background task exit too.
    _open_offline_window("a")
    print(f"  killing a (pid={a['pid']}) to simulate owning-HFS downtime")
    try:
        os.killpg(a["pid"], signal.SIGTERM)
    except ProcessLookupError:
        print("  a was already gone")
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and _alive(a["pid"]):
        time.sleep(0.2)
    if _alive(a["pid"]):
        try:
            os.killpg(a["pid"], signal.SIGKILL)
        except ProcessLookupError:
            pass
        time.sleep(0.5)

    # 3. While Alpha is offline, GFS keeps the row. (The GFS does not
    #    proactively unpublish on owning-instance disconnect — that
    #    would create a thundering-herd republish whenever a flock of
    #    HFSes restart in concert.)
    s, payload = _request(f"{gfs_url}/gfs/spaces")
    _must("gfs-replay: /gfs/spaces while a is down", s, payload)
    listing = payload.get("spaces", []) if isinstance(payload, dict) else []
    if not any(sp["space_id"] == global_space_id for sp in listing):
        raise SystemExit(
            f"gfs-replay: GFS dropped {global_space_id} while owning HFS "
            f"was offline — listing={listing!r}",
        )
    print(f"  during downtime: GFS still lists {global_space_id[:8]}… ✓")

    # 4. Respawn Alpha on the same port + data_dir.
    new_pid = _spawn("a", a["port"])
    state["instances"]["a"]["pid"] = new_pid
    _wait_ready(a["port"])
    print(f"  a respawned: pid={new_pid} ready=200")

    # 5. Settle the GFS WS supervisor reconnect. Worst case is one
    #    reconcile-loop pass (~5 s) + one WS reconnect-delay slot
    #    (1 s default). 8 s is generous for the WS hello to land.
    settle = 8
    print(f"  waiting {settle}s for GFS WS supervisor to reconnect…")
    time.sleep(settle)

    # 6a. Alpha's local connection row still active.
    s, conns = _request(
        f"http://127.0.0.1:{a['port']}/api/gfs/connections",
        token=a_token,
    )
    _must("gfs-replay: /api/gfs/connections", s, conns)
    rows = conns if isinstance(conns, list) else []
    match = [c for c in rows if c.get("id") == gfs_conn_id]
    if not match or match[0].get("status") != "active":
        raise SystemExit(
            f"gfs-replay: Alpha's connection {gfs_conn_id} not active after "
            f"restart — got {rows!r}",
        )
    print(f"  post-restart: Alpha connection {gfs_conn_id[:8]}… status=active ✓")

    # 6b. Alpha's publication mirror survived the restart.
    pubs = _alpha_publications()
    if not any(p.get("space_id") == global_space_id for p in pubs):
        raise SystemExit(
            f"gfs-replay: Alpha's /api/gfs/publications dropped "
            f"{global_space_id} across the restart — got {pubs!r}",
        )
    print(f"  post-restart: Alpha sees publication for {global_space_id[:8]}… ✓")

    # 6c. GFS still lists Alpha's space.
    s, payload = _request(f"{gfs_url}/gfs/spaces")
    _must("gfs-replay: /gfs/spaces post-restart", s, payload)
    listing = payload.get("spaces", []) if isinstance(payload, dict) else []
    if not any(sp["space_id"] == global_space_id for sp in listing):
        raise SystemExit(
            f"gfs-replay: GFS lost the space after Alpha restart — listing={listing!r}",
        )
    print(f"  post-restart: GFS still lists {global_space_id[:8]}… ✓")

    state["gfs_replay_ran"] = True
    _save(state)
    print("gfs-replay: ok (publication survives owning-HFS downtime)")


def _gfs_rows(sql: str, params: tuple = ()) -> list[tuple]:
    """Run a read-only query against the GFS's own SQLite DB.

    The GFS exposes ``subscriber_count`` on ``GET /gfs/spaces/{id}`` but
    never the subscriber *identities* without a space-authority signature
    (``GET /gfs/spaces/{id}/subscribers`` is seed-holder-gated on purpose).
    The harness needs to assert "it is **d** that got registered", not just
    "somebody did", so it reads the ``space_subscribers`` table directly.
    Read-only, same contract as :func:`_rows`.
    """
    conn = sqlite3.connect(GFS_DIR / "gfs.db")
    try:
        return list(conn.execute(sql, params))
    finally:
        conn.close()


def _gfs_write(sql: str, params: tuple = ()) -> None:
    """One write to the GFS's SQLite DB — ONLY to stage a household the demo
    cannot boot (an older follower's seat without a new ``accepts`` label)."""
    conn = sqlite3.connect(GFS_DIR / "gfs.db")
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _gfs_log_size() -> int:
    """Byte length of the GFS process log right now (0 if absent).

    Bookmark for :func:`_gfs_log_lines_matching`, same contract as
    :func:`_log_size` — the GFS log spans the whole ``gfs-*`` chain, so an
    unscoped grep would match evidence produced by an earlier step.
    """
    try:
        return (GFS_DIR / "log.txt").stat().st_size
    except OSError:
        return 0


def _gfs_log_lines_matching(needle: str, *, offset: int = 0) -> list[str]:
    """Lines of the GFS process log containing ``needle``, from byte ``offset``.

    Same file :func:`_wait_for_gfs_ws` polls (``cmd_gfs_up`` writes it),
    sliced on bytes so an offset from :func:`_gfs_log_size` lines up.
    """
    path = GFS_DIR / "log.txt"
    if not path.exists():
        return []
    try:
        raw = path.read_bytes()
    except OSError:
        return []
    return [
        line
        for line in raw[offset:].decode("utf-8", errors="replace").splitlines()
        if needle in line
    ]


def _wait_for_gfs_ws(instance_id: str, *, timeout: float = 60.0) -> None:
    """Block until the GFS has registered ``instance_id``'s WebSocket.

    ``GfsWebSocketSupervisor`` opens the socket from a background reconcile
    loop, so a household that paired seconds ago may not be connected yet.
    Every GFS→household push (relay frames, ``new_subscriber`` notifies,
    sealed key handoffs) needs it, and the HTTPS-inbox fallback the GFS uses
    when the socket is missing is not an authenticated path for relay frames
    (it answers 401), so a frame sent too early is simply lost.
    """
    marker = f"gfs.ws.register: instance={instance_id}"
    log_path = GFS_DIR / "log.txt"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if log_path.exists() and marker in log_path.read_text(errors="replace"):
            return
        time.sleep(1.0)
    raise SystemExit(
        f"gfs-space-subscribe: instance {instance_id} never opened its SH↔GFS "
        f"WebSocket within {timeout:.0f}s (no {marker!r} in {log_path}) — "
        "GFS pushes to it would be lost.",
    )


def cmd_gfs_space_subscribe() -> None:
    """Discover Alpha's global space on Delta and subscribe to it over the GFS.

    Prereqs (chain via ``up`` → ``gfs-up`` → ``gfs-pair`` → ``gfs-traffic``):
    Alpha owns a published ``space_type=global`` space (state key
    ``gfs.global_space_id``), and both Alpha and Delta are active GFS clients.

    Topology matters: ``cmd_gfs_pair`` pairs **a** and **d** with the GFS, but
    a and d are NOT QR-paired with each other (d only pairs with b). So every
    byte d learns about a's space travelled through the GFS — there is no
    HFS↔HFS shortcut that could mask a broken relay. **c** is paired with
    neither and is the negative control in :func:`cmd_gfs_space_post`.

    Sequence:
    1. Force Delta's directory poll — ``POST /api/public_spaces/refresh``
       (admin-only, 202) runs :meth:`PublicSpaceDiscoveryService.refresh_now`
       inline instead of waiting for the scheduled tick.
    2. Assert Alpha's space now shows on Delta's
       ``GET /api/public_spaces`` with the right ``name`` and ``instance_id``.
       This is the regression for the two discovery bugs: polling the wrong
       GFS URL (nothing ever lands in ``public_space_cache``) and mapping the
       listing's ``owning_instance`` onto the wrong local field (the row shows
       up but points at nobody, so the join can never be routed).
    3. Delta subscribes — ``POST /api/spaces/{id}/subscribe``.
    4. Assert Delta now holds a local ``spaces`` row for the space whose
       ``identity_public_key`` equals Alpha's own pin for the same space.
       This is the regression for the missing-mirror bug: without the
       :class:`GfsSpaceMirrorService` stub there is no pinned space-authority
       key, so every relayed frame fails ``_verify_authority`` and is dropped
       — silently, at WARNING, with the subscribe itself still returning 200.
    5. Assert the GFS registered **d** as a subscriber (public
       ``subscriber_count`` moved, and the ``space_subscribers`` row names
       Delta's instance id). Without the GFS-side registration the relay fan-out
       never targets Delta at all.
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' first")
    if not _gfs_alive(state):
        raise SystemExit("run 'gfs-up' first")
    gfs = state.get("gfs") or {}
    space_id = gfs.get("global_space_id")
    if not space_id:
        raise SystemExit("run 'gfs-traffic' first — needs Alpha's global space")
    pairings = gfs.get("pairings") or {}
    if "d" not in pairings:
        raise SystemExit("run 'gfs-pair' first — Delta must be a GFS client")

    a = state["instances"]["a"]
    d = state["instances"]["d"]
    gfs_url = f"http://127.0.0.1:{GFS_PORT}"

    # 0. Wait for Delta's SH↔GFS WebSocket to be REGISTERED on the GFS side.
    #    This is not cosmetic: the ``new_subscriber`` → sealed-content-key
    #    handoff that ``POST /api/spaces/{id}/subscribe`` triggers is
    #    fire-and-forget. If the subscriber's WS isn't up when the GFS fans
    #    that frame out, the GFS falls back to the household's HTTPS
    #    ``/federation/inbox`` — which rejects GFS relay frames with 401 — and
    #    the subscriber is left permanently keyless for that epoch. In a real
    #    deployment the WS has been up for hours; in the harness d pairs
    #    seconds before it subscribes, and the supervisor's reconcile loop
    #    can take ~30 s to open the socket. The GFS's own log is the
    #    authoritative signal (``gfs.ws.register: instance=<d>``).
    _wait_for_gfs_ws(d["instance_id"], timeout=60.0)
    print("  d's SH↔GFS WebSocket is registered on the GFS ✓")

    # Baseline the GFS's subscriber tally BEFORE d subscribes so step 5 can
    # assert the delta rather than an absolute that a re-run would already
    # satisfy.
    s, detail = _request(f"{gfs_url}/gfs/spaces/{space_id}")
    _must("gfs-space-subscribe: pre GET /gfs/spaces/{id}", s, detail)
    subs_before = int(detail.get("subscriber_count") or 0)
    space_name = detail.get("name")
    print(f"  gfs detail: name={space_name!r} subscriber_count={subs_before}")

    # 1. Force d's directory poll instead of waiting for the scheduled tick.
    s, body = _request(
        f"http://127.0.0.1:{d['port']}/api/public_spaces/refresh",
        token=d["token"],
        method="POST",
    )
    _must("d: POST /api/public_spaces/refresh", s, body, ok=(202,))
    print("  d: forced a GFS directory refresh (202) ✓")

    # Settle: ``refresh_now`` awaits the poll inline, but the cache write and
    # the GFS's own response are both async — give the row a moment to land.
    deadline = time.monotonic() + 20.0
    listing: list[dict] = []
    entry: dict | None = None
    while time.monotonic() < deadline:
        s, payload = _request(
            f"http://127.0.0.1:{d['port']}/api/public_spaces",
            token=d["token"],
        )
        if s == 200 and isinstance(payload, list):
            listing = payload
            entry = next(
                (row for row in listing if row.get("space_id") == space_id),
                None,
            )
            if entry is not None:
                break
        time.sleep(1.0)

    # 2. The directory row must exist AND carry the right metadata.
    if entry is None:
        raise SystemExit(
            f"gfs-space-subscribe: {space_id} never appeared on d's "
            f"/api/public_spaces within 20 s — discovery poll is not reaching "
            f"the GFS directory. Listing was {listing!r}",
        )
    if entry.get("name") != space_name:
        raise SystemExit(
            f"gfs-space-subscribe: d's directory row has name="
            f"{entry.get('name')!r}, GFS published {space_name!r}",
        )
    if entry.get("instance_id") != a["instance_id"]:
        raise SystemExit(
            f"gfs-space-subscribe: d's directory row has instance_id="
            f"{entry.get('instance_id')!r}, expected Alpha's "
            f"{a['instance_id']!r} (owning_instance → instance_id mapping)",
        )
    print(f"  d discovers '{entry['name']}' hosted by {entry['instance_id'][:8]}… ✓")

    # 3. Delta subscribes as a read-only member.
    s, sub = _request(
        f"http://127.0.0.1:{d['port']}/api/spaces/{space_id}/subscribe",
        token=d["token"],
        method="POST",
    )
    _must("d: POST /api/spaces/{id}/subscribe", s, sub, ok=(200,))
    if not sub.get("subscribed"):
        raise SystemExit(f"gfs-space-subscribe: subscribe returned {sub!r}")
    print("  d subscribed to the global space ✓")

    # Settle: the mirror fetch + local stub write + the GFS-side subscriber
    # registration all happen inside the request, but the GFS then pushes a
    # ``new_subscriber`` frame to Alpha over the SH↔GFS WebSocket.
    time.sleep(3)

    # 4. d must hold a local ``spaces`` row pinned to Alpha's authority key.
    d_rows = _rows(
        "d",
        "SELECT identity_public_key, name FROM spaces WHERE id = ?",
        (space_id,),
    )
    if not d_rows:
        raise SystemExit(
            f"gfs-space-subscribe: d has no local 'spaces' row for {space_id} — "
            "GfsSpaceMirrorService never seated the stub, so every relayed "
            "frame will fail authority verification.",
        )
    d_pin, d_name = d_rows[0]
    a_rows = _rows(
        "a",
        "SELECT identity_public_key FROM spaces WHERE id = ?",
        (space_id,),
    )
    if not a_rows:
        raise SystemExit(
            f"gfs-space-subscribe: a has no 'spaces' row for {space_id} — "
            "the owning household lost its own space?",
        )
    a_pin = a_rows[0][0]
    if not d_pin:
        raise SystemExit(
            f"gfs-space-subscribe: d's mirrored row for {space_id} has an "
            "empty identity_public_key — the pin was never seated.",
        )
    if d_pin != a_pin:
        raise SystemExit(
            f"gfs-space-subscribe: d pinned identity_public_key={d_pin!r} but "
            f"a's own space authority key is {a_pin!r} — relayed frames will "
            "never verify at d.",
        )
    print(f"  d mirrored the space (name={d_name!r}) pinned to a's key ✓")

    # 5. The GFS must now count d as a subscriber, and the row must name d.
    s, detail = _request(f"{gfs_url}/gfs/spaces/{space_id}")
    _must("gfs-space-subscribe: post GET /gfs/spaces/{id}", s, detail)
    subs_after = int(detail.get("subscriber_count") or 0)
    if subs_after < 1:
        raise SystemExit(
            f"gfs-space-subscribe: GFS subscriber_count is {subs_after} after "
            f"d subscribed (was {subs_before}) — the SH side never called "
            "POST /gfs/spaces/{id}/subscribe.",
        )
    gfs_subs = _gfs_rows(
        "SELECT instance_id FROM space_subscribers WHERE space_id = ?",
        (space_id,),
    )
    registered = {row[0] for row in gfs_subs}
    if d["instance_id"] not in registered:
        raise SystemExit(
            f"gfs-space-subscribe: GFS space_subscribers for {space_id} is "
            f"{sorted(registered)} — Delta ({d['instance_id']}) is not in it.",
        )
    print(
        f"  gfs registered d as a subscriber "
        f"(subscriber_count {subs_before} → {subs_after}) ✓"
    )

    state["gfs_space_id"] = space_id
    state["gfs_subscriber"] = "d"
    _save(state)
    print("gfs-space-subscribe: ok (discovery → mirror → GFS subscriber set)")


def _seat_local_member(
    state: dict,
    label: str,
    space_id: str,
    *,
    username: str,
    password: str,
    display_name: str | None = None,
) -> tuple[str, str]:
    """Provision a local user on ``label`` and seat them in ``space_id``.

    Returns ``(user_id, bearer_token)``. Idempotent: an existing user / an
    existing membership is reused, so the ``gfs-space-*`` steps can be re-run
    against a live sandbox.

    WHY :func:`cmd_gfs_space_post` posts TWICE — once as the setup admin and
    once as a user seated here — read before "simplifying" one away:

    The two mint their ``user_id`` by different rules. The setup admin's is
    username-anchored (``identity_bootstrap.derive_local_user_id``, the single
    minting rule shared by ``StandaloneAdapter.provision_admin`` and the
    ``/api/setup`` routes); a provisioned user's is anchored on a uuid4
    identity anchor via ``UserService.provision``. The GFS public-space relay
    fail-closes on a non-derivable author id — both the relaying seed-holder
    and the subscriber run ``verify_signed_author_inner``, whose self-cert is
    ``derive_user_id(author_pk, anchor_or_username) == author_user_id`` — so
    the demo has to exercise both shapes to prove the check holds for both.
    The old synthetic ``uid-<username>`` admin shape (which failed that
    self-cert, silently dropping every setup-admin post at the subscriber) is
    gone; migration 0049 re-derives it on already-deployed installs.
    """
    inst = state["instances"][label]
    base = f"http://127.0.0.1:{inst['port']}"
    s, created = _request(
        f"{base}/api/admin/users",
        token=inst["token"],
        method="POST",
        body={
            "username": username,
            "password": password,
            "display_name": display_name or username.title(),
        },
    )
    if s == 201:
        user_id = created["user_id"]
        print(f"  {label}: provisioned local user {username} ({user_id[:8]}…)")
    elif s == 409:
        s2, users = _request(f"{base}/api/users", token=inst["token"])
        _must(f"{label}: GET /api/users", s2, users)
        match = [u for u in users if u.get("username") == username]
        if not match:
            raise SystemExit(
                f"{label}: {username} reported as taken but is not in the user "
                f"list — got {users!r}",
            )
        user_id = match[0]["user_id"]
        print(f"  {label}: reusing local user {username} ({user_id[:8]}…)")
    else:
        raise SystemExit(
            f"{label}: POST /api/admin/users failed: HTTP {s} body={created!r}",
        )

    s, tok = _request(
        f"{base}/api/auth/token",
        method="POST",
        body={"username": username, "password": password},
    )
    _must(f"{label}: login as {username}", s, tok, ok=(200,))
    token = tok["token"]

    s, members = _request(f"{base}/api/spaces/{space_id}/members", token=inst["token"])
    _must(f"{label}: GET space members", s, members)
    if any(m.get("user_id") == user_id for m in members):
        print(f"  {label}: {username} is already a space member")
        return user_id, token

    s, inv = _request(
        f"{base}/api/spaces/{space_id}/members",
        token=inst["token"],
        method="POST",
        body={"user_id": user_id},
    )
    _must(f"{label}: invite {username}", s, inv, ok=(202,))
    s, acc = _request(
        f"{base}/api/local_invites/{inv['invitation_id']}/accept",
        token=token,
        method="POST",
    )
    _must(f"{label}: {username} accepts", s, acc, ok=(200,))
    print(f"  {label}: {username} accepted the space invitation ✓")
    return user_id, token


def _await_space_post(state: dict, label: str, space_id: str, post_id: str) -> dict:
    """Poll ``label``'s space feed until ``post_id`` shows up (or give up).

    Returns the post dict. Raises :class:`SystemExit` naming the likely drop
    reason — the two failure shapes both surface in the receiver's log as
    ``space_public.inbound`` WARNINGs.
    """
    inst = state["instances"][label]
    deadline = time.monotonic() + 40.0
    feed: list[dict] = []
    while time.monotonic() < deadline:
        time.sleep(2.0)
        s, body = _request(
            f"http://127.0.0.1:{inst['port']}/api/spaces/{space_id}/feed",
            token=inst["token"],
        )
        if s != 200:
            continue
        feed = body if isinstance(body, list) else (body.get("posts") or [])
        seen = next((p for p in feed if p.get("id") == post_id), None)
        if seen is not None:
            return seen
    raise SystemExit(
        f"{label} never saw post {post_id} in space {space_id} within 40 s. "
        f"Feed was {feed!r}. Check {_instance_dir(label) / 'log.txt'} for "
        "'space_public.inbound' WARNINGs — 'cannot decrypt … missing epoch N' "
        "means the content-key handoff never landed; 'author verification "
        "failed' means the self-cert / author signature didn't check out.",
    )


def cmd_gfs_space_post() -> None:
    """Public space CONTENT over the GFS: a posts, d reads it, c never sees it.

    Prereqs: ``gfs-space-subscribe`` (d is a GFS subscriber of a's global
    space and holds the mirrored authority pin).

    Sequence:
    1. Seat a provisioned local author on Alpha and post in the global space
       via ``POST /api/spaces/{id}/posts`` — the space endpoint, not the
       household feed, which would land a non-federating household post.
    2. Settle. The post has to be encrypted under the per-space content key,
       signed by the author's household identity, authority-signed by the
       space seed-holder, POSTed to the GFS relay, fanned out over the SH↔GFS
       WebSocket to every registered subscriber — and the content key itself
       has to have reached d through the ``new_subscriber`` → sealed
       key-handoff path fired when d subscribed.
    3. Assert d's ``GET /api/spaces/{id}/feed`` shows the post DECRYPTED, with
       the right author user id and body. One assertion, six moving parts: a
       break anywhere in relay, authority signature, ``new_subscriber`` notify,
       sealed content-key handoff, per-author signature, or decrypt shows up
       here and nowhere else.
    4. Post AGAIN as Alpha's **setup admin** and assert d sees that one too,
       attributed to the admin's own ``user_id``. The admin's id is
       username-anchored while the provisioned author's is uuid4-anchored (see
       :func:`_seat_local_member`), and the relay's per-author self-cert has to
       hold for both shapes — a regression to a synthetic admin id fails the
       attribution assertion here instead of silently dropping the post.
    5. Negative control: **c** — subscribed to nothing, GFS-paired with
       nothing, QR-paired with a but not a member of this space — must NOT
       hold EITHER post. This is the §"non-member households MUST NOT see
       space content" hard rule from CLAUDE.md, asserted on the real wire.
    6. Anonymity: the GFS relay is identity-free. The GFS log names a's
       instance id on no relay line and never mentions ``from_instance``; d's
       ``gfs.relay.received`` records carry ``space=`` + ``event=`` and no
       ``from=``; and a never logged the legacy identified-publish downgrade
       ("does not advertise anonymous_publish"). This is the live proof of
       "the GFS does not require, store, log or forward which household
       relayed a public-space event".
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' first")
    space_id = state.get("gfs_space_id")
    if not space_id:
        raise SystemExit("run 'gfs-space-subscribe' first")
    a = state["instances"]["a"]
    c = state["instances"]["c"]

    # 1. A provisioned (derivable-user_id) author on Alpha, seated in the space.
    author_id, author_token = _seat_local_member(
        state,
        "a",
        space_id,
        username="erin",
        password="erin-pw-demo",
        display_name=ERIN_DISPLAY_NAME,
    )
    state["gfs_space_author_user_id"] = author_id

    # Bookmarks for the step-6 anonymity assertions: everything the GFS, d and
    # a log from here on is what THIS publish produced.
    gfs_off = _gfs_log_size()
    d_off = _log_size("d")

    # Marker keeps the step re-runnable against a live sandbox.
    content = f"Global space post over the GFS — Grüße 漢字 😀 {time.time_ns()}"
    s, post = _request(
        f"http://127.0.0.1:{a['port']}/api/spaces/{space_id}/posts",
        token=author_token,
        method="POST",
        body={"type": "text", "content": content},
    )
    _must("a posts in the global space", s, post, ok=(201,))
    post_id = post["id"]
    print(f"  a posted in the global space → id={post_id}")

    # 2./3. Settle: encrypt → author-sign → authority-sign → POST to the GFS
    #       relay → GFS WS fan-out to subscribers → d verifies + decrypts +
    #       persists. The content key reached d via the ``new_subscriber``
    #       handoff at subscribe time.
    seen = _await_space_post(state, "d", space_id, post_id)
    if seen.get("content") != content:
        raise SystemExit(
            f"gfs-space-post: d decrypted content={seen.get('content')!r}, "
            f"expected {content!r}",
        )
    if seen.get("author") != author_id:
        raise SystemExit(
            f"gfs-space-post: d attributes the post to "
            f"{seen.get('author')!r}, expected {author_id!r}",
        )
    print(f"  d sees the post decrypted, authored by {seen['author']} ✓")
    # The host's relay copy went compressed with the preset dictionary
    # (``payload_suite: "zlib1+dict1"``, under its authority signature): the GFS
    # proved ``payload_zlib1_dict1`` and every follower seat there reads it.
    relayed = _gfs_log_lines_matching(
        f"GFS: relaying space_post_public for space {space_id}", offset=gfs_off
    )
    if not any("suite=zlib1+dict1" in line for line in relayed):
        raise SystemExit(
            f"gfs-space-post: the relay was not fanned out compressed: {relayed!r}"
        )
    print("  … relayed compressed (payload_suite zlib1+dict1) ✓")
    # …and to d as a binary frame (``ws_binary1``): d's socket asked for it
    # in its hello, so the ciphertext travelled raw and d rebuilt the exact
    # base64url string before verifying the authority signature.
    d_id = state["instances"]["d"]["instance_id"]
    binary = [
        line
        for line in _gfs_log_lines_matching(f"gfs.ws.register: instance={d_id}")
        if "binary=True" in line
    ]
    if not binary:
        raise SystemExit(
            "gfs-space-post: d's GFS socket never registered for binary frames "
            "(ws_binary1 in its hello)"
        )
    print("  … over d's binary socket (ws_binary1), verified and decrypted ✓")

    # 4. The OTHER author shape: Alpha's setup admin, whose user_id is
    #    username-anchored rather than uuid4-anchored. Same relay, same
    #    self-cert, different minting rule.
    admin_id = a["user_id"]
    admin_content = f"Setup-admin post over the GFS — {time.time_ns()}"
    s, admin_post = _request(
        f"http://127.0.0.1:{a['port']}/api/spaces/{space_id}/posts",
        token=a["token"],
        method="POST",
        body={"type": "text", "content": admin_content},
    )
    _must("a's setup admin posts in the global space", s, admin_post, ok=(201,))
    admin_post_id = admin_post["id"]
    print(f"  a's setup admin posted → id={admin_post_id}")

    # Settle: same encrypt → sign → relay → fan-out path as above; d already
    # holds the epoch content key, so only the per-author self-cert is new.
    admin_seen = _await_space_post(state, "d", space_id, admin_post_id)
    if admin_seen.get("content") != admin_content:
        raise SystemExit(
            f"gfs-space-post: d decrypted content={admin_seen.get('content')!r}, "
            f"expected {admin_content!r}",
        )
    if admin_seen.get("author") != admin_id:
        raise SystemExit(
            f"gfs-space-post: d attributes the setup-admin post to "
            f"{admin_seen.get('author')!r}, expected {admin_id!r} — the admin's "
            "user_id is not the derivable one the relay self-cert checks.",
        )
    print(f"  d sees the setup-admin post decrypted, authored by {admin_id} ✓")

    # 5. Negative control — c is not a member, not a subscriber, not GFS-paired.
    #    Neither author shape may reach it.
    for label, pid in (("provisioned", post_id), ("setup-admin", admin_post_id)):
        c_rows = _rows("c", "SELECT id FROM space_posts WHERE id = ?", (pid,))
        if c_rows:
            raise SystemExit(
                f"gfs-space-post: c holds the {label} space post {pid} — a "
                "non-member household received space content (§ hard rule "
                "violated).",
            )
    s, c_feed = _request(
        f"http://127.0.0.1:{c['port']}/api/spaces/{space_id}/feed",
        token=c["token"],
    )
    if s == 200:
        rows = c_feed if isinstance(c_feed, list) else (c_feed.get("posts") or [])
        have = {p.get("id") for p in rows}
        leaked = have & {post_id, admin_post_id}
        if leaked:
            raise SystemExit(
                f"gfs-space-post: c's feed for {space_id} exposes {sorted(leaked)}",
            )
    print("  c (non-member, non-subscriber) sees neither post ✓")

    # 6. ANONYMITY — the property the identity-free relay exists for. The
    #    ``POST /gfs/publish`` body is ``{space_id, event_type, payload}`` and
    #    the fan-out frame is ``{type, space_id, event_type, payload}``: the
    #    relaying household's id is on neither. Asserted on the three logs
    #    where a regression would actually show up.
    a_inst = a["instance_id"]

    # 6a. The GFS wrote a's instance id on NO publish/relay line. Scoped on
    #     purpose: the GFS legitimately knows a's id from other surfaces — the
    #     ``/gfs/register`` handshake, the authenticated ``/gfs/ws`` hello
    #     (``gfs.ws.register: instance=…``) and the space DIRECTORY listing,
    #     whose ``owning_instance`` is public by design ("GFS: published space
    #     … (owner=…)", written back in ``gfs-traffic``). The claim is about
    #     the CONTENT-relay path only, so the scan covers the lines this step
    #     produced and flags one only if it also mentions relaying.
    relay_terms = (
        "relay",
        "space_post_public",
        "space_subscriber_key_handoff",
        "/gfs/publish",
    )
    leaky = [
        line
        for line in _gfs_log_lines_matching(a_inst, offset=gfs_off)
        if any(term in line for term in relay_terms)
    ]
    if leaky:
        raise SystemExit(
            "gfs-space-post: the GFS logged a's instance id on a relay line — "
            f"the relay is no longer identity-free. Lines: {leaky!r}",
        )
    # ``from_instance`` is the legacy identity field. A household that talks to
    # an ``anonymous_publish`` GFS omits it, so it must not appear anywhere in
    # the GFS log (unscoped — a legacy-downgrade regression could log it at
    # registration or publish time alike).
    legacy = _gfs_log_lines_matching("from_instance")
    if legacy:
        raise SystemExit(
            "gfs-space-post: 'from_instance' appears in the GFS log — the "
            f"legacy identified-publish path was taken. Lines: {legacy!r}",
        )
    print("  the GFS logged no relaying-household id for either post ✓")

    # 6b. d's receive record names the space + event and NOTHING else.
    #     ``dispatch_gfs_relay_frame`` used to log ``from=%s``; a frame that
    #     carries (or a receiver that re-reads) the outer identity shows up as
    #     a ``from=`` on exactly this line.
    relay_lines = [
        line
        for line in _log_lines_matching("d", "gfs.relay.received:", offset=d_off)
        if f"space={space_id}" in line and "event=space_post_public" in line
    ]
    if not relay_lines:
        raise SystemExit(
            f"gfs-space-post: d logged no 'gfs.relay.received: space={space_id} "
            "event=space_post_public' record for this step — the frames landed "
            f"some other way. Check {_log_path('d')}.",
        )
    identified = [line for line in relay_lines if "from=" in line]
    if identified:
        raise SystemExit(
            "gfs-space-post: d's relay record carries a 'from=' — the fan-out "
            f"frame is identity-bearing again. Lines: {identified!r}",
        )
    print(f"  d's {len(relay_lines)} relay record(s) carry no 'from=' ✓")

    # 6c. a took the anonymous path. ``GfsConnectionService`` warns once per
    #     connection when the GFS did NOT advertise ``anonymous_publish`` and
    #     falls back to the legacy identified body; against this build's own
    #     GFS that downgrade must never fire. (``verify``'s log audit would
    #     also fail on the un-allow-listed WARNING — this names the cause.)
    downgrade = _log_lines_matching("a", "does not advertise anonymous_publish")
    if downgrade:
        raise SystemExit(
            "gfs-space-post: a logged the legacy identified-publish downgrade "
            "against a GFS that does advertise anonymous_publish. Lines: "
            f"{downgrade!r}",
        )
    print("  a never fell back to the legacy identified publish ✓")

    state["gfs_space_post_id"] = post_id
    state["gfs_space_post_content"] = content
    state["gfs_space_admin_post_id"] = admin_post_id
    state["gfs_space_admin_post_content"] = admin_content
    _save(state)
    print(
        "gfs-space-post: ok (relay + authority sig + key handoff + decrypt, "
        "both author shapes, identity-free relay)"
    )


def cmd_gfs_space_author_profile() -> None:
    """A GFS follower names a global space's authors (v_60).

    Prereqs: ``gfs-space-subscribe`` + ``gfs-space-post`` (d follows a's
    global space through the GFS and holds no roster; Erin, a member on a,
    posted there).

    1. Erin's post made a publish her profile into the space as an
       ``author_profile`` item. d's ``GET /api/spaces/{id}/authors`` names
       her "Erin" — d has no roster seat for her.
    2. Erin sets a bio and a picture. The profile change republishes; d gets
       the bio and a signed picture URL that returns WebP bytes.
    3. The GFS saw ciphertext only: its log and database never carry Erin's
       name or bio. c (no member, no follower) holds no profile.
    """
    state = _load()
    space_id = state.get("gfs_space_id") if state else None
    if not space_id or not state.get("gfs_space_author_user_id"):
        raise SystemExit("run 'gfs-space-subscribe' + 'gfs-space-post' first")
    a, d = state["instances"]["a"], state["instances"]["d"]
    erin = state["gfs_space_author_user_id"]
    a_url = f"http://127.0.0.1:{a['port']}"
    d_url = f"http://127.0.0.1:{d['port']}"
    gfs_off = _gfs_log_size()

    def _authors() -> dict:
        s, body = _request(f"{d_url}/api/spaces/{space_id}/authors", token=d["token"])
        if s != 200:
            raise SystemExit(
                f"gfs-space-author-profile: d /authors → HTTP {s} {body!r}"
            )
        return {row["user_id"]: row for row in body}

    def _wait(pred, what: str) -> dict:
        deadline = time.monotonic() + 90
        rows: dict = {}
        while time.monotonic() < deadline:
            rows = _authors()
            if pred(rows.get(erin)):
                return rows[erin]
            time.sleep(3)
        raise SystemExit(
            f"gfs-space-author-profile: d never got {what} (last {rows.get(erin)!r})"
        )

    row = _wait(lambda r: r is not None, "Erin's profile")
    if row["display_name"] != ERIN_DISPLAY_NAME:
        raise SystemExit(f"gfs-space-author-profile: d names Erin {row!r}")
    print("  d names Erin from her published profile (no roster seat) ✓")

    bio = ERIN_BIO
    s, login = _request(
        f"{a_url}/api/auth/token",
        method="POST",
        body={"username": "erin", "password": "erin-pw-demo"},
    )
    _must("gfs-space-author-profile: erin login(a)", s, login)
    tok = login["token"]
    s, me = _request(f"{a_url}/api/me", token=tok, method="PATCH", body={"bio": bio})
    _must("gfs-space-author-profile: erin bio", s, me)
    s, pic = _upload_file(
        f"{a_url}/api/me/picture",
        token=tok,
        filename="erin.webp",
        content=_make_demo_webp(),
        content_type="image/webp",
    )
    _must("gfs-space-author-profile: erin picture", s, pic)
    row = _wait(
        lambda r: r is not None and r.get("bio") == bio and r.get("picture_url"),
        "Erin's bio and picture",
    )
    if not row["picture_url"].startswith(
        f"api/spaces/{space_id}/authors/{erin}/picture"
    ):
        raise SystemExit(
            f"gfs-space-author-profile: picture URL {row['picture_url']!r}"
        )
    s, raw = _http_get_bytes(f"{d_url}/{row['picture_url']}")
    if s != 200 or not raw.startswith(b"RIFF"):
        raise SystemExit(f"gfs-space-author-profile: picture → HTTP {s} {raw[:16]!r}")
    print("  d gets Erin's bio and picture after her profile change ✓")

    leaks = [
        line
        for needle in ("Erin", bio, "author_profile")
        for line in _gfs_log_lines_matching(needle, offset=gfs_off)
    ]
    if leaks:
        raise SystemExit(
            f"gfs-space-author-profile: the GFS log names them: {leaks[:3]}"
        )
    for table in ("gfs_envelope_queue",):
        try:
            rows = _gfs_rows(f"SELECT * FROM {table}")
        except Exception:
            rows = []
        blob = repr(rows)
        if "Erin" in blob or bio in blob:
            raise SystemExit(f"gfs-space-author-profile: GFS {table} holds the profile")
    if _rows(
        "c", "SELECT 1 FROM space_author_profiles WHERE space_id = ?", (space_id,)
    ):
        raise SystemExit("gfs-space-author-profile: c (no member) holds a profile")
    print("  the GFS saw ciphertext only; c holds nothing ✓")
    state["gfs_space_author_profile"] = {
        "space_id": space_id,
        "user_id": erin,
        "bio": bio,
    }
    _save(state)
    print("gfs-space-author-profile: ok (a follower names a global space's authors)")


def cmd_gfs_space_follower_names() -> None:
    """A NEW follower names a global space's authors within seconds (v_61),
    and nobody else gets anything for it.

    Prereqs: ``gfs-space-author-profile`` (Erin's profile, with a bio, is
    out; d already follows the space). Runs LAST in its chain: it subscribes
    **e** to a's global space as a follower, which the ``gfs-invite-link*``
    steps (e as a link-joined member of that space) must not find.

    e refreshes its directory and subscribes. The GFS tells a
    (``new_subscriber``); a seals the content key to e and then ONE bundle
    of the cached, author-signed profiles — both TARGETED to e
    (``POST /gfs/publish`` with ``to``). Asserts:

    * e names Erin, with her bio, within 30 s — without Erin posting again;
    * from the subscribe on, every ``POST /gfs/publish`` the GFS took was
      targeted (its "relayed targeted … (1 reached)" line, one per publish),
      and no member published anything (no ``/gfs/member-publish*``) — so the
      other follower, d, was sent nothing for e's subscribe;
    * the GFS log carries neither her name nor her bio.
    """
    state = _load()
    gsap = (state or {}).get("gfs_space_author_profile")
    if not gsap:
        raise SystemExit("run 'gfs-space-author-profile' first")
    space_id, erin, bio = gsap["space_id"], gsap["user_id"], gsap["bio"]
    e = state["instances"]["e"]
    e_url = f"http://127.0.0.1:{e['port']}"
    _wait_for_gfs_ws(e["instance_id"], timeout=60.0)
    s, body = _request(
        f"{e_url}/api/public_spaces/refresh", token=e["token"], method="POST"
    )
    _must("e: POST /api/public_spaces/refresh", s, body, ok=(202,))
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        s, listing = _request(f"{e_url}/api/public_spaces", token=e["token"])
        if s == 200 and any(r.get("space_id") == space_id for r in listing):
            break
        time.sleep(1)
    else:
        raise SystemExit(f"gfs-space-follower-names: e never listed {space_id}")
    gfs_off = _gfs_log_size()
    t0 = time.monotonic()
    s, sub = _request(
        f"{e_url}/api/spaces/{space_id}/subscribe", token=e["token"], method="POST"
    )
    _must("e: POST /api/spaces/{id}/subscribe", s, sub, ok=(200,))
    got: dict | None = None
    while time.monotonic() - t0 < 30:
        s, rows = _request(f"{e_url}/api/spaces/{space_id}/authors", token=e["token"])
        if s == 200:
            got = next((r for r in rows if r.get("user_id") == erin), None)
            if got and got.get("bio") == bio:
                break
        time.sleep(2)
    else:
        raise SystemExit(
            f"gfs-space-follower-names: e did not name Erin within 30 s ({got!r})"
        )
    if got["display_name"] != ERIN_DISPLAY_NAME:
        raise SystemExit(f"gfs-space-follower-names: e names Erin {got!r}")
    print(
        f"  e (new follower) names Erin with her bio after "
        f"{time.monotonic() - t0:.0f} s, no new post ✓"
    )
    # Names come first; her picture follows in a later bundle part, checked
    # against the hash she signed.
    pic_deadline = time.monotonic() + 30
    while not got.get("picture_url") and time.monotonic() < pic_deadline:
        time.sleep(2)
        s, rows = _request(f"{e_url}/api/spaces/{space_id}/authors", token=e["token"])
        if s == 200:
            got = next((r for r in rows if r.get("user_id") == erin), got)
    if not got.get("picture_url"):
        raise SystemExit("gfs-space-follower-names: e never got Erin's picture")
    s, raw = _http_get_bytes(f"{e_url}/{got['picture_url']}")
    if s != 200 or not raw.startswith(b"RIFF"):
        raise SystemExit(f"gfs-space-follower-names: picture → HTTP {s} {raw[:16]!r}")
    print("  e gets Erin's picture from the bundle's picture part ✓")
    # Let a late publish land before counting.
    time.sleep(3)
    publishes = _gfs_log_lines_matching("POST /gfs/publish ", offset=gfs_off)
    targeted = _gfs_log_lines_matching("relayed targeted", offset=gfs_off)
    member = _gfs_log_lines_matching("POST /gfs/member-publish", offset=gfs_off)
    kinds = sorted(
        "bundle" if "space_author_profiles_bundle" in line else "handoff"
        for line in targeted
    )
    if member:
        raise SystemExit(
            f"gfs-space-follower-names: members republished for e's subscribe "
            f"(fanned out to every follower): {member[:2]}"
        )
    if (
        len(targeted) != len(publishes)
        or "bundle" not in kinds
        or not all("(1 reached)" in line for line in targeted)
    ):
        raise SystemExit(
            "gfs-space-follower-names: not every publish for e was targeted at e "
            f"({len(targeted)} targeted of {len(publishes)}: {targeted[:3]})"
        )
    print(
        f"  every GFS publish for e's subscribe was targeted at e alone "
        f"({', '.join(kinds)}); no member republished — d was sent nothing ✓"
    )
    leaks = [
        line
        for needle in ("Erin", bio)
        for line in _gfs_log_lines_matching(needle, offset=gfs_off)
    ]
    if leaks:
        raise SystemExit(f"gfs-space-follower-names: GFS log names her: {leaks[:2]}")
    state["gfs_space_follower_names"] = {"space_id": space_id, "user_id": erin}
    _save(state)
    print(
        "gfs-space-follower-names: ok (a new follower is named at once, "
        "by one targeted bundle)"
    )


def _wait_for_gfs_ws_since(
    instance_id: str, *, offset: int, timeout: float = 60.0
) -> None:
    """:func:`_wait_for_gfs_ws`, but only for a registration logged after
    byte ``offset`` — i.e. a RE-connect."""
    marker = f"gfs.ws.register: instance={instance_id}"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _gfs_log_lines_matching(marker, offset=offset):
            return
        time.sleep(1.0)
    raise SystemExit(
        f"{instance_id} did not re-open its GFS WebSocket within {timeout:.0f}s"
    )


@_offline_step
def cmd_gfs_space_follower_reconnect() -> None:
    """A follower's reconnect re-seals the key but never re-sends the
    author-profiles bundle (v_61) — even after the HOST restarted.

    Prereqs: ``gfs-space-follower-names`` (e follows a's global space and
    got its one bundle). Runs right after it, last in that chain.

    1. Restart **a** (the host): any in-memory "already sent" record is
       gone — before the fix that made e's next reconnect cost a ≤ 1 MB
       bundle again (and a GFS restart one per follower).
    2. Restart **e** (the follower): its GFS socket reconnects (the GFS
       sends ``new_subscriber`` with ``reason: "reconnect"``) and it
       re-subscribes every seat (a held seat: no notify at all).

    Asserts, in the GFS log from e's restart on: at least one targeted
    ``space_subscriber_key_handoff`` reached e's one socket, and no
    ``space_author_profiles_bundle`` was relayed. e still names Erin.
    """
    state = _load()
    gsfn = (state or {}).get("gfs_space_follower_names")
    if not gsfn:
        raise SystemExit("run 'gfs-space-follower-names' first")
    space_id, erin = gsfn["space_id"], gsfn["user_id"]
    a, e = state["instances"]["a"], state["instances"]["e"]

    # 1. Restart the host; let its own reconnect + reconcile settle.
    off = _gfs_log_size()
    _kill_household("a", a)
    state["instances"]["a"]["pid"] = _spawn("a", a["port"])
    _save(state)
    _wait_ready(a["port"])
    _wait_for_gfs_ws_since(a["instance_id"], offset=off)
    print("  a (host) restarted and re-opened its GFS socket ✓")
    time.sleep(8)

    # 1b. H2: Erin posts after the restart. Her profile didn't change, so a
    #     publishes NO profile item (the dedupe is the persisted cache row,
    #     not an in-memory map a restart forgets). Her own row on a — whose
    #     ts moves on every publish — must stay as it was.
    def _erin_row_ts() -> str | None:
        rows = _rows(
            "a",
            "SELECT ts FROM space_author_profiles WHERE space_id = ? "
            "AND author_user_id = ?",
            (space_id, erin),
        )
        return rows[0][0] if rows else None

    before = _erin_row_ts()
    if before is None:
        raise SystemExit("gfs-space-follower-reconnect: a holds no row for Erin")
    a_url = f"http://127.0.0.1:{a['port']}"
    s, login = _request(
        f"{a_url}/api/auth/token",
        method="POST",
        body={"username": "erin", "password": "erin-pw-demo"},
    )
    _must("gfs-space-follower-reconnect: erin login(a)", s, login)
    s, post = _request(
        f"{a_url}/api/spaces/{space_id}/posts",
        token=login["token"],
        method="POST",
        body={"type": "text", "content": f"After the restart — {time.time_ns()}"},
    )
    _must("gfs-space-follower-reconnect: erin posts", s, post, ok=(201,))
    time.sleep(8)
    if _erin_row_ts() != before:
        raise SystemExit(
            "gfs-space-follower-reconnect: a re-published Erin's unchanged "
            "profile after its restart"
        )
    print("  Erin posts after a's restart: her unchanged profile is not re-sent ✓")

    # 2. Restart the follower.
    gfs_off = _gfs_log_size()
    _kill_household("e", e)
    state["instances"]["e"]["pid"] = _spawn("e", e["port"])
    _save(state)
    _wait_ready(e["port"])
    _wait_for_gfs_ws_since(e["instance_id"], offset=gfs_off)
    print("  e (follower) restarted and re-opened its GFS socket ✓")

    handoffs: list[str] = []
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        handoffs = [
            line
            for line in _gfs_log_lines_matching("relayed targeted", offset=gfs_off)
            if "space_subscriber_key_handoff" in line and space_id in line
        ]
        if handoffs:
            break
        time.sleep(1)
    else:
        raise SystemExit(
            "gfs-space-follower-reconnect: e's reconnect produced no targeted "
            "key handoff for the space"
        )
    if not all("(1 reached)" in line for line in handoffs):
        raise SystemExit(
            f"gfs-space-follower-reconnect: a handoff missed e: {handoffs[:2]}"
        )
    # Let a late bundle (the regression) land before counting.
    time.sleep(8)
    bundles = _gfs_log_lines_matching("space_author_profiles_bundle", offset=gfs_off)
    if bundles:
        raise SystemExit(
            f"gfs-space-follower-reconnect: e's reconnect re-sent the bundle "
            f"after a host restart: {bundles[:2]}"
        )
    print(
        f"  e's reconnect: {len(handoffs)} targeted key handoff(s), "
        "no profiles bundle ✓"
    )
    s, rows = _request(
        f"http://127.0.0.1:{e['port']}/api/spaces/{space_id}/authors",
        token=e["token"],
    )
    names = {r["user_id"]: r.get("display_name") for r in rows} if s == 200 else {}
    if names.get(erin) != ERIN_DISPLAY_NAME:
        raise SystemExit(f"gfs-space-follower-reconnect: e lost Erin ({names!r})")
    print(
        "gfs-space-follower-reconnect: ok (a reconnect re-seals the key, "
        "never re-sends the bundle)"
    )


def cmd_gfs_space_rotate() -> None:
    """A content-key epoch rotation must not cut GFS subscribers off.

    Prereqs: ``gfs-space-post`` (d is a subscriber that has already read one
    post, so a failure here is unambiguously about the rotation).

    Removing a member rotates the per-space AES-256 content key (forward
    secrecy — the removed member must not read future posts). Members are
    re-keyed through the ``space_instances`` fan-out, but GFS subscribers hold
    a read-only subscription and are never in ``space_instances``: they need
    the separate :class:`SpaceSubscriberKeyOutbound` re-seal. Before that was
    wired, every post after a rotation was dropped at the subscriber with
    "no key for epoch N" — silently, until the subscriber's next GFS-WS
    reconnect happened to re-trigger a handoff.

    Sequence:
    1. Seat a SECOND provisioned member on Alpha (``frank``) purely so there is
       somebody to remove — the author from ``gfs-space-post`` (``erin``) has
       to survive the rotation to write the post in step 4.
    2. Alpha removes frank — ``DELETE /api/spaces/{id}/members/{user_id}`` —
       which runs ``_rotate_and_distribute_space_key``.
    3. Settle: the new epoch key has to be re-sealed to every GFS subscriber.
    4. Erin posts again, under the NEW epoch.
    5. Assert d can still read it. A drop here is the rotation regression.
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' first")
    space_id = state.get("gfs_space_id")
    if not space_id or not state.get("gfs_space_post_id"):
        raise SystemExit("run 'gfs-space-post' first")
    a = state["instances"]["a"]
    a_base = f"http://127.0.0.1:{a['port']}"

    # 1. The sacrificial member. Distinct from the author so the post in
    #    step 4 still has a derivable-user_id writer after the removal.
    victim_id, _victim_token = _seat_local_member(
        state,
        "a",
        space_id,
        username="frank",
        password="frank-pw-demo",
        display_name="Frank",
    )
    author_id, author_token = _seat_local_member(
        state,
        "a",
        space_id,
        username="erin",
        password="erin-pw-demo",
        display_name=ERIN_DISPLAY_NAME,
    )

    # 2. Remove frank — this is what forces the epoch rotation.
    s, removed = _request(
        f"{a_base}/api/spaces/{space_id}/members/{victim_id}",
        token=a["token"],
        method="DELETE",
    )
    _must("a: remove frank", s, removed, ok=(200,))
    print("  a: removed frank → content-key epoch rotation ✓")

    # 3. Settle: rotate_epoch → re-seal to members → re-seal to GFS
    #    subscribers (SpaceSubscriberKeyOutbound) → d persists the new epoch.
    time.sleep(10)

    # 4. Post again, now under the new epoch.
    content = f"Post after the epoch rotation — {time.time_ns()}"
    s, post = _request(
        f"{a_base}/api/spaces/{space_id}/posts",
        token=author_token,
        method="POST",
        body={"type": "text", "content": content},
    )
    _must("a posts after rotation", s, post, ok=(201,))
    post_id = post["id"]
    print(f"  a posted under the new epoch → id={post_id}")

    # 5. d must still be able to read it.
    seen = _await_space_post(state, "d", space_id, post_id)
    if seen.get("content") != content:
        raise SystemExit(
            f"gfs-space-rotate: d decrypted {seen.get('content')!r}, expected "
            f"{content!r}",
        )
    if seen.get("author") != author_id:
        raise SystemExit(
            f"gfs-space-rotate: d attributes the post to {seen.get('author')!r},"
            f" expected {author_id!r}",
        )
    print("  d reads the post-rotation post — rotated key was re-sealed ✓")

    state["gfs_space_rotated_post_id"] = post_id
    state["gfs_space_rotated_post_content"] = content
    _save(state)
    print("gfs-space-rotate: ok (epoch rotation re-keys GFS subscribers)")


def cmd_gfs_space_no_subscribers() -> None:
    """A space that takes no followers is LISTED but never publicly readable.

    A space carries a ``space_type`` (private / public / global), a
    ``join_mode`` (``invite_only`` / ``open`` / ``request``) telling people how
    to become a posting MEMBER, and — independently — an ``allow_subscribers``
    opt-in saying whether STRANGERS may follow it read-only. The product rule:
    with ``allow_subscribers`` off, the space is published to the GFS
    directory — that is how people discover it and get invited — but its
    CONTENT never leaves the member households. No post is relayed to the GFS,
    and the per-space content key is never sealed to a subscriber, so a
    stranger cannot read a group nobody let them into.

    The space here is deliberately ``join_mode=open``: anyone may JOIN it, and
    that still buys them nothing to read. Under the old (wrong) model, where
    ``open`` implied readable, this space would have relayed — so this step
    also guards against a regression back to that model.

    Prereqs (chain via ``up`` → ``gfs-up`` → ``gfs-pair``): a and d are active
    GFS clients. Independent of the readable-space chain — ``gfs-traffic``
    turns followers ON for its space precisely so that chain keeps proving the
    relay works.

    Sequence:
    1. Alpha creates a SECOND global space and leaves ``allow_subscribers``
       off (the default). ``_auto_publish_on_type`` publishes its METADATA to
       every paired GFS exactly as before.
    2. Assert the GFS directory lists it — ``GET /gfs/spaces`` — and reports
       ``allow_subscribers: false``. The metadata path is deliberately
       untouched by the content gate; a regression that "fixes" the leak by
       refusing to publish would hide the space from the people meant to
       request an invite, and fails here.
    3. Assert d DISCOVERS it too (``POST /api/public_spaces/refresh`` then
       ``GET /api/public_spaces``) — discovery is the whole point of listing —
       and that the listing carries the flag so d's browser can suppress
       Subscribe.
    4. Waits for d's SH↔GFS WebSocket to be registered (a relay frame would be
       fanned out over exactly that socket, so the negative assertion in
       step 6 is only meaningful once it is up), then d tries to subscribe.
       It must be REFUSED — the GFS 403s ``POST /gfs/subscribe`` for a space
       with the flag off, and d's own ``subscribe_to_space`` refuses before
       that on the mirrored stub, which now carries the truthful flag.
    5. Alpha posts into the space.
    6. Assert d got NO relay frame for that space (no ``gfs.relay.received:
       space=<id>`` in d's log since the bookmark) and holds no
       ``space_posts`` row / feed entry for the post. c — non-member,
       non-subscriber, not GFS-paired — must not hold it either.
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' first")
    if not _gfs_alive(state):
        raise SystemExit("run 'gfs-up' first")
    pairings = (state.get("gfs") or {}).get("pairings") or {}
    if "a" not in pairings or "d" not in pairings:
        raise SystemExit("run 'gfs-pair' first — a and d must be GFS clients")

    a = state["instances"]["a"]
    c = state["instances"]["c"]
    d = state["instances"]["d"]
    gfs_url = f"http://127.0.0.1:{GFS_PORT}"

    # 1. A global space anyone may JOIN but nobody may merely READ.
    space_name = f"No Followers Global Space — {time.time_ns()}"
    s, body = _request(
        f"http://127.0.0.1:{a['port']}/api/spaces",
        token=a["token"],
        method="POST",
        body={
            "name": space_name,
            "description": "harness probe: listed for discovery, never relayed",
            "space_type": "global",
            # #874: the owner picks the GFSs a global space is listed on.
            "gfs_ids": _active_gfs_ids(state, "a"),
            # Open to join — and STILL unreadable, because the readability
            # opt-in below is off. That pairing is the point of this step.
            "join_mode": "open",
        },
    )
    body = _must("create-no-subscribers-global-space", s, body, ok=(201,))
    space_id = body["id"]
    print(f"  a: created global space {space_id} with followers OFF")

    # 2. The GFS directory must LIST it — metadata publish is unaffected —
    #    and must report the flag truthfully.
    deadline = time.monotonic() + 30.0
    listing: list[dict] = []
    row: dict | None = None
    while time.monotonic() < deadline:
        s, payload = _request(f"{gfs_url}/gfs/spaces")
        if s == 200:
            listing = payload.get("spaces", []) if isinstance(payload, dict) else []
            row = next((sp for sp in listing if sp["space_id"] == space_id), None)
            if row is not None:
                break
        time.sleep(1.0)
    if row is None:
        raise SystemExit(
            f"gfs-space-no-subscribers: {space_id} never appeared on GET "
            f"/gfs/spaces within 30 s — a space with followers off must still "
            f"be LISTED for discovery. Listing was {listing!r}",
        )
    if row.get("allow_subscribers") is not False:
        raise SystemExit(
            "gfs-space-no-subscribers: the GFS reports allow_subscribers="
            f"{row.get('allow_subscribers')!r} for {space_id}, expected False "
            "— the owner never opted into followers",
        )
    if row.get("join_mode") != "open":
        raise SystemExit(
            "gfs-space-no-subscribers: the GFS reports join_mode="
            f"{row.get('join_mode')!r}, expected 'open' — the membership gate "
            "and the readability flag are independent and both must travel",
        )
    print(f"  the GFS lists '{space_name}' as open-to-join, not readable ✓")

    # 3. d discovers it through the directory poll, flag included.
    s, refreshed = _request(
        f"http://127.0.0.1:{d['port']}/api/public_spaces/refresh",
        token=d["token"],
        method="POST",
    )
    _must("d: POST /api/public_spaces/refresh", s, refreshed, ok=(202,))
    deadline = time.monotonic() + 20.0
    entry: dict | None = None
    d_listing: list[dict] = []
    while time.monotonic() < deadline:
        s, payload = _request(
            f"http://127.0.0.1:{d['port']}/api/public_spaces",
            token=d["token"],
        )
        if s == 200 and isinstance(payload, list):
            d_listing = payload
            entry = next(
                (r for r in d_listing if r.get("space_id") == space_id),
                None,
            )
            if entry is not None:
                break
        time.sleep(1.0)
    if entry is None:
        raise SystemExit(
            f"gfs-space-no-subscribers: {space_id} never appeared on d's "
            f"/api/public_spaces within 20 s — the space is not discoverable. "
            f"Listing was {d_listing!r}",
        )
    if entry.get("allow_subscribers") is not False:
        raise SystemExit(
            "gfs-space-no-subscribers: d's directory row reports "
            f"allow_subscribers={entry.get('allow_subscribers')!r}, expected "
            "False — the browser would offer a Subscribe button that 403s",
        )
    print(f"  d discovers '{entry.get('name')}' and sees it is not readable ✓")

    # d's SH↔GFS WebSocket must be REGISTERED before the negative assertion
    # means anything: a relay frame, had one been produced, is fanned out over
    # exactly that socket. Same wait as ``gfs-space-subscribe``.
    _wait_for_gfs_ws(d["instance_id"], timeout=60.0)
    print("  d's SH↔GFS WebSocket is registered on the GFS ✓")

    # 4. d asks to subscribe — and must be refused. Unlike the earlier
    #    join-mode attempt this is a hard assertion: the mirrored stub now
    #    carries the owner's truthful flag, so d refuses locally, and the GFS
    #    403s the same request independently.
    s, sub = _request(
        f"http://127.0.0.1:{d['port']}/api/spaces/{space_id}/subscribe",
        token=d["token"],
        method="POST",
    )
    if s == 200:
        raise SystemExit(
            "gfs-space-no-subscribers: d's subscribe SUCCEEDED for a space "
            f"with followers off ({space_id}) — both the household and the "
            "GFS must refuse it",
        )
    print(f"  d's subscribe was refused (HTTP {s}) ✓")

    # Bookmark d's log AFTER the subscribe so step 6 only scans what the post
    # produced. (``_spawn`` truncates log.txt on respawn — read at step time.)
    d_off = _log_size("d")

    # 5. Alpha posts into the space.
    content = f"Members-only content that must never leave — {time.time_ns()}"
    s, post = _request(
        f"http://127.0.0.1:{a['port']}/api/spaces/{space_id}/posts",
        token=a["token"],
        method="POST",
        body={"type": "text", "content": content},
    )
    _must("a posts in the no-followers global space", s, post, ok=(201,))
    post_id = post["id"]
    print(f"  a posted in the space → id={post_id}")

    # Settle: long enough that a relay, had one been produced, would have been
    # encrypted, authority-signed, POSTed to the GFS and fanned out to d.
    # ``gfs-space-post`` sees the readable equivalent land well inside this.
    time.sleep(10)

    # 6a. d saw no relay frame for this space at all.
    relay_lines = [
        line
        for line in _log_lines_matching("d", "gfs.relay.received:", offset=d_off)
        if f"space={space_id}" in line
    ]
    if relay_lines:
        raise SystemExit(
            "gfs-space-no-subscribers: d received a GFS relay frame for "
            f"{space_id} — its content stream must be dead. "
            f"Lines: {relay_lines!r}",
        )
    print("  d received no relay frame for the space ✓")

    # 6b. …and holds none of its content, by row and by feed.
    for label, inst in (("d", d), ("c", c)):
        if _rows(label, "SELECT id FROM space_posts WHERE id = ?", (post_id,)):
            raise SystemExit(
                f"gfs-space-no-subscribers: {label} holds post {post_id} from "
                "a space that allows no followers (§ hard rule violated).",
            )
        s, feed = _request(
            f"http://127.0.0.1:{inst['port']}/api/spaces/{space_id}/feed",
            token=inst["token"],
        )
        if s == 200:
            rows = feed if isinstance(feed, list) else (feed.get("posts") or [])
            if any(p.get("id") == post_id for p in rows):
                raise SystemExit(
                    f"gfs-space-no-subscribers: {label}'s feed for {space_id} "
                    f"exposes {post_id}",
                )
    print("  neither d nor c can see the post ✓")

    state["gfs_no_subscribers_space_id"] = space_id
    state["gfs_no_subscribers_post_id"] = post_id
    _save(state)
    print("gfs-space-no-subscribers: ok (listed for discovery, content never relayed)")


# ─── Step: gfs-invite-link ─────────────────────────────────────────────────


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Hand a 3xx back as an ``HTTPError`` instead of following it."""

    def redirect_request(self, *args, **kwargs):  # type: ignore[override]
        return None


#: Where the shipped GFS config sends its public pages (``[server]
#: public_viewer_base_url``) and the origin the viewer's requests carry.
_VIEWER_BASE = "https://viewer.social-home.io"


def _open_through_viewer(
    page_url: str, *, view: str, key: str, value: str, what: str
) -> tuple[int, dict]:
    """Open a GFS public page the way a visitor's browser does now.

    1. ``GET page_url`` answers ``200`` with a tiny hand-off page: the
       page's link-preview tags (``og:title`` …; generic ones for an invite,
       never naming its space) and a ``<meta http-equiv="refresh">`` to the
       PINNED viewer — the shipped base, the committed
       ``socialhome/pinned_viewer.json`` path — with ``v`` / ``g`` / the
       page's id in the fragment only.
    2. The viewer then reads the page's JSON from the GFS named in ``g``,
       cross-origin: ``Access-Control-Allow-Origin: *``, never credentials
       (the invite token in a POST body, never in a URL).

    Returns that JSON answer's ``(status, body)``.
    """
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(urllib.request.Request(page_url), timeout=10.0) as r:
            status, body = r.status, r.read().decode("utf-8")
            csp = r.headers.get("Content-Security-Policy", "")
            referrer = r.headers.get("Referrer-Policy", "")
    except urllib.error.HTTPError as exc:
        raise SystemExit(
            f"{what}: GET {page_url} answered {exc.code}, not the hand-off page"
        ) from exc
    if status != 200 or not csp.startswith("default-src 'none'"):
        raise SystemExit(f"{what}: GET {page_url} is not the inert hand-off page")
    if referrer != "no-referrer" or "<script" in body:
        raise SystemExit(f"{what}: the hand-off page may leak a referrer or run code")
    match = re.search(r'<meta http-equiv="refresh" content="0;url=([^"]+)">', body)
    if match is None:
        raise SystemExit(f"{what}: the hand-off page has no refresh to the viewer")
    if '<meta property="og:title"' not in body:
        raise SystemExit(f"{what}: the hand-off page carries no link preview")
    if view == "join" and "og:image" in body:
        raise SystemExit(f"{what}: an invite's preview shows more than generic tags")
    location = html.unescape(match.group(1))
    manifest = json.loads((REPO_ROOT / "socialhome" / "pinned_viewer.json").read_text())
    page, _, fragment = location.partition("#")
    if page != f"{_VIEWER_BASE}/{manifest['path']}":
        raise SystemExit(f"{what}: hands off to {page!r}, not the pinned viewer")
    if value in page:
        raise SystemExit(f"{what}: the viewer URL names {value!r} outside the fragment")
    params = dict(urllib.parse.parse_qsl(fragment))
    gfs = params.get("g", "")
    if params != {"v": view, "g": gfs, key: value} or not gfs:
        raise SystemExit(f"{what}: fragment {params!r} is not v={view}, g, {key}")
    print(
        f"  {what}: link preview + refresh to the pinned viewer "
        f"{manifest['path']}, all in the fragment ✓"
    )
    headers = {"Origin": _VIEWER_BASE}
    if view == "join":
        req = urllib.request.Request(
            f"{gfs}/gfs/pages/invite",
            method="POST",
            data=json.dumps({"token": value}).encode(),
            headers={**headers, "Content-Type": "application/json"},
        )
    else:
        segment = "spaces" if view == "space" else "people"
        req = urllib.request.Request(
            f"{gfs}/gfs/pages/{segment}/{value}", headers=headers
        )
    try:
        with urllib.request.urlopen(req, timeout=15.0) as r:
            status, hdrs, raw = r.status, r.headers, r.read()
    except urllib.error.HTTPError as exc:
        status, hdrs, raw = exc.code, exc.headers, exc.read()
    if hdrs.get("Access-Control-Allow-Origin") != "*" or hdrs.get(
        "Access-Control-Allow-Credentials"
    ):
        raise SystemExit(
            f"{what}: the page JSON is not readable cross-origin: {dict(hdrs)!r}"
        )
    return status, json.loads(raw or b"{}")


def _decode_invite_blob(code: str) -> dict:
    """``socialhome://invite#<base64url(JSON)>`` → the payload dict.

    Mirrors ``client/src/lib/spaceInviteCode.ts`` (and
    ``federation/invite_code.py`` on the mint side): the blob is
    UNPADDED base64url, so the padding has to be put back before
    decoding. A harness that silently accepted a malformed blob would
    turn a wire regression into a confusing join failure three
    assertions later.
    """
    marker = "socialhome://invite#"
    if not code.startswith(marker):
        raise SystemExit(f"not an invite code: {code[:60]!r}")
    blob = code[len(marker) :].strip()
    padded = blob + "=" * (-len(blob) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except Exception as exc:
        raise SystemExit(
            f"invite blob did not decode as base64url(JSON): {exc!r} "
            f"(blob was {blob[:80]!r}…)",
        ) from exc


def _join_body_from_blob(payload: dict) -> dict:
    """The ``POST /api/spaces/join`` body the SPA sends for a pasted code.

    Mirrors ``client/src/features/spaces/SpaceJoinByCodeDialog.tsx`` field
    for field, deliberately: the harness is proving the path a person
    actually walks, and a body the SPA would never send (say, one that
    forgot ``gfs``) would prove a path nothing takes. The §D2b block
    rides only when all three key fields are present, which is the same
    all-or-nothing rule ``routes/spaces.py::_bootstrap_hint`` applies.
    """
    body: dict = {"token": payload["token"]}
    issuer = str(payload.get("issuer_instance_id") or "")
    if issuer:
        body["issuer_instance_id"] = issuer
    if issuer and all(
        payload.get(k)
        for k in ("issuer_identity_pk", "issuer_keywrap_pk", "issuer_keywrap_sig")
    ):
        body["issuer_identity_pk"] = payload["issuer_identity_pk"]
        body["issuer_keywrap_pk"] = payload["issuer_keywrap_pk"]
        body["issuer_keywrap_sig"] = payload["issuer_keywrap_sig"]
        if payload.get("space_id"):
            body["space_id"] = payload["space_id"]
        if payload.get("issuer_proto_version"):
            body["issuer_proto_version"] = payload["issuer_proto_version"]
        if payload.get("expires_at"):
            body["expires_at"] = payload["expires_at"]
        via = payload.get("via_gfs") or {}
        if via.get("gfs_url"):
            body["gfs"] = via["gfs_url"]
    return body


def _check_writer_cert_on_e(state: dict, failures: list[str], cap) -> None:
    """v_49 tripwire: link-joined **e** holds a verifying writer cert."""
    import json as _json

    from socialhome.crypto import b64url_decode
    from socialhome.domain.writer_cert import WriterCert
    from socialhome.writer_cert import verify_writer_cert

    space_id = state["gfs_invite_space_id"]
    try:
        own = _rows("e", "SELECT identity_public_key FROM instance_identity")
        pin = _rows(
            "e", "SELECT identity_public_key FROM spaces WHERE id=?", (space_id,)
        )
        certs = _rows(
            "e",
            "SELECT epoch, writer_cert FROM space_keys "
            "WHERE space_id=? AND writer_cert IS NOT NULL ORDER BY epoch DESC",
            (space_id,),
        )
    except Exception as exc:
        failures.append(f"e: writer-cert read failed: {exc!r}")
        return
    if not own or not pin:
        failures.append("e: no identity / space row for the invite space")
        return
    if not certs:
        failures.append(
            f"e: holds no writer cert for the invite space (v_"
            f"{int(cap.MIN_FOR_MEMBER_GFS_PUBLISH)}) — the redeem ACK / rekey "
            "did not deliver one"
        )
        return
    epoch, raw = certs[0]
    try:
        cert = WriterCert.from_wire(_json.loads(raw))
        verify_writer_cert(
            cert,
            space_pubkey=bytes.fromhex(pin[0][0]),
            space_id=space_id,
            epoch=int(epoch),
            author_pk=bytes.fromhex(own[0][0]),
            required_scope="write",
        )
    except Exception as exc:
        failures.append(f"e: stored writer cert does not verify: {exc!r}")
        return
    assert b64url_decode(cert.instance_pk) == bytes.fromhex(own[0][0])
    print(
        f"  e holds a verifying writer cert for epoch {epoch} "
        f"(scope={cert.scope}, v_{int(cap.MIN_FOR_MEMBER_GFS_PUBLISH)}) ✓"
    )


def _check_writer_key_on_e(state: dict, failures: list[str], cap) -> None:
    """v_50 tripwire: link-joined **e** holds a verifying writer group key for
    the strict epoch ``gfs-member-publish-strict`` recorded."""
    import json as _json

    from socialhome.domain.writer_key import WriterKeyGrant
    from socialhome.infrastructure.key_manager import KeyManager
    from socialhome.writer_key import verify_writer_key_grant

    space_id = state["gfs_invite_space_id"]
    epoch = int(state["gfs_member_publish_strict_epoch"])
    try:
        pin = _rows(
            "e", "SELECT identity_public_key FROM spaces WHERE id=?", (space_id,)
        )
        row = _rows(
            "e",
            "SELECT writer_key FROM space_keys WHERE space_id=? AND epoch=?",
            (space_id, epoch),
        )
        kek = KeyManager.from_data_dir(_instance_dir("e"))
        if not pin or not row or row[0][0] is None:
            failures.append(
                f"e: holds no writer key for strict epoch {epoch} (v_"
                f"{int(cap.MIN_FOR_STRICT_MEMBER_PUBLISH)})"
            )
            return
        raw = kek.decrypt(
            row[0][0],
            associated_data=f"socialhome-writer-key:{space_id}:{epoch}".encode(),
        )
        grant = WriterKeyGrant.from_wire(_json.loads(raw))
        verify_writer_key_grant(
            grant, space_pubkey=bytes.fromhex(pin[0][0]), space_id=space_id
        )
    except Exception as exc:
        failures.append(f"e: stored writer key does not verify: {exc!r}")
        return
    print(
        f"  e holds a verifying writer group key for epoch {epoch} "
        f"(v_{int(cap.MIN_FOR_STRICT_MEMBER_PUBLISH)}) ✓"
    )


def _instance_row(label: str, instance_id: str) -> dict | None:
    """One ``remote_instances`` row on ``label`` as a dict, or ``None``.

    The seat a §D2b redeem creates is not visible through
    ``/api/pairing/connections`` in enough detail (``remote_inbox_url``
    and ``remote_keywrap_pk`` are deliberately not in the public shape —
    see ``routes/pairing.py::_instance_dict``), so the two assertions
    that matter most read the row itself.
    """
    rows = _rows(
        label,
        "SELECT source, remote_inbox_url, proto_version, remote_keywrap_pk, "
        "relay_via, status FROM remote_instances WHERE id = ?",
        (instance_id,),
    )
    if not rows:
        return None
    r = rows[0]
    return {
        "source": r[0],
        "remote_inbox_url": r[1],
        "proto_version": int(r[2] or 1),
        "remote_keywrap_pk": r[3],
        "relay_via": r[4],
        "status": r[5],
    }


def _assert_space_session_seat(label: str, peer_label: str, state: dict) -> dict:
    """Assert ``label`` holds a §D2b ``space_session`` seat for ``peer_label``.

    The same four facts on both sides — the seat is symmetric by
    construction and a one-sided check would miss the half that breaks.
    Returns the row so a caller can assert further on it.
    """
    peer_id = state["instances"][peer_label]["instance_id"]
    row = _instance_row(label, peer_id)
    if row is None:
        raise SystemExit(
            f"{label}: no remote_instances row for {peer_label} "
            f"({peer_id[:8]}…) — the bootstrap redeem seated nothing.",
        )
    if row["source"] != "space_session":
        raise SystemExit(
            f"{label}: the row for {peer_label} has source={row['source']!r}, "
            "expected 'space_session' — a link-joined household must be a "
            "space-scoped seat, not a social peer (it would then be in every "
            "DM / presence / moment fan-out).",
        )
    if row["remote_inbox_url"]:
        raise SystemExit(
            f"{label}: the {peer_label} seat carries remote_inbox_url="
            f"{row['remote_inbox_url']!r} — a household met through a public "
            "invite link must never learn the other's address (§D2b).",
        )
    if row["status"] != "confirmed":
        raise SystemExit(
            f"{label}: the {peer_label} seat is status={row['status']!r}, "
            "expected 'confirmed' — the space cannot federate against an "
            "unkeyed row.",
        )
    if not row["remote_keywrap_pk"]:
        raise SystemExit(
            f"{label}: the {peer_label} seat has no remote_keywrap_pk — "
            "every later envelope is sealed to that key, so the pair is "
            "seated but mute (migration 0050).",
        )
    print(
        f"  {label}: {peer_label} seated as space_session, no address, "
        f"key-wrap pinned, proto_version={row['proto_version']} ✓"
    )
    return row


def cmd_gfs_invite_link() -> None:
    """§D2b: a total stranger joins a space from a published invite link.

    THE topology this step exists for: **e** is paired with nobody. No QR
    handshake, no trust-relay introduction, and therefore no mesh route
    (discovery only walks CONFIRMED peers). Its one relationship is with
    the connection server. So when e redeems a's link,
    ``request_redeem`` cannot take the direct branch or the mesh branch,
    and the §D2b bootstrap path is the only thing left: a sealed redeem
    through ``POST /gfs/envelope``, a ``space_session`` seat on both
    sides, and ``GfsRelayTransport`` for everything after. a / b / c / d
    are all mesh-reachable from one another, which is exactly why none of
    them can prove this — a redeem between any two of them silently takes
    the ordinary ``SPACE_INVITE_TOKEN_REDEEM`` path.

    Prereqs: ``up`` + ``gfs-up`` + ``gfs-pair`` + ``gfs-traffic`` (a owns
    the published global space and both a and e are clients of the GFS).

    Sequence:
    1. a mints a PUBLISHED ``member`` link —
       ``POST /api/spaces/{id}/invite-tokens {role, publish_to_gfs}`` —
       and the response carries the shareable ``gfs.url``.
    2. Fetch that URL from the connection server with a plain HTTP client,
       exactly as a visitor's browser would: 200, the space's name, the
       ``socialhome://invite#…`` code — and **not** a's inbox URL. The
       page is public, so an address on it would publish a household's
       network location to strangers.
    3. Decode the blob (base64url JSON) and assert the same thing
       structurally: no address field anywhere in the payload. Public keys
       and ids only.
    4. e pastes it: ``POST /api/spaces/join`` with the body
       ``SpaceJoinByCodeDialog.tsx`` sends (:func:`_join_body_from_blob`).
    5. Assert the seat on BOTH sides: ``source='space_session'``, empty
       ``remote_inbox_url``, CONFIRMED, key-wrap key pinned. Plus e's
       ``space_members`` row at role ``member`` (the seat the ISSUER's
       token row decided) and the named stub on e's ``/api/spaces``,
       which is what the ACK's ``space_meta`` seeded.
    6. Assert the relay stayed a relay. The GFS log (booted at DEBUG, see
       ``cmd_gfs_up``) shows envelopes moving, and **no** line names both
       households together, the invite token, or the space's name — the
       #677 property the identity-free routing envelope exists for.
    7. Assert both households logged the redeem: a seated e "over the
       connection-server relay", e got the ACK. Before these lines a
       successful link redeem was the one outcome on this path that left
       no record at all.
    8. Revoke: a ``DELETE``s the link, the ``/join`` page turns 404 (the
       styled "expired or revoked" page, which must not echo the dead
       token back), and a fresh redeem of the same code is refused —
       while e, who already walked through the door, keeps its seat.
    9. Roles: a (the owner) mints an ``admin`` link on a SECOND space.
       Redeeming it seats e as a MEMBER with the admin role PENDING (owner
       decision, 2026-09-19 — a link can leak, so admin is never granted
       straight through); a sees the pending elevation, approves it, and
       only then is e's household an admin on the host — still with no
       signing seed, which a link-joined admin must never hold (the
       connection server pins a space's identity key TOFU-immutably, so
       that credential could never be taken back). e, now an admin on a
       member household, mints a moderator link there: forwarded to a (v_52),
       minted in a's table, redeemed by b into a moderator seat; e lists the
       host's links and revokes a second one (gone from a's table); e's
       admin link is refused (owner-only).
    10. Followers (v_30): a mints a ``subscriber`` link on a THIRD space
       and e redeems it into a read-only seat. Assert the seat on BOTH
       sides (``space_remote_members.role='subscriber'`` on the host —
       the row every write is judged against, and the row that makes the
       household listable and kickable — plus ``space_members`` on e),
       then that it READS (a's post arrives over the relay, decrypted with
       the content key the ACK carried) and cannot WRITE (403). A Follower
       link handed to another household used to be refused outright.
    11. Moderators (migration 0080): a mints a ``moderator`` link on a
       FOURTH space. e redeems the published one over the relay and b
       (paired) an ``internal`` one directly; both land in a ``moderator``
       seat straight through (no pending elevation) — asserted on their own
       ``space_members`` and on a's ``space_remote_members``.
    """
    state = _load()
    if not state or not _gfs_alive(state):
        raise SystemExit("run 'up' + 'gfs-up' + 'gfs-pair' + 'gfs-traffic' first")
    space_id = (state.get("gfs") or {}).get("global_space_id")
    if not space_id:
        raise SystemExit("run 'gfs-traffic' first (no published global space)")
    gfs_conn_id = (
        ((state.get("gfs") or {}).get("pairings") or {}).get("a", {}).get("id")
    )
    if not gfs_conn_id:
        raise SystemExit("run 'gfs-pair' first (a is not a client of the GFS)")

    a = state["instances"]["a"]
    e = state["instances"]["e"]
    a_base = f"http://127.0.0.1:{a['port']}"
    e_base = f"http://127.0.0.1:{e['port']}"
    space_name = "Global Test Space"

    # Bookmarks: everything the three logs get from here on is what THIS
    # redeem produced. a's and the GFS's logs already span the whole run.
    gfs_off = _gfs_log_size()
    a_off = _log_size("a")
    e_off = _log_size("e")

    # 0. Pre-condition, asserted rather than assumed: e must not already
    #    know a. If some earlier step paired them the redeem below would
    #    take the direct branch and this step would pass while testing
    #    nothing.
    if _instance_row("e", a["instance_id"]) is not None:
        raise SystemExit(
            "gfs-invite-link: e already holds a remote_instances row for a — "
            "e is supposed to be paired with NOBODY, so the bootstrap path "
            "would not be exercised. Re-run from a clean 'up'.",
        )

    # 0b. Give the space a BIG cover. A 1200 px WebP is dimension-capped,
    #     not byte-capped, and the ACK used to embed it verbatim: a ~500 KiB
    #     cover sealed past the 256 KiB bootstrap-blob cap, the relay
    #     answered 413, and e's join timed out (504) after a had already
    #     spent the token and seated e.
    s, cover = _upload_file(
        f"{a_base}/api/spaces/{space_id}/cover",
        token=a["token"],
        filename="cover.png",
        content=_make_noisy_png(1200, 800),
        content_type="image/png",
        timeout=60.0,
    )
    _must("a sets a large space cover", s, cover)
    host_cover = _rows(
        "a",
        "SELECT length(bytes_webp) FROM space_covers WHERE space_id = ?",
        (space_id,),
    )
    host_cover_len = host_cover[0][0] if host_cover else 0
    if host_cover_len < 400 * 1024:
        raise SystemExit(
            f"gfs-invite-link: a's stored cover is {host_cover_len} bytes — the "
            "step needs a ~500 KiB one to prove the join survives a big cover.",
        )
    print(f"  a's space has a {host_cover_len // 1024} KiB cover")

    # 1. Mint a published member link.
    s, link = _request(
        f"{a_base}/api/spaces/{space_id}/invite-tokens",
        token=a["token"],
        method="POST",
        body={"role": "member", "uses": 2, "publish_to_gfs": gfs_conn_id},
    )
    link = _must("a mints a published invite link", s, link, ok=(201,))
    gfs_block = link.get("gfs") or {}
    join_url = gfs_block.get("url")
    if not join_url:
        raise SystemExit(
            "gfs-invite-link: the mint response carries no gfs.url — the blob "
            f"was never parked on the bulletin board. Response: {link!r}",
        )
    invite_token = link["token"]
    print(f"  a minted a member link, published at {join_url}")

    # 2. The public page, read exactly as a visitor's browser reads it: a
    #    hand-off page (link preview + refresh) to the pinned viewer, which
    #    reads the page's JSON from the GFS.
    s, page = _open_through_viewer(
        join_url,
        view="join",
        key="t",
        value=join_url.rstrip("/").rsplit("/", 1)[-1],
        what="gfs-invite-link",
    )
    if s != 200:
        raise SystemExit(
            f"gfs-invite-link: POST /gfs/pages/invite answered HTTP {s}, expected 200",
        )
    if not str(page.get("qr", "")).startswith("data:image/png;base64,"):
        raise SystemExit("gfs-invite-link: the invite page carries no QR of the code")
    html = json.dumps(page, ensure_ascii=False)
    if "socialhome://invite#" not in html:
        raise SystemExit(
            "gfs-invite-link: the /join page carries no socialhome://invite# "
            "code — there is nothing for a visitor to copy.",
        )
    if space_name not in html:
        raise SystemExit(
            f"gfs-invite-link: the /join page never names {space_name!r} — a "
            "visitor cannot tell what they are being invited to.",
        )
    a_inbox = f"http://127.0.0.1:{a['port']}"
    if a_inbox in html:
        raise SystemExit(
            f"gfs-invite-link: the /join page leaks a's address ({a_inbox}) — "
            "the page is public, so a household's network location must never "
            "appear on it (§D2b: the redeem travels by instance id).",
        )
    print("  the /join page shows the space + a code, and no household address ✓")

    # 3. Same claim, structurally: decode the blob and look for an address.
    payload = _decode_invite_blob(str(page["invite_code"]))
    flat = json.dumps(payload)
    # ``127.0.0.1:1800`` catches every household port in this sandbox
    # (18001-18005) without catching the connection server's own 18765,
    # which the blob legitimately names in ``via_gfs``.
    for needle in ("inbox", "external_url", "127.0.0.1:1800"):
        if needle in flat:
            raise SystemExit(
                f"gfs-invite-link: the invite blob carries {needle!r} — it "
                "holds public keys and ids ONLY, because the page that serves "
                f"it is public. Payload: {flat[:300]}",
            )
    if payload.get("issuer_instance_id") != a["instance_id"]:
        raise SystemExit(
            "gfs-invite-link: the blob names issuer_instance_id="
            f"{payload.get('issuer_instance_id')!r}, expected a's "
            f"{a['instance_id']!r}",
        )
    via = payload.get("via_gfs") or {}
    if via.get("gfs_url") != f"http://127.0.0.1:{GFS_PORT}":
        raise SystemExit(
            f"gfs-invite-link: the blob's via_gfs is {via!r} — without the "
            "connection server's base URL the redeemer has nowhere to hand "
            "its sealed request.",
        )
    print(f"  the blob carries keys + ids only ({sorted(payload)}) ✓")

    # 4. e pastes the code into its OWN household.
    join_body = _join_body_from_blob(payload)
    s, joined = _request(
        f"{e_base}/api/spaces/join",
        token=e["token"],
        method="POST",
        body=join_body,
        # The handler awaits the sealed ACK on a nonce-keyed Future; the
        # round-trip is two relay hops plus the issuer's whole fail-closed
        # ladder, so give it more than the default read timeout.
        timeout=40.0,
    )
    joined = _must("e redeems the published link", s, joined, ok=(200, 201))
    if joined.get("space_id") != space_id:
        raise SystemExit(
            f"gfs-invite-link: e's join returned space_id="
            f"{joined.get('space_id')!r}, expected {space_id!r}",
        )
    print(f"  e joined {space_id} as {joined.get('role')!r} through the relay ✓")

    # 5. The seat, on both sides, plus e's own view of the space.
    _assert_space_session_seat("a", "e", state)
    e_seat = _assert_space_session_seat("e", "a", state)
    if e_seat["relay_via"] != f"http://127.0.0.1:{GFS_PORT}":
        raise SystemExit(
            f"gfs-invite-link: e's seat for a has relay_via="
            f"{e_seat['relay_via']!r} — without the introducing server's URL "
            "every later envelope has nowhere to go.",
        )
    member_rows = _rows(
        "e",
        "SELECT role FROM space_members WHERE space_id = ? AND user_id = ?",
        (space_id, e["user_id"]),
    )
    if not member_rows or member_rows[0][0] != "member":
        raise SystemExit(
            f"gfs-invite-link: e's space_members row for {space_id} is "
            f"{member_rows!r}, expected one row with role 'member' — the seat "
            "the ISSUER's token row decided.",
        )
    s, spaces = _request(f"{e_base}/api/spaces", token=e["token"])
    _must("e: GET /api/spaces", s, spaces)
    rows = spaces if isinstance(spaces, list) else (spaces.get("spaces") or [])
    mine = next((sp for sp in rows if sp.get("id") == space_id), None)
    if mine is None:
        raise SystemExit(
            f"gfs-invite-link: e's /api/spaces does not list {space_id} — the "
            f"ACK's space_meta never seated a local stub. Got {rows!r}",
        )
    if mine.get("name") != space_name:
        raise SystemExit(
            f"gfs-invite-link: e's stub is named {mine.get('name')!r}, expected "
            f"{space_name!r} — the ACK's space_meta did not seat the config.",
        )
    # The seat itself is asserted on ``space_members`` just above, not
    # here: ``/api/spaces`` returns the SPACE's shape and carries no
    # per-user role field, so reading one off it is always ``None``.
    print(f"  e's /api/spaces shows {mine.get('name')!r} (role member) ✓")

    # 5b. The big cover did not sink the join, and e still got art: a
    #     size-bounded WebP rendition under a's own cover hash.
    e_cover = _rows(
        "e",
        "SELECT bytes_webp, hash FROM space_covers WHERE space_id = ?",
        (space_id,),
    )
    a_hash = _rows("a", "SELECT cover_hash FROM spaces WHERE id = ?", (space_id,))
    if not e_cover or e_cover[0][0][8:12] != b"WEBP":
        raise SystemExit(
            "gfs-invite-link: e holds no WebP cover for the space — the ACK "
            "shipped none (the big cover should ship as a smaller rendition).",
        )
    if len(e_cover[0][0]) > 64 * 1024 or e_cover[0][1] != a_hash[0][0]:
        raise SystemExit(
            f"gfs-invite-link: e's cover is {len(e_cover[0][0])} bytes under "
            f"hash {e_cover[0][1]!r} — expected <= 64 KiB "
            "(SPACE_COVER_BOOTSTRAP_MAX_BYTES) under a's hash "
            f"{a_hash[0][0]!r}.",
        )
    if not _log_lines_matching("a", "shipping a", offset=a_off):
        raise SystemExit(
            "gfs-invite-link: a never logged shrinking the cover for the ACK "
            "('space snapshot: cover … shipping a N-byte rendition').",
        )
    print(
        f"  e got a {len(e_cover[0][0]) // 1024} KiB rendition of the "
        f"{host_cover_len // 1024} KiB cover under a's hash ✓",
    )

    # 6. The relay stayed a relay (#677). The routing envelope is
    #    ``{to_instance, sealed}`` — one id and a ciphertext — so the
    #    server cannot log the pair, the token or the space even if it
    #    tried. Asserted on the GFS's own DEBUG log, which is where a
    #    regression would show up first.
    env_lines = _gfs_log_lines_matching("gfs.envelope:", offset=gfs_off)
    if not env_lines:
        raise SystemExit(
            "gfs-invite-link: the GFS logged no 'gfs.envelope:' record for "
            "this redeem — the sealed legs went somewhere else. Check "
            f"{GFS_DIR / 'log.txt'}.",
        )
    both = [
        line
        for line in _gfs_log_lines_matching("", offset=gfs_off)
        if a["instance_id"] in line and e["instance_id"] in line
    ]
    if both:
        raise SystemExit(
            "gfs-invite-link: a GFS log line names BOTH households — the "
            "relay learned the relationship the identity-free envelope "
            f"exists to withhold. Lines: {both!r}",
        )
    leaked = [
        line for line in _gfs_log_lines_matching(invite_token, offset=gfs_off)
    ] + [
        line
        for line in _gfs_log_lines_matching(space_name, offset=gfs_off)
        if "gfs.envelope" in line
    ]
    if leaked:
        raise SystemExit(
            "gfs-invite-link: the GFS log carries the invite token or the "
            f"space name on a relay line: {leaked!r}",
        )
    print(
        f"  the GFS moved {len(env_lines)} envelope(s) and logged no pair, "
        "no token, no space name ✓"
    )

    # 7. Both households recorded the redeem. A success that logs nothing
    #    is un-diagnosable — the operator asking "did that link work?"
    #    reads these two lines.
    a_seated = [
        line
        for line in _log_lines_matching("a", "invite bootstrap: seated", offset=a_off)
        if e["instance_id"] in line and "connection-server relay" in line
    ]
    if not a_seated:
        raise SystemExit(
            "gfs-invite-link: a logged no 'invite bootstrap: seated … over the "
            f"connection-server relay' line naming e. Check {_log_path('a')}.",
        )
    e_acked = [
        line
        for line in _log_lines_matching("e", "acked our redeem", offset=e_off)
        if a["instance_id"] in line
    ]
    if not e_acked:
        raise SystemExit(
            "gfs-invite-link: e logged no ACK line for a's reply. Check "
            f"{_log_path('e')}.",
        )
    # Print from the message marker, not a tail slice: the instance id
    # sits at the FRONT of these messages and is the whole point of
    # quoting them.
    for who, line in (("a", a_seated[0]), ("e", e_acked[0])):
        msg = line[line.index("invite bootstrap:") :].strip()
        print(f"  {who}: {msg}")

    state["gfs_invite_space_id"] = space_id
    state["gfs_invite_token"] = invite_token
    state["gfs_invite_join_url"] = join_url
    state["gfs_invite_code_payload"] = payload
    _save(state)

    # 8. Revoke. The link dies, the page dies, the SEAT does not — taking
    #    the door away is not the same decision as evicting the people who
    #    already walked through it.
    s, body = _request(
        f"{a_base}/api/spaces/{space_id}/invite-tokens/{invite_token}",
        token=a["token"],
        method="DELETE",
    )
    _must("a revokes the link", s, body, ok=(204,))
    gfs_token = join_url.rstrip("/").rsplit("/", 1)[-1]
    s, dead = _open_through_viewer(
        join_url, view="join", key="t", value=gfs_token, what="gfs-invite-link"
    )
    if s != 404:
        raise SystemExit(
            f"gfs-invite-link: the invite page after the revoke answered HTTP "
            f"{s}, expected 404 — the blob is still on the bulletin board.",
        )
    if set(dead) != {"server_name"}:
        raise SystemExit(
            "gfs-invite-link: the dead link's answer is not the one "
            f'"expired or revoked" shape ({{server_name}}): {dead!r}',
        )
    if invite_token in json.dumps(dead) or gfs_token in json.dumps(dead):
        raise SystemExit(
            "gfs-invite-link: the revoked page echoes the dead token back — "
            "it invites the credential being copy-pasted onward.",
        )
    print("  revoked: the invite page is the one 404 that names no token ✓")
    s, denied = _request(
        f"{e_base}/api/spaces/join",
        token=e["token"],
        method="POST",
        body=join_body,
        timeout=40.0,
    )
    if s != 422:
        raise SystemExit(
            f"gfs-invite-link: a second redeem of the REVOKED code answered "
            f"HTTP {s} (expected 422): {denied!r}",
        )
    print("  a fresh redeem of the revoked code is refused (422) ✓")
    if _instance_row("e", a["instance_id"]) is None:
        raise SystemExit(
            "gfs-invite-link: revoking the link tore down e's seat — revoke "
            "must never un-seat somebody who already joined.",
        )
    print("  e keeps the seat it already had ✓")

    # 9. An ``admin`` link, on a second space, into an admin seat WITHOUT
    #    the space's signing seed.
    admin_space_name = f"Link-admin space {time.time_ns()}"
    s, admin_space = _request(
        f"{a_base}/api/spaces",
        token=a["token"],
        method="POST",
        body={
            "name": admin_space_name,
            "description": "second space — the admin-link seat",
            "space_type": "global",
            # #874: the owner picks the GFSs a global space is listed on.
            "gfs_ids": _active_gfs_ids(state, "a"),
            "join_mode": "invite_only",
        },
    )
    admin_space = _must("a creates the admin-link space", s, admin_space, ok=(201,))
    admin_space_id = admin_space["id"]
    # A mint is refused unless the space is LISTED and active on that
    # server — an invite link is a standing public URL, so the server
    # will not park one for a listing it does not have. The publish is
    # fired by ``_auto_publish_on_type`` on create, i.e. asynchronously,
    # so wait for the directory rather than racing it.
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        s, payload = _request(f"http://127.0.0.1:{GFS_PORT}/gfs/spaces")
        rows = payload.get("spaces", []) if isinstance(payload, dict) else []
        if any(sp.get("space_id") == admin_space_id for sp in rows):
            break
        time.sleep(1.0)
    else:
        raise SystemExit(
            f"gfs-invite-link: the admin-link space {admin_space_id} never "
            "appeared in the GFS directory — a link cannot be published for "
            "an unlisted space.",
        )
    s, admin_link = _request(
        f"{a_base}/api/spaces/{admin_space_id}/invite-tokens",
        token=a["token"],
        method="POST",
        body={"role": "admin", "uses": 1, "publish_to_gfs": gfs_conn_id},
    )
    admin_link = _must("a mints an admin link", s, admin_link, ok=(201,))
    admin_payload = _decode_invite_blob(admin_link["code"])
    s, admin_joined = _request(
        f"{e_base}/api/spaces/join",
        token=e["token"],
        method="POST",
        body=_join_body_from_blob(admin_payload),
        timeout=40.0,
    )
    admin_joined = _must("e redeems the admin link", s, admin_joined, ok=(200, 201))
    # An admin/mod link no longer grants admin straight through (owner
    # decision, 2026-09-19): e is seated as a MEMBER and the admin role is
    # pending a's approval.
    if admin_joined.get("role") != "member":
        raise SystemExit(
            f"gfs-invite-link: e's admin redeem returned role="
            f"{admin_joined.get('role')!r}, expected 'member' — an admin link "
            "seats a member and files a pending elevation.",
        )
    if admin_joined.get("pending_role") != "admin":
        raise SystemExit(
            "gfs-invite-link: e's admin redeem did not carry "
            f"pending_role='admin' (got {admin_joined.get('pending_role')!r}).",
        )
    admin_rows = _rows(
        "e",
        "SELECT role FROM space_members WHERE space_id = ? AND user_id = ?",
        (admin_space_id, e["user_id"]),
    )
    if not admin_rows or admin_rows[0][0] != "member":
        raise SystemExit(
            f"gfs-invite-link: e's space_members row for the admin space is "
            f"{admin_rows!r}, expected role 'member' (admin pending)",
        )
    print("  e redeemed the admin link into a MEMBER seat, admin pending ✓")
    # a (the owner/host) sees a pending admin elevation for e's household.
    s, pending = _request(
        f"{a_base}/api/spaces/{admin_space_id}/join-requests",
        token=a["token"],
    )
    pending = _must("a lists pending join-requests", s, pending, ok=(200,))
    elevations = [r for r in pending if r.get("requested_role") == "admin"]
    if len(elevations) != 1:
        raise SystemExit(
            f"gfs-invite-link: a expected exactly one pending admin elevation, "
            f"saw {pending!r}",
        )
    elev_id = elevations[0]["id"]
    # a approves it — the owner-only promote runs (set_remote_member_role).
    s, approved = _request(
        f"{a_base}/api/spaces/{admin_space_id}/join-requests/{elev_id}/approve",
        token=a["token"],
        method="POST",
        body={},
    )
    _must("a approves e's admin elevation", s, approved, ok=(200, 201))
    # a's roster now records e's household as an admin.
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        host_role = _rows(
            "a",
            "SELECT role FROM space_remote_members WHERE space_id = ? "
            "AND instance_id = ?",
            (admin_space_id, e["instance_id"]),
        )
        if host_role and host_role[0][0] == "admin":
            break
        time.sleep(1.0)
    else:
        raise SystemExit(
            "gfs-invite-link: after approval, a's space_remote_members role for "
            f"e is {host_role!r}, expected 'admin'",
        )
    print("  a approved the elevation — e's household is now admin on the host ✓")
    # …and NOT the signing seed. An admin met through a public link
    # manages the space through SPACE_REMOTE_ADMIN_ACTION; the seed is a
    # credential nobody could take back, because the connection server
    # pins a space's identity key TOFU-immutably on first publish.
    seed_rows = _rows(
        "e",
        "SELECT identity_private_key FROM spaces WHERE id = ?",
        (admin_space_id,),
    )
    if seed_rows and seed_rows[0][0]:
        raise SystemExit(
            "gfs-invite-link: e holds the space SIGNING SEED for the "
            "admin-link space — a household that walked in off a public link "
            "must never get authority that cannot be revoked.",
        )
    shared = _log_lines_matching(
        "a", "space_admin_key_share", offset=a_off
    ) + _log_lines_matching("a", "SPACE_ADMIN_KEY_SHARE", offset=a_off)
    if shared:
        raise SystemExit(
            f"gfs-invite-link: a shipped a seed share on this path: {shared!r}",
        )
    print("  e is admin on the second space and holds NO signing seed ✓")

    # 9b. A member household's admin mints links too (v_52): e — link-joined,
    #     reaching a only over the relay — asks for a moderator link on a's
    #     space; the mint is forwarded to a, which mints it in ITS table and
    #     hands it back. b (paired with a) redeems it straight into a
    #     moderator seat. An admin link stays the owner's: e is refused.
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        e_role = _rows(
            "e",
            "SELECT role FROM space_members WHERE space_id = ? AND user_id = ?",
            (admin_space_id, e["user_id"]),
        )
        if e_role and e_role[0][0] == "admin":
            break
        time.sleep(1.0)
    else:
        raise SystemExit(
            f"gfs-invite-link: e's own seat on the admin space is {e_role!r}, "
            "expected 'admin' after the approval",
        )
    s, fwd_link = _request(
        f"{e_base}/api/spaces/{admin_space_id}/invite-tokens",
        token=e["token"],
        method="POST",
        body={"role": "moderator", "uses": 1},
        timeout=40.0,
    )
    fwd_link = _must("e mints a forwarded moderator link", s, fwd_link, ok=(201,))
    fwd_payload = _decode_invite_blob(fwd_link["code"])
    if fwd_payload.get("issuer_instance_id") != a["instance_id"]:
        raise SystemExit(
            "gfs-invite-link: e's forwarded link names issuer "
            f"{fwd_payload.get('issuer_instance_id')!r}, expected a — the "
            "host mints it",
        )
    if not _rows(
        "a",
        "SELECT 1 FROM space_invite_tokens WHERE token = ? AND role = 'moderator'",
        (fwd_link["token"],),
    ):
        raise SystemExit(
            "gfs-invite-link: a holds no token row for e's forwarded link",
        )
    if _rows(
        "e",
        "SELECT 1 FROM space_invite_tokens WHERE token = ?",
        (fwd_link["token"],),
    ):
        raise SystemExit(
            "gfs-invite-link: e wrote the forwarded link into its OWN table",
        )
    b_fwd = state["instances"]["b"]
    s, b_fwd_joined = _request(
        f"http://127.0.0.1:{b_fwd['port']}/api/spaces/join",
        token=b_fwd["token"],
        method="POST",
        body=_join_body_from_blob(fwd_payload),
        timeout=40.0,
    )
    b_fwd_joined = _must("b redeems e's forwarded link", s, b_fwd_joined)
    if b_fwd_joined.get("role") != "moderator":
        raise SystemExit(
            f"gfs-invite-link: b's redeem of e's forwarded link returned "
            f"{b_fwd_joined!r}, expected role 'moderator'",
        )
    _assert_moderator_seat("b", b_fwd, admin_space_id)
    # …and e sees and revokes the host's links from its own household.
    # A second (unused) forwarded link, so the revoke takes down a live one.
    s, fwd_link2 = _request(
        f"{e_base}/api/spaces/{admin_space_id}/invite-tokens",
        token=e["token"],
        method="POST",
        body={"role": "member", "uses": 1},
        timeout=40.0,
    )
    fwd_link2 = _must("e mints a second forwarded link", s, fwd_link2, ok=(201,))
    s, listed = _request(
        f"{e_base}/api/spaces/{admin_space_id}/invite-tokens",
        token=e["token"],
        timeout=40.0,
    )
    listed = _must("e lists the host's links", s, listed, ok=(200,))
    listed_tokens = {t.get("token") for t in listed.get("tokens", [])}
    if fwd_link2["token"] not in listed_tokens:
        raise SystemExit(
            f"gfs-invite-link: e's forwarded list {sorted(listed_tokens)!r} "
            f"lacks the link it minted ({fwd_link2['token']!r})",
        )
    s, revoked = _request(
        f"{e_base}/api/spaces/{admin_space_id}/invite-tokens/{fwd_link2['token']}",
        token=e["token"],
        method="DELETE",
        timeout=40.0,
    )
    _must("e revokes its forwarded link", s, revoked, ok=(204,))
    if _rows(
        "a",
        "SELECT 1 FROM space_invite_tokens WHERE token = ?",
        (fwd_link2["token"],),
    ):
        raise SystemExit(
            "gfs-invite-link: a still holds the link e revoked through it",
        )
    print("  e listed the host's links and revoked one — gone from a's table ✓")
    s, refused_admin = _request(
        f"{e_base}/api/spaces/{admin_space_id}/invite-tokens",
        token=e["token"],
        method="POST",
        body={"role": "admin", "uses": 1},
    )
    if s != 403:
        raise SystemExit(
            f"gfs-invite-link: e's admin-link mint answered HTTP {s} "
            f"(expected 403 — admin links are the owner's): {refused_admin!r}",
        )
    print(
        "  e (link-joined admin) minted a moderator link through a; b redeemed "
        "it as a moderator; e's admin link refused ✓"
    )

    # 10. A ``subscriber`` (Follower) link, on a THIRD space, into a
    #     read-only seat: e receives the space's content over the relay and
    #     is refused every write. This is the flagship case for a published
    #     link — hand it to a stranger so they can *read* the space.
    fan_space_name = f"Link-follower space {time.time_ns()}"
    s, fan_space = _request(
        f"{a_base}/api/spaces",
        token=a["token"],
        method="POST",
        body={
            "name": fan_space_name,
            "description": "third space — the follower-link seat",
            "space_type": "global",
            # #874: the owner picks the GFSs a global space is listed on.
            "gfs_ids": _active_gfs_ids(state, "a"),
            "join_mode": "invite_only",
        },
    )
    fan_space = _must("a creates the follower-link space", s, fan_space, ok=(201,))
    fan_space_id = fan_space["id"]
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        s, payload = _request(f"http://127.0.0.1:{GFS_PORT}/gfs/spaces")
        rows = payload.get("spaces", []) if isinstance(payload, dict) else []
        if any(sp.get("space_id") == fan_space_id for sp in rows):
            break
        time.sleep(1.0)
    else:
        raise SystemExit(
            f"gfs-invite-link: the follower-link space {fan_space_id} never "
            "appeared in the GFS directory — a link cannot be published for "
            "an unlisted space.",
        )
    s, fan_link = _request(
        f"{a_base}/api/spaces/{fan_space_id}/invite-tokens",
        token=a["token"],
        method="POST",
        body={"role": "subscriber", "uses": 1, "publish_to_gfs": gfs_conn_id},
    )
    fan_link = _must("a mints a follower link", s, fan_link, ok=(201,))
    fan_payload = _decode_invite_blob(fan_link["code"])
    s, fan_joined = _request(
        f"{e_base}/api/spaces/join",
        token=e["token"],
        method="POST",
        body=_join_body_from_blob(fan_payload),
        timeout=40.0,
    )
    fan_joined = _must("e redeems the follower link", s, fan_joined, ok=(200, 201))
    if fan_joined.get("role") != "subscriber":
        raise SystemExit(
            f"gfs-invite-link: e's follower redeem returned role="
            f"{fan_joined.get('role')!r}, expected 'subscriber' — a Follower "
            "link handed to another household used to be refused outright.",
        )
    fan_rows = _rows(
        "e",
        "SELECT role FROM space_members WHERE space_id = ? AND user_id = ?",
        (fan_space_id, e["user_id"]),
    )
    if not fan_rows or fan_rows[0][0] != "subscriber":
        raise SystemExit(
            f"gfs-invite-link: e's space_members row for the follower space is "
            f"{fan_rows!r}, expected role 'subscriber'",
        )
    # The HOST's seat is the authority every write is judged against —
    # migration 0054 is what lets this row exist at all.
    host_seat = _rows(
        "a",
        "SELECT role FROM space_remote_members WHERE space_id = ? "
        "AND instance_id = ? AND user_id = ?",
        (fan_space_id, e["instance_id"], e["user_id"]),
    )
    if not host_seat or host_seat[0][0] != "subscriber":
        raise SystemExit(
            f"gfs-invite-link: a's space_remote_members seat for e is "
            f"{host_seat!r}, expected role 'subscriber' — without that row the "
            "follower household is neither listable nor kickable.",
        )
    print("  e holds a read-only Follower seat on a's third space ✓")

    # …and it READS. The content stream reaches a follower household
    # exactly as it reaches a member: over the pair's relay transport,
    # decryptable with the content key the ACK's space_meta carried.
    fan_content = f"A post only a follower should read — {time.time_ns()}"
    s, fan_post = _request(
        f"{a_base}/api/spaces/{fan_space_id}/posts",
        token=a["token"],
        method="POST",
        body={"type": "text", "content": fan_content},
    )
    fan_post = _must("a posts in the follower space", s, fan_post, ok=(201,))
    fan_seen = _await_space_post(state, "e", fan_space_id, fan_post["id"])
    if fan_seen.get("content") != fan_content:
        raise SystemExit(
            f"gfs-invite-link: the follower decrypted {fan_seen.get('content')!r}, "
            f"expected {fan_content!r} — a Follower seat receives the content "
            "stream and the content key like any member.",
        )
    print("  the follower received a's post over the relay, decrypted ✓")

    # …and it cannot WRITE. Refused on e's own household by the ordinary
    # subscriber gate; the host refuses it a second time regardless (step
    # 12 of the §24.11 pipeline reads the seat asserted above), which is
    # the half a patched household could not talk its way past.
    s, refused = _request(
        f"{e_base}/api/spaces/{fan_space_id}/posts",
        token=e["token"],
        method="POST",
        body={"type": "text", "content": "followers do not get to post"},
    )
    if s != 403:
        raise SystemExit(
            f"gfs-invite-link: the follower's post answered HTTP {s} "
            f"(expected 403): {refused!r}",
        )
    print("  the follower is refused a post (403) ✓")

    # 11. Moderators (migration 0080): a mints a ``moderator`` link on a
    #     FOURTH space. Unlike an admin link it seats straight through — a
    #     moderator holds no settings authority and never the seed — on the
    #     link-joined path (e, published link over the relay) and on the
    #     paired path (b, an ``internal`` link redeemed directly).
    mod_space_name = f"Link-moderator space {time.time_ns()}"
    s, mod_space = _request(
        f"{a_base}/api/spaces",
        token=a["token"],
        method="POST",
        body={
            "name": mod_space_name,
            "description": "fourth space — the moderator-link seats",
            "space_type": "global",
            # #874: the owner picks the GFSs a global space is listed on.
            "gfs_ids": _active_gfs_ids(state, "a"),
            "join_mode": "invite_only",
        },
    )
    mod_space = _must("a creates the moderator-link space", s, mod_space, ok=(201,))
    mod_space_id = mod_space["id"]
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        s, payload = _request(f"http://127.0.0.1:{GFS_PORT}/gfs/spaces")
        rows = payload.get("spaces", []) if isinstance(payload, dict) else []
        if any(sp.get("space_id") == mod_space_id for sp in rows):
            break
        time.sleep(1.0)
    else:
        raise SystemExit(
            f"gfs-invite-link: the moderator-link space {mod_space_id} never "
            "appeared in the GFS directory — a link cannot be published for "
            "an unlisted space.",
        )
    s, mod_link = _request(
        f"{a_base}/api/spaces/{mod_space_id}/invite-tokens",
        token=a["token"],
        method="POST",
        body={"role": "moderator", "uses": 1, "publish_to_gfs": gfs_conn_id},
    )
    mod_link = _must("a mints a moderator link", s, mod_link, ok=(201,))
    if mod_link.get("role") != "moderator":
        raise SystemExit(
            f"gfs-invite-link: a minted a {mod_link.get('role')!r} link, "
            "expected 'moderator'",
        )
    s, mod_joined = _request(
        f"{e_base}/api/spaces/join",
        token=e["token"],
        method="POST",
        body=_join_body_from_blob(_decode_invite_blob(mod_link["code"])),
        timeout=40.0,
    )
    mod_joined = _must("e redeems the moderator link", s, mod_joined, ok=(200, 201))
    if mod_joined.get("role") != "moderator" or mod_joined.get("pending_role"):
        raise SystemExit(
            f"gfs-invite-link: e's moderator redeem returned {mod_joined!r}, "
            "expected role 'moderator' with nothing pending — a moderator link "
            "seats straight through.",
        )
    _assert_moderator_seat("e", e, mod_space_id)
    print("  e redeemed a published moderator link into a MODERATOR seat ✓")

    b = state["instances"]["b"]
    s, b_link = _request(
        f"{a_base}/api/spaces/{mod_space_id}/invite-tokens",
        token=a["token"],
        method="POST",
        body={"role": "moderator", "uses": 1, "via": "internal"},
    )
    b_link = _must("a mints an internal moderator link", s, b_link, ok=(201,))
    s, b_joined = _request(
        f"http://127.0.0.1:{b['port']}/api/spaces/join",
        token=b["token"],
        method="POST",
        body=_join_body_from_blob(_decode_invite_blob(b_link["code"])),
        timeout=40.0,
    )
    b_joined = _must("b redeems the internal moderator link", s, b_joined)
    if b_joined.get("role") != "moderator":
        raise SystemExit(
            f"gfs-invite-link: b's internal moderator redeem returned "
            f"{b_joined!r}, expected role 'moderator'",
        )
    _assert_moderator_seat("b", b, mod_space_id)
    print("  b (paired) redeemed an internal moderator link into a MODERATOR seat ✓")

    state["gfs_invite_admin_space_id"] = admin_space_id
    state["gfs_invite_follower_space_id"] = fan_space_id
    state["gfs_invite_moderator_space_id"] = mod_space_id
    _save(state)
    print(
        "gfs-invite-link: ok (published link → stranger joins over the relay → "
        "revoke → admin link seats a member + pending elevation → owner "
        "approves → admin without the seed → follower seat that reads but "
        "cannot write → moderator links seat moderators, link-joined and "
        "paired)"
    )


def _assert_moderator_seat(label: str, inst: dict, space_id: str) -> None:
    """``label`` holds a ``moderator`` seat in a's ``space_id``: its own
    ``space_members`` row, and a's ``space_remote_members`` row — the seat
    every content-authority check on the host reads."""
    own = _rows(
        label,
        "SELECT role FROM space_members WHERE space_id = ? AND user_id = ?",
        (space_id, inst["user_id"]),
    )
    if not own or own[0][0] != "moderator":
        raise SystemExit(
            f"gfs-invite-link: {label}'s space_members row for the moderator "
            f"space is {own!r}, expected role 'moderator'",
        )
    host = _rows(
        "a",
        "SELECT role FROM space_remote_members WHERE space_id = ? "
        "AND instance_id = ? AND user_id = ? AND tombstoned = 0",
        (space_id, inst["instance_id"], inst["user_id"]),
    )
    if not host or host[0][0] != "moderator":
        raise SystemExit(
            f"gfs-invite-link: a's space_remote_members seat for {label} is "
            f"{host!r}, expected role 'moderator'",
        )


# ─── Step: gfs-invite-link-content ─────────────────────────────────────────


def cmd_gfs_invite_link_content() -> None:
    """A link-joined household is a full member on the wire, both ways.

    Prereqs: ``gfs-invite-link`` (e holds a ``space_session`` seat in a's
    global space and nothing else).

    Seating the pair is only half the job: e and a hold matching
    directional session keys but **no address for each other**, by
    design, and no mesh path either. So every §24.11 envelope between
    them is sealed a second time to the peer's key-wrap key and handed to
    the same ``POST /gfs/envelope`` relay the redeem used
    (``GfsRelayTransport``, selected in ``federation/transport.py`` for
    ``source = space_session``). This step is the live proof that the
    ordinary space vocabulary flows over it.

    Sequence:
    1. a posts in the space; e must see it DECRYPTED in
       ``GET /api/spaces/{id}/feed`` and hold the ``space_posts`` row.
       That single assertion covers the seal, the relay, the §24.11
       pipeline under the wider relay timestamp window, the pair key and
       the content key e got in the ACK's ``space_meta``.
    2. The GFS moved envelopes addressed to e while that happened — the
       delivery really was relayed and not some other transport that
       quietly worked.
    3. e posts; a must see it. The reply leg is the one that would break
       if only the host knew how to reach the other side.
    4. a rotates the per-space content key (removing a member, as
       ``gfs-space-rotate`` does) and posts again. e must decrypt the
       NEW epoch — the re-key has to reach a link-joined member like any
       other, or e goes dark at the first roster change.
    5. ``GET /api/connections`` on e labels a ``source=space_session``
       with transport ``gfs_relay``. Anything else (``https`` in
       particular) points an operator at an inbox URL that will never
       exist.
    """
    state = _load()
    if not state or not _gfs_alive(state):
        raise SystemExit("run 'gfs-invite-link' first")
    space_id = state.get("gfs_invite_space_id")
    if not space_id:
        raise SystemExit("run 'gfs-invite-link' first")
    a = state["instances"]["a"]
    e = state["instances"]["e"]
    a_base = f"http://127.0.0.1:{a['port']}"
    e_base = f"http://127.0.0.1:{e['port']}"

    gfs_off = _gfs_log_size()

    # 1. Host → link-joined member.
    content = f"Post to a household that joined from a link — {time.time_ns()}"
    s, post = _request(
        f"{a_base}/api/spaces/{space_id}/posts",
        token=a["token"],
        method="POST",
        body={"type": "text", "content": content},
    )
    post = _must("a posts in the link-joined space", s, post, ok=(201,))
    post_id = post["id"]
    seen = _await_space_post(state, "e", space_id, post_id)
    if seen.get("content") != content:
        raise SystemExit(
            f"gfs-invite-link-content: e decrypted {seen.get('content')!r}, "
            f"expected {content!r}",
        )
    print("  e received a's post over the relay, decrypted ✓")

    # 1b. …and it is PERSISTED, not merely rendered once. Read the row
    #     rather than a second endpoint: ``/api/spaces/{id}/posts`` is
    #     POST-only (``SpacePostCollectionView``), ``/feed`` above is the
    #     read surface, and the row is the stronger claim anyway.
    if not _rows("e", "SELECT id FROM space_posts WHERE id = ?", (post_id,)):
        raise SystemExit(
            f"gfs-invite-link-content: e's feed showed {post_id} but no "
            "space_posts row survives — the relayed envelope was rendered "
            "without being stored.",
        )

    # 2. It really went through the relay: the GFS carried envelopes
    #    addressed to e in that window. (The blob is opaque, so this is
    #    the honest form of the claim — a count and a recipient.)
    to_e = [
        line
        for line in _gfs_log_lines_matching("gfs.envelope:", offset=gfs_off)
        if e["instance_id"] in line
    ]
    if not to_e:
        raise SystemExit(
            "gfs-invite-link-content: the GFS relayed nothing to e while a's "
            "post was delivered — it arrived by some transport a link-joined "
            f"peer should not have. Check {GFS_DIR / 'log.txt'}.",
        )
    print(f"  the GFS carried {len(to_e)} envelope(s) addressed to e ✓")

    # 3. Link-joined member → host. The reply leg.
    reply = f"Reply from the link-joined household — {time.time_ns()}"
    s, e_post = _request(
        f"{e_base}/api/spaces/{space_id}/posts",
        token=e["token"],
        method="POST",
        body={"type": "text", "content": reply},
    )
    e_post = _must("e posts in the space", s, e_post, ok=(201,))
    a_seen = _await_space_post(state, "a", space_id, e_post["id"])
    if a_seen.get("content") != reply:
        raise SystemExit(
            f"gfs-invite-link-content: a decrypted {a_seen.get('content')!r}, "
            f"expected {reply!r}",
        )
    print("  a received e's reply over the relay ✓")

    # 4. An epoch rotation must re-key a link-joined member too.
    victim_id, _victim_token = _seat_local_member(
        state,
        "a",
        space_id,
        username="gina",
        password="gina-pw-demo",
        display_name="Gina",
    )
    s, removed = _request(
        f"{a_base}/api/spaces/{space_id}/members/{victim_id}",
        token=a["token"],
        method="DELETE",
    )
    _must("a: remove gina", s, removed, ok=(200,))
    print("  a removed gina → content-key epoch rotation")
    # Settle: rotate → re-seal to every member household, e's share of
    # which rides the relay like everything else.
    time.sleep(10)
    rotated = f"Post under the rotated key — {time.time_ns()}"
    s, post2 = _request(
        f"{a_base}/api/spaces/{space_id}/posts",
        token=a["token"],
        method="POST",
        body={"type": "text", "content": rotated},
    )
    post2 = _must("a posts after the rotation", s, post2, ok=(201,))
    seen2 = _await_space_post(state, "e", space_id, post2["id"])
    if seen2.get("content") != rotated:
        raise SystemExit(
            f"gfs-invite-link-content: e decrypted {seen2.get('content')!r} "
            f"after the rotation, expected {rotated!r} — the re-key never "
            "reached a link-joined member.",
        )
    print("  e decrypts the post-rotation post — the re-key rode the relay ✓")

    # 5. The operator-facing label.
    s, conns = _request(f"{e_base}/api/connections", token=e["token"])
    _must("e: GET /api/connections", s, conns)
    row = next((c for c in conns if c.get("instance_id") == a["instance_id"]), None)
    if row is None:
        raise SystemExit(
            f"gfs-invite-link-content: e's /api/connections does not list a — "
            f"got {conns!r}",
        )
    if row.get("source") != "space_session":
        raise SystemExit(
            f"gfs-invite-link-content: e labels a source={row.get('source')!r}, "
            "expected 'space_session'",
        )
    if row.get("transport") != "gfs_relay":
        raise SystemExit(
            f"gfs-invite-link-content: e labels a transport="
            f"{row.get('transport')!r}, expected 'gfs_relay'. 'https' in "
            "particular is a lie — there is no inbox URL to fall back to.",
        )
    print("  e's /api/connections labels a: space_session over gfs_relay ✓")

    # 6. A cover change AFTER the join reaches the link-joined member. It
    #    used to ride SPACE_CONFIG_CHANGED as a cover_hash only, so e kept
    #    the rendition from its join forever. A ~350 KiB cover is over the
    #    relay's bootstrap bound, so e must get the smaller relay variant.
    s, new_cover = _upload_file(
        f"{a_base}/api/spaces/{space_id}/cover",
        token=a["token"],
        filename="cover2.png",
        content=_make_noisy_png(1000, 700),
        content_type="image/png",
        timeout=60.0,
    )
    new_cover = _must("a changes the space cover", s, new_cover)
    new_hash = new_cover["cover_hash"]
    deadline = time.time() + 30
    e_cover: list = []
    while time.time() < deadline:
        e_cover = _rows(
            "e",
            "SELECT hash, bytes_webp FROM space_covers WHERE space_id = ?",
            (space_id,),
        )
        if e_cover and e_cover[0][0] == new_hash:
            break
        time.sleep(0.5)
    else:
        raise SystemExit(
            f"gfs-invite-link-content: e still holds cover "
            f"{(e_cover[0][0] if e_cover else None)!r} 30 s after a changed it "
            f"to {new_hash!r} — the cover change never reached the "
            "link-joined member.",
        )
    e_bytes = bytes(e_cover[0][1])
    if e_bytes[8:12] != b"WEBP" or len(e_bytes) > 64 * 1024:
        raise SystemExit(
            f"gfs-invite-link-content: e's new cover is {len(e_bytes)} bytes "
            "(or not a WebP) — expected the relay-bounded (≤ 64 KiB) rendition.",
        )
    s, served = _http_get_bytes(
        f"{e_base}/api/spaces/{space_id}/cover?v={new_hash}",
        token=e["token"],
    )
    if s != 200 or served != e_bytes:
        raise SystemExit(
            f"gfs-invite-link-content: e's GET /cover answered HTTP {s} with "
            f"{len(served or b'')} bytes, not the {len(e_bytes)}-byte new cover.",
        )
    print(
        f"  a's cover change reached e over the relay "
        f"({len(e_bytes) // 1024} KiB rendition, served by e's /cover) ✓"
    )

    state["gfs_invite_post_id"] = post_id
    state["gfs_invite_post_content"] = content
    state["gfs_invite_rotated_post_id"] = post2["id"]
    _save(state)
    print(
        "gfs-invite-link-content: ok (space content both ways over the relay, "
        "survives a key rotation)"
    )


@_offline_step
def cmd_gfs_member_publish() -> None:
    """v_49 trusted-mode member publish: a link-joined member posts while
    the HOST is offline, and the space's followers still get it live.

    Prereqs: ``gfs-space-subscribe`` (d follows a's global space over the
    GFS) and ``gfs-invite-link`` (+ ``-content``) — e holds a link-joined
    member seat in that space and is paired with nobody but the GFS.

    Sequence:
    1. Stop **a** — the host, the only seed holder. Before v_49 nothing e
       posted could reach a follower now: only a seed holder could put an
       item on the relay.
    2. e posts and publishes the post ITSELF — ``POST /gfs/member-publish``,
       signed by e's household key, authorized by e's writer cert.
    3. d (a follower that is NOT a member) receives the post decrypted
       within the poll window. The GFS log shows the member-publish fan-out
       for the space, and no line carries the post text.
    4. Restart a. a receives e's post over the normal member path (the
       relay envelope queue) and sends NO copy to the GFS (step 12): e is
       v_57, the GFS proved member publish and ``space_item_followers``,
       and every follower there reads ``space_item``.
    5. d holds exactly ONE copy of e's post.

    Between 3 and 4 (PR 3, host still offline): e comments on and reacts to
    its post, posts a second post, edits it and deletes it; d (follower)
    reflects each step from the member relay alone, and the GFS log carries
    none of the text. After 5: a, back and caught up, holds the second post
    deleted, and its relayed copy of the create never resurrects it on d.

    Polls every 3 s (the per-user 60/min bucket on ``/api/spaces/*``).
    """
    state = _load()
    if not state or not _gfs_alive(state):
        raise SystemExit("run the gfs chain through 'gfs-invite-link-content' first")
    space_id = (state.get("gfs") or {}).get("global_space_id")
    if not space_id or not state.get("gfs_invite_space_id"):
        raise SystemExit("run 'gfs-space-subscribe' and 'gfs-invite-link' first")
    if state.get("gfs_invite_space_id") != space_id:
        raise SystemExit(
            "gfs-member-publish: the invite-link space is not the global space "
            "d follows — run 'gfs-invite-link' against gfs.global_space_id"
        )
    a = state["instances"]["a"]
    d = state["instances"]["d"]
    e = state["instances"]["e"]
    e_base = f"http://127.0.0.1:{e['port']}"

    # 1. Host offline.
    _kill_household("a", a)
    gfs_off = _gfs_log_size()

    # 2. The link-joined member posts.
    content = f"Posted while the host is offline — {time.time_ns()}"
    s, post = _request(
        f"{e_base}/api/spaces/{space_id}/posts",
        token=e["token"],
        method="POST",
        body={"type": "text", "content": content},
    )
    post = _must("e posts with the host offline", s, post, ok=(201,))
    post_id = post["id"]

    # 3. The follower gets it live, over the GFS, from e itself.
    deadline = time.monotonic() + 60.0
    seen = None
    while time.monotonic() < deadline and seen is None:
        time.sleep(3.0)
        st, body = _request(
            f"http://127.0.0.1:{d['port']}/api/spaces/{space_id}/feed",
            token=d["token"],
        )
        if st == 429:
            time.sleep(10.0)
            continue
        if st != 200:
            continue
        feed = body if isinstance(body, list) else (body.get("posts") or [])
        seen = next((p for p in feed if p.get("id") == post_id), None)
    if seen is None:
        raise SystemExit(
            f"gfs-member-publish: d never saw e's post {post_id} within 60 s "
            f"while a was down. Check {_instance_dir('e') / 'log.txt'} for "
            "'gfs.member_publish' and the GFS log for the member-publish "
            "route's DEBUG refusal reason.",
        )
    if seen.get("content") != content:
        raise SystemExit(
            f"gfs-member-publish: d decrypted {seen.get('content')!r}, "
            f"expected {content!r}",
        )
    print("  d (follower) received e's post with the host offline ✓")
    fanned = _gfs_log_lines_matching(
        f"gfs.member_publish: space={space_id}", offset=gfs_off
    )
    if not fanned:
        raise SystemExit(
            "gfs-member-publish: the GFS logged no member-publish fan-out for "
            "the space — the post reached d some other way."
        )
    leaked = _gfs_log_lines_matching(content, offset=gfs_off)
    if leaked:
        raise SystemExit(
            f"gfs-member-publish: the GFS log carries the post text: {leaked[:1]!r}"
        )
    print(f"  the GFS fanned the member-published item out ({fanned[-1].strip()}) ✓")
    # Compressed with the preset dictionary (``payload_suite: "zlib1+dict1"``):
    # the GFS proved ``payload_zlib1_dict1`` and every seat of the space said
    # it reads it (seat labels 7).
    if not any("suite=zlib1+dict1" in line for line in fanned):
        raise SystemExit(
            "gfs-member-publish: the member-published item was not fanned out "
            f"compressed — {fanned[-1].strip()!r}. Every follower seat should "
            "carry payload_zlib1_dict1 (space_subscribers.reads_space_item & 4)."
        )
    print("  … compressed (payload_suite zlib1+dict1), and d decrypted it ✓")

    # 3b. PR 3 — comments, reactions and own edits / deletes, still with the
    #     host offline. d (a follower) reads them only from the member relay:
    #     no other path ever carried them to a follower.
    second_id, edited = _member_publish_items(state, space_id, post_id, gfs_off)

    # 4. The host comes back. Step 12: e is a v_57 household that published
    #    the post itself to every listed GFS, the GFS proved member publish
    #    and ``space_item_followers``, and every follower there (d) said it
    #    reads ``space_item`` — so a sends NO copy of e's post to that GFS
    #    (it used to relay it again for older followers). A respawn starts a
    #    fresh ``log.txt``, so a's whole new log is this step's evidence.
    a_off = 0
    new_pid = _spawn("a", a["port"])
    state["instances"]["a"]["pid"] = new_pid
    _save(state)
    _wait_ready(a["port"])
    print(f"  a respawned: pid={new_pid}")
    _await_space_post(state, "a", space_id, post_id)
    print("  a (host) caught e's post up over the member path ✓")
    deadline = time.monotonic() + 30.0
    skipped: list[str] = []
    while time.monotonic() < deadline and not skipped:
        time.sleep(3.0)
        skipped = [
            line
            for line in _log_lines_matching(
                "a", "reads the member's own item", offset=a_off
            )
            if space_id in line
        ]
    if not skipped:
        raise SystemExit(
            "gfs-member-publish: a did not skip its copy of e's post — every "
            "follower on the GFS reads e's own space_item, so the host's relay "
            "is a redundant fan-out (step 12)."
        )
    print("  a sent no copy of e's post: every follower reads e's own item ✓")
    time.sleep(3.0)  # let anything late land on d

    # 5. d received it twice and holds exactly one copy.
    rows = _rows("d", "SELECT id FROM space_posts WHERE id = ?", (post_id,))
    if len(rows) != 1:
        raise SystemExit(f"gfs-member-publish: d holds {len(rows)} copies of the post")
    print("  d holds exactly one copy (receiver dedupe by post id) ✓")

    # 6. e's deleted post stays deleted everywhere once a is back: a got the
    #    create, edit and delete over the member path in order and relayed
    #    the create to the GFS again — d's copy is a duplicate of a deleted
    #    row and is dropped, never resurrected.
    _assert_post_stays_deleted(state, space_id, second_id, edited)
    state["gfs_member_publish_post_id"] = post_id
    _save(state)

    # 7. Moderation outcomes reach the follower on the host's authority: a
    #    moderator's removal of e's post, and e's post approved from the
    #    review queue of the space switched to MODERATED.
    _moderation_outcomes_reach_followers(state, space_id)
    _old_follower_forces_json(state, space_id)
    print(
        "gfs-member-publish: ok (member posts, comments, reactions, edits and "
        "deletes reach followers without the host; moderator removals and "
        "approved posts reach them on the host's authority; compressed where "
        "every follower reads it, JSON otherwise)"
    )


def _old_follower_forces_json(state: dict, space_id: str) -> None:
    """A follower seat without ``payload_zlib1`` (an older household — staged
    by clearing the bit in the GFS DB, since the demo boots only this build)
    makes the GFS refuse a compressed item (409). e notes it, publishes the
    JSON item at once, and d still gets the post. The bit is restored after.
    """
    d = state["instances"]["d"]
    seat = (space_id, d["instance_id"])
    rows = _gfs_rows(
        "SELECT reads_space_item FROM space_subscribers"
        " WHERE space_id=? AND instance_id=?",
        seat,
    )
    if not rows or rows[0][0] != 7:
        raise SystemExit(
            f"gfs-member-publish: d's seat labels are {rows!r}, expected 7 "
            "(space_item + payload_zlib1 + payload_zlib1_dict1)"
        )
    _gfs_write(
        "UPDATE space_subscribers SET reads_space_item=1"
        " WHERE space_id=? AND instance_id=?",
        seat,
    )
    try:
        gfs_off = _gfs_log_size()
        e_off = _log_size("e")
        content = f"Posted while an older follower is seated — {time.time_ns()}"
        st, post = _e_request(
            state,
            "POST",
            f"/api/spaces/{space_id}/posts",
            {"type": "text", "content": content},
        )
        post_id = _must("e posts with an older follower seated", st, post, ok=(201,))[
            "id"
        ]
        _poll_rows(
            "d",
            "SELECT content FROM space_posts WHERE id = ?",
            (post_id,),
            lambda r: bool(r) and r[0][0] == content,
            what="d never got e's post while its seat lacked payload_zlib1",
        )
        refused = _log_lines_matching(
            "e", f"refused a compressed item for space {space_id}", offset=e_off
        )
        if not refused:
            raise SystemExit(
                "gfs-member-publish: e never logged the GFS's payload-suite "
                "refusal — did the compressed item reach an older follower?"
            )
        fanned = _gfs_log_lines_matching(
            f"gfs.member_publish: space={space_id}", offset=gfs_off
        )
        if not fanned or any("suite=zlib1" in line for line in fanned):
            raise SystemExit(
                f"gfs-member-publish: expected only JSON fan-outs, got {fanned!r}"
            )
        print(
            "  an older follower's seat: the GFS refused the compressed item, "
            "e sent JSON, d got the post ✓"
        )
    finally:
        _gfs_write(
            "UPDATE space_subscribers SET reads_space_item=7"
            " WHERE space_id=? AND instance_id=?",
            seat,
        )


def _poll_rows(
    label: str,
    sql: str,
    params: tuple,
    ok,
    *,
    what: str,
    timeout: float = 60.0,
) -> list[tuple]:
    """Poll ``label``'s DB every 3 s until ``ok(rows)``; SystemExit naming
    ``what`` otherwise. (DB reads — no REST rate-limit bucket involved.)"""
    deadline = time.monotonic() + timeout
    rows: list[tuple] = []
    while time.monotonic() < deadline:
        rows = _rows(label, sql, params)
        if ok(rows):
            return rows
        time.sleep(3.0)
    raise SystemExit(
        f"gfs-member-publish: {what} within {timeout:.0f} s (last rows {rows!r}). "
        f"Check {_instance_dir(label) / 'log.txt'} for 'space_item' and "
        f"{_instance_dir('e') / 'log.txt'} for 'gfs.member_publish'."
    )


def _e_request(state: dict, method: str, path: str, body: dict | None = None):
    """One REST call as e's user, backing off on the 60/min bucket."""
    e = state["instances"]["e"]
    for _attempt in range(4):
        st, resp = _request(
            f"http://127.0.0.1:{e['port']}{path}",
            token=e["token"],
            method=method,
            body=body,
        )
        if st != 429:
            return st, resp
        time.sleep(15.0)
    return st, resp


def _member_publish_items(
    state: dict, space_id: str, post_id: str, gfs_off: int
) -> tuple[str, str]:
    """While host a is offline: e comments on and reacts to its post, posts
    a second one, edits it, then deletes it. d (follower) reflects each
    step from the member relay alone. Returns ``(second post id, edited
    text)``."""
    base = f"/api/spaces/{space_id}/posts"
    comment_text = f"A comment with the host offline — {time.time_ns()}"
    st, comment = _e_request(
        state, "POST", f"{base}/{post_id}/comments", {"content": comment_text}
    )
    comment = _must("e comments with the host offline", st, comment, ok=(200, 201))
    comment_id = comment["id"]
    st, body = _e_request(state, "POST", f"{base}/{post_id}/reactions", {"emoji": "🎉"})
    _must("e reacts with the host offline", st, body, ok=(200, 201))
    _poll_rows(
        "d",
        "SELECT content FROM space_post_comments WHERE id = ? AND deleted = 0",
        (comment_id,),
        lambda rows: bool(rows) and rows[0][0] == comment_text,
        what=f"d never received e's comment {comment_id}",
    )
    print("  d (follower) received e's comment over the member relay ✓")
    _poll_rows(
        "d",
        "SELECT reactions FROM space_posts WHERE id = ?",
        (post_id,),
        lambda rows: bool(rows) and "🎉" in (rows[0][0] or ""),
        what="d never received e's reaction",
    )
    print("  d (follower) received e's reaction over the member relay ✓")

    original = f"A post to edit and delete — {time.time_ns()}"
    st, second = _e_request(state, "POST", base, {"type": "text", "content": original})
    second_id = _must("e posts a second post", st, second, ok=(201,))["id"]
    _poll_rows(
        "d",
        "SELECT content FROM space_posts WHERE id = ?",
        (second_id,),
        lambda rows: bool(rows) and rows[0][0] == original,
        what=f"d never received e's second post {second_id}",
    )
    edited = f"Edited with the host offline — {time.time_ns()}"
    st, body = _e_request(state, "PATCH", f"{base}/{second_id}", {"content": edited})
    _must("e edits its post", st, body)
    _poll_rows(
        "d",
        "SELECT content, edited_at FROM space_posts WHERE id = ?",
        (second_id,),
        lambda rows: bool(rows) and rows[0][0] == edited and rows[0][1],
        what="d never reflected e's edit",
    )
    print("  d (follower) reflected e's post edit ✓")
    st, body = _e_request(state, "DELETE", f"{base}/{second_id}")
    _must("e deletes its post", st, body)
    _poll_rows(
        "d",
        "SELECT deleted, content FROM space_posts WHERE id = ?",
        (second_id,),
        lambda rows: bool(rows) and rows[0][0] == 1 and rows[0][1] is None,
        what="d never reflected e's delete",
    )
    print("  d (follower) reflected e's post delete ✓")
    for text in (comment_text, edited, original):
        leaked = _gfs_log_lines_matching(text, offset=gfs_off)
        if leaked:
            raise SystemExit(
                f"gfs-member-publish: the GFS log carries item text: {leaked[:1]!r}"
            )
    print("  the GFS logged none of the items' text ✓")
    return second_id, edited


def _assert_post_stays_deleted(
    state: dict, space_id: str, post_id: str, edited: str
) -> None:
    """After a's restart and catch-up: a holds e's second post deleted
    (never live), and so does d."""
    _poll_rows(
        "a",
        "SELECT deleted FROM space_posts WHERE id = ?",
        (post_id,),
        lambda rows: bool(rows) and rows[0][0] == 1,
        what=f"a (host) never caught up e's delete of {post_id}",
    )
    time.sleep(6.0)  # let a's relayed copy of the create reach d
    for label in ("a", "d"):
        rows = _rows(
            label,
            "SELECT deleted, content FROM space_posts WHERE id = ?",
            (post_id,),
        )
        if not rows or rows[0][0] != 1 or rows[0][1] is not None:
            raise SystemExit(
                f"gfs-member-publish: {label} resurrected e's deleted post "
                f"{post_id}: {rows!r}"
            )
    print("  a and d keep e's deleted post deleted after a came back ✓")


def _inst_request(
    state: dict, label: str, method: str, path: str, body: dict | None = None
):
    """One REST call as ``label``'s admin, backing off on the 60/min bucket."""
    inst = state["instances"][label]
    for _attempt in range(4):
        st, resp = _request(
            f"http://127.0.0.1:{inst['port']}{path}",
            token=inst["token"],
            method=method,
            body=body,
        )
        if st != 429:
            return st, resp
        time.sleep(15.0)
    return st, resp


def _set_posts_access(state: dict, space_id: str, level: str) -> None:
    """The owner a sets the space's posts level; waits until e sees it."""
    st, body = _inst_request(
        state,
        "a",
        "PATCH",
        f"/api/spaces/{space_id}",
        {"features": {"posts_access": level}, "force": True},
    )
    _must(f"a: posts_access={level}", st, body)
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        st, sp = _e_request(state, "GET", f"/api/spaces/{space_id}")
        if (
            st == 200
            and ((sp or {}).get("features") or {}).get("posts_access") == level
        ):
            return
        time.sleep(3.0)
    raise SystemExit(
        f"gfs-member-publish: e never saw posts_access={level} within 60 s"
    )


def _moderation_outcomes_reach_followers(state: dict, space_id: str) -> None:
    """With a (host, seed holder) back: (1) e posts, d gets it; a removes
    it as a moderator and d — a follower with no seat, which no ``SPACE_*``
    event reaches — drops it, from a's authority-signed removal notice on
    the host relay. (2) a switches posts to MODERATED; e's post queues; a
    approves it; d receives it attributed to e — under the signature e made
    when it submitted the item, relayed and marked by a (e's cert is
    comment-scope now, so e could not publish it). The
    GFS log carries neither text nor id. (3) posts back to OPEN.
    DB polls every 3 s; REST calls back off on 429."""
    e = state["instances"]["e"]
    base = f"/api/spaces/{space_id}/posts"
    gfs_off = _gfs_log_size()

    # (1) A moderator's removal.
    doomed = f"A post a moderator removes — {time.time_ns()}"
    st, post = _e_request(state, "POST", base, {"type": "text", "content": doomed})
    doomed_id = _must("e posts a post to be moderated", st, post, ok=(201,))["id"]
    _poll_rows(
        "d",
        "SELECT content FROM space_posts WHERE id = ? AND deleted = 0",
        (doomed_id,),
        lambda rows: bool(rows) and rows[0][0] == doomed,
        what=f"d never received e's post {doomed_id}",
    )
    st, body = _inst_request(state, "a", "DELETE", f"{base}/{doomed_id}")
    _must("a removes e's post as a moderator", st, body)
    _poll_rows(
        "d",
        "SELECT deleted, content FROM space_posts WHERE id = ?",
        (doomed_id,),
        lambda rows: bool(rows) and rows[0][0] == 1 and rows[0][1] is None,
        what="d (follower) never dropped the post a moderator removed",
        timeout=90.0,
    )
    print("  d (follower) dropped the post a moderator removed ✓")

    # (2) An approved post in a MODERATED space.
    _set_posts_access(state, space_id, "moderated")
    print("  a switched the space's posts to Reviewed (e sees it)")
    reviewed = f"A post released from review — {time.time_ns()}"
    st, queued = _e_request(state, "POST", base, {"type": "text", "content": reviewed})
    if st != 202 or not (queued or {}).get("queued"):
        raise SystemExit(
            f"gfs-member-publish: e's post under Reviewed -> {st} {queued!r} "
            "(expected 202 queued)"
        )
    item_id = queued["item_id"]
    approved_id = queued.get("target_id")
    deadline = time.monotonic() + 60.0
    listed = False
    while time.monotonic() < deadline and not listed:
        time.sleep(3.0)
        st, items = _inst_request(
            state, "a", "GET", f"/api/spaces/{space_id}/moderation"
        )
        listed = st == 200 and any(i.get("id") == item_id for i in items)
    if not listed:
        raise SystemExit(
            f"gfs-member-publish: a's moderation queue never listed e's item {item_id}"
        )
    st, body = _inst_request(
        state, "a", "POST", f"/api/spaces/{space_id}/moderation/{item_id}/approve", {}
    )
    body = _must("a approves e's post", st, body)
    approved_id = body.get("target_id") or approved_id
    print(f"  a approved e's queued post → id={approved_id}")
    rows = _poll_rows(
        "d",
        "SELECT content, author, deleted FROM space_posts WHERE id = ?",
        (approved_id,),
        lambda rows: bool(rows) and rows[0][0] == reviewed and rows[0][2] == 0,
        what=f"d (follower) never received the approved post {approved_id}",
        timeout=90.0,
    )
    if rows[0][1] != e["user_id"]:
        raise SystemExit(
            f"gfs-member-publish: d attributes the approved post to {rows[0][1]!r}, "
            f"expected e's user {e['user_id']!r}"
        )
    print("  d (follower) received the approved post, attributed to e ✓")
    for needle in (doomed, reviewed, doomed_id, approved_id, item_id):
        leaked = _gfs_log_lines_matching(needle, offset=gfs_off)
        if leaked:
            raise SystemExit(
                f"gfs-member-publish: the GFS log carries {needle!r}: {leaked[:1]!r}"
            )
    print("  the GFS logged none of the moderated items' text or ids ✓")

    # (3) Back to OPEN for the steps that follow.
    _set_posts_access(state, space_id, "open")
    print("  a switched the space's posts back to open")


@_offline_step
def cmd_gfs_member_publish_strict() -> None:
    """v_50 strict (anonymous) member publish: the owner switches the space
    to strict, and a link-joined member's post reaches a follower over the
    GFS while the host is offline — with nothing identifying on the wire.

    Prereqs: ``gfs-member-publish`` (and everything it needs) — a is back
    up, e holds a writer seat in a's global space, d follows it.

    Sequence:
    1. a (the owner) PATCHes ``features.gfs_publish_mode = "strict"``. That
       rotates the content key: e gets the new epoch's writer GROUP key with
       its cert (over the relay — e is paired with nobody), and the GFS gets
       the owner's notice with ``publish_mode`` + the ``writer_key_cert``.
    2. Stop **a** (host) and **d** (so the GFS has to STORE the item for d).
    3. e posts. Its publisher signs ``POST /gfs/member-publish-anon`` with
       the writer key, over its cookie-less session.
    4. The GFS access log shows the anonymous route, no identified
       ``/gfs/member-publish`` for it, and no GFS log line about the publish
       names e. The row queued for d carries no ``writer_cert`` and nothing
       of e's (instance id, identity key).
    5. Restart d: it drains the queue and decrypts e's post (the cert came
       from inside the ciphertext).
    6. An IDENTIFIED publish into the strict space — signed with e's real
       household key and e's real writer cert, exactly what a v_49 or
       misconfigured household would send — is refused (403).
    7. Restart a, switch the space back to trusted (the chain's later steps
       expect it), and record the strict epoch for ``verify``'s v_50 check.

    Polls every 3 s; backs off on 429. GFS / household logs are bookmarked
    before each respawn.
    """
    import base64 as _b64
    import json as _json
    from dataclasses import replace as _replace
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    from socialhome.crypto import b64url_encode, sign_ed25519
    from socialhome.domain.gfs_member_publish import MemberPublishRequest
    from socialhome.domain.writer_cert import WriterCert
    from socialhome.infrastructure.key_manager import KeyManager

    state = _load()
    if not state or not _gfs_alive(state):
        raise SystemExit("run the gfs chain through 'gfs-member-publish' first")
    space_id = (state.get("gfs") or {}).get("global_space_id")
    if not space_id or state.get("gfs_invite_space_id") != space_id:
        raise SystemExit("run 'gfs-space-subscribe' and 'gfs-invite-link' first")
    if not state.get("gfs_member_publish_post_id"):
        raise SystemExit("run 'gfs-member-publish' first")
    a = state["instances"]["a"]
    d = state["instances"]["d"]
    e = state["instances"]["e"]
    a_base = f"http://127.0.0.1:{a['port']}"
    gfs_base = state["gfs"]["base_url"]
    e_iid = e["instance_id"]
    e_pk_hex = _rows("e", "SELECT identity_public_key FROM instance_identity")[0][0]
    e_leaks = (e_iid, e_pk_hex, b64url_encode(bytes.fromhex(e_pk_hex)))

    def _patch_mode(mode: str) -> None:
        for _attempt in range(4):
            st, body = _request(
                f"{a_base}/api/spaces/{space_id}",
                token=a["token"],
                method="PATCH",
                body={"features": {"gfs_publish_mode": mode}},
            )
            if st != 429:
                break
            time.sleep(15.0)
        _must(f"a sets gfs_publish_mode={mode}", st, body)

    # 1. The owner goes strict; wait for the GFS pin and e's writer key.
    _patch_mode("strict")
    print("  a switched the space to strict (owner-only setting) ✓")
    deadline = time.monotonic() + 90.0
    epoch = None
    gfs_row: list = []
    e_key: list = []
    while time.monotonic() < deadline:
        latest = _rows(
            "a", "SELECT MAX(epoch) FROM space_keys WHERE space_id=?", (space_id,)
        )
        epoch = latest[0][0] if latest else None
        gfs_row = _gfs_rows(
            "SELECT member_publish_mode, writer_key_epoch FROM global_spaces"
            " WHERE space_id=?",
            (space_id,),
        )
        e_key = _rows(
            "e",
            "SELECT epoch FROM space_keys WHERE space_id=? AND epoch=?"
            " AND writer_key IS NOT NULL",
            (space_id, epoch),
        )
        if gfs_row and gfs_row[0] == ("strict", epoch) and e_key:
            break
        time.sleep(3.0)
    else:
        raise SystemExit(
            f"gfs-member-publish-strict: after the switch, GFS row={gfs_row!r} "
            f"(want ('strict', {epoch})) and e's writer key for epoch {epoch} "
            f"present={bool(e_key)} within 90 s. Check "
            f"{_instance_dir('a') / 'log.txt'} for 'GFS epoch notice' and "
            f"{_instance_dir('e') / 'log.txt'} for 'writer key'."
        )
    print(
        f"  GFS: mode=strict, writer key pinned for epoch {epoch}; e holds the "
        "writer key for it ✓"
    )

    # 2. Host and follower offline (d must get the item from the GFS queue).
    _kill_household("a", a)
    _kill_household("d", d)
    gfs_off = _gfs_log_size()

    # 3. e posts.
    content = f"Strict: posted anonymously with the host offline — {time.time_ns()}"
    st, post = _e_request(
        state,
        "POST",
        f"/api/spaces/{space_id}/posts",
        {"type": "text", "content": content},
    )
    post_id = _must("e posts in the strict space", st, post, ok=(201,))["id"]

    # 4. What the GFS saw and stored.
    deadline = time.monotonic() + 60.0
    queued: list = []
    while time.monotonic() < deadline:
        queued = []
        for r in _gfs_rows(
            "SELECT sealed_json FROM gfs_envelope_queue"
            " WHERE frame_type='relay' AND to_instance=?",
            (d["instance_id"],),
        ):
            frame = _json.loads(r[0])
            if frame.get("space_id") == space_id and frame.get("epoch") == epoch:
                queued.append(r)
        if queued:
            break
        time.sleep(3.0)
    anon_lines = [
        line
        for line in _gfs_log_lines_matching("/gfs/member-publish-anon", offset=gfs_off)
        if "POST" in line
    ]
    if not anon_lines or not queued:
        raise SystemExit(
            f"gfs-member-publish-strict: no anonymous publish reached the GFS "
            f"(access lines={anon_lines[-1:]!r}, rows queued for d={len(queued)}). "
            f"Check {_instance_dir('e') / 'log.txt'} for 'gfs.member_publish'."
        )
    print(
        f"  the GFS took e's item on the anonymous route ({anon_lines[-1].strip()}) ✓"
    )
    identified = [
        line
        for line in _gfs_log_lines_matching(
            "POST /gfs/member-publish HTTP", offset=gfs_off
        )
    ]
    if identified:
        raise SystemExit(
            f"gfs-member-publish-strict: an IDENTIFIED member publish reached the "
            f"GFS in strict mode: {identified[:1]!r}"
        )
    about = [
        line
        for line in _gfs_log_lines_matching("", offset=gfs_off)
        if ("member_publish" in line or "member-publish" in line)
        and any(x in line for x in e_leaks)
    ]
    if about:
        raise SystemExit(
            f"gfs-member-publish-strict: a GFS log line about the publish names "
            f"e: {about[:1]!r}"
        )
    for (raw,) in queued:
        frame = _json.loads(raw)
        if "writer_cert" in frame or any(x in raw for x in e_leaks):
            raise SystemExit(
                f"gfs-member-publish-strict: the row stored for d carries the "
                f"cert or e's identity: {sorted(frame)!r}"
            )
        if content in raw:
            raise SystemExit("gfs-member-publish-strict: the stored row is plaintext")
    print(
        "  no identified publish, no GFS line naming e, and the row stored for d "
        f"is {sorted(_json.loads(queued[0][0]))} — no cert, no household ✓"
    )

    # 5. d comes back, drains the queue and decrypts it.
    d_off = _log_size("d")
    new_d = _spawn("d", d["port"])
    state["instances"]["d"]["pid"] = new_d
    _save(state)
    _wait_ready(d["port"])
    print(f"  d respawned: pid={new_d} (log bookmark {d_off})")
    _poll_rows(
        "d",
        "SELECT content FROM space_posts WHERE id = ?",
        (post_id,),
        lambda rows: bool(rows) and rows[0][0] == content,
        what=f"d never decrypted e's strict-mode post {post_id}",
        timeout=90.0,
    )
    print("  d (follower) drained and decrypted e's anonymous post ✓")

    # 6. An identified publish into the strict space is refused — signed with
    #    e's real household key and real writer cert (what a v_49 or
    #    misconfigured household would send). The harness reads e's seed
    #    (KEK-unwrapped from e's own data dir) only to build the request.
    kek = KeyManager.from_data_dir(_instance_dir("e"))
    wrapped = _rows("e", "SELECT identity_private_key FROM instance_identity")[0][0]
    e_seed = kek.decrypt(wrapped)
    cert_row = _rows(
        "e",
        "SELECT writer_cert FROM space_keys WHERE space_id=? AND epoch=?",
        (space_id, epoch),
    )
    cert = WriterCert.from_wire(_json.loads(cert_row[0][0])).v1()
    st, info = _request(f"{gfs_base}/gfs/info")
    _must("GFS /gfs/info", st, info)
    req = MemberPublishRequest(
        instance_id=e_iid,
        gfs_instance_id=str(info["gfs_instance_id"]),
        ts=_dt.now(_tz.utc).isoformat(),
        signature="",
        target=space_id,
        epoch=int(epoch),
        writer_cert=cert,
        payload=_b64.urlsafe_b64encode(os.urandom(48)).decode().rstrip("="),
    )
    req = _replace(
        req, signature=b64url_encode(sign_ed25519(e_seed, req.signing_bytes()))
    )
    ref_off = _gfs_log_size()
    st, body = _request(
        f"{gfs_base}/gfs/member-publish", method="POST", body=req.to_wire()
    )
    if st != 403:
        raise SystemExit(
            f"gfs-member-publish-strict: an identified publish into the strict "
            f"space answered HTTP {st} {body!r}, expected 403"
        )
    why = _gfs_log_lines_matching("strict mode", offset=ref_off)
    print(
        "  an identified publish (e's real key + cert) into the strict space is "
        f"refused 403{' — ' + why[-1].split(':', 3)[-1].strip() if why else ''} ✓"
    )

    # 7. a back, space back to trusted, record for verify.
    a_off = _log_size("a")
    new_a = _spawn("a", a["port"])
    state["instances"]["a"]["pid"] = new_a
    _save(state)
    _wait_ready(a["port"])
    print(f"  a respawned: pid={new_a} (log bookmark {a_off})")
    _await_space_post(state, "a", space_id, post_id)
    print("  a (host) caught e's post up over the member path ✓")
    _patch_mode("trusted")
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        row = _gfs_rows(
            "SELECT member_publish_mode FROM global_spaces WHERE space_id=?",
            (space_id,),
        )
        if row and row[0][0] == "trusted":
            break
        time.sleep(3.0)
    else:
        raise SystemExit(
            "gfs-member-publish-strict: the GFS never learned the switch back to "
            "trusted within 60 s"
        )
    print("  a switched the space back to trusted; the GFS followed ✓")
    state["gfs_member_publish_strict_epoch"] = int(epoch)
    _save(state)
    print(
        "gfs-member-publish-strict: ok (anonymous member publish in a strict "
        "space; identified refused)"
    )


def _check_private_channel(state: dict, failures: list[str], cap) -> None:
    """v_51 tripwire for ``verify``: the private space's channel round-trips
    (a, b, e agree on the id; e holds a wrapped grant; the GFS holds the
    channel and never the space)."""
    space_id = state["gfs_private_channel_space_id"]
    channel_id = state.get("gfs_private_channel_id")
    if int(cap.MIN_FOR_PRIVATE_CHANNELS) != 51:
        failures.append("MIN_FOR_PRIVATE_CHANNELS moved off v_51")
    for label in ("a", "b", "e"):
        try:
            rows = _rows(
                label, "SELECT gfs_channel_id FROM spaces WHERE id = ?", (space_id,)
            )
        except Exception as exc:
            failures.append(f"{label}: private-channel read failed: {exc!r}")
            continue
        if not rows or rows[0][0] != channel_id:
            failures.append(
                f"{label}: private space {space_id} points at channel "
                f"{rows[0][0] if rows else None!r}, expected {channel_id!r}"
            )
    grants = _rows(
        "e",
        "SELECT gfs_channel FROM space_keys WHERE space_id = ? AND gfs_channel IS NOT NULL",
        (space_id,),
    )
    if not grants:
        failures.append("e: holds no channel grant for the private space")
    elif "channel_id" in str(grants[0][0]):
        failures.append("e: the stored channel grant is plaintext (not KEK-wrapped)")
    if not _gfs_rows("SELECT 1 FROM gfs_channels WHERE channel_id = ?", (channel_id,)):
        failures.append(f"GFS: no row for channel {channel_id}")
    b_id = state["instances"]["b"]["instance_id"]
    if not _gfs_rows(
        "SELECT 1 FROM gfs_channel_subscribers WHERE channel_id = ? AND instance_id = ?",
        (channel_id, b_id),
    ):
        failures.append(
            "GFS: the paired member b holds no seat although the owner turned "
            "private_gfs on"
        )
    a_id = state["instances"]["a"]["instance_id"]
    if _gfs_rows(
        "SELECT 1 FROM gfs_channel_subscribers WHERE instance_id = ?", (a_id,)
    ):
        failures.append("GFS: the owner a holds a channel seat")
    off_space = state.get("gfs_private_channel_off_space_id")
    if off_space:
        rows = _rows(
            "a", "SELECT gfs_channel_id FROM spaces WHERE id = ?", (off_space,)
        )
        if rows and rows[0][0]:
            failures.append("a: the OFF private space has a channel")
    if _gfs_rows(
        "SELECT 1 FROM gfs_channels WHERE channel_id = ? OR channel_pk LIKE ?",
        (space_id, f"%{space_id}%"),
    ):
        failures.append("GFS: a channel row names the private space")


def _poll_gfs_rows(sql: str, params: tuple, ok, *, what: str, timeout: float = 60.0):
    """Poll the GFS DB every 3 s until ``ok(rows)``; SystemExit naming
    ``what`` otherwise."""
    deadline = time.monotonic() + timeout
    rows: list[tuple] = []
    while time.monotonic() < deadline:
        rows = _gfs_rows(sql, params)
        if ok(rows):
            return rows
        time.sleep(3.0)
    raise SystemExit(
        f"gfs-private-channel: {what} within {timeout:.0f} s (last rows {rows!r}). "
        f"Check the GFS log and {_instance_dir('a') / 'log.txt'} for 'gfs.channel'."
    )


def _join_private_link(
    state: dict, label: str, space_id: str, owner_base: str, *, via: str
) -> None:
    """``label`` redeems a fresh, UNPUBLISHED member link to a's private
    space (a private space is never listed, so its link is never parked on
    the connection server): e takes the §D2b bootstrap through the relay
    with a ``gfs`` link, a paired household the direct path with an
    ``internal`` link (no key-wrap key in its code)."""
    a = state["instances"]["a"]
    inst = state["instances"][label]
    s, link = _request(
        f"{owner_base}/api/spaces/{space_id}/invite-tokens",
        token=a["token"],
        method="POST",
        body={"role": "member", "uses": 1, "via": via},
    )
    link = _must(f"a mints a {via} private link for {label}", s, link, ok=(201,))
    if link.get("via") != via:
        raise SystemExit(f"gfs-private-channel: a minted a {link.get('via')!r} link")
    payload = _decode_invite_blob(link["code"])
    if via == "internal" and payload.get("issuer_keywrap_pk"):
        raise SystemExit(
            "gfs-private-channel: an internal link's code carries the key-wrap "
            "key — it could be redeemed through the relay."
        )
    if payload.get("via_gfs"):
        raise SystemExit(
            "gfs-private-channel: a private space's link names a connection "
            f"server ({payload['via_gfs']!r}) — it must never be published."
        )
    s, joined = _request(
        f"http://127.0.0.1:{inst['port']}/api/spaces/join",
        token=inst["token"],
        method="POST",
        body=_join_body_from_blob(payload),
        timeout=40.0,
    )
    _must(f"{label} redeems the private link", s, joined, ok=(200, 201))


def _ensure_gfs_client(state: dict, label: str) -> None:
    """Pair ``label`` with the GFS if ``gfs-pair`` did not (it pairs a, d and
    e) and wait for its WebSocket — every channel frame is pushed over it."""
    pairings = state.setdefault("gfs", {}).setdefault("pairings", {})
    info = state["instances"][label]
    if label not in pairings:
        s, resp = _request(
            f"http://127.0.0.1:{info['port']}/api/gfs/connections",
            token=info["token"],
            method="POST",
            body={
                "gfs_url": f"http://127.0.0.1:{GFS_PORT}",
                "token": _gfs_mint_pair_token(),
            },
        )
        resp = _must(f"gfs-pair({label})", s, resp, ok=(201,))
        pairings[label] = {
            "id": resp["id"],
            "gfs_instance_id": resp["gfs_instance_id"],
            "status": resp["status"],
        }
        _save(state)
        print(f"  {label}: paired with the GFS for this step")
    _wait_for_gfs_ws(info["instance_id"])


@_offline_step
def cmd_gfs_private_channel() -> None:
    """v_51 + the owner's ``private_gfs`` option: a PRIVATE space whose owner
    turned the connection server on gets member publishing over it through
    an OPAQUE channel every member household subscribes to — and the server
    never learns the space.

    Prereqs: the gfs chain through ``gfs-member-publish-strict`` (a and e
    are GFS clients; e is paired with nobody; a↔b are paired).

    Sequence:
    1. b (paired with a) becomes a GFS client too, if it is not one yet.
       (d reaches a only over the mesh: a holds no peer row, so it cannot
       know d's version and — like writer certs — sends it no grant.)
    2. a creates a PRIVATE space. e redeems an unpublished member link (the
       §D2b bootstrap through ``/gfs/envelope``), then b redeems another
       (the direct path).
    3. The owner turns ``private_gfs`` ON first (a gfs link for e, an
       internal one for b). a creates the space's channel: a random
       ``channel_id`` and a channel key registered anonymously at the GFS.
       e (link-joined) AND b (paired, on the GFS) get grants with a pass
       and both take a seat; the owner never does.
    4. Stop **a**. b posts over the channel; e receives it decrypted while
       the host is offline. Then e posts: b receives it live over the
       channel (its envelope to a waits at the GFS).
    5. The GFS log and its whole database never contain the private space's
       id, its name or its authority public key (hex or base64url) — only
       the channel id — and the owner never holds a seat.
    6. Restart a (bookmark first); a catches both posts up over the member
       path.
    7. A new private space (option OFF): a gfs link is refused (409
       ``PRIVATE_GFS_OFF``), the default link is internal, b joins and
       posts over federation, and the GFS gains no channel and no seat.

    Polls every 3 s and backs off on 429.
    """
    state = _load()
    if not state or not _gfs_alive(state):
        raise SystemExit("run the gfs chain through 'gfs-member-publish-strict' first")
    a = state["instances"]["a"]
    b = state["instances"]["b"]
    e = state["instances"]["e"]
    a_base = f"http://127.0.0.1:{a['port']}"
    if _instance_row("a", b["instance_id"]) is None:
        raise SystemExit("gfs-private-channel: a and b are not paired — run 'pair'")
    _ensure_gfs_client(state, "b")
    gfs_off = _gfs_log_size()

    # 2. The private space and its two remote members.
    space_name = f"Private channel space {time.time_ns()}"
    s, space = _request(
        f"{a_base}/api/spaces",
        token=a["token"],
        method="POST",
        body={"name": space_name, "space_type": "private"},
    )
    space = _must("a creates a private space", s, space, ok=(200, 201))
    space_id = space["id"]
    pk_rows = _rows(
        "a", "SELECT identity_public_key FROM spaces WHERE id = ?", (space_id,)
    )
    space_pk_hex = pk_rows[0][0] if pk_rows else ""
    if not space_pk_hex:
        raise SystemExit("gfs-private-channel: a's private space has no authority key")
    space_pk_b64 = (
        base64.urlsafe_b64encode(bytes.fromhex(space_pk_hex)).rstrip(b"=").decode()
    )
    # 2a. The owner turns the connection server on for this space first.
    st, resp = _request(
        f"{a_base}/api/spaces/{space_id}",
        token=a["token"],
        method="PATCH",
        body={"features": {"private_gfs": True}},
    )
    _must("a turns private_gfs on", st, resp, ok=(200,))
    print("  a (owner) turned the connection server on for the space ✓")
    _join_private_link(state, "e", space_id, a_base, via="gfs")
    print(f"  e joined private space {space_id} through the relay (gfs link) ✓")
    _join_private_link(state, "b", space_id, a_base, via="internal")
    print("  b (paired with a) joined it directly (internal link) ✓")

    # 3. The channel, the grants, the seats.
    rows = _poll_rows(
        "a",
        "SELECT gfs_channel_id FROM spaces WHERE id = ?",
        (space_id,),
        lambda r: bool(r) and bool(r[0][0]),
        what="a never created the private space's channel",
    )
    channel_id = rows[0][0]
    if space_id in channel_id or len(channel_id) != 32:
        raise SystemExit(f"gfs-private-channel: odd channel id {channel_id!r}")
    for label in ("e", "b"):
        _poll_rows(
            label,
            "SELECT gfs_channel_id FROM spaces WHERE id = ?",
            (space_id,),
            lambda r: bool(r) and r[0][0] == channel_id,
            what=f"{label} never took its channel grant",
        )
    print(f"  a started channel {channel_id}; e and b hold their grants ✓")
    want = {e["instance_id"], b["instance_id"]}
    seats = _poll_gfs_rows(
        "SELECT instance_id FROM gfs_channel_subscribers WHERE channel_id = ?",
        (channel_id,),
        lambda r: want <= {x[0] for x in r},
        what="e and b never both took their seats on the channel",
    )
    held = {x[0] for x in seats}
    if held != want:
        raise SystemExit(
            f"gfs-private-channel: the channel's seats are {sorted(held)} — with "
            "private_gfs on every member household connected to the GFS takes "
            "one (e and b), never the owner a."
        )
    print("  the GFS seats both members e and b (never the owner a) ✓")

    # 4. Host offline; b (paired, publish-only) posts; e gets it live.
    e_off = _log_size("e")
    _kill_household("a", a)
    b_content = f"Paired member posts with the host offline — {time.time_ns()}"
    st, b_post = _request(
        f"http://127.0.0.1:{b['port']}/api/spaces/{space_id}/posts",
        token=b["token"],
        method="POST",
        body={"type": "text", "content": b_content},
    )
    b_post = _must("b posts with the host offline", st, b_post, ok=(201,))
    b_post_id = b_post["id"]
    _poll_rows(
        "e",
        "SELECT content FROM space_posts WHERE id = ?",
        (b_post_id,),
        lambda r: bool(r) and r[0][0] == b_content,
        what=f"e never received b's post {b_post_id} while a was down",
    )
    if not _log_lines_matching(
        "e", f"gfs.relay.received: channel={channel_id}", offset=e_off
    ):
        raise SystemExit(
            "gfs-private-channel: e logged no channel frame — b's post reached "
            "it some other way."
        )
    print("  e received b's post over the channel with the host offline ✓")
    # e posts too: its envelope to a waits at the GFS, and b — seated now —
    # gets it live over the channel while a is still down.
    b_off = _log_size("b")
    content = f"Posted into a private space with the host offline — {time.time_ns()}"
    st, post = _e_request(
        state,
        "POST",
        f"/api/spaces/{space_id}/posts",
        {"type": "text", "content": content},
    )
    post = _must("e posts with the host offline", st, post, ok=(201,))
    post_id = post["id"]
    _poll_rows(
        "b",
        "SELECT content FROM space_posts WHERE id = ?",
        (post_id,),
        lambda r: bool(r) and r[0][0] == content,
        what=f"b never received e's post {post_id} live while a was down",
    )
    if not _log_lines_matching(
        "b", f"gfs.relay.received: channel={channel_id}", offset=b_off
    ):
        raise SystemExit(
            "gfs-private-channel: b logged no channel frame — e's post reached "
            "it some other way."
        )
    print("  b received e's post live over the channel with the host offline ✓")

    # 5. What the GFS knows.
    leaks = (space_id, space_name, space_pk_hex, space_pk_b64)
    log_text = "\n".join(_gfs_log_lines_matching("", offset=0))
    db_bytes = b"".join(p.read_bytes() for p in GFS_DIR.glob("gfs.db*") if p.is_file())
    for needle in leaks:
        if needle in log_text:
            raise SystemExit(
                f"gfs-private-channel: the GFS log names the private space "
                f"({needle[:24]!r}…)."
            )
        if needle.encode() in db_bytes:
            raise SystemExit(
                f"gfs-private-channel: the GFS database holds the private space "
                f"({needle[:24]!r}…)."
            )
    for text in (content, b_content):
        if text in log_text or text.encode() in db_bytes:
            raise SystemExit("gfs-private-channel: the GFS saw a post's text")
    if _gfs_rows(
        "SELECT 1 FROM gfs_channel_subscribers WHERE instance_id = ?",
        (a["instance_id"],),
    ):
        raise SystemExit("gfs-private-channel: the owner a holds a seat")
    if channel_id not in "\n".join(
        _gfs_log_lines_matching("gfs.channel", offset=gfs_off)
    ):
        raise SystemExit(
            "gfs-private-channel: the GFS logged nothing about the channel — "
            "check that it booted at DEBUG."
        )
    print(
        "  the GFS log and database hold the channel id only — never the "
        "space id, name or key ✓"
    )

    # 6. The host comes back and catches the post up over the member path.
    new_pid = _spawn("a", a["port"])
    state["instances"]["a"]["pid"] = new_pid
    _save(state)
    _wait_ready(a["port"])
    for pid, text, who in ((post_id, content, "e"), (b_post_id, b_content, "b")):
        _poll_rows(
            "a",
            "SELECT content FROM space_posts WHERE id = ?",
            (pid,),
            lambda r, text=text: bool(r) and r[0][0] == text,
            what=f"a (host) never caught {who}'s post {pid} up after restarting",
        )
    print("  a caught e's and b's posts up over the member path ✓")

    # 7. A private space with the option OFF (the default) never touches
    #    the GFS: no gfs link, no channel, no seat — b joins and posts over
    #    federation only.
    off_space_id = _check_private_space_off(state, a_base)
    state["gfs_private_channel_space_id"] = space_id
    state["gfs_private_channel_off_space_id"] = off_space_id
    state["gfs_private_channel_id"] = channel_id
    _save(state)
    print(
        "gfs-private-channel: ok (with the owner's private_gfs on, the members "
        "of a private space reach each other over an opaque channel while the "
        "host is offline; the GFS never sees the space; an OFF space never "
        "touches the GFS)"
    )


def _check_private_space_off(state: dict, a_base: str) -> str:
    """A NEW private space is OFF: a ``gfs`` link is refused (409
    ``PRIVATE_GFS_OFF``), the default link is ``internal``, b (paired)
    joins and posts over federation, and the GFS gains no channel row and
    no seat — the space produces no GFS traffic at all."""
    a = state["instances"]["a"]
    b = state["instances"]["b"]
    channels_before = _gfs_rows("SELECT COUNT(*) FROM gfs_channels", ())[0][0]
    seats_before = _gfs_rows("SELECT COUNT(*) FROM gfs_channel_subscribers", ())[0][0]
    s, space = _request(
        f"{a_base}/api/spaces",
        token=a["token"],
        method="POST",
        body={"name": f"Off private space {time.time_ns()}", "space_type": "private"},
    )
    space = _must("a creates an OFF private space", s, space, ok=(200, 201))
    off_id = space["id"]
    s, detail = _request(f"{a_base}/api/spaces/{off_id}", token=a["token"])
    detail = _must("a reads the OFF private space", s, detail, ok=(200,))
    if detail.get("features", {}).get("private_gfs") is not False:
        raise SystemExit("gfs-private-channel: a new private space is not OFF")
    s, refused = _request(
        f"{a_base}/api/spaces/{off_id}/invite-tokens",
        token=a["token"],
        method="POST",
        body={"role": "member", "uses": 1, "via": "gfs"},
    )
    code = (refused or {}).get("error", {}).get("code") if s == 409 else None
    if code != "PRIVATE_GFS_OFF":
        raise SystemExit(
            f"gfs-private-channel: a gfs link on an OFF space answered {s} {refused!r}"
        )
    s, link = _request(
        f"{a_base}/api/spaces/{off_id}/invite-tokens",
        token=a["token"],
        method="POST",
        body={"role": "member", "uses": 1},
    )
    link = _must("a mints the default link on the OFF space", s, link, ok=(201,))
    if link.get("via") != "internal":
        raise SystemExit(
            "gfs-private-channel: the OFF space's default link isn't internal"
        )
    payload = _decode_invite_blob(link["code"])
    s, joined = _request(
        f"http://127.0.0.1:{b['port']}/api/spaces/join",
        token=b["token"],
        method="POST",
        body=_join_body_from_blob(payload),
        timeout=40.0,
    )
    _must("b redeems the OFF space's internal link", s, joined, ok=(200, 201))
    text = f"Posted into an OFF private space — {time.time_ns()}"
    st, post = _request(
        f"http://127.0.0.1:{b['port']}/api/spaces/{off_id}/posts",
        token=b["token"],
        method="POST",
        body={"type": "text", "content": text},
    )
    post = _must("b posts into the OFF space", st, post, ok=(201,))
    _poll_rows(
        "a",
        "SELECT content FROM space_posts WHERE id = ?",
        (post["id"],),
        lambda r: bool(r) and r[0][0] == text,
        what="a never got b's post in the OFF space over federation",
    )
    time.sleep(3.0)
    rows = _rows("a", "SELECT gfs_channel_id FROM spaces WHERE id = ?", (off_id,))
    if rows and rows[0][0]:
        raise SystemExit("gfs-private-channel: the OFF private space got a channel")
    channels_after = _gfs_rows("SELECT COUNT(*) FROM gfs_channels", ())[0][0]
    seats_after = _gfs_rows("SELECT COUNT(*) FROM gfs_channel_subscribers", ())[0][0]
    if (channels_after, seats_after) != (channels_before, seats_before):
        raise SystemExit(
            "gfs-private-channel: the OFF private space changed the GFS "
            f"(channels {channels_before}→{channels_after}, seats "
            f"{seats_before}→{seats_after})"
        )
    db_bytes = b"".join(p.read_bytes() for p in GFS_DIR.glob("gfs.db*") if p.is_file())
    if off_id.encode() in db_bytes or text.encode() in db_bytes:
        raise SystemExit("gfs-private-channel: the GFS holds the OFF space")
    print(
        "  an OFF private space: gfs link refused (409 PRIVATE_GFS_OFF), internal "
        "link by default, no channel, no seat — no GFS traffic ✓"
    )
    return off_id


def _relay_row(label: str, peer_iid: str) -> tuple | None:
    """``(status, gfs_relay, remote_inbox_url, has keywrap pk)`` of
    ``label``'s ``remote_instances`` row for ``peer_iid``."""
    rows = _rows(
        label,
        "SELECT status, gfs_relay, remote_inbox_url, remote_keywrap_pk IS NOT NULL"
        " FROM remote_instances WHERE id=?",
        (peer_iid,),
    )
    return rows[0] if rows else None


def _connection_row(label: str, peer_iid: str) -> dict | None:
    """``label``'s ``GET /api/connections`` row for ``peer_iid``."""
    inst = _load()["instances"][label]
    s, rows = _request(
        f"http://127.0.0.1:{inst['port']}/api/connections", token=inst["token"]
    )
    _must(f"connections({label})", s, rows)
    return next((r for r in rows if r.get("instance_id") == peer_iid), None)


def _switch_gfs_fallback(label: str, peer_iid: str, on: bool) -> dict:
    inst = _load()["instances"][label]
    s, body = _request(
        f"http://127.0.0.1:{inst['port']}/api/pairing/connections/{peer_iid}",
        token=inst["token"],
        method="PATCH",
        body={"gfs_relay": on},
    )
    return _must(f"gfs fallback {'on' if on else 'off'} ({label})", s, body)


def _wait_row(
    label: str, peer_iid: str, pred, what: str, timeout: float = 45.0
) -> dict:
    deadline = time.monotonic() + timeout
    row: dict | None = None
    while time.monotonic() < deadline:
        row = _connection_row(label, peer_iid)
        if row is not None and pred(row):
            return row
        time.sleep(1.0)
    raise SystemExit(f"gfs-fallback-switch: {label} never saw {what}: {row!r}")


def cmd_gfs_fallback_switch() -> None:
    """Turn the GFS fallback on for an EXISTING pair (v_54).

    a and d were paired without any GFS reach (``relay-pair``, an ordinary
    trust-relay pair with addresses) and both are on the demo GFS
    (``gfs-pair``). Neither holds the other's key-wrap key, so neither can
    relay to the other. a's admin switches the fallback on for d: d must
    store a's key (it rides the encrypted ``INSTANCE_CAPABILITIES_UPDATED``)
    while d's own switch stays off and no route forms on either side. Then
    d's admin switches it on: a learns d's key and probes at once, and both
    sides must end with a route through the shared GFS. b (paired with a,
    not on any GFS) must read ``gfs_relay_available`` but no route. Finally
    both switch it off again and the routes must be gone, so the step can
    run again at once (switching off clears the pair's probe throttles).

    Prereqs: ``up`` + ``pair`` + ``relay-pair`` + ``gfs-up`` + ``gfs-pair``.
    """
    state = _load()
    if not state or not _gfs_alive(state):
        raise SystemExit("run 'gfs-up' + 'gfs-pair' first")
    if not state.get("relay_pair_ran"):
        raise SystemExit("run 'relay-pair' first (a <-> d)")
    a = state["instances"]["a"]
    d = state["instances"]["d"]
    a_iid, d_iid = a["instance_id"], d["instance_id"]
    _wait_for_gfs_ws(a_iid)
    _wait_for_gfs_ws(d_iid)

    before_a = _connection_row("a", d_iid)
    if before_a is None or before_a.get("gfs_relay"):
        raise SystemExit(
            f"gfs-fallback-switch: a's row for d must exist with the switch off: {before_a!r}"
        )
    if not before_a.get("gfs_relay_available"):
        raise SystemExit(f"gfs-fallback-switch: d should be available: {before_a!r}")

    try:
        body = _switch_gfs_fallback("a", d_iid, True)
        if body.get("gfs_relay") is not True:
            raise SystemExit(f"gfs-fallback-switch: PATCH answered {body!r}")
        print("  a: GFS fallback on for d")
        _wait_row(
            "d",
            a_iid,
            lambda r: r.get("peer_keywrap_known") is True,
            "a's key-wrap key",
        )
        row_d = _connection_row("d", a_iid) or {}
        if row_d.get("gfs_relay") or row_d.get("gfs_routes"):
            raise SystemExit(
                f"gfs-fallback-switch: d's switch must stay off with no route: {row_d!r}"
            )
        time.sleep(3)
        if (_connection_row("a", d_iid) or {}).get("gfs_routes"):
            raise SystemExit("gfs-fallback-switch: a got a route with only one side on")
        print("  d stored a's verified key; no route while only a is on ✓")

        _switch_gfs_fallback("d", a_iid, True)
        print("  d: GFS fallback on for a")
        _wait_row("a", d_iid, lambda r: (r.get("gfs_routes") or 0) >= 1, "a route to d")
        _wait_row("d", a_iid, lambda r: (r.get("gfs_routes") or 0) >= 1, "a route to a")
        print("  both sides hold a route through the shared GFS ✓")

        b_row = _connection_row("a", state["instances"]["b"]["instance_id"]) or {}
        if b_row.get("gfs_routes"):
            raise SystemExit(
                f"gfs-fallback-switch: b has no GFS, yet a route: {b_row!r}"
            )

        # a switches off: its encrypted capabilities say ``gfs_relay:
        # false`` and d drops ITS routes to a (no relaying into a closed
        # gate), while d's own switch stays on.
        _switch_gfs_fallback("a", d_iid, False)
        print("  a: GFS fallback off for d")
        row_d = _wait_row(
            "d", a_iid, lambda r: not r.get("gfs_routes"), "its routes to a dropped"
        )
        if row_d.get("gfs_relay") is not True:
            raise SystemExit(
                f"gfs-fallback-switch: a's off must not flip d's switch: {row_d!r}"
            )
        print("  d dropped its route to a; d's own switch stays on ✓")
    finally:
        for label, peer in (("a", d_iid), ("d", a_iid)):
            try:
                _switch_gfs_fallback(label, peer, False)
            except SystemExit as exc:  # pragma: no cover — report, keep cleaning
                print(f"  cleanup: {exc}")
    for label, peer in (("a", d_iid), ("d", a_iid)):
        row = _connection_row(label, peer) or {}
        if row.get("gfs_relay") or row.get("gfs_routes"):
            raise SystemExit(
                f"gfs-fallback-switch: {label} still relays after off: {row!r}"
            )
    print(
        "gfs-fallback-switch: ok (key exchange over capabilities → shared-GFS routes → off)"
    )


def cmd_gfs_reach_pair() -> None:
    """Pair a household that offers NO address — through the GFS (pairing ``reach``).

    e issues a ``reach: "gfs"`` code (``inbox_url = ""``: as if e had no
    External URL), a scans it. a's peer-accept and e's peer-confirm can only
    travel sealed through ``POST /gfs/envelope``; both sides must end
    CONFIRMED with the relay opt-in (``gfs_relay = 1``), the other's key-wrap
    key and a ``peer_gfs_routes`` row, and ordinary §24.11 traffic (a profile
    update) must reach e. Then e sets an External URL through the admin route
    and its ``URL_UPDATED`` fills a's empty ``remote_inbox_url`` for e.

    Finally a unpairs e and e's manual URL is cleared again, so e is back to
    being the stranger the ``gfs-invite-link`` steps rely on — but run this
    step AFTER them anyway (it is the last ``gfs-*`` content step), since an
    unpair leaves tombstones a later step should not have to reason about.

    Prereqs: ``up`` + ``gfs-up`` + ``gfs-pair`` (a and e on the GFS). A
    re-run against the same processes must wait a minute: route discovery
    probes a peer at most once per ``PROBE_PEER_MIN_INTERVAL_S`` (60 s), so a
    re-pair inside that window gets no fresh probe/ack to assert on.
    """
    state = _load()
    if not state or not _gfs_alive(state):
        raise SystemExit("run 'gfs-up' + 'gfs-pair' first")
    a = state["instances"]["a"]
    e = state["instances"]["e"]
    a_base = f"http://127.0.0.1:{a['port']}"
    e_base = f"http://127.0.0.1:{e['port']}"
    a_iid, e_iid = a["instance_id"], e["instance_id"]
    failures: list[str] = []

    if _relay_row("a", e_iid) or _relay_row("e", a_iid):
        raise SystemExit(
            "gfs-reach-pair: a and e already know each other — this step "
            "needs e unpaired (fresh 'up' + 'gfs-up' + 'gfs-pair')",
        )
    # Relay frames are pushed down the household's SH↔GFS socket; one sent
    # before it is open is lost (see ``_wait_for_gfs_ws``).
    _wait_for_gfs_ws(a_iid)
    _wait_for_gfs_ws(e_iid)

    try:
        # 1. The code owner (e) offers no address.
        s, qr = _pairing_call(
            "e",
            f"{e_base}/api/pairing/initiate",
            token=e["token"],
            method="POST",
            body={"reach": "gfs"},
        )
        _must("initiate(e, reach=gfs)", s, qr, ok=(201,))
        if qr.get("reach") != "gfs" or qr.get("inbox_url") != "":
            raise SystemExit(f"gfs code must carry reach=gfs and no inbox_url: {qr!r}")
        if not (qr.get("gfs") or {}).get("url") or not qr.get("keywrap_pk"):
            raise SystemExit(
                f"gfs code is missing its GFS block / key-wrap key: {qr!r}"
            )
        print(f"  e: issued a reach=gfs code via {qr['gfs']['url']} (no inbox_url)")

        # 2. a scans it. Nothing reaches e's inbox: the accept rides the relay.
        gfs_off = _gfs_log_size()
        s, ack = _pairing_call(
            "a",
            f"{a_base}/api/pairing/accept",
            token=a["token"],
            method="POST",
            body=qr,
        )
        _must("accept(a)", s, ack)
        print(f"  a: accepted — SAS {ack['verification_code']}")

        # 3. e confirms once the relayed accept has landed (before that the
        #    session has no SAS and confirm answers 4xx without touching state).
        def _confirm():
            s, body = _pairing_call(
                "e",
                f"{e_base}/api/pairing/confirm",
                token=e["token"],
                method="POST",
                body={
                    "token": ack["token"],
                    "verification_code": ack["verification_code"],
                },
            )
            return body if s == 200 else None

        # Every /api/pairing write shares one 5 / 60 s bucket — poll gently.
        _poll(
            "e's confirm (relayed peer-accept landed on e)",
            _confirm,
            timeout=90,
            interval=4.0,
        )
        print("  e: confirmed")

        def _both_confirmed():
            ra, re_ = _relay_row("a", e_iid), _relay_row("e", a_iid)
            return (
                ra
                and re_
                and ra[0] == "confirmed"
                and re_[0] == "confirmed"
                and (ra, re_)
            )

        ra, re_ = _poll("a and e both CONFIRMED", _both_confirmed, timeout=60)
        print(f"  rows: a→e {ra}  e→a {re_}")
        if ra[1] != 1 or re_[1] != 1:
            failures.append(
                f"gfs_relay must be 1 on both rows (a→e={ra[1]}, e→a={re_[1]})"
            )
        if not ra[3] or not re_[3]:
            failures.append("both rows must hold the other side's key-wrap key")
        if ra[2]:
            failures.append(f"a must hold NO inbox URL for e yet, got {ra[2]!r}")
        relayed = _gfs_log_lines_matching("envelope", offset=gfs_off)
        print(f"  GFS log: {len(relayed)} envelope line(s) during the handshake")

        # 4. Routes: seeded on pairing, refreshed by the post-confirm probe/ack.
        def _routes():
            ga = _rows(
                "a",
                "SELECT gfs_connection_id FROM peer_gfs_routes WHERE instance_id=?",
                (e_iid,),
            )
            ge = _rows(
                "e",
                "SELECT gfs_connection_id FROM peer_gfs_routes WHERE instance_id=?",
                (a_iid,),
            )
            return ga and ge and (ga, ge)

        ga, ge = _poll("a peer_gfs_routes row on both sides", _routes, timeout=60)
        print(f"  routes: a→e via {ga[0][0][:8]}  e→a via {ge[0][0][:8]}")
        if ga[0][0] != state["gfs"]["pairings"]["a"]["id"]:
            failures.append("a's route for e must be a's own connection to the GFS")
        if ge[0][0] != state["gfs"]["pairings"]["e"]["id"]:
            failures.append("e's route for a must be e's own connection to the GFS")

        # The post-confirm probe and its ack are federation messages that can
        # ONLY travel through the GFS (a probe is a relay-route check by
        # definition): an ack refreshing a seeded route proves a relayed
        # round trip after the handshake, independent of any DataChannel.
        def _acked():
            out = []
            for label, peer in (("a", e_iid), ("e", a_iid)):
                rows = _rows(
                    label,
                    "SELECT confirmed_at, last_ack_at FROM peer_gfs_routes"
                    " WHERE instance_id=?",
                    (peer,),
                )
                out.append(bool(rows) and rows[0][1] > rows[0][0])
            return any(out) and out

        try:
            acked = _poll("a probe ack refreshing a seeded route", _acked, timeout=60)
            print(
                f"  probe/ack through the GFS refreshed the route on (a, e) = {acked}"
            )
        except SystemExit:
            failures.append(
                "no probe ack ever refreshed a seeded route (relayed round trip)"
            )

        # 5. Ordinary §24.11 traffic a → e. a has no address for e, so the only
        #    ways there are the relay and a DataChannel whose signalling itself
        #    went through the relay.
        new_name = f"Alice via GFS {secrets.token_hex(3)}"
        s, body = _request(
            f"{a_base}/api/me",
            token=a["token"],
            method="PATCH",
            body={"display_name": new_name},
        )
        _must("patch me(a)", s, body)
        _poll(
            "a's profile update on e",
            lambda: new_name in _all_display_names(state, "e").values(),
            timeout=60,
        )
        row = _connection_row("a", e_iid) or {}
        print(
            f"  e: sees {new_name!r} — a→e transport={row.get('transport')!r}"
            f" last_relay_accepted_at={row.get('last_relay_accepted_at')!r}"
        )
        # Informational: once the DataChannel is up, §24.11 traffic prefers it,
        # so whether THIS update rode the relay depends on ICE timing — the
        # relayed round trip is asserted by the probe/ack check above instead.
        # (``last_relay_accepted_at`` is in-memory per peer id, so a re-run
        # against the same processes can show an earlier run's value.)

        # WebRTC: signalling for an address-less peer rides the relay too. Not
        # asserted (ICE timing on a loaded box is not deterministic) — reported.
        try:
            transport = _poll(
                "a DataChannel a↔e",
                lambda: (
                    (_connection_row("a", e_iid) or {}).get("transport") == "rtc"
                    and "rtc"
                ),
                timeout=30,
            )
        except SystemExit:
            transport = (_connection_row("a", e_iid) or {}).get("transport")
        print(f"  a→e transport after 30 s: {transport!r} (informational)")

        # 6. e gains a URL → URL_UPDATED → a's row for e gets it.
        s, put = _request(
            f"{e_base}/api/admin/federation/external-url",
            token=e["token"],
            method="PUT",
            body={"base": e_base},
        )
        _must("external-url(e)", s, put)
        print(f"  e: External URL set, peers_notified={put.get('peers_notified')}")
        if not put.get("peers_notified"):
            failures.append("e's URL change notified no peer")
        got = _poll(
            "URL_UPDATED from e on a",
            lambda: (_relay_row("a", e_iid) or (None, None, ""))[2],
            timeout=60,
        )
        print(f"  a: row for e now has inbox {got!r}")
        if not got.startswith(e_base):
            failures.append(
                f"a's inbox URL for e should be under {e_base}, got {got!r}"
            )
    finally:
        # Best effort, always: a failed poll above must not leave e paired
        # (or holding a manual URL) — that would break every later e-as-
        # stranger step and this step's own re-run guard.
        cleanup_problems = _gfs_reach_pair_cleanup(a, e)

    if cleanup_problems:
        failures.extend(cleanup_problems)
    else:
        print("  a ↔ e unpaired again; e's manual URL cleared")
    if failures:
        raise SystemExit("gfs-reach-pair FAILED:\n  - " + "\n  - ".join(failures))
    print(
        "gfs-reach-pair: ok (address-less code → relayed handshake → relay traffic → URL_UPDATED)"
    )


def _gfs_reach_pair_cleanup(a: dict, e: dict) -> list[str]:
    """Back to the stranger topology: unpair a ↔ e from whichever side still
    holds a row (a 404 just means "never paired / already gone") and clear
    e's manual External URL. Never raises — returns what didn't work."""
    problems: list[str] = []
    a_iid, e_iid = a["instance_id"], e["instance_id"]
    for label, info, peer in (("a", a, e_iid), ("e", e, a_iid)):
        if not _relay_row(label, peer):
            continue
        try:
            s, body = _pairing_call(
                label,
                f"http://127.0.0.1:{info['port']}/api/pairing/connections/{peer}",
                token=info["token"],
                method="DELETE",
            )
        except Exception as exc:  # noqa: BLE001 — cleanup is best effort
            problems.append(f"cleanup: unpair from {label} raised {exc!r}")
            continue
        if s not in (200, 204, 404):
            problems.append(f"cleanup: unpair from {label} → HTTP {s} {body!r}")
    try:
        _poll(
            "e drops a after the UNPAIR",
            lambda: a_iid not in _peer_ids(e) and e_iid not in _peer_ids(a),
            timeout=60,
        )
    except SystemExit as exc:
        problems.append(f"cleanup: {exc}")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"cleanup: peer check raised {exc!r}")
    try:
        s, body = _request(
            f"http://127.0.0.1:{e['port']}/api/admin/federation/external-url",
            token=e["token"],
            method="PUT",
            body={"base": None},
        )
        if s != 200:
            problems.append(f"cleanup: clear e's External URL → HTTP {s} {body!r}")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"cleanup: clear e's External URL raised {exc!r}")
    return problems


def cmd_gfs_down() -> None:
    """Stop the GFS started by :func:`cmd_gfs_up` (idempotent)."""
    state = _load()
    gfs = state.get("gfs")
    if not gfs:
        return
    try:
        os.killpg(gfs["pid"], signal.SIGTERM)
    except ProcessLookupError, PermissionError:
        pass
    time.sleep(1)
    try:
        os.killpg(gfs["pid"], signal.SIGKILL)
    except ProcessLookupError, PermissionError:
        pass
    state.pop("gfs", None)
    _save(state)
    print("gfs-down: ok")


# ─── Steps: gfs-b-up / gfs-b-down / gfs-multi-listing (v_57) ──────────────

#: A second, independent GFS (own data dir, own identity key) next to the
#: main one on 18765 — the cluster steps use 18770-18772.
GFS_B_PORT = 18767
GFS_B_DIR = ROOT / "gfs-b"

#: Harness-only prelude for household d in ``gfs-multi-listing``. A member
#: drops an owner-listed server on a private / loopback address it never
#: connected to, and the pinned session refuses any non-public address —
#: both on purpose (an owner's list must never aim a member at its own
#: network). The demo's GFS B lives on 127.0.0.1, so d runs with loopback
#: treated as public, exactly like ``loopback_is_public`` in
#: ``tests/test_outbound_fetch.py``. Never a production knob: it only exists
#: in this ``-c`` string. ``validate_peer_url`` is NOT patched (it still sees
#: loopback as private, so B's plain-http URL stays valid).
_LOOPBACK_IS_PUBLIC_PRELUDE = (
    "import socialhome.domain.gfs_server as _g, socialhome.outbound_fetch as _o; "
    "_pub = _o.is_public_address; "
    "_o.is_public_address = lambda ip: str(ip) == '127.0.0.1' or _pub(ip); "
    "_priv = _g.is_private_host; "
    "_g.is_private_host = lambda h: False if h == '127.0.0.1' else _priv(h); "
    "import runpy; runpy.run_module('socialhome', run_name='__main__', "
    "alter_sys=True)"
)


#: ``gfs-moment-profile``: open a moment's share copy (stdin) the way the
#: pinned viewer does — with the link's item key ``argv[1]`` for author
#: ``argv[2]`` and moment ``argv[3]`` — and print its JSON body.
_OPEN_SHARE_SNIPPET = (
    "import json, sys; "
    "from socialhome.crypto import b64url_decode; "
    "from socialhome.moment_public_media import open_share; "
    "print(json.dumps(open_share(blob=sys.stdin.buffer.read(), "
    "item_key=b64url_decode(sys.argv[1]), author_user_id=sys.argv[2], "
    "moment_id=sys.argv[3])))"
)


def _spawn_loopback_public(label: str, port: int) -> int:
    """:func:`_spawn` with :data:`_LOOPBACK_IS_PUBLIC_PRELUDE`."""
    d = _instance_dir(label)
    log = open(d / "log.txt", "ab")
    env = {
        **os.environ,
        "SH_MODE": "standalone",
        "SH_CONFIG": str(d / "socialhome.toml"),
        "SH_LOG_LEVEL": "INFO",
    }
    p = subprocess.Popen(
        [sys.executable, "-u", "-c", _LOOPBACK_IS_PUBLIC_PRELUDE],
        stdout=log,
        stderr=subprocess.STDOUT,
        env=env,
        cwd=str(REPO_ROOT),
        start_new_session=True,
    )
    return p.pid


def _gfs_b_alive(state: dict) -> bool:
    pid = (state.get("gfs_b") or {}).get("pid")
    return bool(pid and _alive(pid))


def _gfs_b_rows(sql: str, params: tuple = ()) -> list[tuple]:
    """Read-only query against GFS B's own SQLite DB."""
    conn = sqlite3.connect(GFS_B_DIR / "gfs.db")
    try:
        return list(conn.execute(sql, params))
    finally:
        conn.close()


def _gfs_b_log_size() -> int:
    try:
        return (GFS_B_DIR / "log.txt").stat().st_size
    except OSError:
        return 0


def _gfs_b_log_text(offset: int = 0) -> str:
    try:
        return (
            (GFS_B_DIR / "log.txt")
            .read_bytes()[offset:]
            .decode("utf-8", errors="replace")
        )
    except OSError:
        return ""


def cmd_gfs_b_up() -> None:
    """Start a SECOND GFS on ``127.0.0.1:18767`` with its own data dir and
    identity — another operator's server, for the v_57 multi-listing step.
    Same boot as :func:`cmd_gfs_up` (DEBUG logs, example TOML patched)."""
    state = _load()
    if not state:
        raise SystemExit("run 'up' first")
    from socialhome.global_server.admin import hash_password
    from socialhome.global_server.config import (
        set_password_in_toml,
        write_example_config,
    )

    GFS_B_DIR.mkdir(parents=True, exist_ok=True)
    config_path = GFS_B_DIR / "global_server.toml"
    if not config_path.exists():
        write_example_config(config_path)
    text = config_path.read_text(encoding="utf-8")
    text = text.replace('host     = "0.0.0.0"', 'host     = "127.0.0.1"')
    text = text.replace("port     = 8765", f"port     = {GFS_B_PORT}")
    text = text.replace(
        'base_url = "https://gfs.example.com"',
        f'base_url = "http://127.0.0.1:{GFS_B_PORT}"',
    )
    text = text.replace('data_dir = "/var/lib/sh-gfs"', f'data_dir = "{GFS_B_DIR}"')
    # Its own public id: the owner's list dedupes servers by id, so B must
    # not share A's default ``gfs-node-0``.
    text = text.replace('instance_id = "gfs-node-0"', 'instance_id = "gfs-b"', 1)
    config_path.write_text(text, encoding="utf-8")
    set_password_in_toml(config_path, hash_password("gfs-admin-pw"))
    log = open(GFS_B_DIR / "log.txt", "wb")
    p = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-c",
            "import logging; logging.basicConfig(level=logging.DEBUG, "
            'format="%(asctime)s %(levelname)-8s %(name)s: %(message)s"); '
            "from socialhome.global_server.server import main; main()",
            "--config",
            str(config_path),
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
        cwd=str(REPO_ROOT),
        start_new_session=True,
    )
    deadline = time.monotonic() + 30.0
    last_err: Any = None
    while time.monotonic() < deadline:
        try:
            s, _ = _request(f"http://127.0.0.1:{GFS_B_PORT}/healthz", timeout=2.0)
            if s == 200:
                break
        except Exception as exc:
            last_err = exc
        time.sleep(0.5)
    else:
        raise SystemExit(f"GFS B not ready after 30 s (last error: {last_err!r})")
    s, info = _request(f"http://127.0.0.1:{GFS_B_PORT}/gfs/info")
    _must("GFS B /gfs/info", s, info)
    if (state.get("gfs") or {}).get("port"):
        s, info_a = _request(f"http://127.0.0.1:{GFS_PORT}/gfs/info")
        if s == 200 and info_a.get("public_key") == info.get("public_key"):
            raise SystemExit("gfs-b-up: GFS B shares GFS A's identity key")
    state["gfs_b"] = {
        "pid": p.pid,
        "port": GFS_B_PORT,
        "base_url": f"http://127.0.0.1:{GFS_B_PORT}",
        "gfs_instance_id": info.get("gfs_instance_id"),
    }
    _save(state)
    print(f"  gfs-b: pid={p.pid} port={GFS_B_PORT} id={info.get('gfs_instance_id')!r}")
    print("gfs-b-up: ok")


def cmd_gfs_b_down() -> None:
    """Stop GFS B (idempotent)."""
    state = _load()
    gfs_b = state.get("gfs_b")
    if not gfs_b:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(gfs_b["pid"], sig)
        except ProcessLookupError, PermissionError:
            pass
        time.sleep(1)
    state.pop("gfs_b", None)
    _save(state)
    print("gfs-b-down: ok")


def _as(state: dict, label: str, method: str, path: str, body=None):
    """One REST call as ``label``'s user, backing off on 429."""
    inst = state["instances"][label]
    st, resp = 0, None
    for _attempt in range(4):
        st, resp = _request(
            f"http://127.0.0.1:{inst['port']}{path}",
            token=inst["token"],
            method=method,
            body=body,
        )
        if st != 429:
            break
        time.sleep(15.0)
    return st, resp


def _request_with_headers(
    url: str,
    *,
    token: str | None = None,
    method: str = "GET",
    body: dict | list | None = None,
    timeout: float = 15.0,
) -> tuple[int, Any, dict[str, str]]:
    """:func:`_request`, also returning the response headers."""
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8") or "{}"
            return r.status, json.loads(raw) if raw else {}, dict(r.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            parsed: Any = json.loads(raw)
        except Exception:
            parsed = {"_raw": raw}
        return exc.code, parsed, dict(exc.headers or {})


def _retry_after_s(headers: dict[str, str], fallback: float) -> float:
    """A 429's ``Retry-After`` in seconds (delta form), else *fallback*."""
    for key, value in headers.items():
        if key.lower() == "retry-after":
            try:
                return max(1.0, float(value))
            except ValueError:
                break
    return fallback


def _as_patient(
    state: dict,
    label: str,
    method: str,
    path: str,
    body=None,
    *,
    budget_s: float = 180.0,
):
    """:func:`_as` for a rate-limited route (``/api/pairing/*`` is 5 calls
    / 60 s per household): on 429 wait what ``Retry-After`` says — or back
    off 10 → 20 → 40 s when the server sends none — and try the SAME call
    again, within *budget_s* in total."""
    inst = state["instances"][label]
    deadline = time.monotonic() + budget_s
    backoff = 10.0
    while True:
        st, resp, headers = _request_with_headers(
            f"http://127.0.0.1:{inst['port']}{path}",
            token=inst["token"],
            method=method,
            body=body,
        )
        if st != 429:
            return st, resp
        wait = min(_retry_after_s(headers, backoff), 65.0)
        if time.monotonic() + wait > deadline:
            return st, resp
        print(f"  {label} {method} {path}: 429 — retrying in {wait:.0f} s")
        time.sleep(wait)
        backoff = min(backoff * 2, 40.0)


def _pair_with(state: dict, label: str, base_url: str) -> dict:
    """Connect ``label`` to the GFS at ``base_url`` (fresh landing token)."""
    st, resp = _as(
        state,
        label,
        "POST",
        "/api/gfs/connections",
        {"gfs_url": base_url, "token": _gfs_landing_token(base_url)},
    )
    return _must(f"{label} connects to {base_url}", st, resp, ok=(201,))


@_offline_step
def cmd_gfs_multi_listing() -> None:
    """v_57: every GFS on the owner's list gets every item — a follower on
    GFS B alone sees what a member registered only on GFS A published while
    the host is offline, and B never learns who that member is.

    Prereqs: ``up`` → ``pair`` → ``gfs-up`` → ``gfs-pair`` (a, d, e on A) →
    ``gfs-b-up``. Runs after the rest of the gfs chain (it moves e off A for
    a while and restarts a and d), before ``verify``.

    1. a and e connect to B. a creates a TRUSTED global space, publicly
       readable, and lists it on A and on B (owner selection, ``PUT
       /api/spaces/{id}/gfs-servers``). Both servers
       list it and pin the epoch's writer key (the first-listing notice).
    2. a invites d (paired, registered on A only); d accepts. d holds the
       owner's signed list naming A and B, and the writer key (trusted
       space, v_57).
    3. d restarts with loopback treated as public (the demo's B is on
       127.0.0.1 — see :data:`_LOOPBACK_IS_PUBLIC_PRELUDE`).
    4. e leaves A, discovers the space on B and follows it there only.
    5. a goes offline. d posts, comments and reacts.
    6. e receives all three — B is the only server it follows on. B holds no
       ``client_instances`` row for d, never got an identified member
       publish, and its log names neither d nor the content.
    7. Clean-up: a and d restart normally; e reconnects to A.
    """
    state = _load()
    if not state or not _gfs_alive(state):
        raise SystemExit("run 'gfs-up' + 'gfs-pair' first")
    if not _gfs_b_alive(state):
        raise SystemExit("run 'gfs-b-up' first")
    a, d, e = (state["instances"][x] for x in ("a", "d", "e"))
    b_url = state["gfs_b"]["base_url"]
    a_url = f"http://127.0.0.1:{GFS_PORT}"
    d_iid = d["instance_id"]
    d_pk = _rows("d", "SELECT identity_public_key FROM instance_identity")[0][0]
    d_leaks = (
        d_iid,
        d_pk,
        base64.urlsafe_b64encode(bytes.fromhex(d_pk)).decode().rstrip("="),
    )

    # 1. a and e connect to B; a lists a new trusted global space on A and B.
    a_b = _pair_with(state, "a", b_url)
    e_b = _pair_with(state, "e", b_url)
    print(f"  a and e connected to GFS B ({b_url}) ✓")
    st, space = _as(
        state,
        "a",
        "POST",
        "/api/spaces",
        {
            "name": "Two servers",
            "description": "v_57 multi-GFS listing",
            "space_type": "global",
            # Listed on A at creation (#874); the owner's selection below adds B.
            "gfs_ids": [state["gfs"]["pairings"]["a"]["id"]],
            "join_mode": "invite_only",
        },
    )
    space_id = _must("a creates the global space", st, space, ok=(201,))["id"]
    st, body = _as(
        state,
        "a",
        "PATCH",
        f"/api/spaces/{space_id}",
        {"features": {"allow_subscribers": True}},
    )
    _must("a makes it publicly readable", st, body)
    a_a_id = state["gfs"]["pairings"]["a"]["id"]
    st, body = _as(
        state,
        "a",
        "PUT",
        f"/api/spaces/{space_id}/gfs-servers",
        {"gfs_ids": [a_a_id, a_b["id"]]},
    )
    body = _must("a lists the space on GFS A and B", st, body)
    if sorted(body.get("listed") or []) != sorted([a_a_id, a_b["id"]]):
        raise SystemExit(f"gfs-multi-listing: owner selection answered {body!r}")
    for name, rows_of in (("A", _gfs_rows), ("B", _gfs_b_rows)):
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            rows = rows_of(
                "SELECT writer_key_epoch FROM global_spaces WHERE space_id=?",
                (space_id,),
            )
            if rows and rows[0][0] is not None:
                break
            time.sleep(2.0)
        else:
            raise SystemExit(
                f"gfs-multi-listing: GFS {name} lists no space {space_id} with a "
                f"pinned writer key within 60 s (rows {rows!r}) — check "
                f"{_instance_dir('a') / 'log.txt'} for 'epoch notice'"
            )
    print("  the space is listed on A and B, writer key pinned on both ✓")

    # 2. d joins as a member; it holds the owner's list and the writer key.
    #    The key goes only to a household a KNOWS runs v_57, so a and d
    #    must be paired (the canonical gfs chain leaves them mesh-only;
    #    ``relay-pair`` would pair them too) and have exchanged versions.
    if d_iid not in _peer_ids(a):
        # :func:`_pair_two`, but each call waits out a 429 on its own: the
        # ``/api/pairing/*`` bucket is 5 calls / 60 s, and restarting the
        # whole handshake after a refused confirm would accept twice.
        # Patient calls: run soon after ``pair``, a's /api/pairing/* bucket
        # may still be full — each call waits out its 429 (Retry-After or
        # a bounded backoff) and is retried as is, never the whole handshake.
        st, qr = _as_patient(state, "a", "POST", "/api/pairing/initiate")
        qr = _must("initiate(a→d)", st, qr, ok=(201,))
        st, ack = _as_patient(state, "d", "POST", "/api/pairing/accept", qr)
        ack = _must("accept(d)", st, ack)
        st, conf = _as_patient(
            state,
            "a",
            "POST",
            "/api/pairing/confirm",
            {"token": ack["token"], "verification_code": ack["verification_code"]},
        )
        _must("confirm(a→d)", st, conf)
        print("  paired a ↔ d")
    _poll_rows(
        "a",
        "SELECT proto_version FROM remote_instances WHERE id=?",
        (d_iid,),
        lambda rows: bool(rows) and int(rows[0][0] or 0) >= 57,
        what="a never learned that d runs v_57",
        timeout=120.0,
    )
    st, inv = _as(
        state,
        "a",
        "POST",
        f"/api/spaces/{space_id}/remote-invites",
        {"invitee_instance_id": d_iid, "invitee_user_id": d["user_id"]},
    )
    _must("a invites d", st, inv, ok=(201,))
    time.sleep(4)
    _accept_remote_invite(state, "d", space_id)
    _poll_rows(
        "d",
        "SELECT gfs_servers FROM spaces WHERE id=?",
        (space_id,),
        lambda rows: bool(rows) and rows[0][0] and rows[0][0].count("inbox_url") == 2,
        what="d never stored the owner's two-server list",
        timeout=90.0,
    )
    _poll_rows(
        "d",
        "SELECT 1 FROM space_keys WHERE space_id=? AND writer_key IS NOT NULL",
        (space_id,),
        bool,
        what="d never got the writer key of the trusted space (v_57)",
        timeout=90.0,
    )
    print("  d joined: holds the owner's list (A + B) and the writer key ✓")

    # 3. d restarts treating loopback as public (B is on 127.0.0.1).
    _kill_household("d", d)
    state["instances"]["d"]["pid"] = _spawn_loopback_public("d", d["port"])
    _save(state)
    _wait_ready(d["port"])
    print("  d restarted (loopback treated as public — harness-only shim) ✓")

    # 4. e follows on B only.
    e_a_id = state["gfs"]["pairings"]["e"]["id"]
    st, body = _as(state, "e", "DELETE", f"/api/gfs/connections/{e_a_id}")
    _must("e leaves GFS A", st, body, ok=(200, 204))
    _as(state, "e", "POST", "/api/public_spaces/refresh")
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        st, listing = _as(state, "e", "GET", "/api/public_spaces")
        if st == 200 and isinstance(listing, list):
            if any(row.get("space_id") == space_id for row in listing):
                break
        _as(state, "e", "POST", "/api/public_spaces/refresh")
        time.sleep(3.0)
    else:
        raise SystemExit(
            f"gfs-multi-listing: e (on GFS B only) never discovered {space_id}"
        )
    st, sub = _as(state, "e", "POST", f"/api/spaces/{space_id}/subscribe")
    _must("e follows the space (via B)", st, sub)
    _poll_rows(
        "e",
        "SELECT 1 FROM space_keys WHERE space_id=?",
        (space_id,),
        bool,
        what="e never got the space's content key over B",
        timeout=90.0,
    )
    if _gfs_rows(
        "SELECT 1 FROM space_subscribers WHERE space_id=? AND instance_id=?",
        (space_id, e["instance_id"]),
    ):
        raise SystemExit("gfs-multi-listing: e holds a follower seat on GFS A")
    print("  e follows the space on GFS B only, content key in hand ✓")

    # 5. a offline; d posts, comments and reacts.
    _kill_household("a", a)
    b_off = _gfs_b_log_size()
    content = f"Posted through every listed GFS — {time.time_ns()}"
    st, post = _as(
        state,
        "d",
        "POST",
        f"/api/spaces/{space_id}/posts",
        {"type": "text", "content": content},
    )
    post_id = _must("d posts with the host offline", st, post, ok=(201,))["id"]
    comment_text = f"And a comment — {time.time_ns()}"
    st, comment = _as(
        state,
        "d",
        "POST",
        f"/api/spaces/{space_id}/posts/{post_id}/comments",
        {"content": comment_text},
    )
    comment_id = _must("d comments", st, comment, ok=(200, 201))["id"]
    st, body = _as(
        state,
        "d",
        "POST",
        f"/api/spaces/{space_id}/posts/{post_id}/reactions",
        {"emoji": "🛰"},
    )
    _must("d reacts", st, body, ok=(200, 201))

    # 6. e receives everything via B.
    _poll_rows(
        "e",
        "SELECT content FROM space_posts WHERE id=?",
        (post_id,),
        lambda rows: bool(rows) and rows[0][0] == content,
        what=f"e never received d's post {post_id} over GFS B",
        timeout=90.0,
    )
    _poll_rows(
        "e",
        "SELECT content FROM space_post_comments WHERE id=? AND deleted=0",
        (comment_id,),
        lambda rows: bool(rows) and rows[0][0] == comment_text,
        what="e never received d's comment over GFS B",
        timeout=90.0,
    )
    _poll_rows(
        "e",
        "SELECT reactions FROM space_posts WHERE id=?",
        (post_id,),
        lambda rows: bool(rows) and "🛰" in (rows[0][0] or ""),
        what="e never received d's reaction over GFS B",
        timeout=90.0,
    )
    print("  e (on B only) received d's post, comment and reaction ✓")
    if _gfs_b_rows("SELECT 1 FROM client_instances WHERE instance_id=?", (d_iid,)):
        raise SystemExit("gfs-multi-listing: GFS B holds a client row for d")
    b_log = _gfs_b_log_text(b_off)
    if "POST /gfs/member-publish-anon" not in b_log:
        raise SystemExit("gfs-multi-listing: no anonymous member publish reached GFS B")
    if "POST /gfs/member-publish HTTP" in b_log:
        raise SystemExit(
            "gfs-multi-listing: an IDENTIFIED member publish reached GFS B"
        )
    for leak in (*d_leaks, content, comment_text):
        if leak in b_log:
            raise SystemExit(
                f"gfs-multi-listing: GFS B's log names d or the content ({leak[:16]!r})"
            )
    print(
        "  GFS B: no client row for d, only anonymous publishes, and its log "
        "names neither d nor the content ✓"
    )

    # 7. Clean-up for the rest of the chain.
    _kill_household("d", d)
    state["instances"]["d"]["pid"] = _spawn("d", d["port"])
    state["instances"]["a"]["pid"] = _spawn("a", a["port"])
    _save(state)
    _wait_ready(d["port"])
    _wait_ready(a["port"])
    again = _pair_with(state, "e", a_url)
    # A fresh connection queues every relayed envelope until its socket is
    # up: the next step must not race it.
    _wait_gfs_connected(state, "e", a_url)
    state["gfs"]["pairings"]["e"] = {
        "id": again["id"],
        "gfs_instance_id": again["gfs_instance_id"],
        "status": again["status"],
    }
    state["gfs_multi_listing"] = {
        "space_id": space_id,
        "post_id": post_id,
        "e_b_connection": e_b["id"],
    }
    _save(state)
    print("gfs-multi-listing: ok (a member reached every listed GFS anonymously)")


def _check_multi_listing(state: dict, failures: list[str]) -> None:
    """v_57 tripwire for ``verify``: d still holds the trusted space's writer
    key and the owner's two-server list; e still holds d's post."""
    info = state["gfs_multi_listing"]
    space_id = info["space_id"]
    if not _rows(
        "d",
        "SELECT 1 FROM space_keys WHERE space_id=? AND writer_key IS NOT NULL",
        (space_id,),
    ):
        failures.append("d: no writer key for the trusted multi-GFS space (v_57)")
    rows = _rows("d", "SELECT gfs_servers FROM spaces WHERE id=?", (space_id,))
    if not rows or not rows[0][0] or rows[0][0].count("inbox_url") != 2:
        failures.append("d: the owner's GFS list does not name both servers")
    if not _rows("e", "SELECT 1 FROM space_posts WHERE id=?", (info["post_id"],)):
        failures.append("e: lost d's post from the multi-GFS step")


# ─── Step: gfs-private-channel-multi (v_57) ────────────────────────────────


def _gfs_connection_to(state: dict, label: str, base_url: str) -> dict:
    """``label``'s connection to the GFS at ``base_url`` — the existing one
    when it holds it (an earlier step connected it), else a fresh pairing."""
    st, rows = _as(state, label, "GET", "/api/gfs/connections")
    if st == 200 and isinstance(rows, list):
        want = base_url.rstrip("/")
        for row in rows:
            if (row.get("inbox_url") or "").rstrip("/") == want:
                return row
    return _pair_with(state, label, base_url)


def _wait_gfs_connected(
    state: dict, label: str, base_url: str, timeout: float = 60.0
) -> None:
    """Wait until ``label``'s WebSocket to the GFS at ``base_url`` is up —
    a connection made moments ago (an earlier step's clean-up re-pairs e
    with A) queues every relayed envelope until it is, and a link redeem
    through it would time out waiting for the issuer's answer."""
    want = base_url.rstrip("/")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st, rows = _as(state, label, "GET", "/api/gfs/connections")
        if st == 200 and isinstance(rows, list):
            if any(
                (r.get("inbox_url") or "").rstrip("/") == want and r.get("connected")
                for r in rows
            ):
                return
        time.sleep(2.0)
    raise SystemExit(
        f"gfs: {label}'s connection to {base_url} never came up within {timeout:.0f} s"
    )


def _poll_gfs_b_rows(sql: str, params: tuple, ok, *, what: str, timeout=60.0):
    """:func:`_poll_gfs_rows` against GFS B."""
    deadline = time.monotonic() + timeout
    rows: list[tuple] = []
    while time.monotonic() < deadline:
        rows = _gfs_b_rows(sql, params)
        if ok(rows):
            return rows
        time.sleep(3.0)
    raise SystemExit(
        f"gfs-private-channel-multi: {what} within {timeout:.0f} s (last rows {rows!r})"
    )


#: How long ``gfs-private-channel-multi`` lets the members' host-return
#: catch-up run (``HOST_RETURN_SYNC_DELAY_S`` = 15 s, plus the sync itself)
#: before it restarts d.
HOST_RETURN_SETTLE_S = 25.0


@_offline_step
def cmd_gfs_private_channel_multi() -> None:
    """v_57: a PRIVATE space's channel lives on the GFSs its OWNER picks, and
    a member reaches every one of them — anonymously at the ones it never
    connected to — while a GFS the owner did not pick never hears of it.

    Prereqs: the gfs chain (a, d, e on GFS A; a ↔ d paired) + ``gfs-b-up``;
    runs after ``gfs-multi-listing`` (reuses its B connections when there)
    and before ``verify``.

    1. a and e hold connections to A and B. a creates a TRUSTED private
       space, turns ``private_gfs`` on and picks **A only** as its channel
       server (``PUT /api/spaces/{id}/gfs-servers``, channel mode).
    2. e joins through a gfs link, d (paired) through an internal one. a
       registers the channel on A — B holds NO row for it, though a is
       connected to B and B can carry channels.
    3. a picks **A and B**. B now holds the channel, its trusted-mode writer
       key pinned (v_57); d holds the owner's two-server list.
    4. d restarts with loopback treated as public (B is on 127.0.0.1, see
       :data:`_LOOPBACK_IS_PUBLIC_PRELUDE`). e leaves A — it now reaches the
       channel over B only — and holds its seat on B.
    5. a goes offline. d (registered on A only) posts; e receives it over B.
    6. B: only anonymous ``publish-anon`` requests, no ``client_instances``
       row for d, and its log and database never name the space (id, name,
       authority key), d, or the post's text.
    7. Clean-up: a and d restart normally; e reconnects to A.
    """
    state = _load()
    if not state or not _gfs_alive(state):
        raise SystemExit("run 'gfs-up' + 'gfs-pair' first")
    if not _gfs_b_alive(state):
        raise SystemExit("run 'gfs-b-up' first")
    a, d, e = (state["instances"][x] for x in ("a", "d", "e"))
    a_base = f"http://127.0.0.1:{a['port']}"
    a_url = f"http://127.0.0.1:{GFS_PORT}"
    b_url = state["gfs_b"]["base_url"]
    d_iid = d["instance_id"]
    if d_iid not in _peer_ids(a):
        raise SystemExit(
            "gfs-private-channel-multi: a and d are not paired — run "
            "'gfs-multi-listing' (or 'relay-pair') first"
        )
    d_pk = _rows("d", "SELECT identity_public_key FROM instance_identity")[0][0]

    # A space this run created, so a failed run leaves nothing behind
    # for later steps' log audits (see _undo_private_channel_multi).
    created: dict = {}
    try:
        # 1. Connections, the space, the owner's first choice (A only).
        a_a = _gfs_connection_to(state, "a", a_url)
        a_b = _gfs_connection_to(state, "a", b_url)
        _gfs_connection_to(state, "e", a_url)
        _gfs_connection_to(state, "e", b_url)
        for label in ("a", "e"):
            for url in (a_url, b_url):
                _wait_gfs_connected(state, label, url)
        print("  a and e hold live connections to GFS A and GFS B ✓")
        space_name = f"Private on two GFSs {time.time_ns()}"
        st, space = _as(
            state,
            "a",
            "POST",
            "/api/spaces",
            {"name": space_name, "space_type": "private"},
        )
        space_id = _must("a creates a private space", st, space, ok=(200, 201))["id"]
        created["space_id"] = space_id
        space_pk = _rows(
            "a", "SELECT identity_public_key FROM spaces WHERE id=?", (space_id,)
        )[0][0]
        space_pk_b64 = (
            base64.urlsafe_b64encode(bytes.fromhex(space_pk)).rstrip(b"=").decode()
        )
        st, body = _as(
            state,
            "a",
            "PATCH",
            f"/api/spaces/{space_id}",
            {"features": {"private_gfs": True}},
        )
        _must("a turns private_gfs on", st, body)
        st, view = _as(state, "a", "GET", f"/api/spaces/{space_id}/gfs-servers")
        view = _must("a reads its channel picker", st, view)
        if view.get("mode") != "channel":
            raise SystemExit(f"gfs-private-channel-multi: picker answered {view!r}")
        st, body = _as(
            state,
            "a",
            "PUT",
            f"/api/spaces/{space_id}/gfs-servers",
            {"gfs_ids": [a_a["id"]]},
        )
        body = _must("a picks GFS A as the channel server", st, body)
        if body.get("listed") != [a_a["id"]]:
            raise SystemExit(
                f"gfs-private-channel-multi: owner choice answered {body!r}"
            )
        print("  a picked GFS A as the private channel's server ✓")

        # 2. Members; the channel on A only.
        _join_private_link(state, "e", space_id, a_base, via="gfs")
        _join_private_link(state, "d", space_id, a_base, via="internal")
        print("  e (gfs link) and d (internal link) joined ✓")
        rows = _poll_rows(
            "a",
            "SELECT gfs_channel_id FROM spaces WHERE id=?",
            (space_id,),
            lambda r: bool(r) and bool(r[0][0]),
            what="a never created the private space's channel",
        )
        channel_id = rows[0][0]
        _poll_gfs_rows(
            "SELECT 1 FROM gfs_channels WHERE channel_id=?",
            (channel_id,),
            bool,
            what="GFS A never registered the channel",
        )
        if _gfs_b_rows("SELECT 1 FROM gfs_channels WHERE channel_id=?", (channel_id,)):
            raise SystemExit(
                "gfs-private-channel-multi: GFS B holds the channel though the "
                "owner did not choose it"
            )
        print(f"  channel {channel_id} lives on A only — B never heard of it ✓")

        # 3. A and B.
        grants_before = _rows(
            "d",
            "SELECT epoch, gfs_channel FROM space_keys WHERE space_id=?",
            (space_id,),
        )
        st, body = _as(
            state,
            "a",
            "PUT",
            f"/api/spaces/{space_id}/gfs-servers",
            {"gfs_ids": [a_a["id"], a_b["id"]]},
        )
        body = _must("a picks GFS A and B", st, body)
        if sorted(body.get("listed") or []) != sorted([a_a["id"], a_b["id"]]):
            raise SystemExit(
                f"gfs-private-channel-multi: owner choice answered {body!r}"
            )
        _poll_gfs_b_rows(
            "SELECT writer_key_pk FROM gfs_channels WHERE channel_id=? "
            "AND publish_mode='trusted'",
            (channel_id,),
            lambda r: bool(r) and bool(r[0][0]),
            what="GFS B never got the channel with its trusted writer key pinned",
        )
        _poll_rows(
            "d",
            "SELECT gfs_servers FROM spaces WHERE id=?",
            (space_id,),
            lambda r: bool(r) and bool(r[0][0]) and r[0][0].count("inbox_url") == 2,
            what="d never stored the owner's two channel servers",
            timeout=90.0,
        )
        # The list and the re-issued grant (which names the servers d may
        # publish anonymously to) travel separately: the list with the
        # owner's config, the grant in the roster snapshot ``distribute``
        # sends after it. d killed between the two keeps a grant naming A
        # only and — correctly — never publishes to B. Wait for the grant.
        _poll_rows(
            "d",
            "SELECT epoch, gfs_channel FROM space_keys WHERE space_id=?",
            (space_id,),
            lambda r: bool(r) and r != grants_before,
            what="d never got the owner's re-issued channel grant naming A and B",
            timeout=90.0,
        )
        print(
            "  the channel lives on A and B (writer key pinned, trusted); d holds "
            "the two-server list and the grant naming both ✓"
        )

        # 4. d sees loopback as public; e reaches the channel over B only.
        _kill_household("d", d)
        state["instances"]["d"]["pid"] = _spawn_loopback_public("d", d["port"])
        _save(state)
        _wait_ready(d["port"])
        e_a_id = state["gfs"]["pairings"]["e"]["id"]
        e_leave_off = _log_size("e")
        st, body = _as(state, "e", "DELETE", f"/api/gfs/connections/{e_a_id}")
        _must("e leaves GFS A", st, body, ok=(200, 204))
        # Its push socket to A closes with the connection — not at the next
        # 30 s reconcile, while A could still hand it the channel's items.
        stopped = f"gfs.ws.supervisor: stopped client gfs_id={e_a_id}"
        deadline = time.monotonic() + 5.0
        while not _log_lines_matching("e", stopped, offset=e_leave_off):
            if time.monotonic() >= deadline:
                raise SystemExit(
                    "gfs-private-channel-multi: e's socket to GFS A stayed open "
                    "after e removed the connection"
                )
            time.sleep(0.2)
        _poll_gfs_b_rows(
            "SELECT instance_id FROM gfs_channel_subscribers WHERE channel_id=?",
            (channel_id,),
            lambda r: e["instance_id"] in {x[0] for x in r},
            what="e never took its seat on GFS B",
            timeout=90.0,
        )
        print(
            "  d restarted (loopback public — harness shim); e is on B only, seated there ✓"
        )

        # 5. Host offline; d posts through every channel server.
        _kill_household("a", a)
        b_off = _gfs_b_log_size()
        e_off = _log_size("e")
        content = f"Private channel, two GFSs, host offline — {time.time_ns()}"
        st, post = _as(
            state,
            "d",
            "POST",
            f"/api/spaces/{space_id}/posts",
            {"type": "text", "content": content},
        )
        post_id = _must("d posts with the host offline", st, post, ok=(201,))["id"]
        _poll_rows(
            "e",
            "SELECT content FROM space_posts WHERE id=?",
            (post_id,),
            lambda r: bool(r) and r[0][0] == content,
            what=f"e never received d's post {post_id} over GFS B",
            timeout=90.0,
        )
        if not _log_lines_matching(
            "e", f"gfs.relay.received: channel={channel_id}", offset=e_off
        ):
            raise SystemExit(
                "gfs-private-channel-multi: e logged no channel frame — d's post "
                "reached it some other way"
            )
        print("  e (on B only) received d's post over the channel, host offline ✓")

        # 6. What B saw.
        if _gfs_b_rows("SELECT 1 FROM client_instances WHERE instance_id=?", (d_iid,)):
            raise SystemExit(
                "gfs-private-channel-multi: GFS B holds a client row for d"
            )
        b_log = _gfs_b_log_text(b_off)
        if "POST /gfs/channels/publish-anon" not in b_log:
            raise SystemExit(
                "gfs-private-channel-multi: no anonymous channel publish reached GFS B"
            )
        if "POST /gfs/channels/publish HTTP" in b_log:
            raise SystemExit(
                "gfs-private-channel-multi: an IDENTIFIED channel publish reached GFS B"
            )
        whole_log = _gfs_b_log_text(0)
        db_bytes = b"".join(
            p.read_bytes() for p in GFS_B_DIR.glob("gfs.db*") if p.is_file()
        )
        d_leaks = (
            d_iid,
            d_pk,
            base64.urlsafe_b64encode(bytes.fromhex(d_pk)).decode().rstrip("="),
        )
        for needle in (space_id, space_name, space_pk, space_pk_b64, content):
            if needle in whole_log or needle.encode() in db_bytes:
                raise SystemExit(
                    f"gfs-private-channel-multi: GFS B saw the space or the post "
                    f"({needle[:24]!r}…)"
                )
        for needle in d_leaks:
            if needle in b_log:
                raise SystemExit(
                    f"gfs-private-channel-multi: GFS B's log names d ({needle[:16]!r})"
                )
        print(
            "  GFS B: anonymous publishes only, no row for d, never the space, "
            "d or the text ✓"
        )

        # 7. Clean-up for the rest of the chain — in an order that lets the
        #    returning host's catch-up run against peers that stay put:
        #    a. e back on A FIRST (e's relay route to a goes through A: a host
        #       that returns while e is on B only asks e for a sync whose
        #       chunks e cannot ship);
        again = _pair_with(state, "e", a_url)
        # A fresh connection queues every relayed envelope until its socket is
        # up: the next step must not race it.
        _wait_gfs_connected(state, "e", a_url)
        #    b. a back, until it caught d's post up (its sync with d ran);
        state["instances"]["a"]["pid"] = _spawn("a", a["port"])
        _save(state)
        _wait_ready(a["port"])
        _poll_rows(
            "a",
            "SELECT 1 FROM space_posts WHERE id=?",
            (post_id,),
            bool,
            what="a never caught d's post up after coming back",
            timeout=90.0,
        )
        #    c. let d's own host-return catch-up finish (it waits 15 s), then
        #       restart d normally — a restart mid-sync leaves a's answer to
        #       a request d no longer knows.
        time.sleep(HOST_RETURN_SETTLE_S)
        _kill_household("d", d)
        state["instances"]["d"]["pid"] = _spawn("d", d["port"])
        _save(state)
        _wait_ready(d["port"])
        state["gfs"]["pairings"]["e"] = {
            "id": again["id"],
            "gfs_instance_id": again["gfs_instance_id"],
            "status": again["status"],
        }
        state["gfs_private_channel_multi"] = {
            "space_id": space_id,
            "post_id": post_id,
            "channel_id": channel_id,
        }
        _save(state)
        print(
            "gfs-private-channel-multi: ok (the owner's channel servers only; a "
            "member reached B anonymously)"
        )
    except SystemExit:
        _undo_private_channel_multi(state, created)
        raise


def _undo_private_channel_multi(state: dict, created: dict) -> None:
    """Best-effort clean-up after a failed ``gfs-private-channel-multi``: put
    e back on GFS A first (its relay route to a goes through A), bring a and
    d back up (normal boot) if the step had stopped them, and dissolve the
    space it created (a solo-admin dissolve executes at once and tells its
    members), so the next run starts clean."""
    a_url = f"http://127.0.0.1:{GFS_PORT}"
    try:
        conn = _gfs_connection_to(state, "e", a_url)
        state["gfs"]["pairings"]["e"] = {
            "id": conn["id"],
            "gfs_instance_id": conn["gfs_instance_id"],
            "status": conn["status"],
        }
        _save(state)
    except SystemExit:
        pass
    for label in ("a", "d"):
        info = state["instances"][label]
        if not _alive(info.get("pid") or 0):
            info["pid"] = _spawn(label, info["port"])
            _save(state)
            try:
                _wait_ready(info["port"])
            except Exception:
                pass
    space_id = created.get("space_id")
    if space_id:
        st, _ = _as(state, "a", "DELETE", f"/api/spaces/{space_id}")
        print(f"  clean-up: dissolved the step's space {space_id} (HTTP {st})")


def _check_private_channel_multi(state: dict, failures: list[str]) -> None:
    """v_57 tripwire for ``verify``: d still holds the owner's two channel
    servers, e still holds d's post, and GFS B still never stored the
    space."""
    info = state["gfs_private_channel_multi"]
    space_id = info["space_id"]
    rows = _rows("d", "SELECT gfs_servers FROM spaces WHERE id=?", (space_id,))
    if not rows or not rows[0][0] or rows[0][0].count("inbox_url") != 2:
        failures.append("d: the owner's private channel list does not name both GFSs")
    if not _rows("e", "SELECT 1 FROM space_posts WHERE id=?", (info["post_id"],)):
        failures.append("e: lost d's post from the private multi-GFS step")
    db_bytes = b"".join(
        p.read_bytes() for p in GFS_B_DIR.glob("gfs.db*") if p.is_file()
    )
    if space_id.encode() in db_bytes:
        failures.append("GFS B: its database names the private space")


# ─── Step: pair ────────────────────────────────────────────────────────────


def _pair_two(state: dict, initiator: str, scanner: str) -> None:
    a = state["instances"][initiator]
    b = state["instances"][scanner]

    s, qr = _request(
        f"http://127.0.0.1:{a['port']}/api/pairing/initiate",
        token=a["token"],
        method="POST",
    )
    _must(f"initiate({initiator})", s, qr, ok=(201,))
    s, ack = _request(
        f"http://127.0.0.1:{b['port']}/api/pairing/accept",
        token=b["token"],
        method="POST",
        body=qr,
    )
    _must(f"accept({scanner})", s, ack)
    s, conf = _request(
        f"http://127.0.0.1:{a['port']}/api/pairing/confirm",
        token=a["token"],
        method="POST",
        body={"token": ack["token"], "verification_code": ack["verification_code"]},
    )
    _must(f"confirm({initiator})", s, conf)
    print(f"  paired {initiator} ↔ {scanner}")


def cmd_pair() -> None:
    """Pair the inner ring (a/b/c) pairwise, plus d↔b.

    ``d`` deliberately stays unpaired with ``a`` — :func:`cmd_relay_pair`
    finishes the job through the §11 trust-relay flow.
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' first")

    for initiator, scanner in (
        ("a", "b"),
        ("b", "c"),
        ("a", "c"),
        ("b", "d"),
    ):
        _pair_two(state, initiator, scanner)

    # Settle: peer-confirm + initial peer-directory snapshots.
    time.sleep(3)
    expected = {
        "a": 2,  # b, c
        "b": 3,  # a, c, d
        "c": 2,  # a, b
        "d": 1,  # b only — a-via-relay lands later
        # e pairs with NOBODY. Not an omission: the §D2b bootstrap redeem
        # in ``gfs-invite-link`` only runs when neither a direct pair nor
        # a mesh route reaches the issuer, and a mesh route is a chain of
        # CONFIRMED peers. One QR handshake here and that step quietly
        # degrades into the ordinary ``SPACE_INVITE_TOKEN_REDEEM`` path.
        "e": 0,
    }
    # Read through the ``/api/connections`` alias (same view, same shape):
    # every ``/api/pairing/*`` call — the scanner's accept included, since
    # it requires the admin's token — shares one 5 / 60 s bucket per user,
    # and b has just spent all five (accept a↔b, initiate b↔c + b↔d,
    # confirm ×2). A read on ``/api/pairing/connections`` here 429s.
    for label, info in state["instances"].items():
        s, conns = _request(
            f"http://127.0.0.1:{info['port']}/api/connections",
            token=info["token"],
        )
        _must(f"connections({label})", s, conns)
        confirmed = [c for c in conns if c["status"] == "confirmed"]
        if len(confirmed) != expected[label]:
            raise SystemExit(
                f"{label}: expected {expected[label]} confirmed peers, "
                f"got {len(confirmed)} "
                f"({[c['display_name'] for c in conns]})"
            )
    print("pair: ok (a↔b, b↔c, a↔c, b↔d; e stays unpaired by design)")


def cmd_relay_pair() -> None:
    """Auto-pair a ↔ d via b (§11 simple-pairing / trust-relay flow).

    1. Alpha asks Beta to vouch for an introduction to Delta:
       ``POST /api/pairing/auto-pair-via {via_instance_id, target_instance_id}``.
       Beta forwards the request to Delta over federation — no admin
       click needed on Beta's side.
    2. Delta's admin sees the pending request in
       ``GET /api/pairing/auto-pair-requests`` and approves it via
       ``POST /api/pairing/auto-pair-requests/{id}/approve`` —
       one-click, no QR scan.
    3. After approval the pair lands on both Alpha and Delta as
       ``CONFIRMED`` and the peer-directory snapshot kicks in.

    The QR step in :func:`cmd_pair` already burned much of Alpha's
    ``/api/pairing/*`` rate-limit budget (5 / 60 s per user); we wait
    for the bucket to drain before issuing the auto-pair-via. Without
    this the very first request returns 429.
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' first")

    a = state["instances"]["a"]
    d = state["instances"]["d"]
    b = state["instances"]["b"]

    print("  waiting 65 s for /api/pairing/* rate-limit window to drain...")
    time.sleep(65)

    s, _resp = _request(
        f"http://127.0.0.1:{a['port']}/api/pairing/auto-pair-via",
        token=a["token"],
        method="POST",
        body={
            "via_instance_id": b["instance_id"],
            "target_instance_id": d["instance_id"],
            "target_display_name": d["name"],
        },
    )
    # A previous run of this step may have completed the pairing and then
    # failed on a later assertion; the request endpoint answers 422
    # "already paired" from then on. Treat that as "the pair we want is
    # already in place" and fall through to the assertions, so re-running
    # the step after a flake is actually possible (it was not: every
    # retry died here).
    already_paired = s == 422 and "already paired" in str(_resp)
    if already_paired:
        print("  a ↔ d already trust-relay paired — re-checking assertions")
    else:
        _must("auto-pair-via(a→d)", s, _resp, ok=(202,))
        print(f"  a → request_via(b, d): 202 {_resp}")

    # Give the federated request a beat to land in d's inbox. Polling
    # interval is 2 s — the auto-pair-requests endpoint is rate-limited
    # so faster polls trip 429.
    # Skipped on a re-run: with the pair already in place there is no
    # pending request to approve, and this poll would just burn 30 s and
    # then fail.
    if not already_paired:
        deadline = time.monotonic() + 30.0
        request_id: str | None = None
        while time.monotonic() < deadline:
            s, inbox = _request(
                f"http://127.0.0.1:{d['port']}/api/pairing/auto-pair-requests",
                token=d["token"],
            )
            if s == 200:
                items = inbox if isinstance(inbox, list) else (inbox.get("items") or [])
                if items:
                    request_id = items[0]["request_id"]
                    break
            time.sleep(2.0)
        if request_id is None:
            raise SystemExit("d's auto-pair inbox stayed empty after 30s")

        s, _resp = _request(
            f"http://127.0.0.1:{d['port']}/api/pairing/auto-pair-requests/"
            f"{request_id}/approve",
            token=d["token"],
            method="POST",
        )
        _must("auto-pair approve(d)", s, _resp)
        print(f"  d approves request {request_id}")

    # Settle: d's approval is stored ``pending_sent`` — not confirmed —
    # until a, having confirmed on the ack (relayed through b), sends its
    # first envelope (its on-pair ``INSTANCE_CAPABILITIES_UPDATED``); that
    # envelope confirms d's row, and d then announces itself back. Both
    # hops can ride the outbox on a 5/10/20 s backoff. Sleep past the
    # first retry slot before checking, so we hit each token's
    # ``/api/pairing/*`` bucket only once for this step (the bucket is 5
    # calls per 60 s; we've already spent ~2 on the request/approve
    # round).
    state["relay_pair_ran"] = True
    _save(state)
    # ``INSTANCE_CAPABILITIES_UPDATED`` rides the outbox on the 5/10/20 s
    # backoff described above, so the peer's ``proto_version`` appears
    # some seconds AFTER the pair confirms. This used to be one read
    # after a flat ``time.sleep(10)`` — a fixed wait landing mid-backoff,
    # which failed the step intermittently with "stuck at
    # proto_version=1" while the capabilities event was merely in
    # flight.
    #
    # Retry instead of sleeping longer, but retry SPARINGLY: every
    # ``/api/pairing/*`` path — reads included — shares one bucket of
    # 5 requests / 60 s (``build_rate_limit_middleware`` in
    # ``socialhome/app.py``), and this step has already spent calls on
    # auto-pair-via / the inbox poll / approve. Three widely-spaced
    # reads land at ~25 s, ~45 s and ~65 s after the approve, which
    # covers the whole 5/10/20 backoff while staying inside the bucket.
    # A 429 means the bucket is empty, not that federation failed, so
    # it is skipped rather than raised on.
    for label, peer in (("a", d["instance_id"]), ("d", a["instance_id"])):
        info = state["instances"][label]
        pv = 1
        confirmed = False
        last: object = None
        for delay in (25.0, 20.0, 20.0):
            time.sleep(delay)
            s, conns = _request(
                f"http://127.0.0.1:{info['port']}/api/connections",
                token=info["token"],
            )
            if s == 429:
                print(f"  connections({label}): 429 — bucket empty, retrying")
                continue
            _must(f"connections({label})", s, conns)
            match = [c for c in conns if c["instance_id"] == peer]
            last = match
            if match and match[0]["status"] == "confirmed":
                confirmed = True
                pv = int(match[0].get("proto_version") or 1)
                if pv >= 2:
                    break
        if not confirmed:
            raise SystemExit(
                f"{label} → {peer[:8]}: expected confirmed, got {last!r}",
            )
        if pv < 2:
            raise SystemExit(
                f"{label} → {peer[:8]}: relay-paired peer stuck at "
                f"proto_version={pv} after ~65 s — "
                "INSTANCE_CAPABILITIES_UPDATED never landed on the "
                "trust-relay path",
            )
        print(f"  {label} sees {peer[:8]} at proto_version={pv} (trust-relay) ✓")
    print("relay-pair: ok (a ↔ d confirmed via b)")


# ─── Step: traffic ─────────────────────────────────────────────────────────


def cmd_traffic() -> None:
    """Post one item of each public type from every household."""
    state = _load()
    if not state:
        raise SystemExit("run 'up' first")

    # Momentum follows (a → b, c → b) are issued first so Beta's moment
    # later in the loop fans out to Alpha + Carol as inbox recipients.
    # The follow itself federates as ``USER_FOLLOW`` and Beta's
    # ``moment_follows`` mirror picks it up before the moment is posted.
    state["moment_follows"] = {}
    for follower_label in ("a", "c"):
        follower = state["instances"][follower_label]
        s, _r = _request(
            f"http://127.0.0.1:{follower['port']}/api/moments/follows",
            token=follower["token"],
            method="POST",
            body={
                "user_id": state["instances"]["b"]["user_id"],
                "instance_id": state["instances"]["b"]["instance_id"],
            },
        )
        # 200/201/204 all valid (depending on follow-create vs idempotent
        # re-follow); 409 means we're already following from a prior run
        # of ``traffic`` against a re-used /tmp/sh-demo.
        if s in (200, 201, 204, 409):
            state["moment_follows"][follower_label] = "b"
            print(f"  {follower_label} now follows b on momentum")
        else:
            print(f"  {follower_label} → b follow FAILED: {s} {_r}")
    # Settle the follow before any household posts a moment so Beta's
    # ``moment_follows`` mirror is populated when ``MOMENT_CREATED``
    # fans out.
    if state["moment_follows"]:
        time.sleep(2)

    state.setdefault("moments", {})
    for label, info in state["instances"].items():
        port, token, user = info["port"], info["token"], info["username"]
        url = f"http://127.0.0.1:{port}"

        s, _ = _request(
            f"{url}/api/me",
            token=token,
            method="PATCH",
            body={
                "display_name": f"{user.title()} ({info['name']})",
                "bio": f"Hello from {info['name']}",
            },
        )
        _must(f"profile({label})", s, _)

        s, _ = _request(
            f"{url}/api/feed/posts",
            token=token,
            method="POST",
            body={"content": f"[{label}] post — visible only inside {info['name']}"},
        )
        _must(f"post({label})", s, _, ok=(201,))

        # Moment audience defaults to households in the schema; rate-limited
        # to one per 15min, so a single moment per instance is all we need.
        moment_content = f"🌅 [{label}] moment from {info['name']}"
        s, m_resp = _request(
            f"{url}/api/moments",
            token=token,
            method="POST",
            body={"content": moment_content},
        )
        _must(f"moment({label})", s, m_resp, ok=(201, 429))
        if s == 201:
            # Strip the verify-side signature wrapper if it exists; the
            # signed payload nests the moment under ``data``.
            mp = m_resp.get("data") if isinstance(m_resp.get("data"), dict) else m_resp
            state["moments"][label] = {
                "id": mp.get("id"),
                "content": moment_content,
            }

        s, _ = _request(
            f"{url}/api/highlights/frames",
            token=token,
            method="POST",
            body={
                "media_url": _upload_demo_image(url, token=token, label=label),
                "frame_type": "image",
                "caption_text": f"[{label}] highlight — audience all_paired",
                "audience_kind": "all_paired",
            },
        )
        _must(f"highlight({label})", s, _, ok=(201,))

        print(f"  {label}: profile + post + moment + highlight queued")

    # Cross-household DMs: a → c (transit through pairwise federation).
    a = state["instances"]["a"]
    c = state["instances"]["c"]
    # Cross-household DM uses ``user_id`` so the DM service can resolve
    # Carol from her ``remote_users`` row (mirrored on Alpha when the
    # peer-directory snapshot from Gamma landed); ``username`` is
    # local-only.
    s, conv = _request(
        f"http://127.0.0.1:{a['port']}/api/conversations/dm",
        token=a["token"],
        method="POST",
        body={"user_id": c["user_id"]},
    )
    if s in (200, 201):
        state["dm_a_to_c"] = conv["id"]
        s, msg = _request(
            f"http://127.0.0.1:{a['port']}/api/conversations/{conv['id']}/messages",
            token=a["token"],
            method="POST",
            body={"content": "[a→c] hello carol from alice"},
        )
        if s in (200, 201):
            state["dm_msg_id"] = msg.get("id")
            print(f"  a→c DM created: conv={conv['id']}, msg={state['dm_msg_id']}")
        else:
            print(f"  a→c DM message FAILED: {s} {msg}")

        # v_3 cross-household media DM (a → c): upload a tiny WebP
        # via ``/api/media/upload``, send it as ``type='image'`` in
        # a follow-up DM, stash the message id. The verify step
        # checks that Carol's instance receives the preview
        # immediately + then the full bytes via DM_MEDIA_BLOB.
        webp_bytes = _make_demo_webp()
        s, up = _upload_file(
            f"http://127.0.0.1:{a['port']}/api/media/upload",
            token=a["token"],
            filename="alice.webp",
            content=webp_bytes,
            content_type="image/webp",
        )
        if s in (200, 201):
            s, mmsg = _request(
                f"http://127.0.0.1:{a['port']}/api/conversations/{conv['id']}/messages",
                token=a["token"],
                method="POST",
                body={
                    "type": "image",
                    "media_url": up["url"],
                    "file_name": "alice.webp",
                    "mime_type": "image/webp",
                    "file_size_bytes": len(webp_bytes),
                    "content": "",
                },
            )
            if s in (200, 201):
                state["dm_media_msg_id"] = mmsg.get("id")
                print(
                    f"  a→c media DM created: msg={state['dm_media_msg_id']} "
                    f"({len(webp_bytes)} bytes)",
                )
            else:
                print(f"  a→c media DM FAILED: {s} {mmsg}")
        else:
            print(f"  a→c media upload FAILED: {s} {up}")
    else:
        print(f"  a→c DM SKIPPED (create returned {s} {conv})")

    # Beta creates a space and invites alice + carol via remote-invite.
    b = state["instances"]["b"]
    s, space = _request(
        f"http://127.0.0.1:{b['port']}/api/spaces",
        token=b["token"],
        method="POST",
        body={
            "name": "Tri-household salon",
            "description": "All three houses",
            "join_mode": "invite_only",
        },
    )
    _must("space create(b)", s, space, ok=(201,))
    state["space_id"] = space["id"]
    print(f"  b: space {space['id']} created")

    for guest_label in ("a", "c"):
        guest = state["instances"][guest_label]
        s, inv = _request(
            f"http://127.0.0.1:{b['port']}/api/spaces/{space['id']}/remote-invites",
            token=b["token"],
            method="POST",
            body={
                "invitee_instance_id": guest["instance_id"],
                "invitee_user_id": guest["user_id"],
            },
        )
        _must(f"remote-invite({guest_label})", s, inv, ok=(201,))
        print(f"  b → {guest_label}: invite token issued")

    # Bazaar listing in the salon space — Beta posts a fixed-price item
    # to validate the bazaar surface boots inside a fresh space, and to
    # give Alpha something concrete to inquire about over DM in the
    # next step. The listing itself stays HFS-local (bazaar listings
    # are space-scoped); the *DM about the listing* is the federation
    # path under test.
    listing_title = "Vintage moka pot — barely used"
    s, listing = _request(
        f"http://127.0.0.1:{b['port']}/api/bazaar",
        token=b["token"],
        method="POST",
        body={
            "space_id": space["id"],
            "title": listing_title,
            "description": "Three-cup, brass-coloured. Pickup or shipping.",
            "mode": "fixed",
            "currency": "EUR",
            "price": 1500,
            "duration_days": 30,
        },
    )
    _must("bazaar create(b)", s, listing, ok=(201,))
    # Bazaar listings are keyed by ``post_id`` (not ``id``) — the
    # listing row composes onto the underlying post and shares its
    # primary key. The DM body quotes this value so verify can grep
    # for it on Beta's inbox.
    listing_id = listing["post_id"]
    state["bazaar_listing_id"] = listing_id
    state["bazaar_listing_title"] = listing_title
    print(f"  b: bazaar listing {listing_id[:8]}… created in salon")

    # Alpha → Beta DM *about the bazaar listing*. Bazaar's own DM-the-
    # seller flow is a SPA convenience (deep-link to the conversations
    # tab); on the wire it's a regular DM message that mentions the
    # listing id. We exercise the cross-household DM path: Alpha
    # creates the conversation against Beta's user_id (via federation
    # routing through the b↔a peer link) and posts a text message
    # quoting the listing title + id so the verify step has a stable
    # needle to grep for in Beta's inbox.
    s, conv_ab = _request(
        f"http://127.0.0.1:{a['port']}/api/conversations/dm",
        token=a["token"],
        method="POST",
        body={"user_id": b["user_id"]},
    )
    if s in (200, 201):
        state["dm_a_to_b"] = conv_ab["id"]
        msg_body = (
            f"[a→b] hi Bob — interested in your bazaar listing "
            f"{listing_id} ({listing_title!r}). still available?"
        )
        s, msg = _request(
            f"http://127.0.0.1:{a['port']}/api/conversations/{conv_ab['id']}/messages",
            token=a["token"],
            method="POST",
            body={"content": msg_body},
        )
        if s in (200, 201):
            state["dm_a_to_b_body"] = msg_body
            print(f"  a→b bazaar DM created: conv={conv_ab['id']}, msg={msg.get('id')}")
        else:
            print(f"  a→b bazaar DM message FAILED: {s} {msg}")
    else:
        print(f"  a→b bazaar DM SKIPPED (create returned {s} {conv_ab})")

    _save(state)


def cmd_calendar() -> None:
    """Cross-household space calendar + RSVP federation.

    Prereqs (run :func:`cmd_traffic` first):
    * Beta has a private space with pending remote-invites for Alice
      and Carol on it.

    Sequence:
    1. Alpha and Gamma fetch the inbound invite tokens via
       ``GET /api/remote_invites`` and accept them
       (``POST /api/remote_invites/{token}/accept``). Both households
       become space members on Beta's side.
    2. Beta creates a calendar event in the space
       (``POST /api/spaces/{id}/calendar/events``). The event
       federates as ``SPACE_CALENDAR_EVENT_CREATED`` to Alpha and Gamma.
    3. Alpha and Carol RSVP "going"
       (``POST /api/calendars/events/{id}/rsvp``). The RSVP federates
       back to Beta as ``SPACE_CALENDAR_RSVP``.
    4. Verify on Beta:
       ``GET /api/calendars/events/{id}/rsvps`` returns both Alpha's
       and Carol's user_ids with status="going".
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' first")
    if "space_id" not in state:
        raise SystemExit("run 'traffic' first — needs Beta's tri-household space")

    b = state["instances"]["b"]
    space_id = state["space_id"]

    # 1. Each guest accepts their pending remote-invite.
    for guest_label in ("a", "c"):
        guest = state["instances"][guest_label]
        s, invites = _request(
            f"http://127.0.0.1:{guest['port']}/api/remote_invites",
            token=guest["token"],
        )
        _must(f"remote_invites({guest_label})", s, invites)
        items = invites if isinstance(invites, list) else (invites.get("items") or [])
        target = next(
            (i for i in items if i.get("space_id") == space_id),
            None,
        )
        if target is None:
            raise SystemExit(
                f"{guest_label} has no pending invite for space {space_id}",
            )
        token = target["invite_token"]
        s, _r = _request(
            f"http://127.0.0.1:{guest['port']}/api/remote_invites/{token}/accept",
            token=guest["token"],
            method="POST",
        )
        _must(f"accept-invite({guest_label})", s, _r, ok=(204,))
        print(f"  {guest_label} accepted invite for space {space_id}")

    # Settle: SPACE_PRIVATE_INVITE_ACCEPT round-trips back to Beta and
    # seats the guest as a remote member.
    time.sleep(3)

    # 2. Beta creates a calendar event in the space — with an explicit
    #    IANA ``tz`` so the demo can assert the new v2 field rides
    #    through ``SPACE_CALENDAR_EVENT_CREATED`` to Alpha and Carol.
    start = "2027-01-15T18:00:00+00:00"
    end = "2027-01-15T20:00:00+00:00"
    s, ev = _request(
        f"http://127.0.0.1:{b['port']}/api/spaces/{space_id}/calendar/events",
        token=b["token"],
        method="POST",
        body={
            "summary": "Tri-household tabletop night",
            "start": start,
            "end": end,
            "description": "Bring snacks.",
            "tz": "Europe/Berlin",
        },
    )
    _must("calendar create(b)", s, ev, ok=(201,))
    event_id = ev["id"]
    state["calendar_event_id"] = event_id
    # Sanity: the host's own row carries the explicit tz immediately,
    # before any federation has happened. If this fails the bug is in
    # the create path, not the wire.
    if ev.get("tz") != "Europe/Berlin":
        print(
            f"  calendar create(b): host row tz={ev.get('tz')!r} (expected Europe/Berlin)"
        )
    print(f"  b: calendar event {event_id} created (tz=Europe/Berlin)")

    # Let SPACE_CALENDAR_EVENT_CREATED fan out to a + c.
    time.sleep(4)

    # 3. Alpha + Carol RSVP "going" — RSVP federates back to Beta.
    for guest_label in ("a", "c"):
        guest = state["instances"][guest_label]
        s, _r = _request(
            f"http://127.0.0.1:{guest['port']}/api/calendars/events/{event_id}/rsvp",
            token=guest["token"],
            method="POST",
            body={"status": "going"},
        )
        _must(f"rsvp({guest_label})", s, _r)
        print(f"  {guest_label} RSVP'd 'going'")

    # Settle: SPACE_CALENDAR_RSVP envelopes round-trip to Beta.
    time.sleep(4)

    # 4. Beta sees both RSVPs.
    s, rsvps = _request(
        f"http://127.0.0.1:{b['port']}/api/calendars/events/{event_id}/rsvps",
        token=b["token"],
    )
    _must("rsvps(b)", s, rsvps)
    rows = rsvps.get("rsvps") or []
    going = {r["user_id"]: r["status"] for r in rows if r["status"] == "going"}
    expected = {
        state["instances"]["a"]["user_id"],
        state["instances"]["c"]["user_id"],
    }
    missing = expected - set(going.keys())
    if missing:
        raise SystemExit(
            f"b: missing RSVPs from {sorted(missing)}; got {rows!r}",
        )
    print(f"  b sees {sorted(going.keys())} going ✓")

    # 5. Space timetable (v_39). Beta — the space's owner, so an admin —
    #    turns the space's ``timetable`` feature on (the toggle federates
    #    in ``space_meta.features`` via SPACE_CONFIG_CHANGED), creates a
    #    shared timetable and adds a lesson. Both edits fan out as
    #    SPACE_TIMETABLE_UPSERTED to Alpha and Gamma; ``verify`` reads it
    #    back from each member household and checks a member's write is
    #    refused there.
    s, _r = _request(
        f"http://127.0.0.1:{b['port']}/api/spaces/{space_id}",
        token=b["token"],
        method="PATCH",
        body={"features": {"timetable": True}},
    )
    _must("space features timetable=on(b)", s, _r)
    s, created = _request(
        f"http://127.0.0.1:{b['port']}/api/spaces/{space_id}/timetables",
        token=b["token"],
        method="POST",
        body={"name": "Klasse 5b", "template": "empty", "tz": "Europe/Berlin"},
    )
    _must("space timetable create(b)", s, created, ok=(201,))
    tt = created["timetable"]
    s, edited = _request(
        f"http://127.0.0.1:{b['port']}/api/spaces/{space_id}/timetables/"
        f"{tt['id']}/entries",
        token=b["token"],
        method="POST",
        body={
            "version": tt["version"],
            "weekday": 0,
            "start": "08:00",
            "end": "08:45",
            "title": "Mathe",
        },
    )
    _must("space timetable add lesson(b)", s, edited)
    state["space_timetable_id"] = tt["id"]
    state["space_timetable_version"] = edited["timetable"]["version"]
    print(
        f"  b: space timetable {tt['id'][:8]} created "
        f"(v{state['space_timetable_version']}, lesson 'Mathe')"
    )
    # Let SPACE_CONFIG_CHANGED + SPACE_TIMETABLE_UPSERTED fan out to a + c.
    time.sleep(4)

    # 6. Space task list + task (v_40). Beta creates a list and a task with
    #    priority, labels and a due date; both fan out (SPACE_TASK_LIST_
    #    CREATED, then SPACE_TASK_CREATED) to Alpha and Gamma, which must
    #    hold the list and the task with every field. Beta then lowers the
    #    priority and clears the due date — ``verify`` asserts a and c
    #    reflect that edit (an explicit null clears; labels stay).
    base = f"http://127.0.0.1:{b['port']}/api/spaces/{space_id}/tasks"
    s, lst = _request(
        f"{base}/lists", token=b["token"], method="POST", body={"name": "Umzug"}
    )
    _must("space task list create(b)", s, lst, ok=(201,))
    s, task = _request(
        f"{base}/lists/{lst['id']}/tasks",
        token=b["token"],
        method="POST",
        body={
            "title": "Pack the van",
            "priority": "high",
            "labels": ["Move", "Van"],
            "due_date": "2026-12-24",
        },
    )
    _must("space task create(b)", s, task, ok=(201,))
    state["space_task_list_id"] = lst["id"]
    state["space_task_id"] = task["id"]
    for guest_label in ("a", "c"):
        got = _space_task_on(state, guest_label, lst["id"], task["id"])
        want = {"priority": "high", "labels": ["Move", "Van"], "due_date": "2026-12-24"}
        if got is None or {k: got.get(k) for k in want} != want:
            raise SystemExit(
                f"{guest_label}: space task {task['id'][:8]} -> {got!r} "
                f"(expected list 'Umzug' + {want!r}) — SPACE_TASK_LIST_CREATED / "
                f"SPACE_TASK_CREATED did not round-trip"
            )
        print(f"  {guest_label} sees list 'Umzug' + task with priority/labels/due ✓")
    s, edited = _request(
        f"{base}/{task['id']}",
        token=b["token"],
        method="PATCH",
        body={"priority": "low", "due_date": None},
    )
    _must("space task edit(b)", s, edited)
    print(f"  b: space task {task['id'][:8]} -> priority=low, due_date cleared")
    time.sleep(3)
    _save(state)
    print("calendar: ok")


def _space_task_on(
    state: dict, viewer: str, list_id: str, task_id: str, *, tries: int = 10
) -> dict | None:
    """The space task as ``viewer`` holds it — ``None`` when its list or the
    task never arrived. Polls briefly for federation to settle."""
    info = state["instances"][viewer]
    base = f"http://127.0.0.1:{info['port']}/api/spaces/{state['space_id']}/tasks"
    for _ in range(tries):
        s, lists = _request(f"{base}/lists", token=info["token"])
        if s == 200 and any(
            row.get("id") == list_id and row.get("name") == "Umzug" for row in lists
        ):
            s, rows = _request(f"{base}/lists/{list_id}/tasks", token=info["token"])
            hit = (
                next((t for t in rows if t["id"] == task_id), None)
                if s == 200
                else None
            )
            if hit is not None:
                return hit
        time.sleep(1)
    return None

    _save(state)
    print("traffic: ok")


# ─── Step: verify ──────────────────────────────────────────────────────────


def _all_display_names(state: dict, viewer_label: str) -> dict[str, str]:
    """Return ``{user_id: display_name}`` everyone the viewer can see."""
    info = state["instances"][viewer_label]
    s, fr = _request(
        f"http://127.0.0.1:{info['port']}/api/friends",
        token=info["token"],
    )
    _must(f"friends({viewer_label})", s, fr)
    out: dict[str, str] = {}
    for m in fr["instance"]["members"]:
        out[m["user_id"]] = m["display_name"]
    for h in fr["households"]:
        for m in h["members"]:
            out[m["user_id"]] = m["display_name"]
    return out


def _highlight_captions(state: dict, viewer_label: str) -> set[str]:
    info = state["instances"][viewer_label]
    s, hls = _request(
        f"http://127.0.0.1:{info['port']}/api/highlights",
        token=info["token"],
    )
    _must(f"highlights({viewer_label})", s, hls)
    return {f["caption_text"] or "" for h in hls for f in h["frames"]}


def cmd_verify() -> None:
    """Assert each household sees the others' federated content."""
    state = _load()
    if not state:
        raise SystemExit("run 'up' first")

    failures: list[str] = []

    # 0. Capability version round-trip — every confirmed inner-ring peer
    #    must advertise the build's current ``OURS`` via
    #    INSTANCE_CAPABILITIES_UPDATED. This is the tripwire for a protocol
    #    bump that lands the constant but never propagates: e.g. v_15
    #    (SPACE_REMOTE_ADMIN_ACTION) — a remote admin's config/ban/archive
    #    forward gates on ``peer_supports(min_version=15)``, so if the peer
    #    is stuck at an older version the forward would raise instead of
    #    reaching the host. Latest gated event: v_20 (SPACE_SYNC_REJECTED) —
    #    a host only sends the reconnect-reconcile reply to a peer it sees at
    #    >= v_20, so this same tripwire guards that the backstop round-trips
    #    (a sub-v_20 peer silently falls back to the SPACE_DISSOLVED path).
    #    Latest gated event: v_29 (MIN_FOR_INVITE_BOOTSTRAP_REDEEM) — a
    #    redeemer refuses to attempt a §D2b bootstrap redeem against an
    #    issuer advertising less, because a sub-v_29 issuer has no handler
    #    for the envelope and the attempt would only burn a timeout. That
    #    one does NOT ride INSTANCE_CAPABILITIES_UPDATED: there is no peer
    #    row to read it off, which is exactly why the invite blob carries
    #    ``issuer_proto_version`` and the sealed redeem body carries the
    #    redeemer's. Block 0c below asserts that wire, on the seats the
    #    bootstrap actually created.
    from socialhome.domain.federation_capabilities import (
        OURS as _OURS,
        FederationCapability as _Cap,
    )

    # ``/api/pairing/*`` is rate limited to 5 calls per 60 s per instance
    # (``app.py``), and verify used to spend that budget re-fetching the
    # SAME list three times over — here, for the resync target, and again
    # in block 7 — which left ``share_home``'s two PATCHes to 429 on a
    # fast run. One fetch, reused by every reader that only needs the
    # snapshot; the polls that genuinely need fresh data (the transport
    # probe) still fetch their own.
    #
    # Every read in verify goes through the ``/api/connections`` alias
    # (same view, same shape) rather than ``/api/pairing/connections``:
    # the limiter keys on the ``/api/pairing`` prefix, and the steps
    # before verify (pair, relay-pair, share_home PATCHes) can leave b's
    # bucket empty — this fetch then 429'd, was skipped, and block 7
    # failed "b: no pairing-connections snapshot" on the first run.
    _conns: dict[str, list] = {}
    for viewer in ("a", "b", "c"):
        v = state["instances"][viewer]
        s, conns = _request(
            f"http://127.0.0.1:{v['port']}/api/connections",
            token=v["token"],
        )
        if s != 200 or not isinstance(conns, list):
            continue
        _conns[viewer] = conns
        for row in conns:
            if row.get("status") != "confirmed":
                continue
            pv = int(row.get("proto_version") or 1)
            peer = str(row.get("instance_id") or "")[:8]
            if pv < _OURS:
                failures.append(
                    f"{viewer}: confirmed peer {peer} at proto_version={pv} "
                    f"(< OURS={_OURS}) — capability bump didn't round-trip; "
                    f"SPACE_REMOTE_ADMIN_ACTION (v_15) would be gated off",
                )
            else:
                print(f"  {viewer} sees {peer} at proto_version={pv} (>= {_OURS}) ✓")

    # 0b-bis. v_53 shared-GFS route discovery (GFS_RELAY_PROBE / _ACK): the
    #     paired relay fallback only ever gets a route for a peer seen at
    #     >= MIN_FOR_GFS_RELAY_ROUTES, so the OURS round-trip above is also
    #     what proves every inner-ring pair is probe-able. Pin the threshold
    #     so a bump that moves it off v_53 fails here, not in the field.
    if int(_Cap.MIN_FOR_GFS_RELAY_ROUTES) != 53 or _OURS < 53:
        failures.append(
            "MIN_FOR_GFS_RELAY_ROUTES moved off v_53 (or OURS fell below it) "
            "— shared-GFS route discovery would never probe a peer",
        )
    else:
        print("  v_53 shared-GFS route discovery gate in place ✓")
    # v_54 — the GFS fallback switch for an existing pair: the key-wrap key
    # rides INSTANCE_CAPABILITIES_UPDATED. The OURS round-trip above proves
    # every pair advertises >= v_54; ``gfs-fallback-switch`` proves the key
    # exchange and the routes on the real wire.
    if int(_Cap.MIN_FOR_GFS_RELAY_KEY_EXCHANGE) != 54 or _OURS < 54:
        failures.append(
            "MIN_FOR_GFS_RELAY_KEY_EXCHANGE moved off v_54 (or OURS fell below "
            "it) — the GFS fallback switch would report every peer unavailable",
        )
    else:
        print("  v_54 GFS fallback key exchange gate in place ✓")
    # v_55 — space chat (SPACE_CHAT_* + the ``chat_messages`` catch-up). The
    # OURS round-trip above proves every pair advertises >= v_55, which is
    # what lets ``broadcast_to_space_members`` ship chat events to it at all;
    # a household below it is skipped silently. Pin the threshold so a bump
    # that moves it off v_55 fails here, not in the field.
    if int(_Cap.MIN_FOR_SPACE_CHAT) != 55 or _OURS < 55:
        failures.append(
            "MIN_FOR_SPACE_CHAT moved off v_55 (or OURS fell below it) — space "
            "chat events would never reach a member household",
        )
    else:
        print("  v_55 space chat gate in place ✓")
    # v_56 — §25.6 space sync streams calendar series as stored rows (rule
    # kept) and numbers its chunks; a requester below it gets the expanded
    # occurrences. The OURS round-trip above proves every pair advertises
    # >= v_56, so every demo household takes series rows. Pin the threshold.
    if int(_Cap.MIN_FOR_SYNC_SERIES_ROWS) != 56 or _OURS < 56:
        failures.append(
            "MIN_FOR_SYNC_SERIES_ROWS moved off v_56 (or OURS fell below it) — "
            "space sync would stream every household the expanded calendar"
        )
    else:
        print("  v_56 space-sync series rows gate in place ✓")
    # v_57 — members publish to every GFS on the owner's list (writer key in
    # trusted spaces, anonymous to servers they never joined). The OURS
    # round-trip above proves every pair advertises >= v_57. Pin it, and when
    # ``gfs-multi-listing`` ran, check what it left behind.
    # v_64 — a public moment's sealed, signed ``sent_at``: followers refuse a
    # replayed envelope. Nothing is gated between households (author → GFS →
    # follower); the OURS round-trip above proves every pair is at >= v_64,
    # and ``gfs-moment-profile`` delivers a moment that carries it.
    if int(_Cap.MIN_FOR_PUBLIC_MOMENT_SENT_AT) != 64 or _OURS < 64:
        failures.append(
            "MIN_FOR_PUBLIC_MOMENT_SENT_AT moved off v_64 (or OURS fell below "
            "it) — followers would take replayed public moments"
        )
    else:
        print("  v_64 signed send time on public moments in place ✓")
    # v_65 — authors choose how long followers keep their moments (1–30 d);
    # receivers clamp to 30 d and relay creates < 48 h old / deletes that
    # changed something. Nothing is gated between households; the OURS
    # round-trip above proves every pair is at >= v_65, and the deletes-stick
    # step (3c-ter) round-trips a 3-day expiry from c to d.
    if int(_Cap.MIN_FOR_LONG_LIVED_MOMENTS) != 65 or _OURS < 65:
        failures.append(
            "MIN_FOR_LONG_LIVED_MOMENTS moved off v_65 (or OURS fell below it) "
            "— receivers would keep any moment lifetime a peer signs"
        )
    else:
        print("  v_65 follower moment retention gate in place ✓")
    # v_66 — a moment's delete reaches every copy, even later: the mesh
    # outbox keeps a MOMENT_DELETED 30 days, and a GFS that proves
    # ``moment_delete_log`` keeps its signed certificate 30 days for followers
    # that were away. Nothing is gated between households; the OURS
    # round-trip above proves every pair is at >= v_66, the GFS must sign
    # both new capabilities, and ``gfs-moment-late-delete`` leaves a
    # certificate on the GFS.
    if int(_Cap.MIN_FOR_LATE_MOMENT_DELETES) != 66 or _OURS < 66:
        failures.append(
            "MIN_FOR_LATE_MOMENT_DELETES moved off v_66 (or OURS fell below it) "
            "— a delete would stop reaching a household away for days"
        )
    else:
        print("  v_66 late moment deletes gate in place ✓")
    # v_67 — reactions reach authors further away. Gated on
    # ``peer_supports(via, MIN_FOR_ROUTED_MOMENT_REACTIONS)``; the OURS
    # round-trip above proves every pair is at >= v_67, and step 3c-bis
    # carries d's reaction to c's moment two hops back.
    if int(_Cap.MIN_FOR_ROUTED_MOMENT_REACTIONS) != 67 or _OURS < 67:
        failures.append(
            "MIN_FOR_ROUTED_MOMENT_REACTIONS moved off v_67 (or OURS fell below "
            "it) — a reaction would be routed to a neighbour that can't open "
            "its return token"
        )
    else:
        print("  v_67 routed moment reactions gate in place ✓")
    # v_68 — reactions reach authors followed through a GFS. Gated on the
    # GFS's signed ``moment_reactions`` (checked below) and the author's
    # key-wrap key in its signed moment, never on ``peer_supports``; the OURS
    # round-trip above proves every pair is at >= v_68, and
    # ``gfs-moment-profile`` carries e's reaction to a through the GFS.
    if int(_Cap.MIN_FOR_GFS_MOMENT_REACTIONS) != 68 or _OURS < 68:
        failures.append(
            "MIN_FOR_GFS_MOMENT_REACTIONS moved off v_68 (or OURS fell below it) "
            "— reactions to GFS moments would stay in the follower's household"
        )
    else:
        print("  v_68 GFS moment reactions gate in place ✓")
    if state.get("gfs") and _gfs_alive(state):
        s, info = _request(f"http://127.0.0.1:{GFS_PORT}/gfs/info")
        caps = (info or {}).get("capabilities") or {} if s == 200 else {}
        if (
            caps.get("moment_delete_log") is not True
            or caps.get("moment_media_30d") is not True
            or not (info or {}).get("capabilities_sig")
        ):
            failures.append(
                f"GFS /gfs/info does not sign moment_delete_log + moment_media_30d "
                f"(HTTP {s}, caps={sorted(caps)!r})"
            )
        else:
            print("  v_66 GFS signs moment_delete_log + moment_media_30d ✓")
        if caps.get("moment_reactions") is not True:
            failures.append(
                f"GFS /gfs/info does not sign moment_reactions (HTTP {s}, "
                f"caps={sorted(caps)!r})"
            )
        else:
            print("  v_68 GFS signs moment_reactions ✓")
    # v_69 — everyone sees the full reaction count. The mesh event goes only
    # to v_69 peers (``peer_supports``); the GFS path is gated on the signed
    # ``moment_counts``. Step 3c-bis carries c's totals to d two hops away,
    # ``gfs-moment-profile`` a's totals to e through the GFS.
    if int(_Cap.MIN_FOR_MOMENT_REACTION_COUNTS) != 69 or _OURS < 69:
        failures.append(
            "MIN_FOR_MOMENT_REACTION_COUNTS moved off v_69 (or OURS fell below "
            "it) — totals would reach households that drop the event"
        )
    else:
        print("  v_69 reaction totals gate in place ✓")
    # v_70 — a highlight taken away stays away. A narrowing waits for every
    # household it takes people from to be at v_70; the OURS round-trip above
    # proves every pair is. ``highlight-audience`` narrows a's highlight away
    # from c and widens it back.
    if int(_Cap.MIN_FOR_HIGHLIGHT_AUDIENCE_CHANGE) != 70 or _OURS < 70:
        failures.append(
            "MIN_FOR_HIGHLIGHT_AUDIENCE_CHANGE moved off v_70 (or OURS fell "
            "below it) — a narrowing could reach households that forget it"
        )
    else:
        print("  v_70 highlight audience change gate in place ✓")
    # v_72 — an uninstalled app tells opponents their games ended: one
    # bundled ``APP_SESSION {verb: "close", reason: "ended", sessions}`` per
    # household, only to a peer at >= v_72 (``peer_supports``); an older one
    # gets the old declined / withdrawn close for pending invites only. The OURS round-trip above
    # proves every inner-ring pair advertises >= v_72, so an uninstall on any
    # of them sends the bundle. Pin the threshold so a bump that moves it
    # fails here, not in the field (the receiver rules are covered by
    # ``tests/protocol/test_app_session_scope.py``).
    if int(_Cap.MIN_FOR_APP_SESSION_ENDED) != 72 or _OURS < 72:
        failures.append(
            "MIN_FOR_APP_SESSION_ENDED moved off v_72 (or OURS fell below it) "
            "— an uninstall would send 'ended' to households that drop it"
        )
    else:
        print("  v_72 app games end on uninstall gate in place ✓")
    # v_73 — offers reach sellers in other households: the buyer's household
    # sends ``BAZAAR_OFFER_MADE`` to the seller's household only, the answer
    # (``BAZAAR_OFFER_UPDATED``) comes back to the offerer's only, both gated
    # on ``peer_supports`` (an older seller's household is sent nothing).
    # The OURS round-trip above proves every inner-ring pair advertises
    # >= v_73. Pin the threshold so a bump that moves it fails here; the
    # two-household round trip, the members-only delivery and every refusal
    # are the security tests in ``tests/protocol/test_bazaar_offer_federation.py``.
    if int(_Cap.MIN_FOR_BAZAAR_OFFERS) != 73 or _OURS < 73:
        failures.append(
            "MIN_FOR_BAZAAR_OFFERS moved off v_73 (or OURS fell below it) "
            "— an offer would reach households that drop it"
        )
    else:
        print("  v_73 bazaar offers to the seller's household gate in place ✓")
    # v_74 — "Ask the seller": one sealed ``BAZAAR_THREAD_MESSAGE`` to the
    # other person's household, only to a member household at >= v_74
    # (``space_member_supports``); an older seller's household is sent
    # nothing and the buyer's ask answers 409 ``SELLER_HOUSEHOLD_TOO_OLD``.
    # The OURS round-trip above proves every inner-ring pair advertises
    # >= v_74; ``bazaar-ask`` exercises the one button end to end (the
    # receiver rules are ``tests/protocol/test_bazaar_thread_federation.py``).
    if int(_Cap.MIN_FOR_BAZAAR_THREAD) != 74 or _OURS < 74:
        failures.append(
            "MIN_FOR_BAZAAR_THREAD moved off v_74 (or OURS fell below it) — a "
            "question would reach households that drop it"
        )
    else:
        print("  v_74 ask-the-seller gate in place ✓")
    ask = state.get("bazaar_ask")
    if ask:
        found = _rows(
            "c",
            "SELECT 1 FROM conversation_messages WHERE content = ?",
            (ask["text"],),
        )
        if not found:
            failures.append("bazaar-ask: c no longer holds a's question")
        else:
            print("  bazaar-ask: c still holds a's question ✓")
    # v_75 — a listing's questions and answers: one sealed
    # ``BAZAAR_LISTING_FAQ_UPDATED`` per change to every member household at
    # >= v_75. ``bazaar-faq`` exercises add / edit / remove end to end (the
    # receiver rules are ``tests/protocol/test_bazaar_faq_federation.py``).
    if int(_Cap.MIN_FOR_BAZAAR_FAQ) != 75 or _OURS < 75:
        failures.append(
            "MIN_FOR_BAZAAR_FAQ moved off v_75 (or OURS fell below it) — a "
            "listing's FAQ would reach households that drop it"
        )
    else:
        print("  v_75 listing FAQ gate in place ✓")
    # v_76 — group games: the ``group_*`` verbs of ``APP_SESSION`` between a
    # table's hub and each player's household at >= v_76. ``app-group``
    # runs a three-household table end to end (the receiver rules are
    # ``tests/protocol/test_app_group_federation.py``).
    if int(_Cap.MIN_FOR_APP_GROUPS) != 76 or _OURS < 76:
        failures.append(
            "MIN_FOR_APP_GROUPS moved off v_76 (or OURS fell below it) — a "
            "group table would reach households that drop it"
        )
    else:
        print("  v_76 group games gate in place ✓")
    table = state.get("app_group")
    if table:
        for label in ("a", "b", "c"):
            ended = _rows(
                label,
                "SELECT state FROM app_group_sessions WHERE session_id = ?",
                (table["session_id"],),
            )
            if [r[0] for r in ended] != ["ended"]:
                failures.append(f"app-group: {label} lost the closed table")
            else:
                print(f"  app-group: {label} still holds the closed table ✓")
    # v_78 — the gift draw: the ``draw_*`` verbs of ``APP_SESSION``.
    # ``app-draw`` runs a four-person draw over a, b, c end to end (the
    # tamper and privacy rules are ``tests/protocol/test_app_draw.py``).
    if int(_Cap.MIN_FOR_APP_ENGINES) != 78 or _OURS < 78:
        failures.append(
            "MIN_FOR_APP_ENGINES moved off v_78 (or OURS fell below it) — a draw "
            "would reach households that drop it"
        )
    else:
        print("  v_78 gift draw gate in place ✓")
    drawn = state.get("app_draw")
    if drawn:
        for label in ("a", "b", "c"):
            rows = _rows(
                label,
                "SELECT state_json FROM app_engine_state WHERE session_id = ? AND engine = 'draw'",
                (drawn["session_id"],),
            )
            if not rows or not rows[0][0] or '"enc"' not in rows[0][0]:
                failures.append(f"app-draw: {label} lost its sealed draw")
                continue
            leaked = [
                m
                for path in _instance_dir(label).glob("socialhome.db*")
                for m in drawn["marks"]
                if m.encode() in path.read_bytes()
            ]
            if leaked:
                failures.append(f"app-draw: a wish list is readable on {label}")
            else:
                print(f"  app-draw: {label} keeps its draw sealed ✓")
    # v_77 — games in a space: SPACE_APP_TABLE / SPACE_APP_SESSION to member
    # households with a writer seat at >= v_77, over the space's transport.
    if int(_Cap.MIN_FOR_SPACE_APP_TABLES) != 77 or _OURS < 77:
        failures.append(
            "MIN_FOR_SPACE_APP_TABLES moved off v_77 (or OURS fell below it) — a "
            "space table would reach households that drop it"
        )
    else:
        print("  v_77 space games gate in place ✓")
    duel = state.get("space_table")
    if duel:
        for label in ("c", "d"):
            held = _rows(
                label,
                "SELECT state FROM app_group_sessions WHERE session_id = ?",
                (duel["session_id"],),
            )
            if [r[0] for r in held] != ["playing"]:
                failures.append(f"space-table: {label} lost the duel ({held!r})")
            else:
                print(f"  space-table: {label} still holds the duel ✓")
        outside = [_instance_dir("a"), _instance_dir("b"), GFS_DIR]
        for folder in outside:
            for path in folder.glob("*.db*"):
                if duel["marker"].encode() in path.read_bytes():
                    failures.append(
                        f"space-table: the table's note is readable in {path}"
                    )
        print("  space-table: no non-member household and no GFS holds the table ✓")
    faq = state.get("bazaar_faq")
    if faq:
        for label in ("a", "b", "d"):
            held = _rows(
                label,
                "SELECT question FROM bazaar_listing_faq"
                " WHERE entry_id = ? AND removed = 0",
                (faq["entry_id"],),
            )
            if [r[0] for r in held] != [faq["question"]]:
                failures.append(f"bazaar-faq: {label} no longer holds c's FAQ entry")
            else:
                print(f"  bazaar-faq: {label} still holds c's FAQ entry ✓")
    hla = state.get("highlight_audience")
    if hla:
        stamps = _rows(
            "c",
            "SELECT removed_at, audience_at FROM highlight_stamps"
            " WHERE highlight_id = ?",
            (hla["highlight_id"],),
        )
        if hla["caption"] not in _highlight_captions(state, "c"):
            failures.append(
                "highlight-audience: c lost a's re-widened highlight "
                f"({hla['caption']!r})"
            )
        elif not stamps or not stamps[0][0] or not stamps[0][1] or not (
            stamps[0][1] > stamps[0][0]
        ):
            failures.append(
                "highlight-audience: c's stamp doesn't order the re-widen after "
                f"the removal ({stamps!r})"
            )
        else:
            print("  v_70 c removed, remembered and re-got a's highlight ✓")
    if state.get("gfs") and _gfs_alive(state):
        s, info = _request(f"http://127.0.0.1:{GFS_PORT}/gfs/info")
        caps = (info or {}).get("capabilities") or {} if s == 200 else {}
        if caps.get("moment_counts") is not True:
            failures.append(
                f"GFS /gfs/info does not sign moment_counts (HTTP {s}, "
                f"caps={sorted(caps)!r})"
            )
        else:
            print("  v_69 GFS signs moment_counts ✓")
    gmld = state.get("gfs_moment_late_delete")
    if gmld:
        held = _rows(
            "e",
            "SELECT deleted_at FROM moments WHERE id = ?",
            (gmld["moment_id"],),
        )
        certs = _gfs_rows(
            "SELECT author_user_id FROM gfs_moment_delete_log WHERE moment_id = ?",
            (gmld["moment_id"],),
        )
        if not held or held[0][0] is None:
            failures.append(f"e: deleted public reply still live ({held!r})")
        elif certs != [(gmld["user_id"],)]:
            failures.append(f"GFS delete certificate rows are {certs!r}")
        else:
            print(
                "  v_66 the GFS keeps the delete's certificate; e holds the tombstone ✓"
            )
    else:
        print("  (v_66 late-delete check skipped — run 'gfs-moment-late-delete')")
    if int(_Cap.MIN_FOR_MULTI_GFS_PUBLISH) != 57 or _OURS < 57:
        failures.append(
            "MIN_FOR_MULTI_GFS_PUBLISH moved off v_57 (or OURS fell below it) — "
            "members would get no writer key in a trusted space"
        )
    else:
        print("  v_57 multi-GFS member publish gate in place ✓")
    if state.get("gfs_multi_listing"):
        before = len(failures)
        _check_multi_listing(state, failures)
        if len(failures) == before:
            print(
                "  v_57 multi-GFS listing round-tripped (d keyed, e holds d's post) ✓"
            )
    else:
        print("  (v_57 multi-listing check skipped — run 'gfs-multi-listing')")
    if state.get("gfs_private_channel_multi"):
        before = len(failures)
        _check_private_channel_multi(state, failures)
        if len(failures) == before:
            print(
                "  v_57 private channel on owner-chosen GFSs round-tripped "
                "(d holds A + B, e holds d's post, B never stored the space) ✓"
            )
    else:
        print(
            "  (v_57 private multi-GFS check skipped — run 'gfs-private-channel-multi')"
        )
    # v_58 — a §25.6 ``members`` record's ``picture_hash: null`` from a v_58
    # provider that speaks for the member resets a stale per-space picture.
    # Receiver-side gate on the provider: the OURS round-trip above proves
    # every pair advertises >= v_58, so a host's reset is trusted. Pin it.
    if int(_Cap.MIN_FOR_MEMBER_PICTURE_RESET_SYNC) != 58 or _OURS < 58:
        failures.append(
            "MIN_FOR_MEMBER_PICTURE_RESET_SYNC moved off v_58 (or OURS fell "
            "below it) — a picture reset missed offline would never converge"
        )
    else:
        print("  v_58 member picture reset in space catch-up gate in place ✓")

    # 0c. v_29 round-trip on the §D2b BOOTSTRAP wire. The capability
    #     integer normally travels in INSTANCE_CAPABILITIES_UPDATED, over a
    #     peer row — and on this path there is no peer row yet, by
    #     definition. So v_29 rides two other fields instead: the invite
    #     blob's ``issuer_proto_version`` (which is what the redeemer gates
    #     its attempt on) and the sealed redeem body's ``proto_version``
    #     (which the issuer stamps on the seat it creates). Both land on the
    #     ``space_session`` rows, so asserting those two rows carry >= v_29
    #     is the tripwire for a bump that lands the constant but never
    #     reaches the one wire that cannot fall back to a peer row. Gated on
    #     ``gfs-invite-link`` having run.
    if state.get("gfs_invite_space_id"):
        _min_bootstrap = int(_Cap.MIN_FOR_INVITE_BOOTSTRAP_REDEEM)
        for holder, peer in (("a", "e"), ("e", "a")):
            try:
                _seat = _instance_row(holder, state["instances"][peer]["instance_id"])
            except Exception as exc:
                failures.append(f"{holder}: space_session row read failed: {exc!r}")
                continue
            if _seat is None:
                failures.append(
                    f"{holder}: the space_session seat for {peer} is gone — "
                    "gfs-invite-link seated it",
                )
            elif _seat["proto_version"] < _min_bootstrap:
                failures.append(
                    f"{holder}: the {peer} bootstrap seat reads proto_version="
                    f"{_seat['proto_version']} (< v_{_min_bootstrap}) — the "
                    "version did not ride the §D2b wire, so the redeemer's "
                    "MIN_FOR_INVITE_BOOTSTRAP_REDEEM gate is running on a "
                    "default rather than on what the issuer advertised",
                )
            elif _seat["proto_version"] < _OURS:
                failures.append(
                    f"{holder}: the {peer} bootstrap seat reads proto_version="
                    f"{_seat['proto_version']}, this build is at OURS={_OURS} "
                    "— the seat was stamped from a stale constant",
                )
            else:
                print(
                    f"  {holder}'s bootstrap seat for {peer} carries "
                    f"proto_version={_seat['proto_version']} "
                    f"(>= v_{_min_bootstrap}, == OURS) ✓"
                )
    else:
        print("  (v_29 bootstrap wire skipped — run 'gfs-invite-link')")

    # 0d. v_49 writer certificates on the §D2b wire. The host issued the
    #     link-joined household **e** its writer cert in the redeem ACK
    #     ``space_meta`` (and re-issues it on every content-key rotation);
    #     **e** keeps it on the epoch's ``space_keys`` row. Assert **e**
    #     holds at least one cert for the invite space that verifies against
    #     the pinned space key, names e's OWN identity key and grants
    #     ``write`` — the round-trip a v_49 bump must deliver.
    if state.get("gfs_invite_space_id"):
        _check_writer_cert_on_e(state, failures, _Cap)
    else:
        print("  (v_49 writer-cert check skipped — run 'gfs-invite-link')")
    # 0d-bis. v_50 strict member publish: the writer GROUP key the host
    #     delivered to link-joined **e** for the strict epoch round-trips —
    #     stored KEK-wrapped, its authority cert verifying against e's pin
    #     and its seed matching the pinned writer key.
    if state.get("gfs_member_publish_strict_epoch") is not None:
        _check_writer_key_on_e(state, failures, _Cap)
    else:
        print("  (v_50 writer-key check skipped — run 'gfs-member-publish-strict')")
    # 0d-ter. v_51 private-space channel: e and b still point the private
    #     space at the channel a owns, e holds a KEK-wrapped grant, and the
    #     GFS never stored the space id.
    if state.get("gfs_private_channel_space_id"):
        _check_private_channel(state, failures, _Cap)
    else:
        print("  (v_51 private-channel check skipped — run 'gfs-private-channel')")

    # 0b-bis. v_31 — the mesh-routed origin signature (#692). Every
    #     SPACE_ROUTED leg carries ``origin_sig`` inside its sealed blob
    #     now, and the endpoint verifies it against the identity key it
    #     already holds for ``path[0]`` before the inner event reaches a
    #     handler. The chains that actually ride the mesh
    #     (``invite-redeem-routed``, ``remote-invite-routed``) already
    #     assert the inner events LANDED; what this adds is that they
    #     landed on the SIGNED path. Two tripwires, both read off the
    #     endpoints' own logs:
    #
    #       * no "accepting unsigned inner event from pre-v_31 origin" —
    #         that INFO line is the legacy window, and a same-build demo
    #         must never need it. Seeing it means the sender stopped
    #         signing (the whole hole reopens for any peer the receiver
    #         reads as older) rather than that a peer is behind.
    #       * no "dropping (forged origin)" / "no origin signature" —
    #         a genuine leg failing the verify would mean the signing
    #         bytes disagree across the two ends.
    for _label in ("a", "b", "c", "d"):
        _legacy = _log_lines_matching(_label, "pre-v_31 origin")
        if _legacy:
            failures.append(
                f"{_label}: took the v_31 legacy window on a mesh-routed "
                f"envelope ({_legacy[-1].strip()[:160]}) — every household in "
                "this demo runs the same build, so the origin stopped signing",
            )
        for _needle in ("forged origin", "no origin signature"):
            _bad = _log_lines_matching(_label, _needle)
            if _bad:
                failures.append(
                    f"{_label}: refused a mesh-routed envelope on the v_31 "
                    f"origin check ({_bad[-1].strip()[:160]}) — the origin "
                    "signing bytes and the endpoint's disagree",
                )
    if state.get("invite_redeem_routed_ran") or state.get("remote_invite_routed_ran"):
        print(
            "  mesh-routed inner events landed with a verified v_31 origin "
            "signature (no legacy-window, no refusal) \u2713"
        )
    else:
        print(
            "  (v_31 routed-origin signature only smoke-checked — run "
            "'invite-redeem-routed' / 'remote-invite-routed' for the wire)"
        )

    # 0a-bis. v_30 — a cross-household Follower seat. The host's
    #     ``space_remote_members`` row is the authority every write from
    #     that household is judged against, and it can only carry
    #     ``'subscriber'`` from migration 0054 onward; e's own side must
    #     read the same seat back out of the redeem ACK. Asserting both
    #     ends is the tripwire for the role being dropped somewhere on the
    #     wire (the roster mirror silently discards it, so "the redeem
    #     returned subscriber" alone would not catch a regression).
    _fan_space = state.get("gfs_invite_follower_space_id")
    if _fan_space:
        _host_seat = _rows(
            "a",
            "SELECT role FROM space_remote_members WHERE space_id = ? "
            "AND instance_id = ? AND user_id = ?",
            (
                _fan_space,
                state["instances"]["e"]["instance_id"],
                state["instances"]["e"]["user_id"],
            ),
        )
        if not _host_seat or _host_seat[0][0] != "subscriber":
            failures.append(
                f"a: the follower seat for e in {_fan_space} reads "
                f"{_host_seat!r}, expected role 'subscriber' (v_30)",
            )
        _fan_seat = _rows(
            "e",
            "SELECT role FROM space_members WHERE space_id = ? AND user_id = ?",
            (_fan_space, state["instances"]["e"]["user_id"]),
        )
        if not _fan_seat or _fan_seat[0][0] != "subscriber":
            failures.append(
                f"e: the local seat in {_fan_space} reads {_fan_seat!r}, "
                "expected role 'subscriber' (v_30)",
            )
        if _host_seat and _fan_seat and _host_seat[0][0] == _fan_seat[0][0]:
            print("  the v_30 Follower seat reads 'subscriber' on both sides ✓")
    else:
        print("  (v_30 follower seat skipped — run 'gfs-invite-link')")

    # 0b. INSTANCE_RESYNC_REQUEST round-trip (v_19, #319 ¶6) — ask a confirmed
    #     v_19+ peer to re-advertise its capabilities via the operator
    #     endpoint and assert it's accepted. Proves the new event type +
    #     POST /api/admin/federation/resync + peer_supports(v_19) gate are
    #     wired end-to-end; a sub-v_19 peer would 409 (PEER_TOO_OLD).
    va = state["instances"]["a"]
    resync_target = ""
    for row in _conns.get("a", []):
        if (
            row.get("status") == "confirmed"
            and int(row.get("proto_version") or 1) >= _OURS
            # Never a link-joined seat: ``INSTANCE_RESYNC_REQUEST`` is
            # outside ``SPACE_SESSION_ALLOWED_EVENT_TYPES``, so aiming the
            # probe at e would assert a refusal we deliberately built.
            and row.get("source") != "space_session"
        ):
            resync_target = str(row.get("instance_id") or "")
            break
    if resync_target:
        s, body = _request(
            f"http://127.0.0.1:{va['port']}/api/admin/federation/resync",
            method="POST",
            token=va["token"],
            body={"instance_id": resync_target, "scope": "capabilities"},
        )
        if s == 200:
            print(f"  a → resync(capabilities) accepted for {resync_target[:8]} ✓")
        else:
            failures.append(
                f"a: INSTANCE_RESYNC_REQUEST(capabilities) to "
                f"{resync_target[:8]} returned {s} (expected 200): {body}",
            )
    else:
        print("  (no confirmed v_19+ peer for resync round-trip — skipped)")

    # 1. Profile sync — every household sees the other two users by display name.
    for viewer in ("a", "b", "c"):
        names = _all_display_names(state, viewer)
        for other in ("a", "b", "c"):
            if other == viewer:
                continue
            other_user = state["instances"][other]["user_id"]
            other_uname = state["instances"][other]["username"]
            shown = names.get(other_user)
            if not shown or other_uname not in shown.lower():
                failures.append(
                    f"{viewer}: profile of {other} ({other_uname}) "
                    f"missing or wrong (got {shown!r})"
                )
            else:
                print(f"  {viewer} sees {other}: {shown}")

    # 2. Highlights — each household authored one with audience all_paired,
    #    so the other two should see it.
    for viewer in ("a", "b", "c"):
        captions = _highlight_captions(state, viewer)
        for other in ("a", "b", "c"):
            if other == viewer:
                continue
            needle = f"[{other}] highlight — audience all_paired"
            if needle not in captions:
                failures.append(
                    f"{viewer}: highlight from {other} not visible "
                    f"(captions={sorted(captions)})"
                )
            else:
                print(f"  {viewer} sees {other}'s highlight ✓")

    # 3. DM a→c — Carol's conversation list should include the new DM
    #    and the message body should round-trip.
    if "dm_a_to_c" in state:
        c = state["instances"]["c"]
        s, convs = _request(
            f"http://127.0.0.1:{c['port']}/api/conversations",
            token=c["token"],
        )
        _must("conversations(c)", s, convs)
        conv_list = convs if isinstance(convs, list) else (convs.get("items") or [])
        ids = {c0.get("id") for c0 in conv_list}
        if state["dm_a_to_c"] not in ids:
            failures.append(f"c: conversation {state['dm_a_to_c']} not in c's inbox")
        else:
            s, msgs = _request(
                f"http://127.0.0.1:{c['port']}"
                f"/api/conversations/{state['dm_a_to_c']}/messages",
                token=c["token"],
            )
            _must("messages(c)", s, msgs)
            bodies = [
                m.get("content") for m in (msgs if isinstance(msgs, list) else [])
            ]
            if any("hello carol from alice" in (b or "") for b in bodies):
                print(f"  c received a→c DM ({len(bodies)} msg) ✓")
            else:
                failures.append(f"c: a→c DM body missing (got {bodies!r})")

            # 3a. v_3 media DM a→c — assert Carol's view of the
            #     image message. The DM_MESSAGE envelope carries the
            #     preview (renders immediately as a 320 px WebP);
            #     the DM_MEDIA_BLOB follow-up flips media_url to the
            #     full file under media_dir and clears
            #     media_sync_status. We give the scheduler a short
            #     grace period (~8 s) before asserting the
            #     full-bytes state — its tick interval defaults to
            #     5 s.
            if "dm_media_msg_id" in state:
                # Wait for the scheduler to flush the blob outbox.
                time.sleep(8)
                msg_list = msgs if isinstance(msgs, list) else []
                # Re-fetch since the blob may have landed after the
                # preview's initial GET above.
                s2, msgs2 = _request(
                    f"http://127.0.0.1:{c['port']}"
                    f"/api/conversations/{state['dm_a_to_c']}/messages",
                    token=c["token"],
                )
                _must("media-messages(c)", s2, msgs2)
                msg_list = msgs2 if isinstance(msgs2, list) else []
                media_msg = next(
                    (m for m in msg_list if m.get("type") == "image"),
                    None,
                )
                if media_msg is None:
                    failures.append(
                        "c: media DM a→c not in inbox (no type=image row)",
                    )
                else:
                    media_url = media_msg.get("media_url") or ""
                    sync_status = media_msg.get("media_sync_status")
                    if sync_status is not None and sync_status != "":
                        # Still pending — the blob hasn't landed yet.
                        # Surface as a failure so we notice scheduler
                        # regressions; the 8 s wait should be more
                        # than enough for a healthy boot.
                        failures.append(
                            "c: media DM a→c still pending after wait "
                            f"(sync_status={sync_status!r}, media_url={media_url!r})",
                        )
                    elif not media_url:
                        failures.append(
                            "c: media DM a→c has no media_url after blob land",
                        )
                    else:
                        # GET the signed media URL on Carol's
                        # instance — confirms the file landed under
                        # ``media_dir`` and the signing chain works
                        # for the receiver-side read. The media
                        # route is GET-only (HEAD returns 405), so
                        # we read a few bytes to verify the body is
                        # a real image without decoding the whole
                        # response.
                        full_url = media_url
                        if not full_url.startswith("http"):
                            full_url = (
                                f"http://127.0.0.1:{c['port']}/{full_url.lstrip('/')}"
                            )
                        get_req = urllib.request.Request(
                            full_url,
                            method="GET",
                            headers={"Authorization": f"Bearer {c['token']}"},
                        )
                        ms: int = 0
                        first_bytes = b""
                        try:
                            with urllib.request.urlopen(get_req, timeout=10) as r:
                                ms = r.status
                                first_bytes = r.read(16)
                        except urllib.error.HTTPError as exc:
                            ms = exc.code
                        if ms != 200:
                            failures.append(
                                f"c: media DM a→c file fetch HTTP {ms} ({media_url})",
                            )
                        elif not first_bytes:
                            failures.append(
                                f"c: media DM a→c file is empty ({media_url})",
                            )
                        else:
                            print(
                                "  c received a→c media DM (preview + "
                                "full bytes via DM_MEDIA_BLOB) ✓",
                            )
                            # Binary media channel (capability v_14): a and
                            # c are CONFIRMED direct peers, so the full
                            # bytes above rode the dedicated ``fed-media-v1``
                            # binary frame (no base64), not JSON on
                            # ``fed-v1``. Confirm c advertises >= v_14 to a
                            # so we know the binary path was the eligible
                            # transport — if either side were sub-v_14 the
                            # sender would have transparently fallen back to
                            # JSON (still correct, but not what v_14 ships).
                            _, conns_c = _request(
                                f"http://127.0.0.1:{c['port']}/api/connections",
                                token=c["token"],
                            )
                            a_iid = state["instances"]["a"]["instance_id"]
                            row_a = next(
                                (
                                    r
                                    for r in (
                                        conns_c if isinstance(conns_c, list) else []
                                    )
                                    if r.get("instance_id") == a_iid
                                ),
                                None,
                            )
                            pv_a = int((row_a or {}).get("proto_version") or 1)
                            if pv_a < 14:
                                failures.append(
                                    "c: peer a at proto_version="
                                    f"{pv_a} (< 14) — binary media channel "
                                    "not eligible; check the v_14 bump",
                                )
                            else:
                                print(
                                    "  a↔c at proto_version>=14 — media rode "
                                    "the binary fed-media-v1 channel ✓",
                                )
    else:
        print("  DM a→c was skipped during traffic step")

    # 3b. Bazaar DM a→b — Beta's conversation list should include
    #     Alpha's inquiry, with the listing id quoted in the body.
    if "dm_a_to_b" in state and "bazaar_listing_id" in state:
        b = state["instances"]["b"]
        s, convs = _request(
            f"http://127.0.0.1:{b['port']}/api/conversations",
            token=b["token"],
        )
        _must("conversations(b)", s, convs)
        conv_list = convs if isinstance(convs, list) else (convs.get("items") or [])
        ids = {c0.get("id") for c0 in conv_list}
        if state["dm_a_to_b"] not in ids:
            failures.append(
                f"b: bazaar-inquiry conversation {state['dm_a_to_b']} "
                f"not in Beta's inbox",
            )
        else:
            s, msgs = _request(
                f"http://127.0.0.1:{b['port']}"
                f"/api/conversations/{state['dm_a_to_b']}/messages",
                token=b["token"],
            )
            _must("messages(b/bazaar)", s, msgs)
            bodies = [
                m.get("content") for m in (msgs if isinstance(msgs, list) else [])
            ]
            needle = state["bazaar_listing_id"]
            if any(needle in (m_body or "") for m_body in bodies):
                print("  b received a→b bazaar-inquiry DM ✓")
            else:
                failures.append(
                    f"b: bazaar-inquiry DM body missing listing id "
                    f"{needle!r} (got {bodies!r})",
                )
    elif "bazaar_listing_id" not in state:
        print("  bazaar listing skipped during traffic step")
    else:
        print("  DM a→b was skipped during traffic step")

    # 3c. Momentum visibility — a + c follow b on momentum, so Beta's
    #     moment from cmd_traffic should land in their inbox after
    #     federation settles. Beta's own moment should be visible to
    #     Beta locally.
    beta_moment = state.get("moments", {}).get("b")
    if beta_moment and "moment_follows" in state:
        for viewer_label in ("a", "c"):
            if viewer_label not in state["moment_follows"]:
                continue
            viewer = state["instances"][viewer_label]
            s, payload = _request(
                f"http://127.0.0.1:{viewer['port']}/api/moments",
                token=viewer["token"],
            )
            _must(f"moments({viewer_label})", s, payload)
            inbox = payload.get("data") if isinstance(payload, dict) else payload
            inbox_list = inbox if isinstance(inbox, list) else []
            contents = [m.get("content") for m in inbox_list]
            if any(beta_moment["content"] in (mc or "") for mc in contents):
                print(f"  {viewer_label} sees b's moment in inbox ✓")
            else:
                failures.append(
                    f"{viewer_label}: b's moment {beta_moment['id']!r} "
                    f"not in inbox (got contents={contents!r})",
                )
    elif beta_moment is None:
        print("  Beta's moment was rate-limited during traffic — skipping inbox check")
    else:
        print("  moment-follow step was skipped during traffic")

    # 3c-bis. v_35 — relayed moments carry their origin's signature. d is
    #     never QR-paired with c, so c's moment can only reach d relayed
    #     (via b, or a when the trust-relay pairing put a↔d in place), and
    #     d holds no remote_instances row for c: the relay lands only if
    #     d verified c's origin signature against the key that derives to
    #     c's instance id. Two log tripwires on every household: no
    #     "unsigned moment from pre-v_35" (the legacy window — a same-build
    #     demo must never need it) and no relayed-moment refusal (the
    #     signing bytes disagree across the two ends).
    for _label in ("a", "b", "c", "d"):
        _legacy = _log_lines_matching(_label, "unsigned moment from pre-v_35")
        if _legacy:
            failures.append(
                f"{_label}: took the v_35 legacy window on a relayed moment "
                f"({_legacy[-1].strip()[:160]}) — every household in this demo "
                "runs the same build, so the origin stopped signing",
            )
        _bad = [
            line
            for line in _log_lines_matching(_label, " relayed by ")
            if "— refusing (moment=" in line
        ]
        if _bad:
            failures.append(
                f"{_label}: refused a relayed moment on the v_35 origin check "
                f"({_bad[-1].strip()[:160]}) — origin signing bytes disagree",
            )
    carol_moment = state.get("moments", {}).get("c")
    if carol_moment and "d" in state.get("instances", {}):
        d = state["instances"]["d"]
        s, payload = _request(
            f"http://127.0.0.1:{d['port']}/api/moments",
            token=d["token"],
        )
        _must("moments(d)", s, payload)
        inbox = payload.get("data") if isinstance(payload, dict) else payload
        contents = [
            m.get("content") for m in (inbox if isinstance(inbox, list) else [])
        ]
        if any(carol_moment["content"] in (mc or "") for mc in contents):
            print("  d sees c's relayed moment (v_35 origin signature verified) ✓")
        else:
            failures.append(
                f"d: c's relayed moment {carol_moment['id']!r} not in inbox "
                f"(got contents={contents!r}) — the v_35 origin check refused it",
            )
    else:
        print("  (v_35 relayed-moment check skipped — c's moment rate-limited or no d)")

    # 3c-bis. v_67 — a reaction reaches an author two hops away. d is not
    #     paired with c; c's moment reached d through a relay (b or a), which
    #     appended its return token. d reacts: the reaction is sealed to c's
    #     key-wrap key, handed to the relay d got the moment from, which
    #     opens its token and passes it to c. c must hold d's reaction (and
    #     drop it again when d takes it back); d's API must say the reaction
    #     reaches the author.
    if carol_moment and "d" in state.get("instances", {}):
        failures.extend(_verify_routed_moment_reaction(state, carol_moment["id"]))
        # v_69 — c's totals reach b (paired) and d (two hops, relayed).
        failures.extend(_verify_moment_totals(state, carol_moment["id"]))
    else:
        print("  (v_67 routed-reaction check skipped — c's moment rate-limited or no d)")

    # 3c-ter. Deletes stick. c replies to its own moment (replies are exempt
    #     from the 15-min rate limit, so verify can re-run), the reply relays
    #     to d, c deletes it, and d must drop it and keep a tombstone row
    #     (``deleted_at`` set, content wiped) so a replayed create for the
    #     id is refused rather than resurrecting it.
    #     v_65: c first chooses a non-default follower retention (3 days), so
    #     the reply carries an author-chosen ``expires_at`` that must reach d
    #     unchanged (it is under c's origin signature, and within 30 days).
    if carol_moment and "d" in state.get("instances", {}):
        c = state["instances"]["c"]
        d = state["instances"]["d"]
        s, _pref = _request(
            f"http://127.0.0.1:{c['port']}/api/me",
            token=c["token"],
            method="PATCH",
            body={"preferences": {"moments": {"follower_retention_days": 3}}},
        )
        _must("follower retention 3 d(c)", s, _pref)
        reply_text = f"[c] reply that will be deleted {secrets.token_hex(3)}"
        s, r_resp = _request(
            f"http://127.0.0.1:{c['port']}/api/moments",
            token=c["token"],
            method="POST",
            body={"content": reply_text, "parent_moment_id": carol_moment["id"]},
        )
        _must("moment reply(c)", s, r_resp, ok=(201,))
        rp = r_resp.get("data") if isinstance(r_resp.get("data"), dict) else r_resp
        reply_id = rp.get("id")

        def _d_has(text: str) -> bool:
            s2, p2 = _request(
                f"http://127.0.0.1:{d['port']}/api/moments", token=d["token"]
            )
            items = p2.get("data") if isinstance(p2, dict) else p2
            return any(
                text in (m.get("content") or "")
                for m in (items if isinstance(items, list) else [])
            )

        deadline = time.time() + 30
        while time.time() < deadline and not _d_has(reply_text):
            time.sleep(1)
        if not _d_has(reply_text):
            failures.append(f"d: c's relayed reply {reply_id!r} never arrived")
        else:
            c_row = _rows(
                "c",
                "SELECT expires_at, created_at FROM moments WHERE id=?",
                (reply_id,),
            )
            d_row = _rows("d", "SELECT expires_at FROM moments WHERE id=?", (reply_id,))
            try:
                c_exp = datetime.fromisoformat(c_row[0][0])
                c_created = datetime.fromisoformat(c_row[0][1])
                lifetime_h = (c_exp - c_created).total_seconds() / 3600
            except IndexError, TypeError, ValueError:
                lifetime_h = -1.0
            if not 0 < lifetime_h <= 3 * 24 + 0.1:
                failures.append(
                    f"c: reply {reply_id!r} ignores the 3-day follower retention "
                    f"(lifetime {lifetime_h:.1f} h, row={c_row!r})"
                )
            elif not d_row or d_row[0][0] != c_row[0][0]:
                failures.append(
                    f"d: c's reply {reply_id!r} expiry did not round-trip "
                    f"(c={c_row[0][0]!r}, d={d_row!r}) — v_65 retention lost"
                )
            else:
                print(
                    "  v_65 author-chosen retention (3 d) round-trips c → d "
                    f"({lifetime_h:.0f} h) ✓"
                )
            s, _ = _request(
                f"http://127.0.0.1:{c['port']}/api/moments/{reply_id}",
                token=c["token"],
                method="DELETE",
            )
            _must("moment delete(c)", s, _)
            deadline = time.time() + 30
            while time.time() < deadline and _d_has(reply_text):
                time.sleep(1)
            tomb = _rows(
                "d",
                "SELECT content, deleted_at FROM moments WHERE id=?",
                (reply_id,),
            )
            if _d_has(reply_text):
                failures.append(f"d: c's deleted reply {reply_id!r} still visible")
            elif not tomb or tomb[0][1] is None or tomb[0][0] != "":
                failures.append(
                    f"d: no delete tombstone for c's reply {reply_id!r} (row={tomb!r})"
                )
            else:
                print("  d drops c's deleted reply and keeps its tombstone ✓")
        s, _pref = _request(
            f"http://127.0.0.1:{c['port']}/api/me",
            token=c["token"],
            method="PATCH",
            body={"preferences": {"moments": {}}},
        )
        _must("follower retention reset(c)", s, _pref)

    # 4. Space — Beta's space, both Alice and Carol invited.
    if "space_id" in state:
        b = state["instances"]["b"]
        s, members = _request(
            f"http://127.0.0.1:{b['port']}/api/spaces/{state['space_id']}/members",
            token=b["token"],
        )
        if s != 200:
            print(f"  space members: {s} {members}")
        else:
            mlist = members if isinstance(members, list) else members.get("members", [])
            ids = {m.get("user_id") for m in mlist}
            for label in ("a", "c"):
                uid = state["instances"][label]["user_id"]
                if uid in ids:
                    print(f"  space contains {label} ✓")
                else:
                    print(f"  space pending acceptance from {label}")
        # v_32 roster snapshot: every MEMBER household's roster mirror must
        # hold every other member — content authors are bound to it. Alice
        # joined before Carol, so without the host's snapshot to the later
        # joiner (or the gossip to the earlier one) one of them would miss
        # the other and drop that household's content.
        for viewer, other in (("a", "c"), ("c", "a")):
            v = state["instances"][viewer]
            s, members = _request(
                f"http://127.0.0.1:{v['port']}/api/spaces/{state['space_id']}/members",
                token=v["token"],
            )
            mlist = (
                members
                if isinstance(members, list)
                else (members or {}).get("members", [])
            )
            ids = {m.get("user_id") for m in mlist} if s == 200 else set()
            if state["instances"][other]["user_id"] in ids:
                print(f"  {viewer}'s roster mirror has {other} ✓")
            else:
                failures.append(
                    f"{viewer}'s roster mirror lacks {other} ({s}) — the "
                    "v_32 roster snapshot / gossip did not converge"
                )

    # 5. Trust-relay pair — a ↔ d should be CONFIRMED on both sides
    #    *if* :func:`cmd_relay_pair` was run (excluded from ``all``).
    if "relay_pair_ran" in state:
        a_iid = state["instances"]["a"]["instance_id"]
        d_iid = state["instances"]["d"]["instance_id"]
        for viewer, peer in (("a", d_iid), ("d", a_iid)):
            info = state["instances"][viewer]
            s, conns = _request(
                f"http://127.0.0.1:{info['port']}/api/connections",
                token=info["token"],
            )
            _must(f"connections({viewer})", s, conns)
            match = [c for c in conns if c["instance_id"] == peer]
            if not match or match[0]["status"] != "confirmed":
                failures.append(
                    f"{viewer}: relay-paired peer {peer[:8]} missing or not confirmed",
                )
                continue
            print(f"  {viewer} ↔ {peer[:8]} confirmed via trust relay ✓")
            # Capability handshake must reach trust-relay-paired peers
            # too — the auto-pair coordinator publishes ``PairingConfirmed``
            # for both sides of the relay so the same on-pair announcement
            # subscriber fires. Without this assertion a regression that
            # only the *responder* fires the event (the same bug we hit
            # for QR pairs) would slip past the inner-ring check.
            pv = int(match[0].get("proto_version") or 1)
            if pv < 2:
                failures.append(
                    f"{viewer}: relay-paired peer {peer[:8]} stuck at "
                    f"proto_version={pv} (expected >= 2) — "
                    f"INSTANCE_CAPABILITIES_UPDATED never landed on the "
                    f"trust-relay path?",
                )
            else:
                print(
                    f"  {viewer} sees {peer[:8]} at proto_version={pv} (trust-relay) ✓",
                )
    else:
        print("  trust-relay pair (a ↔ d via b) skipped — run 'relay-pair' to exercise")

    # 6. Calendar RSVPs — Beta's space-calendar event has 'going' from
    #    both Alpha and Carol after federation. Only asserted when
    #    ``cmd_calendar`` ran; that step is excluded from ``all`` until
    #    SPACE_CALENDAR_EVENT_CREATED outbound federation is wired.
    if "calendar_event_id" in state:
        b = state["instances"]["b"]
        s, rsvps = _request(
            f"http://127.0.0.1:{b['port']}/api/calendars/events/"
            f"{state['calendar_event_id']}/rsvps",
            token=b["token"],
        )
        _must("rsvps(b)", s, rsvps)
        going = {
            r["user_id"] for r in (rsvps.get("rsvps") or []) if r["status"] == "going"
        }
        for label in ("a", "c"):
            uid = state["instances"][label]["user_id"]
            if uid in going:
                print(f"  b sees {label} 'going' ✓")
            else:
                failures.append(
                    f"b: RSVP from {label} ({uid}) missing (got {sorted(going)})",
                )

        # 6b. tz field round-trip — Beta authored the event with
        #     ``tz="Europe/Berlin"``. After SPACE_CALENDAR_EVENT_CREATED
        #     federates to Alpha and Carol, their local mirror of the
        #     event must carry the same tz. Asserts the v2 field
        #     actually rides through the wire (the proto_version check
        #     alone only proves the *announcement* propagated; this
        #     proves a v2 field on a federated event reaches the
        #     receivers in shape).
        #
        #     Uses the space-scoped events list rather than the per-
        #     event GET because §D1b cross-household remote-invitees
        #     don't pass the personal-calendar route's space-membership
        #     check — the event lives in ``space_calendar_events`` on
        #     the peer side, addressable only via the space endpoint.
        space_id = state["space_id"]
        evt_id = state["calendar_event_id"]
        # ``Z`` not ``+00:00`` — the URL decoder turns ``+`` into a
        # space, which then fails ``datetime.fromisoformat`` server-
        # side and surfaces as a 422 with a generic detail message.
        window_start = "2027-01-01T00:00:00Z"
        window_end = "2027-02-01T00:00:00Z"
        for guest_label in ("a", "c"):
            guest = state["instances"][guest_label]
            s, events = _request(
                f"http://127.0.0.1:{guest['port']}/api/spaces/{space_id}"
                f"/calendar/events?start={window_start}&end={window_end}",
                token=guest["token"],
            )
            _must(f"space calendar events({guest_label})", s, events)
            evt_list = (
                events if isinstance(events, list) else (events.get("events") or [])
            )
            mirror = next((e for e in evt_list if e.get("id") == evt_id), None)
            if mirror is None:
                failures.append(
                    f"{guest_label}: federated calendar event {evt_id} "
                    f"not visible in space-scoped list — "
                    f"SPACE_CALENDAR_EVENT_CREATED did not land",
                )
                continue
            tz = mirror.get("tz")
            if tz != "Europe/Berlin":
                failures.append(
                    f"{guest_label}: federated event tz={tz!r} "
                    f"(expected 'Europe/Berlin') — v2 tz field did not "
                    f"ride through SPACE_CALENDAR_EVENT_CREATED",
                )
            else:
                print(f"  {guest_label} sees event tz=Europe/Berlin ✓")

    # 6c. Space timetable (v_39) — Beta's shared timetable reached both
    #     member households (SPACE_TIMETABLE_UPSERTED, members only) with
    #     its lesson, the space's timetable feature reached them in the
    #     federated config, and a plain member's edit is refused locally
    #     (writes are owner / admin only).
    if state.get("space_timetable_id"):
        space_id = state["space_id"]
        tt_id = state["space_timetable_id"]
        want_version = state.get("space_timetable_version", 2)
        for guest_label in ("a", "c"):
            guest = state["instances"][guest_label]
            base = f"http://127.0.0.1:{guest['port']}/api/spaces/{space_id}/timetables"
            got = None
            for _ in range(10):
                s, body = _request(base, token=guest["token"])
                if s == 200:
                    got = next(
                        (t for t in body.get("timetables") or [] if t["id"] == tt_id),
                        None,
                    )
                    if got is not None and got.get("version", 0) >= want_version:
                        break
                time.sleep(1)
            if s != 200:
                failures.append(
                    f"{guest_label}: GET space timetables -> {s} {body!r} — the "
                    f"timetable feature did not federate in space_meta.features"
                )
                continue
            if got is None:
                failures.append(
                    f"{guest_label}: space timetable {tt_id} missing — "
                    f"SPACE_TIMETABLE_UPSERTED did not land"
                )
                continue
            titles = [e.get("title") for e in got.get("entries") or []]
            if got.get("version", 0) < want_version or "Mathe" not in titles:
                failures.append(
                    f"{guest_label}: space timetable at v{got.get('version')} "
                    f"with {titles!r} (expected v{want_version} with 'Mathe')"
                )
            else:
                print(
                    f"  {guest_label} sees space timetable {tt_id[:8]} "
                    f"v{got['version']} with 'Mathe' ✓"
                )
            s, _r = _request(
                f"{base}/{tt_id}",
                token=guest["token"],
                method="PATCH",
                body={"version": got["version"], "name": "hijack"},
            )
            if s != 403:
                failures.append(
                    f"{guest_label}: a plain member's timetable edit -> {s} "
                    f"(expected 403 — writes are owner / admin only)"
                )
            else:
                print(f"  {guest_label}: member edit refused (403) ✓")

    # 6d. Space task (v_40) — Beta's edit (priority high → low, due date
    #     cleared with an explicit null) reached both member households,
    #     and the labels survived it.
    if state.get("space_task_id"):
        want = {"priority": "low", "labels": ["Move", "Van"], "due_date": None}
        for guest_label in ("a", "c"):
            got = None
            for _ in range(10):
                got = _space_task_on(
                    state,
                    guest_label,
                    state["space_task_list_id"],
                    state["space_task_id"],
                    tries=1,
                )
                if got is not None and {k: got.get(k) for k in want} == want:
                    break
                time.sleep(1)
            if got is None or {k: got.get(k) for k in want} != want:
                failures.append(
                    f"{guest_label}: space task -> {got!r} (expected {want!r}) — "
                    f"SPACE_TASK_UPDATED priority / null due_date did not round-trip"
                )
            else:
                print(
                    f"  {guest_label} sees space task edit: priority=low, "
                    f"due_date cleared, labels kept ✓"
                )

    # 6e. Space moderator (v_41). Beta — the owner — promotes Alice to
    #     ``moderator`` over the remote-member PATCH. Alice's own household
    #     applies SPACE_MEMBER_ROLE_CHANGED to her seat, and Carol's roster
    #     mirror (the authority-signed JOINED gossip) carries the new role.
    #     Beta then demotes her back to ``member`` so a re-run starts clean.
    if "space_id" in state:
        b = state["instances"]["b"]
        a_info = state["instances"]["a"]
        space_id = state["space_id"]
        role_url = (
            f"http://127.0.0.1:{b['port']}/api/spaces/{space_id}/remote-members/"
            f"{a_info['instance_id']}/{a_info['user_id']}"
        )

        def _role_seen(viewer: str, want: str) -> str | None:
            v = state["instances"][viewer]
            got = None
            for _ in range(10):
                s, members = _request(
                    f"http://127.0.0.1:{v['port']}/api/spaces/{space_id}/members",
                    token=v["token"],
                )
                mlist = (
                    members
                    if isinstance(members, list)
                    else (members or {}).get("members", [])
                )
                got = next(
                    (
                        m.get("role")
                        for m in (mlist if s == 200 else [])
                        if m.get("user_id") == a_info["user_id"]
                    ),
                    None,
                )
                if got == want:
                    return got
                time.sleep(1)
            return got

        s, body = _request(
            role_url, token=b["token"], method="PATCH", body={"role": "moderator"}
        )
        if s != 200:
            failures.append(f"b: promote a to moderator -> {s} {body!r}")
        else:
            for viewer in ("a", "c"):
                got = _role_seen(viewer, "moderator")
                if got != "moderator":
                    failures.append(
                        f"{viewer}: a's space role is {got!r} (expected "
                        f"'moderator') — v_41 SPACE_MEMBER_ROLE_CHANGED / "
                        f"roster gossip did not round-trip"
                    )
                else:
                    print(f"  {viewer} sees a as space moderator ✓")
            s, body = _request(
                role_url, token=b["token"], method="PATCH", body={"role": "member"}
            )
            if s != 200:
                failures.append(f"b: demote a back to member -> {s} {body!r}")
            elif _role_seen("a", "member") != "member":
                failures.append("a: demotion back to member did not land")
            else:
                print("  b demotes a back to member ✓")

    # 6f. ADMIN_ONLY space pages (v_42). Beta — the owner — sets the
    #     space's ``pages`` access level to ``admin_only``. The level
    #     federates with the space config, and Alpha's own household then
    #     refuses Alice's page create locally with 403 ``ACCESS_ADMIN_ONLY``
    #     (member stubs enforce it too — not just the host). Beta resets it
    #     to ``open`` so a re-run starts clean.
    if "space_id" in state:
        host_i = state["instances"]["b"]
        guest_i = state["instances"]["a"]
        space_id = state["space_id"]
        b_space = f"http://127.0.0.1:{host_i['port']}/api/spaces/{space_id}"
        a_space = f"http://127.0.0.1:{guest_i['port']}/api/spaces/{space_id}"
        s, body = _request(
            b_space,
            token=host_i["token"],
            method="PATCH",
            body={"features": {"pages_access": "admin_only"}},
        )
        if s != 200:
            failures.append(f"b: set pages_access=admin_only -> {s} {body!r}")
        else:
            seen = None
            for _ in range(15):
                s, sp = _request(a_space, token=guest_i["token"])
                seen = ((sp or {}).get("features") or {}).get("pages_access")
                if s == 200 and seen == "admin_only":
                    break
                time.sleep(1)
            if seen != "admin_only":
                failures.append(
                    f"a: space pages_access is {seen!r} (expected 'admin_only') "
                    f"— the access level did not federate"
                )
            else:
                s, body = _request(
                    f"{a_space}/pages",
                    token=guest_i["token"],
                    method="POST",
                    body={"title": "Not allowed", "content": "x"},
                )
                code = ((body or {}).get("error") or {}).get("code")
                if s != 403 or code != "ACCESS_ADMIN_ONLY":
                    failures.append(
                        f"a: member page create under admin_only -> {s} {body!r} "
                        f"(expected 403 ACCESS_ADMIN_ONLY) — v_42 local gate"
                    )
                else:
                    print("  a refuses a member's page in an admin-only wiki ✓")
            s, body = _request(
                b_space,
                token=host_i["token"],
                method="PATCH",
                body={"features": {"pages_access": "open"}},
            )
            if s != 200:
                failures.append(f"b: reset pages_access=open -> {s} {body!r}")

    # 7. Capability handshake — every confirmed inner-ring peer should
    #    have announced their proto_version via
    #    ``INSTANCE_CAPABILITIES_UPDATED`` at startup. After ``up`` + a
    #    short settle window we expect each of a/b/c to see the others
    #    at proto_version >= 2 (the version this build advertises). A
    #    peer still pinned at 1 means the announcement never landed —
    #    most likely the outbound didn't fire or the inbound handler is
    #    not registered. The harness asserts the round-trip so future
    #    additive-but-not-fail-soft features have a safety net.
    #
    #    Reads block 0's snapshot rather than re-fetching: block 0 already
    #    asserted the STRICTLY STRONGER ``>= OURS`` on that same list, so a
    #    second fetch could never fail here on its own — it only spent a
    #    slot of the 5-per-60 s ``/api/pairing/*`` budget that
    #    ``share_home`` below then 429'd on.
    for viewer in ("a", "b", "c"):
        conns = _conns.get(viewer)
        if conns is None:
            failures.append(f"{viewer}: no pairing-connections snapshot")
            continue
        peers_by_id = {c["instance_id"]: c for c in conns}
        for other in ("a", "b", "c"):
            if other == viewer:
                continue
            other_iid = state["instances"][other]["instance_id"]
            row = peers_by_id.get(other_iid)
            if row is None:
                failures.append(
                    f"{viewer}: missing pairing connection row for {other}",
                )
                continue
            pv = int(row.get("proto_version") or 1)
            if pv < 2:
                failures.append(
                    f"{viewer}: peer {other} stuck at proto_version={pv} "
                    f"(expected >= 2) — INSTANCE_CAPABILITIES_UPDATED "
                    f"never landed?",
                )
            else:
                print(f"  {viewer} sees {other} at proto_version={pv} ✓")

    # 8. Transport: every confirmed inner-ring pair should have ridden the
    #    WebRTC DataChannel up by the time ``verify`` runs. The traffic /
    #    calendar round-trips give the channel ~30 s to settle.
    #
    #    On a busy container the ICE handshake (STUN gather, connectivity
    #    checks, DTLS) can stretch past that. Poll every 2 s for up to
    #    60 s before declaring a fallback — a real ``https`` regression
    #    will stay stuck across all polls; a slow-settle just needs the
    #    extra patience. Each poll re-reads ``/api/pairing/connections``
    #    on every src, so the inner-ring view is taken atomically per
    #    pass.
    d_iid = state["instances"]["d"]["instance_id"]

    def _check_transports() -> tuple[list[str], list[str], bool]:
        """Probe every inner-ring confirmed peer's transport once.

        Returns ``(success_lines, warning_lines, throttled)``. A peer that hasn't
        flipped to ``rtc`` yet emits a *warning* (not a failure) —
        WebRTC peer-connections legitimately don't establish reliably
        on loopback because perfect-negotiation glare aborts one side
        of the DTLS handshake. Federation transparently falls through
        to HTTPS-inbox and content still lands; the per-step content
        assertions earlier in this function are what actually pin
        delivery. The transport probe is informational — useful to
        see at a glance which pairs flipped — but not a gate.

        ``throttled`` says the ``/api/pairing/*`` bucket (5 per 60 s per
        instance) ran dry mid-probe. That HAS to be a signal rather than
        a ``_must`` abort: verify already spends 3 of the 5 on a before
        it gets here, so a slow RTC settle reaches the cap on the third
        poll and used to take the WHOLE verify down with a 429 — an
        informational probe failing the run it was only meant to
        annotate. The caller stops polling and prints what it has.
        """
        ok_lines: list[str] = []
        warn_lines: list[str] = []
        for src in ("a", "b", "c"):
            info = state["instances"][src]
            s, conns = _request(
                f"http://127.0.0.1:{info['port']}/api/connections",
                token=info["token"],
            )
            if s == 429:
                return ok_lines, warn_lines, True
            _must(f"connections({src})", s, conns)
            for row in conns:
                if row.get("status") != "confirmed":
                    continue
                # d isn't part of the inner-ring traffic test — it's
                # only paired with b in the demo, so the channel may
                # or may not have flipped to RTC by verify time.
                if row["instance_id"] == d_iid:
                    continue
                # A link-joined seat can NEVER be ``rtc``: RTC signalling
                # travels over the peer relationship this pair does not
                # have, which is why the row reads ``gfs_relay``. Warning
                # about it would report the design as a failure to settle.
                if row.get("source") == "space_session":
                    continue
                transport = row.get("transport") or "https"
                if transport == "rtc":
                    ok_lines.append(
                        f"  {src} sees {row['display_name']} on transport=rtc ✓"
                    )
                else:
                    warn_lines.append(
                        f"  WARN: {src} sees {row['display_name']!r} on "
                        f"transport={transport!r} (RTC didn't settle — "
                        "loopback glare; content delivery still verified"
                        " above)"
                    )
        return ok_lines, warn_lines, False

    # The ``/api/pairing/*`` bucket is 5 calls per 60s per instance,
    # and we already spent the budget in earlier verify steps and
    # share_home OFF→ON. Cap the poll at 3 attempts spaced 15s apart
    # (≤3 calls per instance per minute — well within budget) so a
    # slow RTC handshake gets ~30s of patience without exhausting
    # the limiter.
    ok_lines: list[str] = []
    warn_lines: list[str] = []
    for attempt in range(3):
        pass_ok, pass_warn, throttled = _check_transports()
        if throttled:
            print(
                "  (transport probe: /api/pairing/* bucket empty — reporting "
                "the last complete pass; RTC convergence is informational)"
            )
            break
        ok_lines, warn_lines = pass_ok, pass_warn
        if not warn_lines:
            break
        if attempt < 2:
            time.sleep(15)
    # RTC convergence is informational, not a verify gate (see
    # ``_check_transports`` docstring + the
    # ``DTLS handshake failed`` / ``fed RTC: ICE candidate``
    # entries in ``_LOG_BENIGN``). Print whichever side fired.
    for line in ok_lines:
        print(line)
    for line in warn_lines:
        print(line)

    # 9. Crash check — every instance still alive (WebRTC didn't blow up).
    for label, info in state["instances"].items():
        if not _alive(info["pid"]):
            failures.append(f"{label}: process pid={info['pid']} is gone")

    # 11. Home-location propagation — each inner-ring household should see
    #     every confirmed peer's home_lat/home_lon populated after pairing.
    #     Coords are seeded in ``cmd_up`` (see ``_seed_home_coords``) and
    #     exchanged during the §11 pairing handshake via the peer-accept body.
    #     The assertion validates the full carry-through: seed → peer-accept →
    #     remote_instances → /api/friends response.
    #
    #     Expected coords per label (4dp precision from schema):
    #     a=Berlin(52.52,13.405), b=Hamburg(53.55,9.99), c=Frankfurt(50.11,8.68)
    _expected_coords: dict[str, tuple[float, float]] = {
        label: (_SEED_COORDS[label][0], _SEED_COORDS[label][1])
        for label in ("a", "b", "c", "d")
    }
    for viewer in ("a", "b", "c"):
        info = state["instances"][viewer]
        s, fr = _request(
            f"http://127.0.0.1:{info['port']}/api/friends",
            token=info["token"],
        )
        _must(f"friends({viewer})", s, fr)
        households_by_id: dict[str, dict] = {}
        for hh in fr.get("households", []):
            households_by_id[hh["instance_id"]] = hh
        for other in ("a", "b", "c"):
            if other == viewer:
                continue
            other_iid = state["instances"][other]["instance_id"]
            hh = households_by_id.get(other_iid)
            if hh is None:
                failures.append(
                    f"{viewer}: peer {other} missing from /api/friends households",
                )
                continue
            lat = hh.get("home_lat")
            lon = hh.get("home_lon")
            if lat is None or lon is None:
                failures.append(
                    f"{viewer}: peer {other} has NULL home_lat/home_lon in "
                    f"/api/friends — LOCAL_HOME_LOCATION_CHANGED did not "
                    f"propagate (or coord not sent during pairing handshake)",
                )
            else:
                exp_lat, exp_lon = _expected_coords[other]
                # Compare at 4 dp (schema precision).
                if round(lat, 4) != round(exp_lat, 4) or round(lon, 4) != round(
                    exp_lon, 4
                ):
                    failures.append(
                        f"{viewer}: peer {other} home coords mismatch — "
                        f"got ({lat}, {lon}), expected ({exp_lat}, {exp_lon})",
                    )
                else:
                    print(
                        f"  {viewer} sees {other}'s home ({lat}, {lon}) ✓",
                    )

    # 10. share_home toggle — flip Alpha's share_home for Bob OFF, assert Bob
    #     clears Alpha's home coords, then flip ON and assert they are restored.
    #
    #     This exercises PeerHomeSharingService.set_share_home end-to-end:
    #       OFF → fires null-coord LOCAL_HOME_LOCATION_CHANGED → Bob clears row
    #       ON  → fires current-coord LOCAL_HOME_LOCATION_CHANGED → Bob restores row
    a = state["instances"]["a"]
    b = state["instances"]["b"]
    bob_id = b["instance_id"]

    def _patch_share_home(value: bool) -> tuple[int, dict]:
        """PATCH share_home, riding out an empty ``/api/pairing`` bucket.

        The bucket is 5 calls / 60 s per instance, and verify has already
        spent it on Alpha by now: 3 capability / connection reads, plus
        up to 3 transport-probe passes when RTC is slow to settle (that
        probe runs *before* this step). A 429 here means the demo crowded
        the limiter, not that the toggle is broken — retry up to 3 times
        with a 25 s spacer so the sliding window drains (one slot frees
        every ~12 s on average; 25 s buys two).
        """
        s: int = 0
        r: dict = {}
        for attempt in range(3):
            s, r = _request(
                f"http://127.0.0.1:{a['port']}/api/pairing/connections/{bob_id}",
                token=a["token"],
                method="PATCH",
                body={"share_home": value},
            )
            if s == 429 and attempt < 2:
                time.sleep(25)
                continue
            break
        return s, r

    # Flip OFF on Alpha's side.
    s, r = _patch_share_home(False)
    if s not in (200, 204):
        failures.append(f"share_home OFF patch failed: HTTP {s} {r!r}")
    else:
        # Give the outbound envelope a moment to arrive and be processed.
        time.sleep(1)
        # Assert Bob sees NULL home_lat for Alpha's remote_instances row.
        a_id = a["instance_id"]
        db_path = _instance_dir("b") / "socialhome.db"
        con = sqlite3.connect(str(db_path))
        try:
            row = con.execute(
                "SELECT home_lat, home_lon FROM remote_instances WHERE id = ?",
                (a_id,),
            ).fetchone()
        finally:
            con.close()
        if row is None:
            failures.append(
                "share_home OFF: Alpha's remote_instances row not found on Bob's DB"
            )
        elif row[0] is not None or row[1] is not None:
            failures.append(
                f"share_home OFF: Bob still has Alpha's coords "
                f"(home_lat={row[0]}, home_lon={row[1]}); expected NULL"
            )
        else:
            print("  share_home OFF: Bob's home_lat/home_lon for Alpha is NULL ✓")

        # Flip ON and verify coords are restored.
        s2, r2 = _patch_share_home(True)
        if s2 not in (200, 204):
            failures.append(f"share_home ON patch failed: HTTP {s2} {r2!r}")
        else:
            time.sleep(1)
            con = sqlite3.connect(str(db_path))
            try:
                row2 = con.execute(
                    "SELECT home_lat, home_lon FROM remote_instances WHERE id = ?",
                    (a_id,),
                ).fetchone()
            finally:
                con.close()
            if row2 is None or row2[0] is None or row2[1] is None:
                failures.append(
                    f"share_home ON: Bob still has NULL coords for Alpha "
                    f"after re-enable (row={row2!r})"
                )
            else:
                print(
                    f"  share_home ON: Bob sees Alpha's home ({row2[0]}, {row2[1]}) ✓"
                )

    # 12. User preferences round-trip — PATCH /api/me/preferences on
    #    Alice's instance, re-fetch, assert the value persisted, and
    #    verify Bob's preferences are unchanged (no cross-talk).
    a = state["instances"]["a"]
    b = state["instances"]["b"]
    s, r = _request(
        f"http://127.0.0.1:{a['port']}/api/me/preferences",
        token=a["token"],
        method="PATCH",
        body={"hide_highlights": True},
    )
    if s not in (200, 204):
        failures.append(f"a: PATCH /api/me/preferences failed: HTTP {s} {r!r}")
    else:
        s2, r2 = _request(
            f"http://127.0.0.1:{a['port']}/api/me/preferences",
            token=a["token"],
        )
        if s2 != 200:
            failures.append(f"a: GET /api/me/preferences failed after PATCH: HTTP {s2}")
        elif not r2.get("hide_highlights"):
            failures.append(f"a: hide_highlights not persisted (got {r2!r})")
        else:
            print("  a: user preferences round-trip (hide_highlights=True) ✓")
        # No cross-talk — Bob's preferences should be unmodified.
        s3, r3 = _request(
            f"http://127.0.0.1:{b['port']}/api/me/preferences",
            token=b["token"],
        )
        if s3 != 200:
            failures.append(f"b: GET /api/me/preferences failed: HTTP {s3}")
        elif r3.get("hide_highlights"):
            failures.append(
                f"b: hide_highlights unexpectedly true after Alice's PATCH "
                f"(cross-talk? got {r3!r})"
            )
        else:
            print("  b: user preferences unchanged after Alice's PATCH ✓")
        # Restore Alice's preference to avoid affecting other verify steps.
        _request(
            f"http://127.0.0.1:{a['port']}/api/me/preferences",
            token=a["token"],
            method="PATCH",
            body={"hide_highlights": False},
        )

    # 13. GFS public-space content — the opt-in ``gfs-*`` chain. Only
    #    asserted when it actually ran; the chain is excluded from ``all``
    #    because booting a GFS is heavyweight.
    if "gfs_space_id" in state:
        gfs_space_id = state["gfs_space_id"]
        d = state["instances"]["d"]
        c = state["instances"]["c"]

        # 13a. d's mirrored authority pin still matches a's own. A pin that
        #      drifts (or gets re-``save``d away by a later metadata refresh)
        #      silently kills every relayed frame at the subscriber, because
        #      ``SpacePublicInbound._verify_authority`` checks against exactly
        #      this column.
        d_pin_rows = _rows(
            "d",
            "SELECT identity_public_key FROM spaces WHERE id = ?",
            (gfs_space_id,),
        )
        a_pin_rows = _rows(
            "a",
            "SELECT identity_public_key FROM spaces WHERE id = ?",
            (gfs_space_id,),
        )
        if not d_pin_rows:
            failures.append(
                f"d: local 'spaces' mirror for GFS space {gfs_space_id} is gone",
            )
        elif not a_pin_rows:
            failures.append(
                f"a: lost its own 'spaces' row for {gfs_space_id}",
            )
        elif d_pin_rows[0][0] != a_pin_rows[0][0]:
            failures.append(
                f"d: mirrored identity_public_key for {gfs_space_id} no longer "
                f"matches a's ({d_pin_rows[0][0]!r} != {a_pin_rows[0][0]!r})",
            )
        else:
            print("  d's mirrored space-authority pin still matches a's ✓")

        # 13b. d still sees every post relayed over the GFS — the pre-rotation
        #      one AND (when ``gfs-space-rotate`` ran) the post-rotation one.
        #      A regression that only re-keys ``space_instances`` members
        #      leaves the second one missing while the first still reads.
        expected_posts = {
            k: state[k]
            for k in ("gfs_space_post_id", "gfs_space_rotated_post_id")
            if k in state
        }
        s, body = _request(
            f"http://127.0.0.1:{d['port']}/api/spaces/{gfs_space_id}/feed",
            token=d["token"],
        )
        if s != 200:
            failures.append(
                f"d: GET /api/spaces/{gfs_space_id}/feed failed: HTTP {s}",
            )
        else:
            rows = body if isinstance(body, list) else (body.get("posts") or [])
            have = {p.get("id") for p in rows}
            for key, pid in expected_posts.items():
                if pid in have:
                    print(f"  d still sees {key}={pid[:8]}… ✓")
                else:
                    failures.append(
                        f"d: GFS-relayed post {pid} ({key}) missing from the "
                        f"space feed (got {sorted(have)})",
                    )

        # 13c. c is the negative control — never a member, never a subscriber,
        #      never GFS-paired. It must hold NONE of the space content. This
        #      is the §"non-member households MUST NOT see space content"
        #      hard rule, re-asserted at the end of the run.
        for key, pid in expected_posts.items():
            if _rows("c", "SELECT id FROM space_posts WHERE id = ?", (pid,)):
                failures.append(
                    f"c: holds GFS space post {pid} ({key}) — non-member "
                    "household received space content",
                )
            else:
                print(f"  c still does not hold {key} ✓")
        s, c_feed = _request(
            f"http://127.0.0.1:{c['port']}/api/spaces/{gfs_space_id}/feed",
            token=c["token"],
        )
        if s == 200:
            rows = c_feed if isinstance(c_feed, list) else (c_feed.get("posts") or [])
            leaked = {p.get("id") for p in rows} & set(expected_posts.values())
            if leaked:
                failures.append(
                    f"c: space feed for {gfs_space_id} exposes {sorted(leaked)}",
                )

        # 13d. The setup-admin-authored post — the OTHER author-id shape. The
        #      admin's user_id is username-anchored, a provisioned user's is
        #      uuid4-anchored, and the relay's per-author self-cert
        #      (``derive_user_id(author_pk, anchor_or_username) ==
        #      author_user_id``) has to hold for both. A regression that mints
        #      a synthetic admin id again drops this post at the subscriber
        #      with "author verification failed" while erin's still arrives.
        if "gfs_space_admin_post_id" in state:
            admin_pid = state["gfs_space_admin_post_id"]
            admin_uid = state["instances"]["a"]["user_id"]
            s, body = _request(
                f"http://127.0.0.1:{d['port']}/api/spaces/{gfs_space_id}/feed",
                token=d["token"],
            )
            if s != 200:
                failures.append(
                    f"d: GET /api/spaces/{gfs_space_id}/feed failed: HTTP {s}",
                )
            else:
                rows = body if isinstance(body, list) else (body.get("posts") or [])
                mine = next((p for p in rows if p.get("id") == admin_pid), None)
                if mine is None:
                    failures.append(
                        f"d: setup-admin GFS post {admin_pid} missing from the "
                        f"space feed (got {sorted(p.get('id') for p in rows)})",
                    )
                elif mine.get("author") != admin_uid:
                    failures.append(
                        f"d: setup-admin GFS post {admin_pid} attributed to "
                        f"{mine.get('author')!r}, expected {admin_uid!r}",
                    )
                else:
                    print("  d still sees a's setup-admin GFS post ✓")
            if _rows("c", "SELECT id FROM space_posts WHERE id = ?", (admin_pid,)):
                failures.append(
                    f"c: holds setup-admin GFS space post {admin_pid} — "
                    "non-member household received space content",
                )
            else:
                print("  c still does not hold the setup-admin GFS post ✓")

        # 13e. The relay is IDENTITY-FREE (``gfs-space-post`` step 6). Re-read
        #      from d's own log: every ``gfs.relay.received`` record for this
        #      space must name the space + event and nothing else. A ``from=``
        #      back on that line means either the GFS started stamping the
        #      relaying household onto the fan-out frame again or the receiver
        #      started reading (and logging) the outer identity.
        if "gfs_space_post_id" in state:
            # Every log d wrote this run: later GFS steps respawn d, and the
            # record is from gfs-space-post.
            relay_lines = [
                line
                for line in _all_log_lines_matching("d", "gfs.relay.received:")
                if f"space={gfs_space_id}" in line
            ]
            if not relay_lines:
                failures.append(
                    f"d: no 'gfs.relay.received: space={gfs_space_id}' record "
                    "in any of d's logs",
                )
            else:
                identified = [line for line in relay_lines if "from=" in line]
                if identified:
                    failures.append(
                        "d: GFS relay record carries a 'from=' — the fan-out "
                        f"frame is identity-bearing again: {identified!r}",
                    )
                else:
                    print(
                        f"  ✓ GFS relay identity-free ({len(relay_lines)} "
                        "relay record(s) on d, none with 'from=')"
                    )
    else:
        print(
            "  GFS public-space content skipped — run 'gfs-space-subscribe' / "
            "'gfs-space-post' / 'gfs-space-rotate' to exercise"
        )

    # 13f. A global space with followers OFF (``gfs-space-no-subscribers``):
    #    still LISTED in the GFS directory, still unreadable by everyone else,
    #    and still reported as not-readable on the wire. Gated on the step
    #    having run — the whole gfs-* chain is opt-in.
    if "gfs_no_subscribers_space_id" in state:
        io_space_id = state["gfs_no_subscribers_space_id"]
        io_post_id = state.get("gfs_no_subscribers_post_id")
        s, payload = _request(f"http://127.0.0.1:{GFS_PORT}/gfs/spaces")
        if s != 200:
            failures.append(f"GFS: GET /gfs/spaces failed: HTTP {s}")
        else:
            rows = payload.get("spaces", []) if isinstance(payload, dict) else []
            row = next((sp for sp in rows if sp.get("space_id") == io_space_id), None)
            if row is None:
                failures.append(
                    f"GFS: space {io_space_id} dropped out of the directory — "
                    "a space with followers off must stay listed so people can "
                    "discover it and ask for an invite",
                )
            elif row.get("allow_subscribers") is not False:
                failures.append(
                    f"GFS: space {io_space_id} now reports allow_subscribers="
                    f"{row.get('allow_subscribers')!r} — nobody turned it on",
                )
            elif row.get("join_mode") != "open":
                failures.append(
                    f"GFS: space {io_space_id} reports join_mode="
                    f"{row.get('join_mode')!r}, expected 'open' — the two "
                    "directory dials are independent and both must round-trip",
                )
            else:
                print("  the GFS still lists it, open-to-join and unreadable ✓")
        if io_post_id:
            for label in ("d", "c"):
                if _rows(
                    label, "SELECT id FROM space_posts WHERE id = ?", (io_post_id,)
                ):
                    failures.append(
                        f"{label}: holds post {io_post_id} — content of a "
                        "space that allows no followers reached a household "
                        "that was never let in",
                    )
                else:
                    print(f"  {label} still cannot see that post ✓")

    # 13g. §D2b invite-link durability (``gfs-invite-link`` /
    #    ``gfs-invite-link-content``). Everything the two steps proved has
    #    to still be true at the end of the run: the pair is a
    #    space-scoped seat with no address on both sides, the content that
    #    crossed the relay is still there, and the revoked /join page is
    #    still dead. Gated on the steps having run — the whole gfs-* chain
    #    is opt-in.
    if state.get("gfs_invite_space_id"):
        il_space = state["gfs_invite_space_id"]
        for holder, peer in (("a", "e"), ("e", "a")):
            try:
                row = _instance_row(holder, state["instances"][peer]["instance_id"])
            except Exception as exc:
                failures.append(f"{holder}: seat re-check failed: {exc!r}")
                continue
            if row is None:
                failures.append(
                    f"{holder}: the space_session seat for {peer} is gone",
                )
            elif row["source"] != "space_session":
                failures.append(
                    f"{holder}: the {peer} seat drifted to source="
                    f"{row['source']!r} — a link-joined household must never "
                    "become a social peer (it would join every DM / presence "
                    "/ moment fan-out)",
                )
            elif row["remote_inbox_url"]:
                failures.append(
                    f"{holder}: the {peer} seat acquired remote_inbox_url="
                    f"{row['remote_inbox_url']!r} — neither household may "
                    "learn the other's address on this path",
                )
            else:
                print(f"  {holder} still holds {peer} as an addressless seat ✓")
        il_post = state.get("gfs_invite_post_id")
        if il_post:
            if _rows("e", "SELECT id FROM space_posts WHERE id = ?", (il_post,)):
                print("  e still holds the post that came over the relay ✓")
            else:
                failures.append(
                    f"e: post {il_post} is gone — it arrived over the "
                    "connection-server relay and should persist like any other",
                )
        il_rotated = state.get("gfs_invite_rotated_post_id")
        if il_rotated and not _rows(
            "e", "SELECT id FROM space_posts WHERE id = ?", (il_rotated,)
        ):
            failures.append(
                f"e: the post-rotation post {il_rotated} is gone — the re-key "
                "reached e once and then the row was lost",
            )
        il_url = state.get("gfs_invite_join_url")
        if il_url:
            s, dead_body = _open_through_viewer(
                il_url,
                view="join",
                key="t",
                value=il_url.rstrip("/").rsplit("/", 1)[-1],
                what="verify: revoked invite",
            )
            dead = json.dumps(dead_body)
            if s != 404:
                failures.append(
                    f"GFS: the revoked /join page answers HTTP {s}, expected "
                    "404 — a revoked link must stay a dead link",
                )
            elif state.get("gfs_invite_token", "") in dead:
                failures.append(
                    "GFS: the revoked /join page echoes the dead token back",
                )
            else:
                print("  the revoked /join page is still a token-free 404 ✓")
        il_admin = state.get("gfs_invite_admin_space_id")
        if il_admin:
            seed = _rows(
                "e",
                "SELECT identity_private_key FROM spaces WHERE id = ?",
                (il_admin,),
            )
            if seed and seed[0][0]:
                failures.append(
                    f"e: holds the signing seed for {il_admin} — an admin met "
                    "through a public link must never hold space authority",
                )
            else:
                print("  e's link-admin seat still carries no signing seed ✓")
        if not il_space:
            failures.append("gfs-invite-link state is incoherent (no space id)")
    else:
        print(
            "  §D2b invite-link skipped — run 'gfs-invite-link' / "
            "'gfs-invite-link-content' to exercise"
        )

    # 13h. Local community (``local-community``, #1057): everyone the step
    #    seated — paired (b), mesh (c), edge-after-review (a), GFS (e) —
    #    still holds a seat on the host, the host's queue keeps verdicts
    #    only, and no household's DB grew a join point since. Gated: the
    #    step needs the opt-in GFS chain.
    lc = state.get("local_community")
    if lc:
        lc_space = lc["space_id"]
        for who, uid in lc["seated"].items():
            if not _lc_seated("d", lc_space, uid):
                failures.append(f"d: local community lost {who}'s seat")
            if not _lc_member_here(who, lc_space, uid):
                failures.append(f"{who}: no longer holds the local community")
        verdicts = {
            r[0]
            for r in _rows(
                "d",
                "SELECT area_verdict FROM space_join_requests WHERE space_id=?",
                (lc_space,),
            )
        }
        if not verdicts <= {"inside", "edge"}:
            failures.append(f"d: local-community join requests hold {verdicts!r}")
        for holder in ("a", "b", "c", "d", "e"):
            hits = _db_mentions(
                _instance_dir(holder) / "socialhome.db", ["join_location"], []
            )
            if hits:
                failures.append(f"{holder}: stored a join location: {hits}")
        print("  local community: seats held, verdicts only, no join point stored ✓")
    else:
        print("  local community skipped — run 'local-community' to exercise")

    # 13b. admin-promote-kick durability — dave's promotion on d must
    #    still read 'admin' after everything that ran since (app-session,
    #    remote-invite-decline, replay's c restart). Gated because ``all``
    #    runs ``verify`` before that step; a standalone ``verify`` after
    #    it re-asserts the row.
    if state.get("admin_promote_kick_ran"):
        apk_space = state.get("remote_invite_routed_space_id")
        apk_d = state["instances"]["d"]
        try:
            apk_rows = _rows(
                "d",
                "SELECT role FROM space_members WHERE space_id=? AND user_id=?",
                (apk_space, apk_d["user_id"]),
            )
        except Exception as exc:
            failures.append(f"d: admin-promote-kick role re-check failed: {exc!r}")
        else:
            if not apk_rows:
                failures.append(
                    f"d: dave's space_members row for {apk_space} is gone "
                    "(admin-promote-kick seated him as admin)",
                )
            elif apk_rows[0][0] != "admin":
                failures.append(
                    f"d: dave's role for {apk_space} is {apk_rows[0][0]!r}, "
                    "expected 'admin' — the admin-promote-kick promotion "
                    "did not survive",
                )
            else:
                print("  d still holds dave's admin role ✓")

    # 13c. v_36: new posts, events and moments can only be announced by
    #     the household that created them — their ids commit to their
    #     creator. The ids traffic / calendar minted must verify for the
    #     creating user (and the other households accepted them on that
    #     basis: the arrival checks above ran against these same ids).
    from socialhome.federation.owner_bound_id import (
        MOMENT_KIND as _MOMENT_KIND,
        SPACE_CALENDAR_EVENT_KIND as _EVENT_KIND,
        SPACE_POST_KIND as _POST_KIND,
        OwnerBinding as _OwnerBinding,
        check_owner_bound_id as _check_bound,
    )

    _bound_cases: list[tuple[str, str, str, str, str]] = [
        (
            f"{label}'s moment",
            _MOMENT_KIND,
            str(m.get("id") or ""),
            "",
            state["instances"][label]["user_id"],
        )
        for label, m in (state.get("moments") or {}).items()
    ]
    if state.get("space_id"):
        _b_uid = state["instances"]["b"]["user_id"]
        if state.get("bazaar_listing_id"):
            _bound_cases.append(
                (
                    "b's bazaar wrapper post",
                    _POST_KIND,
                    state["bazaar_listing_id"],
                    state["space_id"],
                    _b_uid,
                )
            )
        if state.get("calendar_event_id"):
            _bound_cases.append(
                (
                    "b's space calendar event",
                    _EVENT_KIND,
                    state["calendar_event_id"],
                    state["space_id"],
                    _b_uid,
                )
            )
    for _what, _kind, _rid, _sp, _owner in _bound_cases:
        _binding = _check_bound(_kind, _rid, space_id=_sp, owner_user_id=_owner)
        if _binding is not _OwnerBinding.VALID:
            failures.append(
                f"{_what} id {_rid} is {_binding.value}, not bound to its "
                f"creator {_owner!r} (v_36)"
            )
        else:
            print(f"  {_what} id is bound to its creator (v_36) ✓")

    # 13x. ``gfs-space-follower-names`` (v_61): e, a follower that joined
    #      after Erin's profile went out, still names her. Opt-in.
    gsfn = state.get("gfs_space_follower_names")
    if gsfn:
        e = state["instances"]["e"]
        s, body = _request(
            f"http://127.0.0.1:{e['port']}/api/spaces/{gsfn['space_id']}/authors",
            token=e["token"],
        )
        names = {r["user_id"]: r.get("display_name") for r in body} if s == 200 else {}
        if names.get(gsfn["user_id"]) != ERIN_DISPLAY_NAME:
            failures.append(f"e: new follower does not name Erin ({names!r})")
        else:
            print("  e (new follower) names Erin ✓")

    # 13y. ``gfs-space-author-profile`` (v_60): d, a follower with no roster,
    #      still names Erin with her bio from her published profile. Opt-in.
    gsap = state.get("gfs_space_author_profile")
    if gsap:
        d = state["instances"]["d"]
        s, body = _request(
            f"http://127.0.0.1:{d['port']}/api/spaces/{gsap['space_id']}/authors",
            token=d["token"],
        )
        rows = {r["user_id"]: r for r in body} if s == 200 else {}
        got = rows.get(gsap["user_id"])
        if not got or (got.get("display_name"), got.get("bio")) != (
            ERIN_DISPLAY_NAME,
            gsap["bio"],
        ):
            failures.append(f"d: GFS follower's author profile for Erin is {got!r}")
        else:
            print("  d names Erin from her published profile ✓")

    # 13z. ``gfs-moment-profile``: e (paired with nobody) still names a's
    #      user on the public moment by name and bio, never the id — and its
    #      follow row still holds the signed profile. Opt-in.
    gmp = state.get("gfs_moment_profile")
    if gmp:
        e = state["instances"]["e"]
        s, body = _request(
            f"http://127.0.0.1:{e['port']}/api/moments/{gmp['moment_id']}",
            token=e["token"],
        )
        author = (body or {}).get("moment", {}).get("author") if s == 200 else None
        if not author:
            failures.append(f"e: public moment {gmp['moment_id']} gone (HTTP {s})")
        elif (author.get("display_name"), author.get("bio")) != (
            gmp["display_name"],
            gmp["bio"],
        ):
            failures.append(f"e: public moment author drifted to {author!r}")
        else:
            print("  e names the GFS-followed author with name + bio ✓")
        rows = _rows(
            "e",
            "SELECT followed_display_name, followed_bio, followed_picture_digest"
            " FROM moment_public_follows WHERE followed_user_id = ?",
            (gmp["user_id"],),
        )
        if not rows or rows[0][0] != gmp["display_name"] or not rows[0][2]:
            failures.append(f"e: moment_public_follows profile is {rows!r}")
        # v_68 — e's reaction to Noor's public moment reached a through the
        # GFS (and nothing paired them).
        if gmp.get("reaction"):
            held = _rows(
                "a",
                "SELECT emoji FROM moment_reactions"
                " WHERE moment_id = ? AND reactor_user_id = ?",
                (gmp["moment_id"], e.get("user_id")),
            )
            if held != [(gmp["reaction"],)]:
                failures.append(
                    f"a: e's reaction through the GFS is {held!r}, expected "
                    f"{gmp['reaction']!r}"
                )
            else:
                print("  v_68 a holds e's reaction that came through the GFS ✓")
            # v_69 — and e holds a's totals for it, through the GFS.
            totals = _rows(
                "e",
                "SELECT reaction_summary FROM moments WHERE id = ?",
                (gmp["moment_id"],),
            )
            got = json.loads(totals[0][0]) if totals and totals[0][0] else None
            if not got or got.get("counts", {}).get(gmp["reaction"]) != 1:
                failures.append(f"e: a's totals through the GFS are {got!r}")
            else:
                print("  v_69 e holds a's reaction totals that came through the GFS ✓")

    # 14. Log audit — scan each backend's stdout/stderr for unhandled
    #    exceptions, ERROR-level lines, federation-pipeline rejects.
    #    Anything we can't account for (i.e. doesn't match the
    #    benign-noise allow-list) becomes a verify failure so the
    #    next run forces it to be either fixed or explicitly excused.
    failures.extend(_audit_logs(state))

    if failures:
        print("\n--- FAIL ---")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    print("verify: ok")


# ─── Log audit — surface backend exceptions ────────────────────────────────


#: Substrings that mark a log line as known-benign noise we should NOT
#: flag in :func:`_audit_logs`. Each entry is a comment so the next
#: person to trip a new line knows whether to suppress or fix.
_LOG_BENIGN: tuple[str, ...] = (
    # Outbox retry warning while a peer is briefly unreachable —
    # expected during the inner-ring pair handshake settle window
    # (peer is booting / DNS/RPS not yet warm). Narrow to the
    # specific "returned HTTP" pattern so a *different* outbox
    # error (e.g. shutdown corruption, schema mismatch) still
    # surfaces.
    "outbox: ",
    " returned HTTP ",
    # libdatachannel native ICE state-machine status lines. The
    # harness intentionally configures STUN against an external
    # endpoint while the instances themselves talk loopback-only,
    # so the C library logs candidate-gathering failures /
    # connectivity-timer expiries that are pure environment noise.
    # Narrow to the specific juice/SCTP status messages so a
    # different libdatachannel-level error still surfaces.
    "juice: Changing state to",
    "juice: Connectivity timer",
    "juice: Got STUN mapped address",
    "juice: STUN server binding successful",
    "juice: Candidate gathering done",
    "juice: Using STUN server",
    # Outbox terminal-drop on HTTP 410: by design when dual-transport
    # delivery (perfect-negotiation RTC + HTTPS-inbox fallback)
    # causes the second arrival to hit the receiver's replay cache.
    # The outbox correctly gives up after the 410. A regression
    # would look like the SAME ``msg_id`` being dropped without the
    # receiver ever processing it — but the per-step content checks
    # in ``cmd_verify`` (e.g. "c received a→c DM") catch that case,
    # so this log line on its own is benign.
    "OutboxProcessor: peer permanently rejected",
    "returned terminal HTTP 410",
    # HTTPS-inbox transient failure during the inner-ring handshake
    # window. Pairs with the outbox-retry path above; the outbox
    # observes the error, reschedules, and the eventual delivery
    # succeeds via the other transport. The trailing empty error
    # text (``failed: ``) shows when ``str(aiohttp.ClientConnectorError)``
    # returns empty — that's a cosmetic stdlib quirk, not a logic
    # bug.
    "HTTPS-inbox send to ",
    # DTLS handshake timing out on the WebRTC peer-connection. On
    # loopback the perfect-negotiation glare resolution (PR #384)
    # frequently aborts one side's offer mid-DTLS, leaving the C++
    # library to log a handshake timeout before the PeerConnection
    # transitions to ``failed`` / ``closed``. Federation falls
    # through to HTTPS-inbox immediately, so message delivery is
    # unaffected — ``cmd_verify``'s content assertions catch any
    # actual missed delivery regardless of which transport carried
    # it. If you see DTLS timeouts WITHOUT a subsequent successful
    # HTTPS-inbox delivery (message body never lands), THAT is the
    # real regression — start with the perfect-negotiation logic
    # in ``socialhome/federation/transport.py``.
    # SCTP stream reset failing while a peer's association is already
    # gone: steps that kill a household to simulate it going offline
    # (e.g. ``gfs-member-publish`` stopping host a before ``verify``)
    # leave its peers' DataChannels to be torn down against a dead
    # association, and libdatachannel logs the failed reset. Delivery
    # is unaffected (the step's own content checks prove it). Narrow
    # to this exact native message so other SCTP errors still surface.
    "SctpTransport::sendReset",
    "DTLS handshake failed",
    "DTLS recv: Handshake timeout",
    # Same envelope of RTC-init noise on the Python side: an ICE
    # candidate arrives after the 30 s buffer window because the
    # peer never produced an SDP (glare aborted one side). The
    # candidate is dropped; the fallback transport carries the
    # message.
    "fed RTC: ICE candidate for ",
    # The rate-limit middleware's own audit log fires when
    # relay-pair waits the 65 s window; expected by design.
    "rate_limit",
    # TURN-server advisory fires on every cold start when no
    # ``webrtc_turn_url`` is configured. The demo runs all four
    # households on loopback where STUN alone is sufficient, so
    # this is pure operator-facing advice for production
    # deployments — keep at WARN level there but suppress in the
    # harness audit. A different webrtc_ice WARNING (e.g. ICE
    # gathering failure, bad TURN credentials) would still surface.
    "WebRTC: no TURN server configured",
    # The harness's OWN negative control in the ``gfs-*`` chain. Both
    # ``cmd_gfs_space_post`` step 5 and ``cmd_verify`` 13c ask **c** — a
    # household that is deliberately not a member, not a subscriber and not
    # GFS-paired — for the global space's feed, to prove the §"non-member
    # households MUST NOT see space content" rule on the real wire. c does not
    # hold that space, so ``SpaceFeedView`` maps a ``KeyError`` to 404 and
    # ``BaseView`` logs the mapping at WARNING. The 404 IS the expected
    # outcome; the assertion that matters (c holds no ``space_posts`` row and
    # its feed exposes neither post) runs regardless. Only fires when the
    # opt-in GFS chain is run in the same session as ``verify``.
    "SpaceFeedView: KeyError surfaced from handler",
    # Earlier entries removed because the underlying conditions
    # were fixed upstream (instead of permanently allowlisted):
    # * ``FederationEventType.FEDERATION_RTC_ICE`` /
    #   ``rtcAddRemoteCandidate: runtime failure`` /
    #   ``Got a remote candidate without remote description``
    #   were all symptoms of an offer-vs-candidate race in
    #   ``aiolibdatachannel`` < 2026.5.10. The 2026.5.10 buffer
    #   fix eliminates them entirely; if they reappear, the
    #   audit catches a regression.
    # * The previous ``rtc::impl::IceTransport::LogCallback`` /
    #   ``aiolibdatachannel:rtc::`` blanket-suppressions covered
    #   every C++-side log including *real* ERROR lines. Replaced
    #   with the specific juice/STUN status entries above so a
    #   genuine libdatachannel failure still trips the audit.
)

#: Substrings that — when they appear — are real signal worth
#: surfacing. These are the patterns an exception traceback or an
#: explicit ``log.error`` / ``log.warning`` produces.
_LOG_INTERESTING: tuple[str, ...] = (
    "Traceback (most recent call last):",
    "ERROR:",
    "WARNING:",
    "Exception:",
)


def _audit_logs(state: dict) -> list[str]:
    """Return a list of failure strings, one per offending log block.

    Each instance writes ``log.txt`` under its data dir; the GFS does
    the same under ``gfs/log.txt``. We split the file into "blocks"
    (each block is a single ERROR/WARNING/Traceback and the
    indented frames that follow it), and a block is suppressed if
    *any* line within it matches a substring in :data:`_LOG_BENIGN`.
    That way an RTC ICE traceback whose tail line names the
    allow-listed cause stays suppressed even though the leading
    ``Traceback`` line itself doesn't contain the benign needle.
    Reports up to 5 distinct offending blocks per file (anything more
    is usually the same root cause repeating)."""
    failures: list[str] = []
    with _NOISE_LOCK:
        windows_all = _load_noise_windows()
    sources = list((state.get("instances") or {}).items())
    if state.get("gfs"):
        sources.append(("gfs", {"log_path": str(GFS_DIR / "log.txt")}))
    for label, info in sources:
        path = Path(info.get("log_path") or _instance_dir(label) / "log.txt")
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        windows = _noise_windows_for(label, windows_all)
        hits: list[str] = []
        for lineno, header, block_text in _split_log_into_numbered_blocks(text):
            if not any(marker in header for marker in _LOG_INTERESTING):
                continue
            if any(needle in block_text for needle in _LOG_BENIGN):
                continue
            if _expected_in_window(lineno, block_text, windows):
                continue
            hits.append(header)
            if len(hits) >= 5:
                break
        for h in hits:
            failures.append(f"{label}: log audit — {h.strip()[:200]}")
    return failures


def _split_log_into_blocks(text: str) -> list[tuple[str, str]]:
    """:func:`_split_log_into_numbered_blocks` without the line numbers."""
    return [(h, b) for _, h, b in _split_log_into_numbered_blocks(text)]


def _split_log_into_numbered_blocks(text: str) -> list[tuple[int, str, str]]:
    """Group lines into ``(header_lineno, header, full_block)`` tuples
    (``header_lineno`` 0-based — what a noise window's bounds count).

    A block starts at the first non-indented line with one of
    :data:`_LOG_INTERESTING` markers and continues through every
    subsequent indented frame (``  File "...``,
    ``    cursor.execute(...)`` etc.) plus the final exception-type
    summary line. Blank lines reset the block. The header is what
    we match for "is this interesting"; the full block text is what
    the benign filter scans, so a Traceback whose final line names
    a known-benign cause gets suppressed cleanly.
    """
    out: list[tuple[int, str, str]] = []
    cur_header: str | None = None
    cur_lineno = 0
    cur_lines: list[str] = []

    def _flush() -> None:
        nonlocal cur_header, cur_lines
        if cur_header is not None:
            out.append((cur_lineno, cur_header, "\n".join(cur_lines)))
        cur_header = None
        cur_lines = []

    for lineno, line in enumerate(text.splitlines()):
        if not line.strip():
            _flush()
            continue
        # Continuation lines: leading whitespace, or the
        # ``ExceptionType: ...`` summary that closes a Traceback.
        is_continuation = cur_header is not None and (
            line.startswith((" ", "\t")) or _looks_like_exc_summary(line)
        )
        if is_continuation:
            cur_lines.append(line)
            continue
        # Otherwise this line starts a new (header) block.
        _flush()
        cur_header = line
        cur_lineno = lineno
        cur_lines = [line]
    _flush()
    return out


#: Python logging level prefixes that look exception-shaped but are
#: actually log-record headers (``LEVEL:logger.name:message``). Without
#: this guard the block splitter folds every ERROR / WARNING / INFO
#: line into the previous block as a "continuation," so the whole
#: log collapses to one giant block and the benign-suppression
#: corpus contains every needle. Result: the audit silently misses
#: real errors (e.g. DTLS handshake failures, outbox terminal
#: drops). Pin: ``test_audit_block_splitter_separates_log_levels`` in
#: ``tests/skills/test_federation_demo_audit.py``.
_LOG_LEVEL_PREFIXES: frozenset[str] = frozenset(
    {
        "DEBUG",
        "INFO",
        "WARNING",
        "ERROR",
        "CRITICAL",
        "FATAL",
        "NOTSET",
    }
)


def _looks_like_exc_summary(line: str) -> bool:
    """Heuristic: lines that look like ``ExceptionType: detail`` —
    closing summary of a traceback. We coalesce them onto the
    in-progress block so the benign filter can scan them.

    Accepts dotted forms (``mod.sub.RTCError: …``) as well as bare
    type names (``ValueError: …``); the rule is "no whitespace
    before the first colon, the rightmost dotted component starts
    with a capital, AND it isn't a Python logging level prefix"
    (the latter is what distinguishes a real exception summary from
    a stdlib ``LEVEL:logger.name:`` log record).
    """
    stripped = line.strip()
    if ":" not in stripped:
        return False
    head = stripped.split(":", 1)[0]
    if not head or " " in head:
        return False
    last = head.rsplit(".", 1)[-1]
    if not last or not last[0].isupper():
        return False
    # ``ERROR:`` / ``WARNING:`` / ``INFO:`` / etc. are log-level
    # prefixes, not exception types. Reject them so the block
    # splitter treats them as new headers, not continuations of
    # whatever block is currently open.
    return last not in _LOG_LEVEL_PREFIXES


# ─── Step: down ────────────────────────────────────────────────────────────


def cmd_visibility() -> None:
    """Per-pair user-visibility — Alpha hides a local user from Beta.

    Validates the outbound peer-user-visibility filter (§Connection
    Detail UX): Alpha provisions a second local user (``ada``), Beta's
    ``/api/friends`` confirms ada is mirrored locally, Alpha PATCHes
    ``/api/pairing/connections/{beta_id}/visible-users`` to hide ada,
    and we assert ada disappears from Beta's ``/api/friends`` after
    federation settles. While ada is hidden we also fire a DM, a
    moment, and an ``all_paired`` highlight from ada and assert none
    of them reach Beta — exercising every user-scoped outbound gate
    added by feat/visibility-filter-full-coverage (DM_MESSAGE,
    MOMENT_CREATED, HIGHLIGHT_CREATED/FRAME_APPENDED). Gamma is the
    positive control: ada isn't hidden there, so the same highlight
    is asserted to land on Gamma. Then we flip ada back to visible and
    confirm she reappears in Beta's ``/api/friends``.

    Sequence:
    1. Provision a second user on Alpha via
       ``POST /api/admin/users {username, password, display_name}``.
    2. Patch the new user's profile so a ``USER_UPDATED`` envelope
       fans out to Beta — i.e. Beta now mirrors ``ada`` in
       ``remote_users``. Wait briefly for federation settle.
    3. Assert Beta's ``/api/friends`` includes Alpha's household and
       lists Ada among its members.
    4. Hide ada from Beta via the visibility PATCH; the route fans a
       ``USER_REMOVED`` to Beta.
    5. Wait, then assert Beta's ``/api/friends`` no longer lists ada
       (the rest of the household stays).
    5b. While ada is hidden, fire a moment + an ``audience_kind=
        all_paired`` highlight + a 1:1 DM to Bob. Assert none of them
        land on Beta. Assert the highlight DOES land on Gamma (proves
        the filter is per-peer, not a global blackout on ada).
    6. Flip ada back to visible; the route fans a ``USER_UPDATED``.
    7. Wait, then assert Beta sees ada again.

    Prereq: ``up`` + ``pair`` (a ↔ b ↔ c must be confirmed).
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' first")

    a = state["instances"]["a"]
    b = state["instances"]["b"]
    a_url = f"http://127.0.0.1:{a['port']}"
    b_url = f"http://127.0.0.1:{b['port']}"

    # ``cmd_pair`` burns through the /api/pairing rate-limit window
    # (mostly via the four QR handshakes + their settle). The visible-
    # users PATCH lives under the same ``/api/pairing/*`` bucket as
    # /api/pairing/initiate, so a quick run of ``up → pair →
    # visibility`` would 429 here. Drain the window the same way
    # ``cmd_relay_pair`` does.
    if state.get("rate_limit_drained_for") != "visibility":
        wait = 65
        print(f"  waiting {wait}s for /api/pairing/* rate-limit window to drain...")
        time.sleep(wait)
        state["rate_limit_drained_for"] = "visibility"
        _save(state)

    # 1. Provision a second user on Alpha.
    new_username = "ada"
    new_display = "Ada Lovelace"
    s, prov = _request(
        f"{a_url}/api/admin/users",
        token=a["token"],
        method="POST",
        body={
            "username": new_username,
            "password": "harness-pwd-ada",
            "display_name": new_display,
            "is_admin": False,
        },
    )
    # 409 = already exists from a prior run; reuse.
    if s == 409:
        s, listing = _request(
            f"{a_url}/api/users",
            token=a["token"],
        )
        _must("list users(a)", s, listing)
        ada = next(
            (u for u in listing if u.get("username") == new_username),
            None,
        )
        if ada is None:
            raise SystemExit("visibility: ada exists per 409 but not in /api/users")
        ada_user_id = ada["user_id"]
    else:
        _must("provision ada(a)", s, prov, ok=(201,))
        ada_user_id = prov["user_id"]
    state["visibility_user_id"] = ada_user_id
    print(f"  a: provisioned {new_username} ({ada_user_id[:8]}…)")

    # 2. Trigger a profile update so USER_UPDATED federates to Beta.
    #    ``UserProfileUpdated`` is published by ``user_service.update_profile``
    #    which only the user themselves can invoke (PATCH /api/me).
    #    Log in as ada to get a bearer token, then PATCH /api/me.
    s, login = _request(
        f"{a_url}/api/auth/token",
        method="POST",
        body={"username": new_username, "password": "harness-pwd-ada"},
    )
    _must("ada login(a)", s, login)
    ada_token = login["token"]
    s, _ = _request(
        f"{a_url}/api/me",
        token=ada_token,
        method="PATCH",
        body={"display_name": new_display, "bio": "Hello from Alpha (visibility test)"},
    )
    _must("ada profile patch(a)", s, _)
    time.sleep(3)

    # 3. Beta sees Ada in /api/friends.
    def _friends_users_for(
        viewer_url: str, viewer_token: str, owner_iid: str
    ) -> set[str]:
        s, payload = _request(
            f"{viewer_url}/api/friends",
            token=viewer_token,
        )
        _must("friends(b)", s, payload)
        households = payload.get("households", []) or []
        for h in households:
            if h.get("instance_id") == owner_iid:
                return {m.get("user_id") for m in (h.get("members") or [])}
        return set()

    seen = _friends_users_for(b_url, b["token"], a["instance_id"])
    if ada_user_id not in seen:
        raise SystemExit(
            f"visibility precheck: ada {ada_user_id} not yet visible to Beta "
            f"(saw {sorted(seen)!r}) — federation may not have settled yet",
        )
    print("  pre-check: Beta sees ada via /api/friends ✓")

    # 3b. Resolve Bob's user_id from Alpha's federated view (Alpha
    #     mirrors Beta's users in ``remote_users`` after the USERS_SYNC
    #     on pair-confirm). Used both for the pre-hide DM (step 3c) and
    #     the during-hide DM (step 5b).
    s, alpha_friends = _request(f"{a_url}/api/friends", token=a["token"])
    _must("alpha friends list", s, alpha_friends)
    bob_user_id: str | None = None
    for h in alpha_friends.get("households") or []:
        if h.get("instance_id") == b["instance_id"]:
            for m in h.get("members") or []:
                if m.get("remote_username") == "bob":
                    bob_user_id = m.get("user_id")
                    break
    if bob_user_id is None:
        raise SystemExit("visibility: cannot resolve bob user_id from Alpha")

    # 3c. While ada is still visible, post a moment + highlight from
    #     ada AND open a DM ada→bob so Beta receives them. Step 5
    #     (hide) then proves the cascade purge: USER_REMOVED triggers
    #     receiver-side hard-delete of every moment / highlight / DM
    #     conversation involving the deprovisioned user.
    pre_needle = f"pre-{int(time.time())}"
    print(f"  pre-hide: ada posts moment + highlight + DM (needle={pre_needle})")

    s, _mom = _request(
        f"{a_url}/api/moments",
        token=ada_token,
        method="POST",
        body={"content": f"[ada pre-hide] moment {pre_needle}"},
    )
    # Allow 429 (1-per-15-min); the cascade assertion below still
    # holds when the moment never gets created (trivially no row to
    # purge), and the highlight/DM legs are enough to prove cascade.
    if s not in (201, 429):
        _must("ada pre-hide moment(a)", s, _mom, ok=(201, 429))

    s, _hl = _request(
        f"{a_url}/api/highlights/frames",
        token=ada_token,
        method="POST",
        body={
            "media_url": _upload_demo_image(
                a_url, token=ada_token, label="ada pre-hide"
            ),
            "frame_type": "image",
            "caption_text": f"[ada pre-hide] highlight {pre_needle}",
            "audience_kind": "all_paired",
        },
    )
    _must("ada pre-hide highlight(a)", s, _hl, ok=(201,))

    s, conv = _request(
        f"{a_url}/api/conversations/dm",
        token=ada_token,
        method="POST",
        body={"user_id": bob_user_id},
    )
    _must("ada→bob dm conv (pre-hide)", s, conv, ok=(200, 201))
    pre_conv_id = conv.get("id")
    s, _dm = _request(
        f"{a_url}/api/conversations/{pre_conv_id}/messages",
        token=ada_token,
        method="POST",
        body={"content": f"[ada→bob pre-hide] dm {pre_needle}"},
    )
    _must("ada→bob dm message (pre-hide)", s, _dm, ok=(200, 201))

    # Let federation settle so Beta has time to mirror the content.
    time.sleep(6)

    # Sanity: Beta has at least the highlight (the most reliable of
    # the three to land — moment 429s on rate-limit, DM rides relay).
    def _captions(url: str, token: str) -> set[str]:
        s, hls = _request(f"{url}/api/highlights", token=token)
        _must("highlights", s, hls)
        caps: set[str] = set()
        for h in hls if isinstance(hls, list) else []:
            for f in h.get("frames") or []:
                caps.add(f.get("caption_text") or "")
        return caps

    beta_pre_caps = _captions(b_url, b["token"])
    if not any(pre_needle in cap for cap in beta_pre_caps):
        raise SystemExit(
            f"visibility precheck: Beta should have ada's pre-hide "
            f"highlight before we hide her — saw {sorted(beta_pre_caps)!r}",
        )
    print("  pre-hide: Beta has ada's highlight ✓ (cascade-target seeded)")

    # 4. Hide ada from Beta.
    s, body = _request(
        f"{a_url}/api/pairing/connections/{b['instance_id']}/visible-users",
        token=a["token"],
        method="PATCH",
        body={"updates": [{"user_id": ada_user_id, "visible": False}]},
    )
    _must("hide ada(a→b)", s, body)
    rows = {u["user_id"]: u for u in body.get("users", [])}
    if rows.get(ada_user_id, {}).get("visible") is not False:
        raise SystemExit(
            f"visibility: hide PATCH did not flip ada to hidden — got {body!r}",
        )
    print("  a hid ada from Beta — USER_REMOVED fan-out queued")

    # 5. Wait for USER_REMOVED to land and Beta's mirror to drop ada.
    time.sleep(4)
    seen_after_hide = _friends_users_for(b_url, b["token"], a["instance_id"])
    if ada_user_id in seen_after_hide:
        raise SystemExit(
            f"visibility: Beta still sees ada {ada_user_id} after hide — "
            f"saw {sorted(seen_after_hide)!r}",
        )
    print("  post-hide: Beta no longer lists ada ✓")

    # 5a. Cascade purge. USER_REMOVED inbound on Beta hard-deletes
    #     every moment / highlight authored by ada plus every DM
    #     conversation she ever sent in. The pre-hide highlight we
    #     seeded at step 3c must therefore be gone from Beta's
    #     ``/api/highlights`` view.
    beta_caps_after_hide = _captions(b_url, b["token"])
    if any(pre_needle in cap for cap in beta_caps_after_hide):
        raise SystemExit(
            f"visibility cascade: Beta still has ada's pre-hide highlight "
            f"after USER_REMOVED — purge didn't fire. needle={pre_needle}, "
            f"caps={sorted(beta_caps_after_hide)!r}",
        )
    print("  cascade: Beta purged ada's pre-hide highlight ✓")

    # And the pre-hide DM conversation is hard-deleted from Beta's
    # side — walk every conversation and grep for the needle.
    def _beta_message_bodies() -> list[str]:
        s, beta_convs = _request(f"{b_url}/api/conversations", token=b["token"])
        _must("beta conversations", s, beta_convs)
        rows = (
            beta_convs
            if isinstance(beta_convs, list)
            else beta_convs.get("conversations") or []
        )
        bodies: list[str] = []
        for cv in rows:
            cid = cv.get("id")
            if not cid:
                continue
            s, msgs = _request(
                f"{b_url}/api/conversations/{cid}/messages",
                token=b["token"],
            )
            if s != 200:
                continue
            mrows = msgs if isinstance(msgs, list) else msgs.get("messages") or []
            for m in mrows:
                bodies.append(m.get("content") or "")
        return bodies

    beta_bodies_after_hide = _beta_message_bodies()
    if any(pre_needle in body for body in beta_bodies_after_hide):
        raise SystemExit(
            f"visibility cascade: Beta still has ada's pre-hide DM body "
            f"after USER_REMOVED — purge didn't fire. needle={pre_needle}",
        )
    print("  cascade: Beta purged ada's pre-hide DM conversation ✓")

    # 5b. While ada is hidden, exercise the user-scoped outbound gates
    #     added by feat/visibility-filter-full-coverage: DM, moment,
    #     highlight. Beta must NOT receive any of them; Gamma (still
    #     un-blocked) IS allowed to see ada's all_paired highlight,
    #     so the filter is proven per-peer rather than global.
    c = state["instances"]["c"]
    c_url = f"http://127.0.0.1:{c['port']}"
    needle = f"hide-{int(time.time())}"
    print(f"  ada fires DM + moment + highlight while hidden (needle={needle})")

    # 5b.i — moment (fans MOMENT_CREATED to every paired peer; gated
    # per-peer on author_user_id == ada).
    s, mom_resp = _request(
        f"{a_url}/api/moments",
        token=ada_token,
        method="POST",
        body={"content": f"[ada hidden] moment {needle}"},
    )
    # 429 is acceptable (a prior cmd_traffic run on the same ada
    # could have hit the 1-per-15-min window; the per-peer assertions
    # below don't need a fresh moment if the rate limit fired).
    if s not in (201, 429):
        _must("ada moment(a)", s, mom_resp, ok=(201, 429))

    # 5b.ii — highlight with audience_kind=all_paired (fans
    # HIGHLIGHT_CREATED to a's paired peers; Beta filtered, Gamma not).
    s, _hl = _request(
        f"{a_url}/api/highlights/frames",
        token=ada_token,
        method="POST",
        body={
            "media_url": _upload_demo_image(a_url, token=ada_token, label="ada"),
            "frame_type": "image",
            "caption_text": f"[ada hidden] highlight {needle}",
            "audience_kind": "all_paired",
        },
    )
    _must("ada highlight(a)", s, _hl, ok=(201,))

    # 5b.iii — ada → bob DM (DM_MESSAGE; gated per-peer on
    # sender_user_id == ada).
    s, conv = _request(
        f"{a_url}/api/conversations/dm",
        token=ada_token,
        method="POST",
        body={"user_id": bob_user_id},
    )
    _must("ada→bob dm conv", s, conv, ok=(200, 201))
    dm_conv_id = conv.get("id")
    s, dm_msg = _request(
        f"{a_url}/api/conversations/{dm_conv_id}/messages",
        token=ada_token,
        method="POST",
        body={"content": f"[ada→bob hidden] dm {needle}"},
    )
    _must("ada→bob dm message", s, dm_msg, ok=(200, 201))

    # Settle: gates fire at send-time so envelopes are dropped before
    # they hit the outbox — but the local writes still happen, the
    # event bus still publishes, and the local realtime path still
    # runs. A few seconds is plenty for any cross-instance frame that
    # *would* arrive to do so on a quiet loopback host.
    time.sleep(6)

    # 5b.iv — Beta must NOT see ada's moment.
    s, beta_moments = _request(f"{b_url}/api/moments", token=b["token"])
    _must("beta moments", s, beta_moments)
    beta_moment_blobs = (
        beta_moments
        if isinstance(beta_moments, list)
        else beta_moments.get("moments") or []
    )
    beta_moment_texts: list[str] = []
    for m in beta_moment_blobs:
        body = m.get("data") if isinstance(m.get("data"), dict) else m
        beta_moment_texts.append(body.get("content") or "")
    if any(needle in t for t in beta_moment_texts):
        raise SystemExit(
            f"visibility: Beta received ada's moment despite hide — needle={needle}",
        )
    print("  post-hide: ada's moment did NOT reach Beta ✓")

    # 5b.v — Beta must NOT see ada's highlight.
    beta_caps = _captions(b_url, b["token"])
    if any(needle in c for c in beta_caps):
        raise SystemExit(
            f"visibility: Beta received ada's highlight despite hide — needle={needle}",
        )
    print("  post-hide: ada's highlight did NOT reach Beta ✓")

    # 5b.vi — Beta must NOT see ada's DM body. Walk every conversation
    # on Beta's side and grep message bodies for the needle.
    s, beta_convs = _request(f"{b_url}/api/conversations", token=b["token"])
    _must("beta conversations", s, beta_convs)
    beta_conv_rows = (
        beta_convs
        if isinstance(beta_convs, list)
        else beta_convs.get("conversations") or []
    )
    saw_dm = False
    for cv in beta_conv_rows:
        cid = cv.get("id")
        if not cid:
            continue
        s, msgs = _request(
            f"{b_url}/api/conversations/{cid}/messages",
            token=b["token"],
        )
        if s != 200:
            continue
        rows = msgs if isinstance(msgs, list) else msgs.get("messages") or []
        for m in rows:
            if needle in (m.get("content") or ""):
                saw_dm = True
                break
        if saw_dm:
            break
    if saw_dm:
        raise SystemExit(
            f"visibility: Beta received ada's DM body despite hide — needle={needle}",
        )
    print("  post-hide: ada's DM did NOT reach Beta ✓")

    # 5b.vii — Positive control: Gamma is NOT in the hide set, so an
    # ``audience_kind=all_paired`` highlight from ada must still fan
    # there. This proves the filter is per-peer (peer_user_visibility
    # is keyed on ``instance_id``) rather than a global blackout on
    # the sender.
    gamma_caps = _captions(c_url, c["token"])
    if not any(needle in cap for cap in gamma_caps):
        raise SystemExit(
            f"visibility: Gamma should have seen ada's highlight "
            f"(filter is per-peer, not global) — needle={needle}, "
            f"gamma_caps={sorted(gamma_caps)!r}",
        )
    print("  positive: Gamma sees ada's highlight (filter is per-peer) ✓")

    # 6. Flip back to visible — Alpha's PATCH sends USER_UPDATED.
    s, body = _request(
        f"{a_url}/api/pairing/connections/{b['instance_id']}/visible-users",
        token=a["token"],
        method="PATCH",
        body={"updates": [{"user_id": ada_user_id, "visible": True}]},
    )
    _must("unhide ada(a→b)", s, body)
    print("  a re-exposed ada to Beta — USER_UPDATED fan-out queued")

    # 7. Wait for USER_UPDATED to land and Beta to repopulate the row.
    #    Outbox redelivery + USER_UPDATED inbound + remote_users insert
    #    is several federation-pipeline hops; allow generous settle.
    deadline = time.monotonic() + 30.0
    seen_after_show: set[str] = set()
    while time.monotonic() < deadline:
        seen_after_show = _friends_users_for(b_url, b["token"], a["instance_id"])
        if ada_user_id in seen_after_show:
            break
        time.sleep(2)
    if ada_user_id not in seen_after_show:
        raise SystemExit(
            f"visibility: Beta still doesn't see ada after un-hide within 30s — "
            f"saw {sorted(seen_after_show)!r}",
        )
    print("  post-unhide: Beta sees ada again ✓")

    state["visibility_ran"] = True
    _save(state)
    print("visibility: ok (per-pair user-visibility filter works both ways)")


@_offline_step
def cmd_sync_https_fallback() -> None:
    """§25.6 HTTPS chunk-stream fallback (Part C).

    Runs *after* :func:`cmd_space_sync_catchup_media` — reuses the
    space dave already joined there. Sequence:

    1. Kill dave's process. c posts new content in the shared space
       so the realtime SPACE_POST_CREATED fan-out misses dave.
    2. Restart dave with ``SH_FORCE_SYNC_HTTPS=1`` in the env. The
       scheduler's :meth:`enqueue_sync_for_space` reads this env on
       each SPACE_SYNC_BEGIN dispatch and flips ``prefer_direct``
       to ``False``, forcing the relay path regardless of whether
       WebRTC would have worked.
    3. After settle, dave's space feed must contain the new post.
       The only way it can arrive is via ``SPACE_SYNC_CHUNK``
       federation events — the DataChannel never opened (we never
       built an offer), and the realtime broadcast happened while
       dave was dead.

    Asserts the Part C wiring all the way through: requester
    BEGIN with prefer_direct=False → provider accepts in
    transport_mode="https" → ``stream_initial`` ships chunks via
    ``federation.send_event(SPACE_SYNC_CHUNK)`` → receiver's
    ``_handle_space_sync_chunk`` forwards to
    ``SpaceSyncReceiver.on_chunk`` → posts persist.

    Note the host here is reachable only over the **mesh** (dave joined
    c's space via a relay in the preceding step), which is what made
    this step the regression test for #648:

    * dave's restart drops every ephemeral private half he had minted,
      so c's cached ``target_eph_pk`` for him is dead — c would seal the
      whole stream under it and dave would discard every chunk in
      silence. Fixed by the host invalidating its cached route when it
      admits a mesh requester's BEGIN.
    * nothing used to re-issue the BEGIN at all: both scheduler triggers
      walk CONFIRMED peers, and dave's ``sync_id`` died with his
      process. Fixed by the scheduler's mesh catch-up sweep — which is
      why the settle below must outlast
      ``STARTUP_MESH_CATCHUP_DELAY_SECONDS``.

    So the assertions cover three things, not one: the new post arrives,
    the pre-invite metadata is *still* there (i.e. a real stream ran,
    rather than the needle sneaking in via c's outbox redelivery), and
    dave's log carries no ``no cached target_eph_priv`` drop.
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' first")
    if not state.get("space_sync_catchup_media_ran"):
        raise SystemExit(
            "run 'space-sync-catchup-media' first — this step reuses its space"
        )
    c = state["instances"]["c"]
    d = state["instances"]["d"]
    space_id = state["space_sync_catchup_media_space_id"]
    needle_marker = f"https-fallback-{time.time_ns()}"
    needle = f"[c] HTTPS-only post {needle_marker}"

    # 1. Tear down dave so the realtime fan-out misses him.
    _open_offline_window("d")
    print(f"  killing d (pid={d['pid']}) so realtime fan-out misses him")
    try:
        os.killpg(d["pid"], signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and _alive(d["pid"]):
        time.sleep(0.2)
    if _alive(d["pid"]):
        try:
            os.killpg(d["pid"], signal.SIGKILL)
        except ProcessLookupError:
            pass
        time.sleep(0.5)

    # c posts while dave is dead.
    s, post = _request(
        f"http://127.0.0.1:{c['port']}/api/spaces/{space_id}/posts",
        token=c["token"],
        method="POST",
        body={"type": "text", "content": needle},
    )
    _must("c posts (d offline)", s, post, ok=(201,))
    print(f"  c posted while d offline (needle={needle_marker})")

    # 2. Restart dave forcing HTTPS-mode sync.
    d_log_path = _instance_dir("d") / "log.txt"
    new_pid = _spawn("d", d["port"], extra_env={"SH_FORCE_SYNC_HTTPS": "1"})
    # ``_spawn`` opens log.txt with "wb" — the file is truncated, so the
    # post-respawn scan in step 5 starts at 0. (Bookmarking the OLD file's
    # size here, as this step once did, sliced the new, shorter log past
    # its end and the tripwire read an empty string every run.)
    d_log_before = 0
    state["instances"]["d"]["pid"] = new_pid
    # Persist the new pid IMMEDIATELY, not at the end of the step. The
    # trailing ``_save`` never runs when an assertion below fails, which left
    # ``state.json`` pointing at the pid we just killed — so ``down`` reaped a
    # dead process and the respawned d kept port 18004. The next ``up`` then
    # died with "setup_required=false on a fresh data dir" and every later run
    # was silently invalid.
    _save(state)
    _wait_ready(d["port"])
    print(f"  d respawned with SH_FORCE_SYNC_HTTPS=1: pid={new_pid}")

    # 3. Settle by POLLING, not by one fixed sleep. For a mesh-only host
    #    there is no PairingConfirmed to ride on: recovery comes from the
    #    scheduler's startup mesh catch-up sweep, which fires
    #    STARTUP_MESH_CATCHUP_DELAY_SECONDS (45 s) after boot and then
    #    retries on its backoff schedule — a household that has just
    #    rebooted usually can't route anywhere on the first pass, so the
    #    BEGIN fails ``no_route`` until the transports come up. Polling
    #    passes as soon as the metadata lands instead of always paying the
    #    worst case. (A 20 s settle, what this step used to allow, fired
    #    before the sweep even started, so it could never pass.)
    deadline = time.monotonic() + 240.0
    posts: list = []
    matched: list = []
    while time.monotonic() < deadline:
        time.sleep(10)
        s, body = _request(
            f"http://127.0.0.1:{d['port']}/api/spaces/{space_id}/feed",
            token=d["token"],
        )
        if s != 200:
            continue
        posts = body.get("posts") if isinstance(body, dict) else body
        if not isinstance(posts, list):
            raise SystemExit(f"unexpected feed shape: {body!r}")
        matched = [p for p in posts if needle_marker in (p.get("content") or "")]
        if matched:
            break
    if not matched:
        raise SystemExit(
            f"HTTPS fallback failed: d did NOT receive needle "
            f"{needle_marker!r}; saw {[p.get('content') for p in posts]!r}",
        )
    print(f"  d received the post via HTTPS fallback ✓ ({needle_marker})")

    # 4. Prove a real sync stream ran, not just an outbox redelivery of
    #    the one new post: the pre-invite metadata from the previous
    #    step must still be present after the restart. (dave keeps his
    #    DB across the respawn, so this also catches a sync that wiped
    #    or failed to re-land it.)
    pre_invite_post = state.get("space_sync_catchup_media_post_id")
    if pre_invite_post:
        ids = {p.get("id") for p in posts}
        if pre_invite_post not in ids:
            raise SystemExit(
                f"sync-https-fallback: d's feed lost the pre-invite post "
                f"{pre_invite_post} after the restart — the needle likely "
                f"arrived via outbox redelivery rather than a sync stream",
            )
        print(f"  d still has the pre-invite post {pre_invite_post} ✓")

    # 5. #648 tripwire: not one chunk may be dropped for want of a
    #    target ephemeral. This is the exact warning the bug produced,
    #    and it is silent by design — no NACK, and the host's send
    #    already reported success — so the log is the only signal.
    #
    #    Since v_28 the target logs the same ``no cached target_eph_priv``
    #    substring for an envelope it could NOT open but *nacked*
    #    (``…; nacked to <prev hop>``, INFO): that is recovery — the
    #    origin invalidates, rediscovers and retransmits — not the #648
    #    symptom. Only the pre-v_28-style silent drop (``…; dropping``,
    #    WARNING — still emitted when the previous hop is pre-v_28 or the
    #    nack send itself failed) counts here, so a line is a drop iff it
    #    carries the substring AND lacks ``nacked to``.
    if d_log_path.exists():
        d_log_after = d_log_path.read_text(errors="replace")[d_log_before:]
        drops = sum(
            1
            for line in d_log_after.splitlines()
            if "no cached target_eph_priv" in line and "nacked to" not in line
        )
        if drops:
            raise SystemExit(
                f"sync-https-fallback: d dropped {drops} routed envelope(s) "
                f"for want of a cached target_eph_priv — the host sealed "
                f"under a key that died with d's previous process (#648)",
            )
        print("  d dropped no routed envelopes for a dead ephemeral ✓")

    state["sync_https_fallback_ran"] = True
    _save(state)
    print("sync-https-fallback: ok (SPACE_SYNC_CHUNK federation transport works)")


@_offline_step
def cmd_highlight_audience() -> None:
    """Change for all of today (v_70, #1191): a narrows today's highlight to
    b's household — c deletes it and remembers the removal; a frame posted
    under the narrowed audience reaches b only; widening back gives c the
    whole day again.

    Ends where it started (``all_paired``), so later steps that post a's
    frames for everyone (``replay``) still join today's highlight.
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' first")
    a = state["instances"]["a"]
    b = state["instances"]["b"]
    a_url = f"http://127.0.0.1:{a['port']}"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _todays(label: str) -> dict | None:
        info = state["instances"][label]
        s, hls = _request(
            f"http://127.0.0.1:{info['port']}/api/highlights", token=info["token"]
        )
        _must(f"highlights({label})", s, hls)
        for row in hls:
            h = row["highlight"]
            if h["author_user_id"] == a["user_id"] and h["highlight_date"] == today:
                return row
        return None

    mine = _todays("a")
    if mine is None or mine["highlight"].get("audience_kind") != "all_paired":
        s, resp = _request(
            f"{a_url}/api/highlights/frames",
            token=a["token"],
            method="POST",
            body={
                "media_url": _upload_demo_image(a_url, token=a["token"], label="hl-aud"),
                "frame_type": "image",
                "caption_text": "[a] highlight-audience first frame",
                "audience_kind": "all_paired",
            },
        )
        _must("highlight-audience first frame(a)", s, resp, ok=(201,))
        mine = _todays("a")
    if mine is None:
        raise SystemExit("highlight-audience: a has no highlight today")
    hid = mine["highlight"]["id"]

    def _wait(what: str, check, bound: float = 30.0) -> None:
        deadline = time.monotonic() + bound
        while time.monotonic() < deadline:
            if check():
                print(f"  {what} ✓")
                return
            time.sleep(1)
        raise SystemExit(f"highlight-audience: {what} — not within {bound:.0f}s")

    _wait("c holds a's highlight before the change", lambda: _todays("c") is not None)

    # 1. Narrow to b's household: c drops out and keeps the removal.
    s, resp = _request(
        f"{a_url}/api/highlights/{hid}/audience",
        token=a["token"],
        method="PATCH",
        body={"audience_kind": "households", "audience": [b["instance_id"]]},
    )
    _must("highlight-audience narrow(a)", s, resp, ok=(200,))
    _wait("c deleted it after the narrowing", lambda: _todays("c") is None)
    _wait(
        "c remembers the removal (highlight_stamps.removed_at)",
        lambda: bool(
            _rows(
                "c",
                "SELECT 1 FROM highlight_stamps WHERE highlight_id = ?"
                " AND removed_at IS NOT NULL",
                (hid,),
            )
        ),
    )

    # 2. A frame under the narrowed audience reaches b only.
    caption = f"[a] highlight-audience b only {time.time_ns()}"
    s, resp = _request(
        f"{a_url}/api/highlights/frames",
        token=a["token"],
        method="POST",
        body={
            "media_url": _upload_demo_image(a_url, token=a["token"], label="hl-aud-b"),
            "frame_type": "image",
            "caption_text": caption,
            "audience_kind": "households",
            "audience": [b["instance_id"]],
        },
    )
    _must("highlight-audience narrowed frame(a)", s, resp, ok=(201,))
    _wait("b got the narrowed frame", lambda: caption in _highlight_captions(state, "b"))
    if _todays("c") is not None:
        raise SystemExit("highlight-audience: c holds the highlight after narrowing")
    print("  c never got the narrowed frame ✓")

    # 3. Widen back: c gets the whole day again, the new frame included.
    s, resp = _request(
        f"{a_url}/api/highlights/{hid}/audience",
        token=a["token"],
        method="PATCH",
        body={"audience_kind": "all_paired", "audience": []},
    )
    _must("highlight-audience widen(a)", s, resp, ok=(200,))
    _wait(
        "c holds the whole day again after widening",
        lambda: caption in _highlight_captions(state, "c"),
    )
    state["highlight_audience"] = {"highlight_id": hid, "caption": caption}
    _save(state)
    print("highlight-audience: ok")


def cmd_replay() -> None:
    """Federation outbox redelivery — kill Carol, post from Alpha, restart.

    Validates the §24 ResilientFederationOutbox path: when a paired
    peer is unreachable, the sender's outbox marks the entry pending
    and retries on a backoff. Once the peer is back up its
    ``/api/instance/config`` becomes reachable and the next outbox tick
    flushes the queued envelopes.

    Sequence:
    1. SIGTERM Carol's process; wait for exit.
    2. Alpha creates a new ``audience_kind=all_paired`` highlight with
       a unique caption — the harness later asserts Carol receives
       this exact caption (i.e. it didn't pre-exist from
       :func:`cmd_traffic`).
    3. Settle ~4 s so Alpha's outbox makes (and fails) one delivery
       attempt against the now-dead Carol — the entry transitions
       to ``unreachable`` status.
    4. Respawn Carol on the same port; wait for ``/api/instance/config``
       to answer 200.
    5. Poll Carol's ``/api/highlights`` until the caption lands, bounded
       by the outbox's REAL worst case. ``BACKOFF_SECONDS`` starts
       ``(5, 10, 20, 40, …)`` with ``JITTER_RATIO = 0.30`` and there is no
       "peer is back → flush now" fast path, so the retry that first
       finds Carol alive lands anywhere from ~10 s (2nd slot, low jitter)
       to ~45 s (3rd slot, high jitter) after the highlight. A fixed
       35 s sleep — tuned for an older ``{0, 5, 30, …}`` schedule — missed
       the high-jitter tail roughly one run in four; the poll asserts the
       same property (redelivery within the schedule) without guessing.
       The elapsed time is printed so a regression in the schedule is
       visible in the step output.
    6. Assert Carol received the caption within that bound.

    Run this *after* :func:`cmd_pair` so the a↔c link is confirmed
    (``cmd_traffic`` is optional — the test only depends on the
    pair). Re-running it twice in a single ``up`` is fine; the
    caption uses :func:`time.time_ns` so each run picks a unique
    needle.
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' first")

    a = state["instances"]["a"]
    c = state["instances"]["c"]
    needle_marker = f"replay-{time.time_ns()}"
    caption = f"[a] resilient highlight {needle_marker}"

    # 1. Tear down Carol — SIGTERM the process group so libdatachannel's
    #    background threads also exit cleanly.
    _open_offline_window("c")
    print(f"  killing c (pid={c['pid']}) to simulate offline peer")
    try:
        os.killpg(c["pid"], signal.SIGTERM)
    except ProcessLookupError:
        print("  c was already gone")
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and _alive(c["pid"]):
        time.sleep(0.2)
    if _alive(c["pid"]):
        try:
            os.killpg(c["pid"], signal.SIGKILL)
        except ProcessLookupError:
            pass
        time.sleep(0.5)

    # 2. Alpha posts a highlight while Carol is dead. Alpha's outbox
    #    will queue the envelope; the next federation flush against
    #    Carol fails fast.
    s, _hl = _request(
        f"http://127.0.0.1:{a['port']}/api/highlights/frames",
        token=a["token"],
        method="POST",
        body={
            "media_url": _upload_demo_image(
                f"http://127.0.0.1:{a['port']}", token=a["token"], label="replay"
            ),
            "frame_type": "image",
            "caption_text": caption,
            "audience_kind": "all_paired",
        },
    )
    _must("replay highlight(a)", s, _hl, ok=(201,))
    print(f"  a created highlight while c offline (caption={needle_marker})")

    # 3. Let Alpha's outbox attempt + fail one delivery so the row is
    #    in the redeliver-pending state.
    time.sleep(4)

    # 4. Respawn Carol on the same port; reuse the existing per-instance
    #    data_dir so identity + paired peers stay intact.
    new_pid = _spawn("c", c["port"])
    state["instances"]["c"]["pid"] = new_pid
    _wait_ready(c["port"])
    print(f"  c respawned: pid={new_pid} ready=200")

    # 5. Outbox redelivery window. ``BACKOFF_SECONDS`` is (5, 10, 20, 40,
    #    …) with ±30 % jitter and NO reconnect-triggered flush, so the retry
    #    that first finds Carol alive lands ~10–45 s after the highlight
    #    (2nd slot low-jitter … 3rd slot high-jitter). Poll until it lands
    #    instead of sleeping a fixed 35 s — that fixed window missed the
    #    high-jitter tail about one run in four. The bound covers the 3rd
    #    slot's worst case plus Carol's respawn with headroom; the 4th slot
    #    (≥ 52 s after the 3rd) would mean the 3rd attempt ALSO failed
    #    against a live Carol, which is a real bug, not timing.
    deadline = time.monotonic() + REPLAY_REDELIVERY_BOUND_S
    started = time.monotonic()
    print(
        f"  polling c for the replayed highlight (bound {REPLAY_REDELIVERY_BOUND_S}s, "
        f"outbox slots 5/10/20 s ±30 %)…"
    )
    captions: set[str] = set()
    while time.monotonic() < deadline:
        captions = _highlight_captions(state, "c")
        if caption in captions:
            break
        time.sleep(2)
    elapsed = time.monotonic() - started

    # 6. Carol should now have Alpha's highlight despite having been
    #    down at the moment Alpha posted it.
    if caption in captions:
        print(f"  c received the replayed highlight after {elapsed:.0f}s ✓")
    else:
        raise SystemExit(
            f"replay: Carol did not receive {caption!r} within "
            f"{REPLAY_REDELIVERY_BOUND_S}s — captions seen: {sorted(captions)!r}",
        )

    state["replay_ran"] = True
    _save(state)
    print("replay: ok")


def cmd_invite_redeem() -> None:
    """Cross-instance space-invite token redeem over federation.

    Validates the PR-1 federation flow for ``socialhome://invite#…``
    codes minted on one instance + pasted on another:

    1. Carol creates a private space on **c** and mints a one-use
       invite token. (Alice is already a CONFIRMED peer of Carol
       from ``cmd_pair``'s a↔c handshake.)
    2. Alice POSTs the token to her own ``/api/spaces/join`` with
       ``issuer_instance_id=<carol's id>``. The backend recognises
       the issuer as a CONFIRMED peer and routes the redeem over
       ``SPACE_INVITE_TOKEN_REDEEM``; Carol's instance validates
       the token + seats Alice as a remote member + sends back
       ``SPACE_INVITE_TOKEN_REDEEM_ACK``; Alice's instance resolves
       the awaiting Future and records the space membership locally.
    3. Carol posts in the space.
    4. Assert the post reaches Alice's ``GET /api/spaces/{id}/feed``.

    Establishes the direct-pair baseline that
    :func:`cmd_relay_invite_redeem` (PR 2) will compare against
    the relayed case (a wants to join d's space via b).
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' first")
    a = state["instances"]["a"]
    c = state["instances"]["c"]

    # 1. Carol creates a private space + mints a token.
    s, space = _request(
        f"http://127.0.0.1:{c['port']}/api/spaces",
        token=c["token"],
        method="POST",
        body={
            "name": "Carol's federation lab",
            "space_type": "private",
            "join_mode": "invite_only",
            "emoji": "🧪",
        },
    )
    _must("create space(c)", s, space, ok=(201,))
    space_id = space["id"]
    print(f"  c created space: {space_id}")

    s, token_res = _request(
        f"http://127.0.0.1:{c['port']}/api/spaces/{space_id}/invite-tokens",
        token=c["token"],
        method="POST",
        body={"uses": 1},
    )
    _must("mint invite token(c)", s, token_res, ok=(201,))
    invite_token = token_res["token"]
    print(f"  c minted invite token: {invite_token[:8]}…")

    # 2. Alice redeems via federation.
    s, joined = _request(
        f"http://127.0.0.1:{a['port']}/api/spaces/join",
        token=a["token"],
        method="POST",
        body={
            "token": invite_token,
            "issuer_instance_id": c["instance_id"],
        },
    )
    _must("redeem invite(a→c)", s, joined, ok=(200, 201))
    if joined.get("space_id") != space_id:
        raise SystemExit(
            f"redeem returned wrong space_id: got {joined.get('space_id')!r}, "
            f"want {space_id!r}",
        )
    print(f"  a redeemed token over federation → space {space_id}")

    # 3. Verify on Carol's side that Alice was seated as a remote
    #    space member. The load-bearing assertion lives on the
    #    ``space_remote_members`` table — the public
    #    ``GET /api/spaces/{id}/members`` endpoint only surfaces
    #    local SpaceMember rows. The HTTP 200 from step 2 already
    #    proves the federation handshake completed end-to-end (the
    #    receiver's awaiting Future only resolves on the inbound
    #    ACK), but the DB-level check is a stronger pin.
    import sqlite3

    db_path = _instance_dir("c") / "socialhome.db"
    conn = sqlite3.connect(db_path)
    try:
        rows = list(
            conn.execute(
                "SELECT user_id, instance_id FROM space_remote_members "
                "WHERE space_id = ?",
                (space_id,),
            )
        )
    finally:
        conn.close()
    alice_user_id = a["user_id"]
    alice_instance_id = a["instance_id"]
    seated = any(r[0] == alice_user_id and r[1] == alice_instance_id for r in rows)
    if not seated:
        raise SystemExit(
            "invite-redeem: Alice not seated in c.space_remote_members — "
            f"rows: {rows!r}",
        )
    print(f"  c.space_remote_members has alice ✓ ({len(rows)} total)")

    # The cross-household *content* delivery (Carol's posts reaching
    # Alice's feed) is a separate gap that depends on mirroring the
    # Space row on the receiver's instance — tracked as a follow-up
    # to this PR. The redeem flow itself is fully validated above.

    state["invite_redeem_ran"] = True
    state["invite_redeem_space_id"] = space_id
    _save(state)
    print("invite-redeem: ok")


def cmd_invite_redeem_routed() -> None:
    """Mesh-routed cross-instance space-invite token redeem (PR 2, v_6).

    Validates ``SPACE_ROUTED`` end-to-end: c (the receiver) wants to
    join a private space hosted on d (the issuer). After
    ``cmd_pair`` + ``cmd_relay_pair`` the only households unpaired
    with each other are c and d — c↔b↔d is the only mesh path.

    Sequence:

    1. **d creates** a private space + mints a one-use invite token.
    2. **c POSTs** the token to its own ``/api/spaces/join`` with
       ``issuer_instance_id=<d's id>``. d is NOT a direct peer of c
       so the receiver-side coordinator triggers a
       ``SPACE_FIND_ROUTE`` probe; b responds with a ROUTE_FOUND
       carrying d's per-route ephemeral X25519 pub. The redeem then
       ships as ``SPACE_ROUTED(direction=forward)`` with the inner
       payload sealed under that ephemeral — b sees only the opaque
       ``sealed`` blob.
    3. **d unseals**, validates the token + seats c as a remote
       member, ships the ACK back as
       ``SPACE_ROUTED(direction=reply)``.
    4. **c unseals the ACK** and resolves the awaiting Future inside
       ``POST /api/spaces/join``.

    Assertions:

    - HTTP 200/201 from c's ``/api/spaces/join`` (proves the full
      forward + reply mesh path round-tripped end-to-end).
    - ``d.space_remote_members`` shows c's user (proves the inner
      SPACE_INVITE_TOKEN_REDEEM was dispatched at the target after
      unseal).
    - b's log shows ``SPACE_ROUTED`` envelopes flowing but no
      ``SPACE_INVITE_TOKEN_REDEEM`` (proves the relay never
      dispatched the inner event — i.e. never decrypted it).
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' first")
    c = state["instances"]["c"]
    d = state["instances"]["d"]

    # 1. d creates a private space + mints a token.
    s, space = _request(
        f"http://127.0.0.1:{d['port']}/api/spaces",
        token=d["token"],
        method="POST",
        body={
            "name": "Delta's mesh lab",
            "space_type": "private",
            "join_mode": "invite_only",
            "emoji": "🕸",
        },
    )
    _must("create space(d)", s, space, ok=(201,))
    space_id = space["id"]
    print(f"  d created space: {space_id}")

    s, token_res = _request(
        f"http://127.0.0.1:{d['port']}/api/spaces/{space_id}/invite-tokens",
        token=d["token"],
        method="POST",
        body={"uses": 1},
    )
    _must("mint invite token(d)", s, token_res, ok=(201,))
    invite_token = token_res["token"]
    print(f"  d minted invite token: {invite_token[:8]}…")

    # Truncate b's log so the post-run scan is bounded to this step.
    b_log_path = _instance_dir("b") / "log.txt"
    b_log_before_size = b_log_path.stat().st_size if b_log_path.exists() else 0

    # 2. c redeems — d is NOT a direct peer → mesh path via b.
    s, joined = _request(
        f"http://127.0.0.1:{c['port']}/api/spaces/join",
        token=c["token"],
        method="POST",
        body={
            "token": invite_token,
            "issuer_instance_id": d["instance_id"],
        },
    )
    _must("redeem invite(c→d via mesh)", s, joined, ok=(200, 201))
    if joined.get("space_id") != space_id:
        raise SystemExit(
            f"routed redeem returned wrong space_id: "
            f"got {joined.get('space_id')!r}, want {space_id!r}",
        )
    print(f"  c redeemed token over mesh → space {space_id}")

    # 3. Assert d seated c as a remote member (proves the inner
    #    REDEEM was actually dispatched at d after unseal).
    import sqlite3

    db_path = _instance_dir("d") / "socialhome.db"
    conn = sqlite3.connect(db_path)
    try:
        rows = list(
            conn.execute(
                "SELECT user_id, instance_id FROM space_remote_members "
                "WHERE space_id = ?",
                (space_id,),
            )
        )
    finally:
        conn.close()
    carol_user_id = c["user_id"]
    carol_instance_id = c["instance_id"]
    seated = any(r[0] == carol_user_id and r[1] == carol_instance_id for r in rows)
    if not seated:
        raise SystemExit(
            "routed invite-redeem: Carol not seated in "
            f"d.space_remote_members — rows: {rows!r}",
        )
    print(f"  d.space_remote_members has carol ✓ ({len(rows)} total)")

    # 4. Assert the relay (b) never dispatched the inner REDEEM —
    #    i.e. it only saw SPACE_ROUTED envelopes, never decrypted
    #    the inner payload.
    if b_log_path.exists():
        b_log_after = b_log_path.read_text(errors="replace")[b_log_before_size:]
        if "SPACE_INVITE_TOKEN_REDEEM" in b_log_after:
            raise SystemExit(
                "routed invite-redeem: relay b dispatched the inner "
                "SPACE_INVITE_TOKEN_REDEEM — encryption invariant "
                "broken (relays must not see inner event_type "
                "post-unseal).",
            )
        if "SPACE_ROUTED" not in b_log_after:
            print(
                "  WARN: no SPACE_ROUTED entries in b's log; the "
                "mesh path may have skipped b (alt route via a).",
            )
        else:
            print("  b relayed SPACE_ROUTED without unsealing ✓")

    state["invite_redeem_routed_ran"] = True
    state["invite_redeem_routed_space_id"] = space_id
    _save(state)
    print("invite-redeem-routed: ok")


def cmd_remote_invite_routed() -> None:
    """Mesh-routed admin-initiated private invite (PR 3, v_6).

    Validates the ``SPACE_PRIVATE_INVITE`` family riding ``SPACE_ROUTED``
    when the admin's household isn't directly paired with the invitee's
    household. Topology after ``cmd_pair`` + ``cmd_relay_pair`` leaves
    c ↔ d unpaired (only path is c↔b↔d), so c inviting dave is the
    canonical mesh-private-invite scenario.

    Sequence:

    1. **c creates** a private space + posts a remote-invite targeting
       dave on d. Backend ``SpaceService.invite_remote_user`` sees that
       d is not a CONFIRMED peer of c, runs ``RouteDiscoveryService``,
       and ships ``SPACE_PRIVATE_INVITE`` as ``SPACE_ROUTED(forward)``
       through b. b forwards the opaque ciphertext without decrypting.
    2. **d unseals**, ``PrivateSpaceInviteHandler._on_invite`` lands the
       row in d's local invite repo and ``GET /api/remote_invites`` on
       d surfaces it.
    3. **d accepts** via ``POST /api/remote_invites/{token}/accept``.
       Backend runs a *fresh* discovery (the original reply-leg
       ephemerals have expired in the user-time gap) and ships
       ``SPACE_PRIVATE_INVITE_ACCEPT`` as a new ``SPACE_ROUTED(forward)``
       leg back through b.
    4. **c unseals**, seats dave in ``c.space_remote_members``.

    Assertions:

    - c's ``POST /api/spaces/{id}/remote-invites`` returns 201 (mesh
      send succeeded — no direct-pair short-circuit needed).
    - d's ``/api/remote_invites`` includes the new invite within a
      reasonable window.
    - d's accept POST returns 200/204.
    - c's ``space_remote_members`` shows dave (proves the inner ACCEPT
      was dispatched at the issuer after unseal).
    - b's log shows ``SPACE_ROUTED`` envelopes flowing but no
      ``SPACE_PRIVATE_INVITE`` / ``_ACCEPT`` (relay never decrypted).
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' first")
    c = state["instances"]["c"]
    d = state["instances"]["d"]

    # 1. c creates a private space.
    s, space = _request(
        f"http://127.0.0.1:{c['port']}/api/spaces",
        token=c["token"],
        method="POST",
        body={
            "name": "Carol's mesh salon",
            "space_type": "private",
            "join_mode": "invite_only",
            "emoji": "🛰",
        },
    )
    _must("create space(c, mesh-private)", s, space, ok=(201,))
    space_id = space["id"]
    print(f"  c created space: {space_id}")

    # Truncate b's log so the post-run scan is bounded to this step.
    b_log_path = _instance_dir("b") / "log.txt"
    b_log_before_size = b_log_path.stat().st_size if b_log_path.exists() else 0

    # 2. c invites dave — c is NOT directly paired with d → mesh path.
    s, inv = _request(
        f"http://127.0.0.1:{c['port']}/api/spaces/{space_id}/remote-invites",
        token=c["token"],
        method="POST",
        body={
            "invitee_instance_id": d["instance_id"],
            "invitee_user_id": d["user_id"],
        },
    )
    _must("c → d remote-invite(mesh)", s, inv, ok=(201,))
    print("  c → d: invite issued via mesh (route discovery + SPACE_ROUTED)")

    # 3. Wait for the invite to round-trip to d's inbox.
    time.sleep(10)
    s, invites = _request(
        f"http://127.0.0.1:{d['port']}/api/remote_invites",
        token=d["token"],
    )
    _must("d.remote_invites(after mesh)", s, invites)
    dave_invite_token = None
    invites_list = invites if isinstance(invites, list) else invites.get("invites", [])
    for row in invites_list:
        if row.get("space_id") == space_id:
            dave_invite_token = row.get("invite_token") or row.get("token")
            break
    if dave_invite_token is None:
        raise SystemExit(
            f"remote-invite-routed: d's inbox did not receive the invite for "
            f"space {space_id}; got {invites_list!r}",
        )
    print(f"  d sees invite in inbox ✓ (token={dave_invite_token[:8]}…)")

    # 4. d accepts — accept also routes via mesh (d→b→c).
    s, accepted = _request(
        f"http://127.0.0.1:{d['port']}/api/remote_invites/{dave_invite_token}/accept",
        token=d["token"],
        method="POST",
        body={},
    )
    _must("d accepts invite(mesh)", s, accepted, ok=(200, 204))
    print("  d accepted; ACCEPT routes back via mesh d→b→c")

    # 5. Wait for the ACCEPT to round-trip back to c's seat.
    time.sleep(10)
    import sqlite3

    db_path = _instance_dir("c") / "socialhome.db"
    conn = sqlite3.connect(db_path)
    try:
        rows = list(
            conn.execute(
                "SELECT user_id, instance_id FROM space_remote_members "
                "WHERE space_id = ?",
                (space_id,),
            )
        )
    finally:
        conn.close()
    dave_user_id = d["user_id"]
    dave_instance_id = d["instance_id"]
    seated = any(r[0] == dave_user_id and r[1] == dave_instance_id for r in rows)
    if not seated:
        raise SystemExit(
            "remote-invite-routed: dave not seated in c.space_remote_members "
            f"after mesh ACCEPT — rows: {rows!r}",
        )
    print(f"  c.space_remote_members has dave ✓ ({len(rows)} total)")

    # 6. Assert b never dispatched the inner SPACE_PRIVATE_INVITE family
    #    — proves the relay couldn't read the encrypted payload.
    if b_log_path.exists():
        b_log_after = b_log_path.read_text(errors="replace")[b_log_before_size:]
        for forbidden in (
            "SPACE_PRIVATE_INVITE_ACCEPT",
            # The inner forward leg too — bare "SPACE_PRIVATE_INVITE"
            # would also catch _ACCEPT/_DECLINE substrings, so we
            # check the exact tokens.
        ):
            if forbidden in b_log_after:
                raise SystemExit(
                    f"remote-invite-routed: relay b dispatched inner "
                    f"{forbidden} — encryption invariant broken.",
                )
        if "SPACE_ROUTED" not in b_log_after:
            print(
                "  WARN: no SPACE_ROUTED entries in b's log; the mesh "
                "path may have skipped b (alt route via a).",
            )
        else:
            print("  b relayed SPACE_ROUTED without unsealing ✓")

    state["remote_invite_routed_ran"] = True
    state["remote_invite_routed_space_id"] = space_id
    _save(state)
    print("remote-invite-routed: ok")


def cmd_remote_invite_decline() -> None:
    """Decline-path coverage for the admin-initiated private invite
    (direct pair). Complements ``cmd_remote_invite_routed`` by
    exercising the DECLINE leg, which ``cmd_traffic`` + ``cmd_calendar``
    never touch (both invitees there accept).

    Sequence:

    1. **c creates** a fresh private space + posts a remote-invite
       targeting alice (direct pair c↔a).
    2. **a's inbox** picks it up; ``a`` POSTs decline.
    3. The ``SPACE_PRIVATE_INVITE_DECLINE`` envelope round-trips to c,
       which marks the invitation row ``declined``.

    Assertions:

    - The invitation row on c moves to ``status='declined'``.
    - a is NOT seated in c's ``space_remote_members`` (decline must
      not accidentally seat the user).
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' first")
    a = state["instances"]["a"]
    c = state["instances"]["c"]

    s, space = _request(
        f"http://127.0.0.1:{c['port']}/api/spaces",
        token=c["token"],
        method="POST",
        body={
            "name": "Decline test",
            "space_type": "private",
            "join_mode": "invite_only",
            "emoji": "🚫",
        },
    )
    _must("create space(c, decline)", s, space, ok=(201,))
    space_id = space["id"]
    print(f"  c created space: {space_id}")

    s, inv = _request(
        f"http://127.0.0.1:{c['port']}/api/spaces/{space_id}/remote-invites",
        token=c["token"],
        method="POST",
        body={
            "invitee_instance_id": a["instance_id"],
            "invitee_user_id": a["user_id"],
        },
    )
    _must("c → a remote-invite(direct)", s, inv, ok=(201,))
    print("  c → a: invite issued (direct pair)")

    time.sleep(5)
    s, invites = _request(
        f"http://127.0.0.1:{a['port']}/api/remote_invites",
        token=a["token"],
    )
    _must("a.remote_invites", s, invites)
    invites_list = invites if isinstance(invites, list) else invites.get("invites", [])
    alice_token = None
    for row in invites_list:
        if row.get("space_id") == space_id:
            alice_token = row.get("invite_token") or row.get("token")
            break
    if alice_token is None:
        raise SystemExit(
            f"remote-invite-decline: a's inbox did not receive the invite for "
            f"space {space_id}; got {invites_list!r}",
        )

    s, declined = _request(
        f"http://127.0.0.1:{a['port']}/api/remote_invites/{alice_token}/decline",
        token=a["token"],
        method="POST",
        body={},
    )
    _must("a declines invite", s, declined, ok=(200, 204))
    print("  a declined; DECLINE routes back to c")

    time.sleep(5)
    import sqlite3

    db_path = _instance_dir("c") / "socialhome.db"
    conn = sqlite3.connect(db_path)
    try:
        invitation_rows = list(
            conn.execute(
                "SELECT status FROM space_invitations WHERE invite_token = ?",
                (alice_token,),
            )
        )
        member_rows = list(
            conn.execute(
                "SELECT user_id FROM space_remote_members WHERE space_id = ?",
                (space_id,),
            )
        )
    finally:
        conn.close()
    if not invitation_rows or invitation_rows[0][0] != "declined":
        raise SystemExit(
            f"remote-invite-decline: c's invitation row status != 'declined' "
            f"— got {invitation_rows!r}",
        )
    if any(r[0] == a["user_id"] for r in member_rows):
        raise SystemExit(
            f"remote-invite-decline: alice was seated despite declining — "
            f"rows: {member_rows!r}",
        )
    print("  c.space_invitations marked declined ✓")
    print("  c.space_remote_members does NOT contain alice ✓")
    print("remote-invite-decline: ok")


def cmd_space_post_routed() -> None:
    """Mesh-routed space content (PR 3, ``send_with_mesh_fallback``).

    After ``cmd_remote_invite_routed`` seats dave (on d) as a remote
    member of c's mesh-private space — and crucially, d is NOT a
    direct peer of c — c posts in that space. The new
    ``broadcast_to_space_members`` lists every member instance_id
    from ``space_instances`` (skipping the ``remote_instances.status =
    CONFIRMED`` filter that previously excluded mesh-only members);
    each per-peer ship goes through
    ``FederationService.send_with_mesh_fallback`` which, finding d
    has no CONFIRMED pair row, routes the inner
    ``SPACE_POST_CREATED`` envelope via ``SPACE_ROUTED`` along the
    discovered c→b→d chain. b sees only the opaque ``sealed`` blob.

    Assertions:

    - c's ``POST /api/feed/posts`` against the mesh space returns
      201 with a post id.
    - After settle, ``d.space_posts`` contains the post id (proves
      the inner SPACE_POST_CREATED was dispatched at d *after*
      unseal — fanout actually reached d via mesh).
    - b's log contains ``SPACE_ROUTED`` envelopes flowing during
      this window but NOT ``SPACE_POST_CREATED`` (encryption
      invariant — relays must not see the inner event type).
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' first")
    space_id = state.get("remote_invite_routed_space_id")
    if not space_id:
        raise SystemExit("run 'remote-invite-routed' first to seat dave")
    c = state["instances"]["c"]

    # Truncate b's log so the post-run scan is bounded to this step.
    b_log_path = _instance_dir("b") / "log.txt"
    b_log_before_size = b_log_path.stat().st_size if b_log_path.exists() else 0

    # POST to the SPACE endpoint, not the household-feed endpoint —
    # the latter ignores ``space_id`` in the body and lands the row
    # as a household post that never federates to space members.
    s, post = _request(
        f"http://127.0.0.1:{c['port']}/api/spaces/{space_id}/posts",
        token=c["token"],
        method="POST",
        body={
            "type": "text",
            "content": "Hello mesh — c posting to d via b",
        },
    )
    _must("c posts in mesh space", s, post, ok=(201,))
    post_id = post["id"]
    print(f"  c posted in mesh space → id={post_id}")

    # Sleep for fan-out: discovery (~2s) + SPACE_ROUTED forward (~1-3s)
    # + receiver dispatch + DB write. 12s is comfortable on a busy host.
    time.sleep(12)

    import sqlite3

    db_path = _instance_dir("d") / "socialhome.db"
    conn = sqlite3.connect(db_path)
    try:
        rows = list(
            conn.execute(
                "SELECT id, content FROM space_posts WHERE space_id = ? AND id = ?",
                (space_id, post_id),
            )
        )
    finally:
        conn.close()
    if not rows:
        raise SystemExit(
            f"space-post-routed: post {post_id} not in d.space_posts "
            f"for space {space_id} — mesh fanout didn't reach d",
        )
    print(f"  d.space_posts has the post ✓ (content={rows[0][1]!r})")

    if b_log_path.exists():
        b_log_after = b_log_path.read_text(errors="replace")[b_log_before_size:]
        if "SPACE_POST_CREATED" in b_log_after:
            raise SystemExit(
                "space-post-routed: relay b dispatched inner "
                "SPACE_POST_CREATED — encryption invariant broken.",
            )
        if "SPACE_ROUTED" not in b_log_after:
            print(
                "  WARN: no SPACE_ROUTED entries in b's log; the mesh "
                "path may have skipped b (alt route via a).",
            )
        else:
            print("  b relayed SPACE_ROUTED without unsealing ✓")

    state["space_post_routed_ran"] = True
    _save(state)
    print("space-post-routed: ok")


def _mesh_member_cert_failures(member: str, host: str, space_id: str) -> list[str]:
    """Why household ``member`` (mesh-only toward ``host``) does NOT hold a
    verifying writer cert for ``space_id`` — empty when it does.

    Checks both ends: the host recorded the member's mesh claim (migration
    0078: ``space_instances.proto_version`` / ``identity_pk``, the member's
    own identity key) and still holds no ``remote_instances`` row for it,
    and the member stored a cert for the epoch that verifies against the
    pinned space key under its OWN identity key.
    """
    import json as _json

    from socialhome.crypto import b64url_decode
    from socialhome.domain.writer_cert import WriterCert
    from socialhome.writer_cert import verify_writer_cert

    own = _rows(
        member, "SELECT instance_id, identity_public_key FROM instance_identity"
    )
    if not own:
        return [f"{member}: no instance_identity row"]
    member_id, member_pk = own[0]
    if _rows(host, "SELECT 1 FROM remote_instances WHERE id=?", (member_id,)):
        return [
            f"{host} holds a remote_instances row for {member} — not a "
            "mesh-only membership any more"
        ]
    claim = _rows(
        host,
        "SELECT proto_version, identity_pk FROM space_instances "
        "WHERE space_id=? AND instance_id=?",
        (space_id, member_id),
    )
    if not claim or claim[0][0] is None:
        return [f"{host} recorded no mesh claim for {member} in {space_id}: {claim!r}"]
    if claim[0][1] != member_pk:
        return [
            f"{host} recorded {member}'s identity_pk={claim[0][1]!r}, expected its own key"
        ]
    pin = _rows(
        member, "SELECT identity_public_key FROM spaces WHERE id=?", (space_id,)
    )
    certs = _rows(
        member,
        "SELECT epoch, writer_cert FROM space_keys "
        "WHERE space_id=? AND writer_cert IS NOT NULL ORDER BY epoch DESC",
        (space_id,),
    )
    if not pin:
        return [f"{member}: no stub row for {space_id}"]
    if not certs:
        return [f"{member} holds no writer cert for {space_id} yet"]
    epoch, raw = certs[0]
    try:
        cert = WriterCert.from_wire(_json.loads(raw))
        verify_writer_cert(
            cert,
            space_pubkey=bytes.fromhex(pin[0][0]),
            space_id=space_id,
            epoch=int(epoch),
            author_pk=bytes.fromhex(member_pk),
            required_scope="write",
        )
    except Exception as exc:
        return [f"{member}'s writer cert for {space_id} does not verify: {exc!r}"]
    if b64url_decode(cert.instance_pk) != bytes.fromhex(member_pk):
        return [f"{member}'s writer cert names another household's key"]
    print(
        f"  {member} (mesh-only toward host {host}) holds a verifying writer "
        f"cert for epoch {epoch} (scope={cert.scope}); {host} recorded its "
        f"mesh claim v_{claim[0][0]} ✓"
    )
    return []


def cmd_mesh_member_cert() -> None:
    """Mesh-only member households get their writer certs (migration 0078).

    Two spaces whose member reaches the host only over the mesh (b relays):

    * c's mesh-private space with dave on **d** (``remote-invite-routed`` —
      the accept carries d's version claim, recorded before the seat's
      roster snapshot, which then carries d's cert);
    * d's space that **c** joined by token (``invite-redeem-routed`` — the
      mesh redeem carries c's claim; the host records it once the seat is
      committed and the raise sends c a snapshot with its cert).

    For each: the host recorded the member's claim on ``space_instances``
    (and still has no ``remote_instances`` row for it), and the member holds
    a cert that verifies against the pinned space key under its OWN identity
    key. Polls (the snapshot rides SPACE_ROUTED); read-only DB checks, no
    REST calls (the 60/min bucket is untouched).
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' first")
    cases = []
    if state.get("remote_invite_routed_space_id"):
        cases.append(("d", "c", state["remote_invite_routed_space_id"]))
    if state.get("invite_redeem_routed_space_id"):
        cases.append(("c", "d", state["invite_redeem_routed_space_id"]))
    if not cases:
        raise SystemExit(
            "mesh-member-cert: run 'remote-invite-routed' / 'invite-redeem-routed' first"
        )
    for member, host, space_id in cases:
        deadline = time.monotonic() + 90
        failures: list[str] = []
        while time.monotonic() < deadline:
            failures = _mesh_member_cert_failures(member, host, space_id)
            if not failures:
                break
            time.sleep(3)
        if failures:
            raise SystemExit("mesh-member-cert: " + "; ".join(failures))
    state["mesh_member_cert_ran"] = True
    _save(state)
    print("mesh-member-cert: ok")


def cmd_space_media_blob() -> None:
    """Cross-household media bytes federation (PR #4xx, ``SPACE_MEDIA_BLOB``).

    Builds on ``cmd_remote_invite_routed`` which seated dave (on d) as
    a remote member of c's mesh-private space. A post with an image
    URL was previously a broken render on the receiver — the
    metadata federated but the bytes lived only on the sender's
    media path. ``SpaceMediaSyncService`` closes the gap: after the
    SPACE_POST_CREATED broadcast, one outbox row per (peer, blob)
    enqueues; the scheduler ships chunked SPACE_MEDIA_BLOB events
    that the receiver writes into its own media_path.

    Assertions:

    - c uploads a real WebP via ``POST /api/media/upload`` →
      filename returned.
    - c creates a space post referencing that filename.
    - After settle, d's media_path contains a file with the SAME
      filename and SAME bytes.
    - d's ``/api/media/{filename}?exp=&sig=…`` (signed by d) serves
      the bytes 200 — i.e. the rendered ``<img>`` would land.
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' first")
    space_id = state.get("remote_invite_routed_space_id")
    if not space_id:
        raise SystemExit(
            "space-media-blob: run 'remote-invite-routed' first to seat dave",
        )
    c = state["instances"]["c"]

    # Truncate b's log so the post-run scan is bounded to this
    # step. b is the mesh relay between c and d; the encryption
    # invariant says b sees ``SPACE_ROUTED`` envelopes but NEVER
    # the inner ``SPACE_MEDIA_BLOB`` payload — same as
    # ``space-post-routed`` checks for SPACE_POST_CREATED.
    b_log_path = _instance_dir("b") / "log.txt"
    b_log_before_size = b_log_path.stat().st_size if b_log_path.exists() else 0

    # 1. Build a real WebP byte stream — the upload endpoint runs
    #    every image through ImageProcessor, so we have to ship
    #    something Pillow can decode.
    from PIL import Image
    from io import BytesIO

    img = Image.new("RGB", (32, 32), color=(180, 90, 30))
    buf = BytesIO()
    img.save(buf, format="PNG")
    png_bytes = buf.getvalue()

    # 2. Upload via c.
    boundary = "----sh-demo-boundary"
    body = (
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="demo.png"\r\n'
            f"Content-Type: image/png\r\n\r\n"
        ).encode()
        + png_bytes
        + f"\r\n--{boundary}--\r\n".encode()
    )
    upload_req = urllib.request.Request(
        f"http://127.0.0.1:{c['port']}/api/media/upload",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {c['token']}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    with urllib.request.urlopen(upload_req) as resp:
        upload_json = json.loads(resp.read())
    filename = upload_json["filename"]
    media_url = upload_json["url"]  # ``api/media/<hash>.webp``
    print(f"  c uploaded {filename} → {media_url}")

    # 3. c posts in the mesh-private space, referencing the upload.
    s, post = _request(
        f"http://127.0.0.1:{c['port']}/api/spaces/{space_id}/posts",
        token=c["token"],
        method="POST",
        body={
            "type": "image",
            "image_urls": [media_url],
        },
    )
    _must("c posts image in mesh space", s, post, ok=(201,))
    post_id = post["id"]
    print(f"  c posted image → post_id={post_id}")

    # 4. Wait for SPACE_POST_CREATED + SPACE_MEDIA_BLOB(s) to land
    #    on d. The media outbox scheduler ticks every 5s; the
    #    federation outbox another few seconds — give it some
    #    headroom.
    time.sleep(20)

    # 5. Bytes should now exist on d's media path with the same
    #    filename + same content.
    d_media_path = _instance_dir("d") / "media" / filename
    if not d_media_path.is_file():
        raise SystemExit(
            f"space-media-blob: {filename} missing from d's media path "
            f"({d_media_path}) — SPACE_MEDIA_BLOB didn't land",
        )
    d_bytes = d_media_path.read_bytes()
    # ImageProcessor transcoded to WebP — bytes won't match the
    # uploaded PNG. But they MUST be identical between c and d.
    c_media_path = _instance_dir("c") / "media" / filename
    c_bytes = c_media_path.read_bytes()
    if c_bytes != d_bytes:
        raise SystemExit(
            f"space-media-blob: bytes mismatch — c has {len(c_bytes)}B, "
            f"d has {len(d_bytes)}B",
        )
    print(f"  d.media has {filename} ({len(d_bytes)} bytes) ✓")
    print("  bytes match between c and d ✓")

    # 6. The mesh relay (b) MUST never have dispatched the inner
    #    SPACE_MEDIA_BLOB — same encryption invariant the
    #    space-post-routed step asserts. SPACE_ROUTED envelopes
    #    are fine; the inner event type leaking through would
    #    mean the relay decrypted the bytes.
    if b_log_path.exists():
        b_log_after = b_log_path.read_text(errors="replace")[b_log_before_size:]
        if "SPACE_MEDIA_BLOB" in b_log_after:
            raise SystemExit(
                "space-media-blob: relay b dispatched inner "
                "SPACE_MEDIA_BLOB — encryption invariant broken.",
            )
        if "SPACE_ROUTED" not in b_log_after:
            print(
                "  WARN: no SPACE_ROUTED entries in b's log; the mesh "
                "path may have skipped b (alt route via a).",
            )
        else:
            print("  b relayed SPACE_ROUTED without unsealing ✓")

    state["space_media_blob_ran"] = True
    _save(state)
    print("space-media-blob: ok")


def cmd_space_gallery_media_blob() -> None:
    """Gallery media bytes federate to remote members (PR #4xx).

    Same shape as ``cmd_space_media_blob`` but exercises the gallery
    upload path:

    1. c creates a per-space album in the mesh-private space dave joined.
    2. c uploads an image into that album.
    3. After settle, d's media path contains the thumbnail + full
       bytes — same filenames as on c.
    4. d holds the METADATA too: the album row (v_33
       ``SPACE_GALLERY_ALBUM_CREATED`` — before it, an album made after
       dave joined never reached d, and d refused the item as naming an
       unknown album while the bytes still landed) and the item row.
    5. The other direction — a remote uploader: dave uploads into c's
       album on d; c must hold that item's row, attributed to dave, plus
       its bytes.
    6. c renames the album; d's copy follows (``_UPDATED``).
    7. b (the relay) saw ``SPACE_ROUTED`` envelopes but NEVER the
       inner ``SPACE_GALLERY_*`` or ``SPACE_MEDIA_BLOB``.

    Closes the gap Pascal called out: galleries previously federated
    only the URL strings (``to_thumbnail_dict``); receivers got
    broken thumbnails because the bytes never crossed the wire.
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' first")
    space_id = state.get("remote_invite_routed_space_id")
    if not space_id:
        raise SystemExit(
            "space-gallery-media-blob: run 'remote-invite-routed' first",
        )
    c = state["instances"]["c"]
    d = state["instances"]["d"]

    b_log_path = _instance_dir("b") / "log.txt"
    b_log_before_size = b_log_path.stat().st_size if b_log_path.exists() else 0

    # 1. c creates a per-space album.
    s, album = _request(
        f"http://127.0.0.1:{c['port']}/api/spaces/{space_id}/gallery/albums",
        token=c["token"],
        method="POST",
        body={"name": "Demo Album", "description": "for the demo"},
    )
    _must("c creates album", s, album, ok=(201,))
    album_id = album["id"]
    print(f"  c created album → {album_id}")

    # 2. c uploads an item into the album.
    from PIL import Image
    from io import BytesIO

    img = Image.new("RGB", (32, 32), color=(30, 90, 200))
    buf = BytesIO()
    img.save(buf, format="PNG")
    png_bytes = buf.getvalue()
    boundary = "----sh-demo-gallery-boundary"
    body = (
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="gallery.png"\r\n'
            f"Content-Type: image/png\r\n\r\n"
        ).encode()
        + png_bytes
        + f"\r\n--{boundary}--\r\n".encode()
    )
    upload_req = urllib.request.Request(
        f"http://127.0.0.1:{c['port']}/api/gallery/albums/{album_id}/items",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {c['token']}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    with urllib.request.urlopen(upload_req) as resp:
        item = json.loads(resp.read())
    # The route returns the new item including its url + thumbnail_url.
    item_id = item["id"]
    item_url = item.get("url") or ""
    thumb_url = item.get("thumbnail_url") or ""
    print(f"  c uploaded gallery item {item_id}")

    # 3. Wait for SPACE_GALLERY_ITEM_CREATED + the matching
    #    SPACE_MEDIA_BLOB chunks to land on d.
    time.sleep(20)

    # 4. Both filenames should be present on d.
    for url in {item_url, thumb_url}:
        if not url:
            continue
        filename = url.rsplit("/", 1)[-1].split("?", 1)[0]
        d_path = _instance_dir("d") / "media" / filename
        if not d_path.is_file():
            raise SystemExit(
                f"space-gallery-media-blob: {filename} missing from d "
                f"({d_path}) — the SPACE_MEDIA_BLOB for the gallery "
                f"item didn't land",
            )
        c_path = _instance_dir("c") / "media" / filename
        if c_path.read_bytes() != d_path.read_bytes():
            raise SystemExit(
                f"space-gallery-media-blob: bytes mismatch for {filename}",
            )
        print(f"  d.media has {filename} ✓")

    # 4. The metadata rows, not just the bytes (v_33).
    def _gallery_items_on(label: str, inst: dict) -> list[dict]:
        s_, got = _request(
            f"http://127.0.0.1:{inst['port']}/api/gallery/albums/{album_id}/items",
            token=inst["token"],
        )
        if s_ != 200:
            raise SystemExit(
                f"space-gallery-media-blob: {label} has no album {album_id} "
                f"(HTTP {s_}: {got}) — SPACE_GALLERY_ALBUM_CREATED didn't land"
            )
        return got if isinstance(got, list) else got.get("items", [])

    s, d_album = _request(
        f"http://127.0.0.1:{d['port']}/api/gallery/albums/{album_id}",
        token=d["token"],
    )
    _must("d holds c's album", s, d_album, ok=(200,))
    if d_album.get("name") != "Demo Album":
        raise SystemExit(
            f"space-gallery-media-blob: d's album is named {d_album.get('name')!r}"
        )
    print(f"  d holds album {album_id} ✓")
    # v_34: a space album's id commits to its creator, so no other
    # household can announce it first as theirs. The id c minted must
    # verify for c's user in this space — and d accepted it on that basis.
    from socialhome.federation.owner_bound_id import (
        GALLERY_ALBUM_KIND,
        OwnerBinding,
        check_owner_bound_id,
    )

    s, c_me = _request(f"http://127.0.0.1:{c['port']}/api/me", token=c["token"])
    _must("c /api/me", s, c_me, ok=(200,))
    binding = check_owner_bound_id(
        GALLERY_ALBUM_KIND,
        album_id,
        space_id=space_id,
        owner_user_id=c_me["user_id"],
    )
    if binding is not OwnerBinding.VALID:
        raise SystemExit(
            f"space-gallery-media-blob: album id {album_id} is {binding.value}, "
            f"not bound to its creator {c_me['user_id']!r} (v_34)"
        )
    if d_album.get("owner_user_id") not in (None, c_me["user_id"]):
        raise SystemExit(
            f"space-gallery-media-blob: d holds the album for "
            f"{d_album.get('owner_user_id')!r}, expected {c_me['user_id']!r}"
        )
    print("  album id is bound to its creator (v_34) ✓")
    # v_36: the upload's id commits to its uploader the same way.
    from socialhome.federation.owner_bound_id import GALLERY_ITEM_KIND

    item_binding = check_owner_bound_id(
        GALLERY_ITEM_KIND,
        item_id,
        space_id=space_id,
        owner_user_id=c_me["user_id"],
    )
    if item_binding is not OwnerBinding.VALID:
        raise SystemExit(
            f"space-gallery-media-blob: item id {item_id} is "
            f"{item_binding.value}, not bound to its uploader "
            f"{c_me['user_id']!r} (v_36)"
        )
    print("  upload id is bound to its uploader (v_36) ✓")
    d_items = {i["id"]: i for i in _gallery_items_on("d", d)}
    if item_id not in d_items:
        raise SystemExit(
            f"space-gallery-media-blob: d has the bytes but no row for item "
            f"{item_id} (items on d: {sorted(d_items)}) — the item was refused"
        )
    print(f"  d holds item {item_id} (uploaded_by={d_items[item_id]['uploaded_by']}) ✓")

    # 5. A remote uploader: dave uploads into c's album from d.
    s, d_me = _request(f"http://127.0.0.1:{d['port']}/api/me", token=d["token"])
    _must("d /api/me", s, d_me, ok=(200,))
    dave_id = d_me["user_id"]
    img2 = Image.new("RGB", (24, 24), color=(200, 60, 30))
    buf2 = BytesIO()
    img2.save(buf2, format="PNG")
    body2 = (
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="dave.png"\r\n'
            f"Content-Type: image/png\r\n\r\n"
        ).encode()
        + buf2.getvalue()
        + f"\r\n--{boundary}--\r\n".encode()
    )
    upload_req2 = urllib.request.Request(
        f"http://127.0.0.1:{d['port']}/api/gallery/albums/{album_id}/items",
        data=body2,
        method="POST",
        headers={
            "Authorization": f"Bearer {d['token']}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    with urllib.request.urlopen(upload_req2) as resp:
        dave_item = json.loads(resp.read())
    dave_item_id = dave_item["id"]
    print(f"  dave (d) uploaded gallery item {dave_item_id} into c's album")

    # 6. c renames the album while dave's upload is in flight.
    s, patched = _request(
        f"http://127.0.0.1:{c['port']}/api/gallery/albums/{album_id}",
        token=c["token"],
        method="PATCH",
        body={"name": "Demo Album (renamed)"},
    )
    _must("c renames album", s, patched, ok=(200, 204))

    deadline = time.monotonic() + 60
    c_row = None
    d_name = None
    while time.monotonic() < deadline:
        c_items = {i["id"]: i for i in _gallery_items_on("c", c)}
        c_row = c_items.get(dave_item_id)
        _s, d_album = _request(
            f"http://127.0.0.1:{d['port']}/api/gallery/albums/{album_id}",
            token=d["token"],
        )
        d_name = d_album.get("name") if isinstance(d_album, dict) else None
        if c_row is not None and d_name == "Demo Album (renamed)":
            break
        time.sleep(2)
    if c_row is None:
        raise SystemExit(
            f"space-gallery-media-blob: c never stored dave's item {dave_item_id} "
            f"— a remote member's upload didn't reach the host's gallery"
        )
    if c_row["uploaded_by"] != dave_id:
        raise SystemExit(
            f"space-gallery-media-blob: dave's item on c is attributed to "
            f"{c_row['uploaded_by']!r}, expected {dave_id!r}"
        )
    print(f"  c holds dave's item {dave_item_id} (uploaded_by={dave_id[:12]}…) ✓")
    for url in {dave_item.get("url") or "", dave_item.get("thumbnail_url") or ""}:
        if not url:
            continue
        filename = url.rsplit("/", 1)[-1].split("?", 1)[0]
        deadline = time.monotonic() + 30
        c_path = _instance_dir("c") / "media" / filename
        while not c_path.is_file() and time.monotonic() < deadline:
            time.sleep(2)
        if not c_path.is_file():
            raise SystemExit(
                f"space-gallery-media-blob: dave's {filename} missing from c"
            )
        print(f"  c.media has dave's {filename} ✓")
    if d_name != "Demo Album (renamed)":
        raise SystemExit(
            f"space-gallery-media-blob: d's album is still named {d_name!r} "
            f"— SPACE_GALLERY_ALBUM_UPDATED didn't land"
        )
    print("  d's album followed c's rename ✓")

    # 7. Relay-encryption invariant: b sees SPACE_ROUTED but never
    #    the inner SPACE_GALLERY_* or SPACE_MEDIA_BLOB.
    if b_log_path.exists():
        b_log_after = b_log_path.read_text(errors="replace")[b_log_before_size:]
        for forbidden in (
            "SPACE_GALLERY_ALBUM_CREATED",
            "SPACE_GALLERY_ALBUM_UPDATED",
            "SPACE_GALLERY_ITEM_CREATED",
            "SPACE_MEDIA_BLOB",
        ):
            if forbidden in b_log_after:
                raise SystemExit(
                    f"space-gallery-media-blob: relay b dispatched inner "
                    f"{forbidden} — encryption invariant broken.",
                )
        if "SPACE_ROUTED" in b_log_after:
            print("  b relayed SPACE_ROUTED without unsealing ✓")

    state["space_gallery_media_blob_ran"] = True
    _save(state)
    print("space-gallery-media-blob: ok")


def cmd_space_sync_catchup_media() -> None:
    """§25.6 sync catch-up ships HISTORICAL media bytes to a new joiner.

    The realtime path (``cmd_space_media_blob`` /
    ``cmd_space_gallery_media_blob``) only fires when a post or gallery
    item is created AFTER the peer is a member. A newcomer joining a
    long-running space saw post/gallery rows but rendered broken
    ``<img src>`` tags because the bytes never crossed the wire.

    This phase covers the catch-up path. Sequence:

    1. **c creates a fresh mesh-private space** (separate from the
       one in ``remote-invite-routed`` — that's the realtime case).
    2. **c populates** the space *before* inviting anyone:
       - upload an image, create a post referencing it
       - create a gallery album and upload an item into it
    3. **c invites dave** via mesh (b relay).
    4. **d accepts** — the §25.6 sync runs ``stream_initial`` against
       d's session. Post the metadata sentinel, the provider enumerates
       posts + gallery items + their media URLs and enqueues
       ``space_media_outbox`` rows targeting d.
    5. **After settle**, d's media path has BOTH the post image AND the
       gallery item files (thumbnail + full).
    6. **Relay invariant**: b sees ``SPACE_ROUTED`` envelopes but
       never the inner ``SPACE_MEDIA_BLOB`` — the per-target
       ephemeral X25519 seal keeps the relay opaque.
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' first")
    c = state["instances"]["c"]
    d = state["instances"]["d"]

    # 1. c creates a fresh private space (independent of the one
    #    remote-invite-routed seated dave in).
    s, space = _request(
        f"http://127.0.0.1:{c['port']}/api/spaces",
        token=c["token"],
        method="POST",
        body={
            "name": "Carol's archive (catchup)",
            "space_type": "private",
            "join_mode": "invite_only",
            "emoji": "📼",
        },
    )
    _must("create space(c, catchup)", s, space, ok=(201,))
    space_id = space["id"]
    print(f"  c created space (catchup test): {space_id}")

    # 2a. c uploads an image and posts it — BEFORE inviting anyone, so
    #     the realtime fan-out has zero recipients. Only the catch-up
    #     path will deliver this to dave.
    from PIL import Image
    from io import BytesIO

    img = Image.new("RGB", (32, 32), color=(100, 150, 200))
    buf = BytesIO()
    img.save(buf, format="PNG")
    png_bytes = buf.getvalue()
    boundary = "----sh-demo-catchup-post"
    body = (
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="catchup-post.png"\r\n'
            f"Content-Type: image/png\r\n\r\n"
        ).encode()
        + png_bytes
        + f"\r\n--{boundary}--\r\n".encode()
    )
    upload_req = urllib.request.Request(
        f"http://127.0.0.1:{c['port']}/api/media/upload",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {c['token']}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    with urllib.request.urlopen(upload_req) as resp:
        post_upload = json.loads(resp.read())
    post_filename = post_upload["filename"]
    post_media_url = post_upload["url"]
    print(f"  c uploaded post image → {post_filename}")

    s, post = _request(
        f"http://127.0.0.1:{c['port']}/api/spaces/{space_id}/posts",
        token=c["token"],
        method="POST",
        body={
            "type": "image",
            "image_urls": [post_media_url],
        },
    )
    _must("c posts image (pre-invite)", s, post, ok=(201,))
    post_id = post["id"]
    print(f"  c posted image (pre-invite) → post_id={post_id}")

    # 2b. c creates a gallery album + uploads an item — also pre-invite.
    s, album = _request(
        f"http://127.0.0.1:{c['port']}/api/spaces/{space_id}/gallery/albums",
        token=c["token"],
        method="POST",
        body={"name": "Catchup Album", "description": "pre-invite"},
    )
    _must("c creates album (pre-invite)", s, album, ok=(201,))
    album_id = album["id"]
    print(f"  c created album (pre-invite) → {album_id}")

    img2 = Image.new("RGB", (32, 32), color=(220, 50, 80))
    buf2 = BytesIO()
    img2.save(buf2, format="PNG")
    gallery_png_bytes = buf2.getvalue()
    boundary2 = "----sh-demo-catchup-gallery"
    body2 = (
        (
            f"--{boundary2}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="catchup-gallery.png"\r\n'
            f"Content-Type: image/png\r\n\r\n"
        ).encode()
        + gallery_png_bytes
        + f"\r\n--{boundary2}--\r\n".encode()
    )
    upload_req2 = urllib.request.Request(
        f"http://127.0.0.1:{c['port']}/api/gallery/albums/{album_id}/items",
        data=body2,
        method="POST",
        headers={
            "Authorization": f"Bearer {c['token']}",
            "Content-Type": f"multipart/form-data; boundary={boundary2}",
        },
    )
    with urllib.request.urlopen(upload_req2) as resp:
        gallery_item = json.loads(resp.read())
    gallery_url = gallery_item.get("url") or ""
    gallery_thumb_url = gallery_item.get("thumbnail_url") or ""
    print(f"  c uploaded gallery item (pre-invite) → {gallery_item['id']}")

    # 3. Truncate b's log so the relay-invariant scan is bounded.
    b_log_path = _instance_dir("b") / "log.txt"
    b_log_before_size = b_log_path.stat().st_size if b_log_path.exists() else 0

    # 4. c invites dave via mesh — c is NOT directly paired with d.
    s, inv = _request(
        f"http://127.0.0.1:{c['port']}/api/spaces/{space_id}/remote-invites",
        token=c["token"],
        method="POST",
        body={
            "invitee_instance_id": d["instance_id"],
            "invitee_user_id": d["user_id"],
        },
    )
    _must("c → d remote-invite(catchup)", s, inv, ok=(201,))
    print("  c → d: invite issued via mesh (post-population)")

    # 5. Wait for invite to round-trip + d to surface it in inbox.
    time.sleep(10)
    s, invites = _request(
        f"http://127.0.0.1:{d['port']}/api/remote_invites",
        token=d["token"],
    )
    _must("d.remote_invites(catchup)", s, invites)
    dave_token = None
    invites_list = invites if isinstance(invites, list) else invites.get("invites", [])
    for row in invites_list:
        if row.get("space_id") == space_id:
            dave_token = row.get("invite_token") or row.get("token")
            break
    if dave_token is None:
        raise SystemExit(
            f"space-sync-catchup-media: d's inbox did not receive the "
            f"invite for space {space_id}; got {invites_list!r}",
        )

    # 6. d accepts → §25.6 sync fires → catch-up enqueues happen.
    s, accepted = _request(
        f"http://127.0.0.1:{d['port']}/api/remote_invites/{dave_token}/accept",
        token=d["token"],
        method="POST",
        body={},
    )
    _must("d accepts invite(catchup)", s, accepted, ok=(200, 204))
    print("  d accepted; §25.6 sync drains historical metadata + bytes")

    # 7. Wait for sync + outbox scheduler ticks to deliver the bytes.
    #    The media outbox scheduler runs every 5s; each chunk plus the
    #    federation outbox add a few seconds. Be generous.
    time.sleep(30)

    # 7b. METADATA first — the rows, not just the bytes (#648).
    #
    # This step used to assert only that the image *bytes* landed on d,
    # which is why #648 shipped: media travels a durable retry outbox
    # (``space_media_outbox`` rows with attempts + backoff) and
    # self-heals, while the §25.6 metadata stream is one-shot,
    # in-memory, fire-and-forget. A mesh member could therefore end up
    # with the space, the content key and every byte on disk, and zero
    # post rows — a space that renders empty — with every assertion
    # here still green. Assert the rows.
    d_post_ids = _rows(
        "d",
        "SELECT id FROM space_posts WHERE space_id = ?",
        (space_id,),
    )
    if post_id not in {r[0] for r in d_post_ids}:
        raise SystemExit(
            f"space-sync-catchup-media: d has no space_posts row for the "
            f"pre-invite post {post_id} (saw {[r[0] for r in d_post_ids]}) — "
            f"the catch-up delivered media bytes but no metadata",
        )
    print(f"  d.space_posts has pre-invite post {post_id} ✓ (catch-up)")

    # The pre-invite post's image MUST show in d's own system ("Posts")
    # album. Every household keeps its own system album (one per space,
    # its own id), so the host's album is never stored here; since #868
    # a synced post is mirrored into the joiner's album through
    # ``SpacePostSynced`` → ``SystemAlbumBridge``, exactly like a live one.
    d_albums = {
        r[0]
        for r in _rows(
            "d",
            "SELECT id FROM gallery_albums WHERE space_id = ?",
            (space_id,),
        )
    }
    d_system = _rows(
        "d",
        "SELECT id FROM gallery_albums WHERE space_id = ? AND is_system = 1",
        (space_id,),
    )
    if len(d_system) != 1:
        raise SystemExit(
            f"space-sync-catchup-media: d should hold exactly one system "
            f"album for the space, has {[r[0] for r in d_system]}",
        )
    mirrored = _rows(
        "d",
        "SELECT id FROM gallery_items WHERE album_id = ? AND source_post_id = ?",
        (d_system[0][0], post_id),
    )
    if not mirrored:
        raise SystemExit(
            f"space-sync-catchup-media: d's Posts album {d_system[0][0]} has "
            f"no mirror of the pre-invite post {post_id}",
        )
    print("  d's own Posts album mirrors the pre-invite post ✓ (catch-up)")

    # The USER-created album and its item must arrive too (#650). These
    # used to be impossible: ``gallery_albums.owner_user_id`` and
    # ``gallery_items.uploaded_by`` carried ``REFERENCES users(user_id)``,
    # which a REMOTE owner can never satisfy, so the insert raised and the
    # receiver swallowed it — the joiner got image bytes and no rows.
    # Migration 0046 drops those FKs, matching ``space_posts.author`` (a
    # bare TEXT, no FK, precisely because the author may be remote).
    if album_id not in d_albums:
        raise SystemExit(
            f"space-sync-catchup-media: d has no gallery album row for the "
            f"pre-invite album {album_id} (saw {sorted(d_albums)})",
        )
    print(f"  d.gallery_albums has pre-invite album {album_id} ✓ (catch-up)")

    d_items = {
        r[0]
        for r in _rows(
            "d",
            "SELECT id FROM gallery_items WHERE album_id = ?",
            (album_id,),
        )
    }
    if not d_items:
        raise SystemExit(
            f"space-sync-catchup-media: d has no gallery_items rows in the "
            f"pre-invite album {album_id} — the album synced but its "
            f"contents did not",
        )
    print(f"  d.gallery_items has {len(d_items)} row(s) in {album_id} ✓")

    # The REST surface must agree with the DB — that's what a user sees.
    s, feed = _request(
        f"http://127.0.0.1:{d['port']}/api/spaces/{space_id}/feed",
        token=d["token"],
    )
    _must("d reads the space feed", s, feed, ok=(200,))
    # ``/feed`` returns either ``{"posts": [...]}`` or a bare list depending
    # on the route revision — same tolerance cmd_sync_https_fallback applies.
    feed_posts = feed.get("posts") if isinstance(feed, dict) else feed
    feed_ids = {p.get("id") for p in (feed_posts or [])}
    if post_id not in feed_ids:
        raise SystemExit(
            f"space-sync-catchup-media: d's /feed omits the pre-invite post "
            f"{post_id} (saw {sorted(i for i in feed_ids if i)})",
        )
    print("  d GET /feed surfaces the pre-invite post ✓")

    # 8. Bytes for the pre-invite post image MUST land on d.
    d_post_path = _instance_dir("d") / "media" / post_filename
    if not d_post_path.is_file():
        raise SystemExit(
            f"space-sync-catchup-media: post image {post_filename} "
            f"missing from d ({d_post_path}) — catch-up didn't deliver "
            f"the post's bytes",
        )
    c_post_path = _instance_dir("c") / "media" / post_filename
    if c_post_path.read_bytes() != d_post_path.read_bytes():
        raise SystemExit(
            f"space-sync-catchup-media: post bytes mismatch for {post_filename}",
        )
    print(f"  d.media has post image {post_filename} ✓ (catch-up)")

    # 9. Bytes for the pre-invite gallery item (thumb + full) MUST land.
    for url in {gallery_url, gallery_thumb_url}:
        if not url:
            continue
        filename = url.rsplit("/", 1)[-1].split("?", 1)[0]
        d_path = _instance_dir("d") / "media" / filename
        if not d_path.is_file():
            raise SystemExit(
                f"space-sync-catchup-media: gallery file {filename} "
                f"missing from d ({d_path}) — catch-up didn't deliver "
                f"the gallery bytes",
            )
        c_path = _instance_dir("c") / "media" / filename
        if c_path.read_bytes() != d_path.read_bytes():
            raise SystemExit(
                f"space-sync-catchup-media: gallery bytes mismatch for {filename}",
            )
        print(f"  d.media has gallery file {filename} ✓ (catch-up)")

    # 10. Relay invariant: b never dispatched the inner SPACE_MEDIA_BLOB.
    if b_log_path.exists():
        b_log_after = b_log_path.read_text(errors="replace")[b_log_before_size:]
        for forbidden in (
            "SPACE_MEDIA_BLOB",
            "SPACE_POST_CREATED",
            "SPACE_GALLERY_ITEM_CREATED",
        ):
            if forbidden in b_log_after:
                raise SystemExit(
                    f"space-sync-catchup-media: relay b dispatched inner "
                    f"{forbidden} — encryption invariant broken.",
                )
        if "SPACE_ROUTED" in b_log_after:
            print("  b relayed SPACE_ROUTED without unsealing ✓")

    state["space_sync_catchup_media_ran"] = True
    state["space_sync_catchup_media_space_id"] = space_id
    # sync-https-fallback re-checks this after dave's restart to tell a
    # real sync stream apart from an outbox redelivery of the new post.
    state["space_sync_catchup_media_post_id"] = post_id
    _save(state)
    print("space-sync-catchup-media: ok")


@_offline_step
def cmd_admin_promote_kick() -> None:
    """Cross-household admin promotion (#114 phase 1, v_8+) — sequenced so
    it deterministically proves the v_28 ``SPACE_ROUTE_STALE`` nack.

    Builds on ``cmd_remote_invite_routed`` which left dave (on d) seated
    as a remote member of c's mesh-private space. c and d are not paired,
    so everything c sends d rides ``SPACE_ROUTED`` via b.

    Sequence:

    a. **Warm c→d.** c posts in the shared space and the post must land
       on d (``_await_space_post``), so c's ``_route_cache`` entry for d
       is live (< ``ROUTE_CACHE_TTL_S`` = 270 s) and sealed under the
       ephemeral key d's *current* process holds.
    b. **Kill d** (SIGTERM the process group, as ``sync-https-fallback``
       does) and wait until it is gone. d's ephemeral private halves live
       only in RAM, so the key c cached in (a) is now dead.
    c. **While d is down, c promotes dave** via
       ``PATCH /api/spaces/{id}/remote-members/{instance}/{user}`` with
       ``{"role":"admin"}``. c updates ``space_remote_members.role`` and
       broadcasts ``SPACE_MEMBER_ROLE_CHANGED``; for d that is a
       route-cache HIT, so c seals under d's OLD key and ships
       ``SPACE_ROUTED`` to b, which accepts — c reports ``ok``. b's hop to
       d fails and the envelope lands in b's durable ``federation_outbox``.
    d. **Respawn d** on the same data dir (with the ``SH_FORCE_SYNC_HTTPS=1``
       env the fallback step left it running with) and wait for
       ``/api/instance/config``.
    e. b's outbox redelivers the stale-sealed envelope (5/10/20 s ladder)
       → d holds no private half for that key → d signs and sends
       ``SPACE_ROUTE_STALE`` to b → b walks it back to c → c verifies it
       against the identity key it pinned at discovery, invalidates the
       route, rediscovers (d mints a fresh key) and retransmits the
       identical inner event once → d applies the role.

    Assertions (all hard — this ordering exercises the nack
    deterministically, so its absence is a failure, not a skip):

    - c's PATCH returns 200 with the new role AND c logged no
      ``broadcast_to_space_members … did not reach`` for d. That WARNING
      would mean the cache was NOT warm (step a didn't take): c probed
      for d while d was down, got ``no_route``, and the nack path was
      never exercised. A real finding — reported, never papered over.
    - d's ``space_members.role`` for dave flips to ``'admin'`` (polled up
      to 90 s: outbox redelivery cadence + nack walk-back + fresh
      ``SPACE_FIND_ROUTE`` + retransmit).
    - d's post-respawn log carries the nack emission
      (``no cached target_eph_priv … nacked to <b>``).
    - c's log, from the PATCH on, carries the origin's recovery line
      ``rediscovered, retransmitted`` naming d's instance id **and**
      ``space_member_role_changed`` (c may retransmit other envelopes to d in
      the same window — only the role change proves the step). The word
      before it is ``invalidated`` (c's cache still pointed at the dead
      key and was dropped) or ``already rebuilt`` (``invalidate_if_eph``
      found the route refreshed already — the respawned d's catch-up
      ``SPACE_SYNC_BEGIN`` beat the nack to c). Both are real recoveries:
      role applied, d nacked, c retransmitted.

    Why the previous ordering was not a proof: this step used to run
    straight after ``sync-https-fallback`` and lean on *that* step's
    respawn of d to leave c's route stale. But the respawned d BEGINs a
    mesh catch-up sync to c, and c's ``_handle_space_sync_begin``
    invalidates and rediscovers its route to the requester (#648) — so by
    the time c PATCHed it usually held a FRESH route, and the nack path
    was hit only if the PATCH happened to beat d's BEGIN. Pre-v_28 that
    same race is why the step failed intermittently: when the PATCH won,
    d dropped the stale seal in silence and the role change was lost
    until the cache expired. Killing d *between* the warm-up and the
    PATCH removes the race. Routing the stale envelope through b's outbox
    also proves the outbox → nack → retransmit interplay: the nack has to
    reach c inside its 270 s pending-record window
    (``routed_envelope._PENDING_ROUTED_TTL_S ==
    route_discovery.ROUTE_CACHE_TTL_S``), which comfortably covers every
    rung of b's 5/10/20/40 s outbox ladder (~75 s cumulative). If c's
    one-shot rediscovery races d's boot and finds no route, c logs
    ``no route on rediscovery; deferring one retransmit`` and retries
    exactly once past the discovery negative cooldown; a second miss
    logs ``still no route … on the deferred attempt; giving up``.

    The kick exercise (dave kicking someone via
    ``SPACE_REMOTE_ADMIN_KICK``) lives in
    ``tests/services/test_space_service_federation_coverage.py`` —
    end-to-end via the demo would require seating a *second* remote
    member specifically to be the kick target, which would duplicate the
    unit coverage without adding signal beyond the role-propagation
    assertion above.
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' first")
    space_id = state.get("remote_invite_routed_space_id")
    if not space_id:
        raise SystemExit(
            "admin-promote-kick: run 'remote-invite-routed' first to seat dave",
        )
    c = state["instances"]["c"]
    d = state["instances"]["d"]

    # a. Warm c's route to d with a post d must receive.
    warm_marker = f"route-warm-{time.time_ns()}"
    s, post = _request(
        f"http://127.0.0.1:{c['port']}/api/spaces/{space_id}/posts",
        token=c["token"],
        method="POST",
        body={
            "type": "text",
            "content": f"[c] warming the mesh route to d {warm_marker}",
        },
    )
    _must("c posts to warm the c→d route", s, post, ok=(201,))
    _await_space_post(state, "d", space_id, post["id"])
    print(f"  c→d route warmed: d received post {post['id']} ✓")

    # b. Kill d so the ephemeral key c just sealed under dies with it.
    _open_offline_window("d")
    print(f"  killing d (pid={d['pid']}) — its RAM-only ephemeral keys die with it")
    try:
        os.killpg(d["pid"], signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and _alive(d["pid"]):
        time.sleep(0.2)
    if _alive(d["pid"]):
        try:
            os.killpg(d["pid"], signal.SIGKILL)
        except ProcessLookupError:
            pass
        time.sleep(0.5)

    # c. While d is down, c promotes dave. Bookmark c's log first so the
    #    evidence scan covers only this step — c's log spans the whole
    #    run and the nack path may well have fired earlier.
    c_log_mark = _log_size("c")
    s, body = _request(
        f"http://127.0.0.1:{c['port']}/api/spaces/{space_id}"
        f"/remote-members/{d['instance_id']}/{d['user_id']}",
        token=c["token"],
        method="PATCH",
        body={"role": "admin"},
    )
    _must("c promotes dave to admin", s, body, ok=(200,))
    if body.get("role") != "admin":
        raise SystemExit(
            f"admin-promote-kick: PATCH returned unexpected role: {body!r}",
        )
    print("  c promoted dave to admin while d was down ✓")
    # The broadcast to d must have been a route-cache HIT: c sealed under
    # the dead key and b accepted the outer envelope, so c saw ``ok``. A
    # MISS means c probed for d while d was down, got ``no_route`` and
    # logged the mesh-loss WARNING — the nack path was never exercised.
    lost = [
        line
        for line in _log_lines_matching("c", "did not reach", offset=c_log_mark)
        if d["instance_id"] in line
    ]
    if lost:
        for line in lost[:3]:
            print(f"    c: {line.strip()[:220]}")
        raise SystemExit(
            "admin-promote-kick: c's SPACE_MEMBER_ROLE_CHANGED did NOT ride the "
            "cached route to d (WARNING above) — the route cache was not warm "
            "when c PATCHed, so the v_28 nack path was not exercised. Check that "
            "the warm-up post in step (a) was mesh-routed to d and that "
            "ROUTE_CACHE_TTL_S has not shrunk.",
        )

    # d. Respawn d on the same data dir. ``_spawn`` truncates d's log.txt,
    #    so the post-respawn scans below start at offset 0.
    new_pid = _spawn("d", d["port"], extra_env={"SH_FORCE_SYNC_HTTPS": "1"})
    state["instances"]["d"]["pid"] = new_pid
    # Persist the pid IMMEDIATELY — see ``cmd_sync_https_fallback`` for how
    # a stale pid in state.json poisons every later run.
    _save(state)
    _wait_ready(d["port"])
    print(f"  d respawned: pid={new_pid}")

    # e. Poll d's stub for the role flip: b's outbox redelivery (first
    #    rungs 5 / 10 / 20 s, ±30 %) + nack walk-back + a fresh
    #    SPACE_FIND_ROUTE flood + retransmit.
    role_sql = "SELECT role FROM space_members WHERE space_id=? AND user_id=?"
    deadline = time.monotonic() + 90.0
    rows: list[tuple] = []
    while time.monotonic() < deadline:
        time.sleep(2)
        rows = _rows("d", role_sql, (space_id, d["user_id"]))
        if rows and rows[0][0] == "admin":
            break

    # Evidence, gathered before deciding so a failure prints it.
    #   ``stale_at_target``: what d logs for every SPACE_ROUTED it cannot
    #   open (INFO "…; nacked to …" under v_28, WARNING "…; dropping"
    #   pre-v_28 / on a failed nack send).
    #   ``nack_recovered``: the stable substring of the origin's INFO
    #   success line in ``routed_envelope._on_route_stale_at_origin`` (and
    #   its ``(deferred attempt)`` sibling). Deliberately NOT anchored on
    #   the word before it: ``invalidated`` and ``already rebuilt`` are
    #   both legitimate outcomes (see the docstring), and the match is
    #   further required to name d's instance id below.
    stale_at_target = "no cached target_eph_priv"
    nack_recovered = "rediscovered, retransmitted"
    nacked = [
        line
        for line in _log_lines_matching("d", stale_at_target)
        if "nacked to" in line
    ]
    dropped = [
        line
        for line in _log_lines_matching("d", stale_at_target)
        if "nacked to" not in line
    ]
    # The retransmit line must name BOTH d's instance id AND the role-change
    # event. c may legitimately retransmit other envelopes to d in this window
    # (run 3 of #673 matched a ``space_sync_direct_failed`` retransmit first),
    # and a line for any other event proves nothing about the role change.
    recovered = [
        line
        for line in _log_lines_matching("c", nack_recovered, offset=c_log_mark)
        if d["instance_id"] in line and "space_member_role_changed" in line
    ]

    def _dump_evidence() -> None:
        for line in (nacked + dropped)[:3]:
            print(f"    d: {line.strip()[:220]}")
        for line in _log_lines_matching("c", "SPACE_ROUTE_STALE", offset=c_log_mark)[
            :5
        ]:
            print(f"    c: {line.strip()[:220]}")

    if not rows:
        raise SystemExit(
            f"admin-promote-kick: dave's space_members row missing on d "
            f"(space={space_id})",
        )
    if rows[0][0] != "admin":
        _dump_evidence()
        raise SystemExit(
            f"admin-promote-kick: dave's role on d is {rows[0][0]!r}, expected "
            "'admin' within 90 s — the stale-sealed SPACE_MEMBER_ROLE_CHANGED "
            "was not recovered. c 'still no route … on the deferred attempt; "
            "giving up' → c verified the nack, its one-shot SPACE_FIND_ROUTE "
            "(2 s window) raced d's boot, and the single deferred retransmit "
            "found no route either. c 'deferring one retransmit' with no later "
            "'rediscovered, retransmitted' line → the deferred attempt is still "
            "pending (negative cooldown + 5 s) or failed (WARNING 'deferred "
            "retransmit … failed'). d 'nacked to' + no c line at all → the nack "
            "was dropped at a hop or rejected at the origin — c's 270 s "
            "pending-record window (routed_envelope._PENDING_ROUTED_TTL_S) "
            "outlasts b's whole 5/10/20/40 s outbox ladder, so a late arrival is "
            "not the cause. "
            "d '; dropping' → a hop is pre-v_28 or the nack send failed. No d "
            "line at all → b's outbox never redelivered the envelope. Grep "
            f"'SPACE_ROUTE_STALE' in {_log_path('c')} and {_log_path('d')}.",
        )
    print("  d's space_members.role for dave = 'admin' ✓")

    if not nacked:
        _dump_evidence()
        raise SystemExit(
            "admin-promote-kick: the role applied but d never emitted a "
            "SPACE_ROUTE_STALE nack (no 'no cached target_eph_priv … nacked to' "
            "line after the respawn) — the role change did not travel the "
            "stale route this step set up, so nothing here proves the v_28 "
            f"path. Inspect {_log_path('d')}.",
        )
    print("  d nacked the stale-sealed envelope ✓ (v_28)")
    print(f"    d: {nacked[0].strip()[:220]}")
    if not recovered:
        _dump_evidence()
        raise SystemExit(
            "admin-promote-kick: d nacked, the role applied, but c never logged "
            f"{nack_recovered!r} naming {d['instance_id'][:8]} — the role change "
            "arrived by some other path than the verified nack + retransmit. "
            f"Inspect {_log_path('c')} for 'SPACE_ROUTE_STALE'.",
        )
    variant = "already rebuilt" if "already rebuilt" in recovered[0] else "invalidated"
    attempt = " (deferred attempt)" if "(deferred attempt)" in recovered[0] else ""
    print(
        "  c verified the nack against d's pinned identity key: route "
        f"{variant}, rediscovered, role change retransmitted{attempt} ✓ (v_28)"
    )
    print(f"    c: {recovered[0].strip()[:220]}")
    state["admin_promote_kick_nack_proven"] = True

    state["admin_promote_kick_ran"] = True
    _save(state)
    print("admin-promote-kick: ok")


def cmd_app_session() -> None:
    """App-to-app federation smoke test (v_17+/v_18, ``APP_SESSION`` + ``APP_MESSAGE``).

    Exercises the cross-household app federation surface introduced in PR4
    and extended in the chess-p2p branch (per-user routing, v_18):

    1. **Open a session (legacy path)** — Alice (a) POSTs to
       ``POST /api/apps/{app_id}/sessions`` with ``peer_instance_id = b``
       (back-compat household-addressed open).  The backend allocates a
       ``session_id``, sends an ``APP_SESSION {verb:"open"}`` event to b,
       and returns the ``session_id``.
    2. **Send a message (legacy path)** — Alice POSTs to
       ``POST /api/apps/{app_id}/messages`` with the new ``session_id``,
       ``peer_instance_id = b``, and a small payload dict.  The server
       selects the ``fed-app-v1`` binary channel or the ``APP_MESSAGE``
       JSON fallback transparently.
    3. **Contacts endpoint (v_18)** — Alice fetches
       ``GET /api/apps/{app_id}/contacts`` and asserts the response is
       ``{contacts: [...]}`` with ``is_local`` / ``instance_id`` /
       ``user_ref`` / ``display_name`` / ``online`` fields present.
    4. **Person-routed session (v_18, when a v_18 contact is available)** —
       If the contacts list contains at least one remote contact on b, Alice
       opens a person-addressed session via
       ``POST /api/apps/{app_id}/sessions`` with a ``target`` body and asserts
       the 201 response carries a ``session_id``.

    **Guard condition:** The test requires at least one app to be installed
    and enabled on both a and b.  In the demo environment the app catalog is
    not connected to a real GitHub release endpoint, so there will generally be
    no installed apps.  The step therefore:

    - Probes ``GET /api/apps`` on both a and b.
    - If both return a non-empty list with at least one common ``app_id``,
      runs the full open-session + send-message round-trip and asserts
      both API calls return the expected 2xx codes.
    - If no common app is found, skips gracefully and logs a note — the
      REST and federation machinery is covered by unit tests; this demo step
      validates the wiring end-to-end when the environment supports it.

    The WS ``app.message`` delivery is intentionally NOT asserted here —
    the demo environment has no long-lived WebSocket listener, and the unit
    tests in ``tests/services/test_app_federation_service.py`` cover the
    delivery path with an in-memory WS mock.

    NOTE (v_18 runtime validation): steps 3 and 4 use the real instances booted
    by ``cmd_up``/``cmd_pair`` in this session.  The person-routed send (step 4)
    is only attempted when the contacts list actually contains a remote contact
    on b; when no contacts are present (e.g. because USERS_SYNC hasn't settled)
    it is skipped with a note — the unit tests in
    ``tests/services/test_app_federation_service.py`` cover the full delivery
    path with a WS mock.
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' first")
    a = state["instances"]["a"]
    b = state["instances"]["b"]

    # 1. Probe installed apps on both households.
    s_a, apps_a = _request(
        f"http://127.0.0.1:{a['port']}/api/apps",
        token=a["token"],
    )
    s_b, apps_b = _request(
        f"http://127.0.0.1:{b['port']}/api/apps",
        token=b["token"],
    )
    if s_a not in (200,) or s_b not in (200,):
        print(
            f"  app-session: GET /api/apps failed (a={s_a}, b={s_b}) — skipping",
        )
        state["app_session_skipped"] = "api_unavailable"
        _save(state)
        print("app-session: skipped (GET /api/apps unavailable)")
        return

    ids_a = {app["app_id"] for app in (apps_a if isinstance(apps_a, list) else [])}
    ids_b = {app["app_id"] for app in (apps_b if isinstance(apps_b, list) else [])}
    common = ids_a & ids_b
    if not common:
        print(
            "  app-session: no common installed app on a + b — "
            "skipping (unit tests cover the federation path)",
        )
        state["app_session_skipped"] = "no_common_app"
        _save(state)
        print("app-session: skipped (no shared installed app)")
        return

    app_id = next(iter(common))
    print(f"  app-session: using app_id={app_id!r}")

    # 2. Open a cross-household session (a → b) via the legacy path.
    s, session_res = _request(
        f"http://127.0.0.1:{a['port']}/api/apps/{app_id}/sessions",
        token=a["token"],
        method="POST",
        body={"peer_instance_id": b["instance_id"]},
    )
    _must("open app session (a→b, legacy)", s, session_res, ok=(200, 201))
    session_id = (session_res or {}).get("session_id")
    if not session_id:
        raise SystemExit(
            f"app-session: POST /sessions returned no session_id: {session_res!r}",
        )
    print(f"  opened session (legacy): {session_id}")

    # Allow APP_SESSION event to propagate before sending the message.
    time.sleep(2)

    # 3. Send an app message from a to b within the session (legacy path).
    s, msg_res = _request(
        f"http://127.0.0.1:{a['port']}/api/apps/{app_id}/messages",
        token=a["token"],
        method="POST",
        body={
            "session_id": session_id,
            "peer_instance_id": b["instance_id"],
            "payload": {"move": "e2-e4", "seq": 1},
        },
    )
    _must("send app message (a→b, legacy)", s, msg_res, ok=(200, 204))
    print("  sent app message (move: e2-e4) via legacy path ✓")

    # 4. v_18: probe the contacts endpoint (GET /api/apps/{id}/contacts).
    s_c, contacts_res = _request(
        f"http://127.0.0.1:{a['port']}/api/apps/{app_id}/contacts",
        token=a["token"],
    )
    if s_c != 200:
        print(f"  app-session: GET /contacts returned {s_c} — skipping v_18 assertions")
    else:
        contacts = (contacts_res or {}).get("contacts", [])
        print(f"  contacts: {len(contacts)} entries")
        # Validate contact shape.
        for c in contacts[:3]:  # spot-check first few
            assert "instance_id" in c, f"contact missing instance_id: {c!r}"
            assert "user_ref" in c, f"contact missing user_ref: {c!r}"
            assert "display_name" in c, f"contact missing display_name: {c!r}"
            assert "is_local" in c, f"contact missing is_local: {c!r}"
            assert "online" in c, f"contact missing online: {c!r}"
        print("  contacts shape ok ✓")

        # 5. v_18: person-routed session — pick a remote contact on b if available.
        b_contacts = [
            c
            for c in contacts
            if not c.get("is_local") and c.get("instance_id") == b.get("instance_id")
        ]
        if b_contacts:
            target_contact = b_contacts[0]
            target = {
                "instance_id": target_contact["instance_id"],
                "user_ref": target_contact["user_ref"],
                "is_local": False,
            }
            s_pr, pr_res = _request(
                f"http://127.0.0.1:{a['port']}/api/apps/{app_id}/sessions",
                token=a["token"],
                method="POST",
                body={"target": target},
            )
            _must(
                "open person-routed session (a→b user, v_18)",
                s_pr,
                pr_res,
                ok=(200, 201),
            )
            pr_session_id = (pr_res or {}).get("session_id")
            if not pr_session_id:
                raise SystemExit(
                    f"app-session: person-routed POST /sessions returned no "
                    f"session_id: {pr_res!r}",
                )
            print(f"  person-routed session opened: {pr_session_id} ✓")
            state["app_session_person_routed_ran"] = True
            state["app_session_person_routed_id"] = pr_session_id
        else:
            print(
                "  app-session: no remote contacts on b yet "
                "(USERS_SYNC may not have settled) — skipping person-routed open; "
                "unit tests cover the full delivery path"
            )

    state["app_session_ran"] = True
    state["app_session_id"] = session_id
    state["app_session_app_id"] = app_id
    _save(state)
    print("app-session: ok")


def _space_chat_conv(label: str, info: dict, space_id: str) -> dict:
    """``GET /api/spaces/{id}/chat`` on ``label`` (the household's own chat
    summary — every household maps the space to its own conversation)."""
    s, body = _request(
        f"http://127.0.0.1:{info['port']}/api/spaces/{space_id}/chat",
        token=info["token"],
    )
    _must(f"space chat summary({label})", s, body)
    return body


def _chat_row(label: str, conv_id: str, needle: str) -> dict | None:
    """The newest message of ``conv_id`` on ``label`` whose content contains
    ``needle`` (deleted rows included — their content is blanked)."""
    import sqlite3

    conn = sqlite3.connect(_instance_dir(label) / "socialhome.db")
    try:
        conn.row_factory = sqlite3.Row
        rows = list(
            conn.execute(
                "SELECT * FROM conversation_messages WHERE conversation_id=?"
                " AND content LIKE ? ORDER BY created_at DESC",
                (conv_id, f"%{needle}%"),
            )
        )
    finally:
        conn.close()
    return dict(rows[0]) if rows else None


def _chat_row_by_id(label: str, msg_id: str) -> dict | None:
    import sqlite3

    conn = sqlite3.connect(_instance_dir(label) / "socialhome.db")
    try:
        conn.row_factory = sqlite3.Row
        rows = list(
            conn.execute("SELECT * FROM conversation_messages WHERE id=?", (msg_id,))
        )
    finally:
        conn.close()
    return dict(rows[0]) if rows else None


def cmd_space_chat() -> None:
    """Every space's members' chat across households (v_55).

    Prereqs: ``traffic`` + ``calendar`` (Beta's private space — Beta the
    owner, Alpha and Gamma remote members). Runs standalone after them.

    1. **Follower household.** Beta mints a ``subscriber`` (Follower)
       invite link (``via: internal``) and Delta — paired with Beta only —
       redeems it over federation, so d sits in the space as a read-only
       follower household. Its chat summary must say ``enabled: false``.
    2. a / b / c read their own chat summary (each household maps the
       space to its own conversation — no conversation id rides the wire).
    3. **Create.** a posts; b and c hold it (``SPACE_CHAT_MESSAGE_CREATED``).
    4. **Reply.** c replies to a's message; a and b hold it, linked to
       their copy of a's message.
    5. **Edit.** b posts and edits its message; a and c hold the edit
       (``SPACE_CHAT_MESSAGE_UPDATED``).
    6. **React.** a reacts 👍 to c's reply; b and c hold the reaction
       (``SPACE_CHAT_REACTION``).
    7. **Moderator delete.** b — the space's owner — deletes c's reply;
       it is deleted (content gone) on a, b and c
       (``SPACE_CHAT_MESSAGE_DELETED``).
    8. **Follower household gets nothing.** d holds no chat message of
       this run (``only_instances`` limits fan-out to households with a
       writer seat).
    9. **v_55.** ``OURS`` is at least ``MIN_FOR_SPACE_CHAT`` and a, b and c
       see each other at it.
    """
    from socialhome.domain.federation_capabilities import (
        OURS as _OURS,
        FederationCapability as _Cap,
    )

    state = _load()
    if not state:
        raise SystemExit("run 'up' first")
    space_id = state.get("space_id")
    if not space_id:
        raise SystemExit("run 'traffic' and 'calendar' first — needs Beta's space")
    inst = state["instances"]
    a, b, c, d = inst["a"], inst["b"], inst["c"], inst["d"]
    nonce = secrets.token_hex(3)

    def wait(what: str, check, timeout: float = 45.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if check():
                return
            time.sleep(1.0)
        raise SystemExit(f"space-chat: timed out waiting for {what}")

    # 1. d joins as a follower household (once per demo run).
    if not state.get("space_chat_follower_d"):
        s, link = _request(
            f"http://127.0.0.1:{b['port']}/api/spaces/{space_id}/invite-tokens",
            token=b["token"],
            method="POST",
            body={"role": "subscriber", "uses": 1, "via": "internal"},
        )
        _must("mint follower link(b)", s, link, ok=(201,))
        s, joined = _request(
            f"http://127.0.0.1:{d['port']}/api/spaces/join",
            token=d["token"],
            method="POST",
            body={
                "token": link["token"],
                "issuer_instance_id": b["instance_id"],
                "space_id": space_id,
            },
            timeout=40.0,
        )
        _must("redeem follower link(d→b)", s, joined, ok=(200, 201))
        state["space_chat_follower_d"] = True
        _save(state)
        print(f"  d joined space {space_id[:8]} as a follower household")

    def d_is_follower_on(label: str) -> bool:
        return any(
            r[0] == "subscriber"
            for r in _rows(
                label,
                "SELECT role FROM space_remote_members WHERE space_id=?"
                " AND user_id=? AND tombstoned=0",
                (space_id, d["user_id"]),
            )
        )

    wait("b to seat d as a follower", lambda: d_is_follower_on("b"))
    wait(
        "a and c to learn d's follower seat",
        lambda: all(d_is_follower_on(x) for x in ("a", "c")),
    )
    print("  b, a and c hold d's follower seat ✓")
    d_chat = _space_chat_conv("d", d, space_id)
    if d_chat.get("enabled") is not False or d_chat.get("conversation_id"):
        raise SystemExit(f"space-chat: follower d got a chat: {d_chat!r}")
    print("  d (follower) sees no chat ✓")

    # 2. Every member household's own chat.
    convs: dict[str, str] = {}
    for label, info in (("a", a), ("b", b), ("c", c)):
        chat = _space_chat_conv(label, info, space_id)
        if chat.get("enabled") is not True or not chat.get("conversation_id"):
            raise SystemExit(f"space-chat: {label} has no chat: {chat!r}")
        convs[label] = chat["conversation_id"]
    print("  chats: " + ", ".join(f"{k}={v[:8]}" for k, v in convs.items()))

    def send(label: str, info: dict, text: str, reply_to: str | None = None) -> str:
        body: dict = {"content": text}
        if reply_to:
            body["reply_to_id"] = reply_to
        s, msg = _request(
            f"http://127.0.0.1:{info['port']}/api/conversations/"
            f"{convs[label]}/messages",
            token=info["token"],
            method="POST",
            body=body,
        )
        _must(f"chat send({label})", s, msg, ok=(200, 201))
        return msg["id"]

    # 3. a posts; b and c receive it.
    hello = f"space-chat hello from a {nonce}"
    hello_id = send("a", a, hello)
    for x in ("b", "c"):
        wait(f"{x} to receive a's message", lambda x=x: _chat_row(x, convs[x], hello))
    print("  a → chat; b and c hold it ✓")

    # 4. c replies to a's message (by c's copy of it); a and b hold the reply
    #    linked to their own copy.
    c_hello = _chat_row("c", convs["c"], hello)
    assert c_hello is not None
    reply = f"space-chat reply from c {nonce}"
    reply_id = send("c", c, reply, reply_to=c_hello["id"])
    for x in ("a", "b"):
        wait(f"{x} to receive c's reply", lambda x=x: _chat_row(x, convs[x], reply))
        got = _chat_row(x, convs[x], reply)
        mine_hello = _chat_row(x, convs[x], hello)
        assert got is not None and mine_hello is not None
        if got["reply_to_id"] != mine_hello["id"]:
            raise SystemExit(
                f"space-chat: {x}'s copy of c's reply points at "
                f"{got['reply_to_id']!r}, not its copy of a's message "
                f"{mine_hello['id']!r}"
            )
    print("  c replies; a and b hold it, linked to a's message ✓")

    # 5. b posts and edits its own message; a and c hold the edit.
    draft = f"space-chat from b {nonce} draft"
    b_id = send("b", b, draft)
    for x in ("a", "c"):
        wait(f"{x} to receive b's message", lambda x=x: _chat_row(x, convs[x], draft))
    edited = f"space-chat from b {nonce} edited"
    s, body = _request(
        f"http://127.0.0.1:{b['port']}/api/conversations/{convs['b']}/messages/{b_id}",
        token=b["token"],
        method="PATCH",
        body={"content": edited},
    )
    _must("chat edit(b)", s, body)
    for x in ("a", "c"):

        def has_edit(x: str = x) -> bool:
            row = _chat_row(x, convs[x], edited)
            return bool(row and row["edited_at"])

        wait(f"{x} to receive b's edit", has_edit)
        if _chat_row(x, convs[x], draft):
            raise SystemExit(f"space-chat: {x} still holds b's pre-edit text")
    print("  b edits its message; a and c hold the edit ✓")

    # 6. a reacts to c's reply; b and c hold the reaction.
    a_reply = _chat_row("a", convs["a"], reply)
    assert a_reply is not None
    s, body = _request(
        f"http://127.0.0.1:{a['port']}/api/conversations/{convs['a']}/messages/"
        f"{a_reply['id']}/reactions/{urllib.request.quote('👍')}",
        token=a["token"],
        method="PUT",
    )
    _must("chat react(a)", s, body, ok=(200, 201, 204))
    for x in ("b", "c"):

        def has_reaction(x: str = x) -> bool:
            row = _chat_row(x, convs[x], reply)
            if row is None:
                return False
            return bool(
                _rows(
                    x,
                    "SELECT 1 FROM message_reactions WHERE message_id=? AND user_id=?"
                    " AND emoji=?",
                    (row["id"], a["user_id"], "👍"),
                )
            )

        wait(f"{x} to receive a's reaction", has_reaction)
    print("  a reacts 👍 to c's reply; b and c hold it ✓")

    # 7. b — the owner — deletes c's reply (a moderator delete); it is gone
    #    on a, b and c.
    b_reply = _chat_row("b", convs["b"], reply)
    assert b_reply is not None
    s, body = _request(
        f"http://127.0.0.1:{b['port']}/api/conversations/{convs['b']}/messages/"
        f"{b_reply['id']}",
        token=b["token"],
        method="DELETE",
    )
    _must("chat moderator delete(b)", s, body)
    reply_ids = {"a": a_reply["id"], "b": b_reply["id"], "c": reply_id}
    for x in ("a", "b", "c"):

        def gone(x: str = x) -> bool:
            row = _chat_row_by_id(x, reply_ids[x])
            return bool(row and row["deleted"] == 1 and reply not in row["content"])

        wait(f"{x} to drop c's reply", gone)
    print("  b (owner) deletes c's reply; deleted on a, b and c ✓")

    # 8. The follower household received nothing of the chat.
    leaked = _rows(
        "d",
        "SELECT id, content FROM conversation_messages WHERE content LIKE ?",
        (f"%{nonce}%",),
    )
    d_space_convs = _rows(
        "d", "SELECT id FROM conversations WHERE space_id=?", (space_id,)
    )
    d_msgs = (
        _rows(
            "d",
            "SELECT COUNT(*) FROM conversation_messages WHERE conversation_id=?",
            (d_space_convs[0][0],),
        )[0][0]
        if d_space_convs
        else 0
    )
    if leaked or d_msgs:
        raise SystemExit(
            f"space-chat: follower household d holds chat messages: "
            f"{leaked!r} / {d_msgs} in its space chat"
        )
    print("  d (follower household) holds no chat message ✓")

    # 9. v_55.
    if _OURS < _Cap.MIN_FOR_SPACE_CHAT:
        raise SystemExit(
            f"space-chat: OURS={_OURS} < MIN_FOR_SPACE_CHAT={_Cap.MIN_FOR_SPACE_CHAT}"
        )
    for label, info in (("a", a), ("b", b), ("c", c)):
        s, conns = _request(
            f"http://127.0.0.1:{info['port']}/api/connections", token=info["token"]
        )
        _must(f"connections({label})", s, conns)
        for row in conns:
            peer = row.get("instance_id")
            if row.get("status") != "confirmed" or peer not in {
                a["instance_id"],
                b["instance_id"],
                c["instance_id"],
            }:
                continue
            pv = int(row.get("proto_version") or 1)
            if pv < _Cap.MIN_FOR_SPACE_CHAT:
                raise SystemExit(
                    f"space-chat: {label} sees {peer[:8]} at v{pv} "
                    f"(< {_Cap.MIN_FOR_SPACE_CHAT})"
                )
    print(f"  a, b, c see each other at >= v{_Cap.MIN_FOR_SPACE_CHAT} (OURS={_OURS}) ✓")
    state["space_chat"] = {"conversations": convs, "nonce": nonce, "hello_id": hello_id}
    _save(state)
    print("space-chat: ok")


def cmd_bazaar_ask() -> None:
    """"Ask the seller" from the listing (v_74).

    Prereqs: ``space-chat`` (Beta's space: owner b, members a + c, d a
    follower household). Runs standalone after it.

    1. b (owner) turns the space's Bazaar on; c lists a lamp in it; the
       listing reaches a and d (``BAZAAR_LISTING_CREATED``).
    2. **One button, the right path.** a asks the seller: a and c are
       paired directly, so the answer is the normal 1:1 DM
       (``kind: "dm"`` — no listing thread, no new wire). a's question
       reaches c.
    3. **Writers only.** d — a follower household — asks too and is
       refused (403 ``SUBSCRIBER_READ_ONLY``), or never holds the listing.
    4. **v_74.** ``OURS`` is at least ``MIN_FOR_BAZAAR_THREAD`` and a, b and
       c see each other at it, so a question to an unpaired seller's
       household would be sent (the sealed thread path between unpaired
       households is the six-household protocol test; every household in
       this demo's space is paired with the others).
    """
    from socialhome.domain.federation_capabilities import (
        OURS as _OURS,
        FederationCapability as _Cap,
    )

    state = _load()
    if not state:
        raise SystemExit("run 'up' first")
    space_id = state.get("space_id")
    if not space_id or not state.get("space_chat_follower_d"):
        raise SystemExit("run 'space-chat' first — needs Beta's space with d")
    inst = state["instances"]
    a, b, c, d = inst["a"], inst["b"], inst["c"], inst["d"]
    nonce = secrets.token_hex(3)

    def wait(what: str, check, timeout: float = 45.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if check():
                return
            time.sleep(1.0)
        raise SystemExit(f"bazaar-ask: timed out waiting for {what}")

    # 1. The Bazaar on, a listing by c.
    s, body = _request(
        f"http://127.0.0.1:{b['port']}/api/spaces/{space_id}",
        token=b["token"],
        method="PATCH",
        body={"features": {"bazaar": True}},
    )
    _must("bazaar on(b)", s, body)
    def bazaar_on(label: str) -> bool:
        rows = _rows(label, "SELECT feature_bazaar FROM spaces WHERE id=?", (space_id,))
        return bool(rows and rows[0][0])

    wait("c to see the Bazaar on", lambda: bazaar_on("c"))
    s, listing = _request(
        f"http://127.0.0.1:{c['port']}/api/bazaar",
        token=c["token"],
        method="POST",
        body={
            "space_id": space_id,
            "title": f"Reading lamp {nonce}",
            "mode": "fixed",
            "currency": "EUR",
            "price": 2500,
            "duration_days": 7,
        },
    )
    _must("bazaar create(c)", s, listing, ok=(201,))
    post_id = listing["post_id"]

    def sees(label: str, info: dict) -> bool:
        st, _ = _request(
            f"http://127.0.0.1:{info['port']}/api/bazaar/{post_id}",
            token=info["token"],
        )
        return st == 200

    wait("a to hold c's listing", lambda: sees("a", a))
    print(f"  c listed {post_id[:8]}… in space {space_id[:8]}; a holds it ✓")

    # 2. a asks: paired with c → the normal DM.
    s, ask = _request(
        f"http://127.0.0.1:{a['port']}/api/bazaar/{post_id}/ask",
        token=a["token"],
        method="POST",
    )
    _must("ask(a)", s, ask, ok=(201,))
    if ask.get("kind") != "dm":
        raise SystemExit(f"bazaar-ask: a↔c are paired, expected a DM: {ask!r}")
    text = f"bazaar-ask: is the lamp still there? {nonce}"
    s, msg = _request(
        f"http://127.0.0.1:{a['port']}/api/conversations/"
        f"{ask['conversation_id']}/messages",
        token=a["token"],
        method="POST",
        body={"content": text},
    )
    _must("ask send(a)", s, msg, ok=(201,))
    wait(
        "c to receive a's question",
        lambda: bool(
            _rows(
                "c", "SELECT 1 FROM conversation_messages WHERE content=?", (text,)
            )
        ),
    )
    print("  a → Ask the seller → DM to c (paired) ✓")

    # 3. d follows only: no asking.
    wait("d to hold c's listing", lambda: sees("d", d), timeout=30.0)
    s, refused = _request(
        f"http://127.0.0.1:{d['port']}/api/bazaar/{post_id}/ask",
        token=d["token"],
        method="POST",
    )
    code = (
        refused.get("error", {}).get("code") if isinstance(refused, dict) else None
    )
    if s != 403 or code != "SUBSCRIBER_READ_ONLY":
        raise SystemExit(f"bazaar-ask: follower d could ask: {s} {refused!r}")
    print("  d (follower) can't ask ✓")

    # 4. v_73 round-trips between the writer households.
    if _OURS < _Cap.MIN_FOR_BAZAAR_THREAD:
        raise SystemExit(
            f"bazaar-ask: OURS={_OURS} < MIN_FOR_BAZAAR_THREAD="
            f"{_Cap.MIN_FOR_BAZAAR_THREAD}"
        )
    for label, info in (("a", a), ("b", b), ("c", c)):
        s, conns = _request(
            f"http://127.0.0.1:{info['port']}/api/connections", token=info["token"]
        )
        _must(f"connections({label})", s, conns)
        for row in conns:
            peer = row.get("instance_id")
            if row.get("status") != "confirmed" or peer not in {
                a["instance_id"],
                b["instance_id"],
                c["instance_id"],
            }:
                continue
            pv = int(row.get("proto_version") or 1)
            if pv < _Cap.MIN_FOR_BAZAAR_THREAD:
                raise SystemExit(
                    f"bazaar-ask: {label} sees {peer[:8]} at v{pv} "
                    f"(< {_Cap.MIN_FOR_BAZAAR_THREAD})"
                )
    print(
        f"  a, b, c see each other at >= v{_Cap.MIN_FOR_BAZAAR_THREAD} "
        f"(OURS={_OURS}) ✓"
    )
    state["bazaar_ask"] = {"post_id": post_id, "text": text}
    _save(state)
    print("bazaar-ask: ok")


def cmd_app_group() -> None:
    """Group games — a table of three households, the roster (v_76).

    Prereqs: ``up`` + ``pair`` (a↔b, a↔c paired). Installs a stand-in
    group app (``demo-table``: ``players {3, 6}``, ``tables: true``) on a, b
    and c straight into ``installed_apps`` — the demo has no catalog; the
    group routes never load the bundle.

    1. a reads its invite sheet (``GET /invitable``): b's and c's admins
       are offered with ``groups: true`` (their households run v_76).
    2. a opens a table with them (``POST /groups``); b and c each get ONE
       sealed ``group_invite`` and list the table as ``invited``.
    3. b and c join (``group_join`` → ``group_seat`` + coalesced
       ``group_roster``); all three converge on seats 1–3.
    4. b (not the host) can't start (403); a starts — ``group_start``
       reaches b and c (``playing``).
    4b. **Messages.** b sends one to everyone and one sealed to c's seat
       only (``APP_MESSAGE.group``, relayed by a — the hub). c's mailbox
       (``POST /held``) gets both, numbered by a; a's gets the one to
       everyone only, and the whisper's marker appears nowhere in a's
       database files: the relay passed on ciphertext.
    5. a closes it — ``group_close`` ends it on b and c (``cancelled``).
    6. **v_76.** ``OURS`` >= ``MIN_FOR_APP_GROUPS`` and a sees b and c at
       it. Forged snapshots, strangers' joins, a non-host's commands and
       the block / children rules are
       ``tests/protocol/test_app_group_federation.py`` /
       ``tests/services/test_app_group_service.py``.
    """
    import sqlite3

    from socialhome.domain.federation_capabilities import (
        OURS as _OURS,
        FederationCapability as _Cap,
    )

    state = _load()
    if not state:
        raise SystemExit("run 'up' first")
    inst = state["instances"]
    a, b, c = inst["a"], inst["b"], inst["c"]
    app_id = "demo-table"
    manifest = (
        '{"entry": "index.html", "icon": null, "capabilities":'
        ' ["storage", "federation"], "players": {"min": 3, "max": 6},'
        ' "tables": true}'
    )
    for label in ("a", "b", "c"):
        con = sqlite3.connect(str(_instance_dir(label) / "socialhome.db"), timeout=10)
        try:
            con.execute(
                "INSERT OR IGNORE INTO installed_apps(app_id, name, version,"
                " enabled, manifest_json, bundle_path, bundle_sha256,"
                " source_url, installed_at) VALUES(?, 'Demo table', '1', 1, ?,"
                " 'apps/demo-table/1', 'demo', 'demo', '2026-10-31')",
                (app_id, manifest),
            )
            con.commit()
        finally:
            con.close()

    def api(node: dict, path: str, *, method: str = "GET", body=None):
        return _request(
            f"http://127.0.0.1:{node['port']}/api/apps/{app_id}{path}",
            token=node["token"],
            method=method,
            body=body,
        )

    def wait(what: str, check, timeout: float = 60.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if check():
                return
            time.sleep(1.0)
        raise SystemExit(f"app-group: timed out waiting for {what}")

    def view(node: dict) -> list[dict]:
        s, body = api(node, "/groups")
        return (body or {}).get("groups", []) if s == 200 else []

    # 1. The invite sheet offers b's and c's admins for a group.
    s, sheet = api(a, "/invitable")
    _must("invitable(a)", s, sheet)
    picks = []
    for node in (b, c):
        hit = [
            p
            for p in sheet["people"]
            if p["instance_id"] == node["instance_id"]
            and p["user_ref"] == node["username"]
        ]
        if not hit or hit[0].get("groups") is not True:
            raise SystemExit(f"app-group: {node['username']} not offered for a group")
        picks.append(
            {"instance_id": node["instance_id"], "user_ref": node["username"],
             "is_local": False}
        )
    # 2. Open the table.
    s, opened = api(a, "/groups", method="POST", body={"targets": picks})
    _must("open table(a)", s, opened, ok=(201,))
    sid = opened["group"]["session_id"]
    for label, node in (("b", b), ("c", c)):
        wait(
            f"{label} to hold the invite",
            lambda node=node: any(
                g["session_id"] == sid and g["my_state"] == "invited"
                for g in view(node)
            ),
        )
        print(f"  app-group: {label} got the invite ✓")
    # 3. b and c join; everyone converges on three seats.
    for label, node in (("b", b), ("c", c)):
        s, joined = api(node, f"/groups/{sid}/join", method="POST")
        _must(f"join({label})", s, joined)

    def seats(node: dict) -> list[int]:
        g = next((g for g in view(node) if g["session_id"] == sid), None)
        return sorted(
            p["seat"] for p in (g or {}).get("players", []) if p["state"] == "seated"
        )

    for label, node in (("a", a), ("b", b), ("c", c)):
        wait(f"{label} to see three seats", lambda node=node: seats(node) == [1, 2, 3])
        print(f"  app-group: {label} sees seats 1-3 ✓")
    # 4. Only the host starts.
    s, refused = api(b, f"/groups/{sid}/start", method="POST")
    if s != 403:
        raise SystemExit(f"app-group: b (not the host) could start: {s} {refused!r}")
    s, started = api(a, f"/groups/{sid}/start", method="POST")
    _must("start(a)", s, started)

    def state_of(node: dict) -> str | None:
        g = next((g for g in view(node) if g["session_id"] == sid), None)
        return g["state"] if g else None

    for label, node in (("b", b), ("c", c)):
        wait(f"{label} to see the game start", lambda node=node: state_of(node) == "playing")
        print(f"  app-group: {label} sees the game playing ✓")
    # 4b. Messages: one to everyone, one sealed to c's seat only.
    def me(node: dict) -> int:
        g = next((g for g in view(node) if g["session_id"] == sid), None)
        return int((g or {}).get("me") or 0)

    seat_b, seat_c = me(b), me(c)
    marker = f"DEMO-WHISPER-{secrets.token_hex(6)}"
    got: dict[str, list[dict]] = {"a": [], "c": []}

    def held(label: str, node: dict) -> list[dict]:
        s, body = api(node, "/held", method="POST")
        if s == 200:
            got[label].extend((body or {}).get("group_messages", []))
        return got[label]

    # The mailbox keeps each sender's latest: c takes the first before the
    # second (from the same seat) replaces it.
    s, sent = api(
        b, f"/groups/{sid}/messages", method="POST", body={"data": {"round": 1}}
    )
    _must("message to everyone(b)", s, sent)
    wait(
        "c to get the message to everyone",
        lambda: any(not m["direct"] for m in held("c", c)),
    )
    s, sent = api(
        b,
        f"/groups/{sid}/messages",
        method="POST",
        body={"data": {"whisper": marker}, "to": [seat_c]},
    )
    _must("sealed message(b → c)", s, sent)
    wait(
        "c to get the sealed message",
        lambda: any(m["direct"] for m in held("c", c)),
    )
    if {m["from_seat"] for m in got["c"]} != {seat_b}:
        raise SystemExit(f"app-group: c's messages name the wrong seat: {got['c']!r}")
    by_kind = {m["direct"]: m for m in got["c"]}
    if by_kind[True]["data"] != {"whisper": marker} or by_kind[False]["data"] != {
        "round": 1
    }:
        raise SystemExit(f"app-group: c got the wrong messages: {got['c']!r}")
    if not by_kind[False]["seq"] < by_kind[True]["seq"]:
        raise SystemExit(f"app-group: the hub's numbering is off: {got['c']!r}")
    print("  app-group: c got the message to everyone and the sealed one ✓")
    wait("a to get the message to everyone", lambda: bool(held("a", a)))
    if any(m["direct"] for m in got["a"]) or marker in json.dumps(got["a"]):
        raise SystemExit(f"app-group: a (the hub) got the whisper: {got['a']!r}")
    for path in _instance_dir("a").glob("socialhome.db*"):
        if marker.encode() in path.read_bytes():
            raise SystemExit(f"app-group: the whisper is readable in a's {path.name}")
    print("  app-group: a (the relay) holds no plaintext of the sealed message ✓")
    # 5. The host closes it.
    s, closed = api(a, f"/groups/{sid}/close", method="POST")
    _must("close(a)", s, closed)
    for label, node in (("b", b), ("c", c)):
        wait(f"{label} to see the table end", lambda node=node: state_of(node) == "ended")
        print(f"  app-group: {label} sees the table closed ✓")
    # 6. v_76 round-trips between the three.
    if _OURS < _Cap.MIN_FOR_APP_GROUPS:
        raise SystemExit(
            f"app-group: OURS={_OURS} < MIN_FOR_APP_GROUPS={_Cap.MIN_FOR_APP_GROUPS}"
        )
    for peer in (b, c):
        rows = _rows(
            "a",
            "SELECT proto_version FROM remote_instances WHERE id = ?",
            (peer["instance_id"],),
        )
        if not rows or int(rows[0][0] or 1) < _Cap.MIN_FOR_APP_GROUPS:
            raise SystemExit(f"app-group: a sees {peer['instance_id'][:8]} below v_76")
    state["app_group"] = {"session_id": sid, "app_id": app_id}
    _save(state)
    print("app-group: ok")


def cmd_app_draw() -> None:
    """The gift draw (v_78) — four people in three households draw names,
    with one pair who must not draw each other, and the host household
    learns only its own people's names.

    Prereqs: ``up`` + ``pair`` (a↔b, a↔c paired). Installs a stand-in
    draw app (``demo-draw``: ``players {3, 30}``, ``tables: true``) on a,
    b and c straight into ``installed_apps``, and a second person on a
    (``dora``).

    1. a's admin opens a table with dora (local), b's and c's admins
       (``POST /groups``); all three join — four seats on a, b, c.
    2. a sets the draw (``POST …/engines/draw/configure``, a patch): name, budget, day and the pair
       (a's admin, dora). Everyone writes a wish list with a marker
       (``POST …/engines/draw/wish``).
    3. a draws names (``POST …/engines/draw/start``); the households run the
       rounds by themselves (the ``engine`` verb — the mix, the cards, any redo
       with its proof) until each person's ``GET …/engines/draw`` says ``ready``.
    4. **The result.** Each person's own ``GET …/engines/draw/result``: the four
       form one circle, the pair don't draw each other, each sheet shows
       the recipient's own wish list.
    5. **Nobody else learns it.** No wish-list marker is in any household's
       database files in plaintext (the draw is KEK-wrapped), and none in
       a's (the host household's) log; a's admin reads only their own
       result.
    6. **v_78.** ``OURS`` >= ``MIN_FOR_APP_ENGINES`` and a sees b and c at it.
       Tampering mixers, false complaints, replays, offline homes and the
       hand-over are ``tests/protocol/test_app_draw.py``.
    """
    import sqlite3

    from socialhome.domain.federation_capabilities import (
        OURS as _OURS,
        FederationCapability as _Cap,
    )

    state = _load()
    if not state:
        raise SystemExit("run 'up' first")
    inst = state["instances"]
    a, b, c = inst["a"], inst["b"], inst["c"]
    app_id = "demo-draw"
    manifest = (
        '{"entry": "index.html", "icon": null, "capabilities":'
        ' ["storage", "federation"], "players": {"min": 3, "max": 30},'
        ' "tables": true, "engines": ["draw"]}'
    )
    for label in ("a", "b", "c"):
        con = sqlite3.connect(str(_instance_dir(label) / "socialhome.db"), timeout=10)
        try:
            con.execute(
                "INSERT OR IGNORE INTO installed_apps(app_id, name, version,"
                " enabled, manifest_json, bundle_path, bundle_sha256,"
                " source_url, installed_at) VALUES(?, 'Demo draw', '1', 1, ?,"
                " 'apps/demo-draw/1', 'demo', 'demo', '2026-10-31')",
                (app_id, manifest),
            )
            con.commit()
        finally:
            con.close()
    a_url = f"http://127.0.0.1:{a['port']}"
    # A second person on a (the host household): dora.
    s, made = _request(
        f"{a_url}/api/admin/users",
        token=a["token"],
        method="POST",
        body={
            "username": "dora",
            "password": "harness-pwd-dora",
            "display_name": "Dora Draw",
            "is_admin": False,
        },
    )
    if s == 409:
        s, listing = _request(f"{a_url}/api/users", token=a["token"])
        _must("list users(a)", s, listing)
        dora_id = next(u["user_id"] for u in listing if u.get("username") == "dora")
    else:
        _must("provision dora(a)", s, made, ok=(201,))
        dora_id = made["user_id"]
    s, login = _request(
        f"{a_url}/api/auth/token",
        method="POST",
        body={"username": "dora", "password": "harness-pwd-dora"},
    )
    _must("dora login(a)", s, login)
    dora = {**a, "token": login["token"], "username": "dora"}
    people = {"a": a, "dora": dora, "b": b, "c": c}

    def api(node: dict, path: str, *, method: str = "GET", body=None):
        return _request(
            f"http://127.0.0.1:{node['port']}/api/apps/{app_id}{path}",
            token=node["token"],
            method=method,
            body=body,
        )

    def wait(what: str, check, timeout: float = 120.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if check():
                return
            time.sleep(1.0)
        raise SystemExit(f"app-draw: timed out waiting for {what}")

    # 1. The table: a's admin, dora (local), b's and c's admins.
    picks = [
        {"instance_id": a["instance_id"], "user_ref": dora_id, "is_local": True},
        {"instance_id": b["instance_id"], "user_ref": b["username"], "is_local": False},
        {"instance_id": c["instance_id"], "user_ref": c["username"], "is_local": False},
    ]
    s, opened = api(a, "/groups", method="POST", body={"targets": picks})
    _must("open table(a)", s, opened, ok=(201,))
    sid = opened["group"]["session_id"]

    def mine(node: dict) -> dict | None:
        s, body = api(node, "/groups")
        groups = (body or {}).get("groups", []) if s == 200 else []
        return next((g for g in groups if g["session_id"] == sid), None)

    for label in ("dora", "b", "c"):
        node = people[label]
        wait(f"{label} to hold the invite", lambda node=node: mine(node) is not None)
        s, joined = api(node, f"/groups/{sid}/join", method="POST")
        _must(f"join({label})", s, joined)

    def seated(node: dict) -> int:
        g = mine(node) or {}
        return sum(1 for p in g.get("players", []) if p["state"] == "seated")

    for label, node in people.items():
        wait(f"{label} to see four seats", lambda node=node: seated(node) == 4)
    print("  app-draw: four people in three households at the table ✓")
    seat = {label: int((mine(node) or {}).get("me") or 0) for label, node in people.items()}
    # 2. Settings, the pair, wish lists.
    s, conf = api(
        a,
        f"/groups/{sid}/engines/draw/configure",
        method="POST",
        body={"name": "Demo gift draw", "budget": "about 20 €", "day": "2026-12-24"},
    )
    _must("configure(a)", s, conf)
    # configure is a patch: the pair keeps the name, budget and day.
    s, conf = api(
        a,
        f"/groups/{sid}/engines/draw/configure",
        method="POST",
        body={"pairs": [[seat["a"], seat["dora"]]]},
    )
    _must("configure pair(a)", s, conf)
    if (conf.get("state") or {}).get("settings", {}).get("name") != "Demo gift draw":
        raise SystemExit(f"app-draw: adding a pair wiped the settings: {conf!r}")
    marks = {label: f"DEMO-WISH-{label}-{secrets.token_hex(6)}" for label in people}
    for label, node in people.items():
        s, w = api(node, f"/groups/{sid}/engines/draw/wish", method="POST", body={"text": marks[label]})
        _must(f"wish({label})", s, w)
    a_log_from = _log_size("a")
    # 3. Draw names.
    s, started = api(a, f"/groups/{sid}/engines/draw/start", method="POST", body={})
    _must("draw names(a)", s, started)

    def ready(node: dict) -> bool:
        s, body = api(node, f"/groups/{sid}/engines/draw")
        d = (body or {}).get("state") or {}
        return s == 200 and d.get("state") == "ready" and d.get("has_result") is True

    for label, node in people.items():
        wait(f"{label}'s name to be ready", lambda node=node: ready(node), timeout=240.0)
    print("  app-draw: every household did its part — four names ready ✓")
    # 4. The results: one circle, the pair apart, the right wish lists.
    # The admins of a, b and c share one display name, so a sheet's person
    # is told apart by (name, household) — the card carries both.
    household = {i[0]: i[4] for i in INSTANCES}
    home_of = {"a": "a", "dora": "a", "b": "b", "c": "c"}
    names: dict[tuple[str, str], str] = {}
    for label, node in people.items():
        s, me = _request(f"http://127.0.0.1:{node['port']}/api/me", token=node["token"])
        _must(f"me({label})", s, me)
        names[(me["display_name"], household[home_of[label]])] = label
    gives: dict[str, str] = {}
    for label, node in people.items():
        s, body = api(node, f"/groups/{sid}/engines/draw/result")
        _must(f"result({label})", s, body)
        res = body["result"]
        to = names.get((res["name"], res["household"]))
        if to is None or to == label:
            raise SystemExit(
                f"app-draw: {label} drew {res['name']!r} of {res['household']!r}"
            )
        if res["wish"] != marks[to]:
            raise SystemExit(f"app-draw: {label}'s sheet shows the wrong wish list")
        gives[label] = to
    if sorted(gives.values()) != sorted(people):
        raise SystemExit(f"app-draw: not everyone gets one gift: {gives!r}")
    seen, cur = set(), "a"
    while cur not in seen:
        seen.add(cur)
        cur = gives[cur]
    if seen != set(people):
        raise SystemExit(f"app-draw: the draw is not one circle: {gives!r}")
    if gives["a"] == "dora" or gives["dora"] == "a":
        raise SystemExit(f"app-draw: the pair drew each other: {gives!r}")
    print("  app-draw: one circle, the pair apart, each sheet the right wish list ✓")
    # 5. Nobody else learns it.
    for label in ("a", "b", "c"):
        for path in _instance_dir(label).glob("socialhome.db*"):
            raw = path.read_bytes()
            for who, mark in marks.items():
                if mark.encode() in raw:
                    raise SystemExit(
                        f"app-draw: {who}'s wish list is readable in {label}'s {path.name}"
                    )
    a_log = _log_path("a").read_bytes()[a_log_from:]
    for who, mark in marks.items():
        if mark.encode() in a_log:
            raise SystemExit(f"app-draw: {who}'s wish list is in a's log")
    print("  app-draw: no wish list or result in plaintext on any disk or a's log ✓")
    # 6. v_78 between the three.
    if _OURS < _Cap.MIN_FOR_APP_ENGINES:
        raise SystemExit(f"app-draw: OURS={_OURS} < MIN_FOR_APP_ENGINES={_Cap.MIN_FOR_APP_ENGINES}")
    for peer in (b, c):
        rows = _rows(
            "a",
            "SELECT proto_version FROM remote_instances WHERE id = ?",
            (peer["instance_id"],),
        )
        if not rows or int(rows[0][0] or 1) < _Cap.MIN_FOR_APP_ENGINES:
            raise SystemExit(f"app-draw: a sees {peer['instance_id'][:8]} below v_78")
    state["app_draw"] = {"session_id": sid, "marks": list(marks.values())}
    _save(state)
    print("app-draw: ok")


def cmd_space_table() -> None:
    """Games in a space (v_77) — a table in a shared space, joined by a
    household reached only over the mesh.

    Runs in c's private space from ``remote-invite-routed``: c hosts it, d
    (dave) is a member c is NOT paired with — every envelope between them
    rides ``SPACE_ROUTED`` through b, which is no member of that space.
    Installs a stand-in two-player app (``demo-duel``, ``players {2, 2}``,
    ``tables: true``) on c and d straight into ``installed_apps``.

    1. c's games list (``PUT /api/spaces/{id}/games/allowed``) allows
       ``demo-duel`` — it reaches d with the space's config.
    2. c opens a table with a marker note (``POST /api/spaces/{id}/games``);
       d's Games list shows the card (``SPACE_APP_TABLE``, routed).
    3. d joins (``SPACE_APP_SESSION`` ``group_join`` → ``group_seat``); a
       two-player table starts at once (D11) — both see ``playing`` at two
       seats, and d holds c's seat key checked against c's identity key.
    4. d sends a group message (``SPACE_APP_SESSION`` kind ``message``);
       c's mailbox gets it.
    5. **Nobody else hears of it**: a and b hold no row of the table and
       the marker is in none of their database files (b relayed only
       ciphertext); ``verify`` re-checks the GFS's database too.
    6. **v_77.** ``OURS`` >= ``MIN_FOR_SPACE_APP_TABLES``; c and d each
       know the other at it (d's mesh claim on c, c's pairing-free claim).
    The receiver rules, followers, forged cards and joins, swapped seat
    keys and the moderator close are
    ``tests/protocol/test_space_app_table_federation.py`` /
    ``tests/services/test_space_app_table_service.py``.
    """
    import sqlite3

    from socialhome.domain.federation_capabilities import (
        OURS as _OURS,
        FederationCapability as _Cap,
    )

    state = _load()
    if not state:
        raise SystemExit("run 'up' first")
    space_id = state.get("remote_invite_routed_space_id")
    if not space_id:
        raise SystemExit("space-table: run 'remote-invite-routed' first")
    inst = state["instances"]
    c, d = inst["c"], inst["d"]
    app_id = "demo-duel"
    manifest = (
        '{"entry": "index.html", "icon": null, "capabilities":'
        ' ["storage", "people", "federation"], "players": {"min": 2, "max": 2},'
        ' "tables": true}'
    )
    for label in ("c", "d"):
        con = sqlite3.connect(str(_instance_dir(label) / "socialhome.db"), timeout=10)
        try:
            con.execute(
                "INSERT OR IGNORE INTO installed_apps(app_id, name, version,"
                " enabled, manifest_json, bundle_path, bundle_sha256,"
                " source_url, installed_at) VALUES(?, 'Demo duel', '1', 1, ?,"
                " 'apps/demo-duel/1', 'demo', 'demo', '2026-10-31')",
                (app_id, manifest),
            )
            con.commit()
        finally:
            con.close()

    def games(node: dict, path: str = "", *, method: str = "GET", body=None):
        return _request(
            f"http://127.0.0.1:{node['port']}/api/spaces/{space_id}/games{path}",
            token=node["token"],
            method=method,
            body=body,
        )

    def wait(what: str, check, timeout: float = 90.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if check():
                return
            time.sleep(1.0)
        raise SystemExit(f"space-table: timed out waiting for {what}")

    # 1. The games list.
    s, body = games(c, "/allowed", method="PUT", body={"allowed_apps": [app_id]})
    _must("games list(c)", s, body)

    def listed(node: dict) -> dict:
        s, body = games(node)
        return body if s == 200 and isinstance(body, dict) else {}

    wait("d to get the games list", lambda: listed(d).get("allowed_apps") == [app_id])
    print("  space-table: the games list reached d ✓")
    # 2. c opens a table; d sees its card.
    marker = f"DEMO-TABLE-{secrets.token_hex(6)}"
    s, opened = games(c, method="POST", body={"app_id": app_id, "note": marker})
    _must("open table(c)", s, opened, ok=(201,))
    sid = opened["table"]["session_id"]

    def card(node: dict) -> dict | None:
        return next(
            (t for t in listed(node).get("tables", []) if t["session_id"] == sid), None
        )

    wait("d to see the card", lambda: (card(d) or {}).get("state") == "open")
    if card(d)["note"] != marker or card(d)["seated"] != 1:
        raise SystemExit(f"space-table: d's card is off: {card(d)!r}")
    print("  space-table: d (mesh only) sees c's open table ✓")
    # 3. d joins; a two-player table starts at once.
    s, joined = games(d, f"/{sid}/join", method="POST")
    _must("join(d)", s, joined)

    def group(node: dict) -> dict:
        return (card(node) or {}).get("group") or {}

    for label, node in (("c", c), ("d", d)):
        wait(
            f"{label} to see the game playing at two seats",
            lambda node=node: group(node).get("state") == "playing"
            and sorted(
                pl["seat"] for pl in group(node).get("players", [])
                if pl["state"] == "seated"
            )
            == [1, 2],
        )
        print(f"  space-table: {label} sees the duel playing ✓")
    rows = _rows(
        "d",
        "SELECT roster_json FROM app_group_sessions WHERE session_id = ?",
        (sid,),
    )
    roster = json.loads(rows[0][0]) if rows else []
    host = next((e for e in roster if e["instance"] == c["instance_id"]), {})
    if not host.get("seat_key") or not host.get("identity_pk"):
        raise SystemExit(f"space-table: d holds no checked seat key of c: {roster!r}")
    print("  space-table: d holds c's seat key with c's identity key ✓")
    # 4. A message over the space.
    s, sent = _request(
        f"http://127.0.0.1:{d['port']}/api/apps/{app_id}/groups/{sid}/messages",
        token=d["token"],
        method="POST",
        body={"data": {"move": marker}},
    )
    _must("message(d)", s, sent)
    got: list[dict] = []

    def held() -> list[dict]:
        s, body = _request(
            f"http://127.0.0.1:{c['port']}/api/apps/{app_id}/held",
            token=c["token"],
            method="POST",
        )
        if s == 200:
            got.extend((body or {}).get("group_messages", []))
        return got

    wait("c to get d's move", lambda: any(m["data"] == {"move": marker} for m in held()))
    print("  space-table: c got d's move over the space ✓")
    # 5. Nobody outside the space heard of it.
    for label in ("a", "b"):
        if _rows(label, "SELECT 1 FROM app_group_sessions WHERE session_id = ?", (sid,)):
            raise SystemExit(f"space-table: {label} (no member) holds the table")
        for path in _instance_dir(label).glob("socialhome.db*"):
            if marker.encode() in path.read_bytes():
                raise SystemExit(
                    f"space-table: the table's note is readable in {label}'s {path.name}"
                )
    print("  space-table: a and b (the relay) hold nothing of the table ✓")
    # 6. v_77 between c and d.
    if _OURS < _Cap.MIN_FOR_SPACE_APP_TABLES:
        raise SystemExit(
            f"space-table: OURS={_OURS} < "
            f"MIN_FOR_SPACE_APP_TABLES={_Cap.MIN_FOR_SPACE_APP_TABLES}"
        )
    state["space_table"] = {"session_id": sid, "marker": marker, "space_id": space_id}
    _save(state)
    print("space-table: ok")


def cmd_bazaar_faq() -> None:
    """A listing's questions and answers (v_75).

    Prereqs: ``bazaar-ask`` (c's lamp listing in Beta's space: owner b,
    members a + c, d a follower household). Runs standalone after it.

    1. c (the seller) adds two questions and answers to the lamp
       (``POST /api/bazaar/{id}/faq``); each reaches a, b and d — every
       member household, the follower's included: it reads the listing
       (``BAZAAR_LISTING_FAQ_UPDATED``, one entry per event).
    2. c rewords one and removes the other; the edit and the removal reach
       every household — nobody keeps the removed words.
    3. **Only the seller.** a (a member, not the seller) is refused (403).
    4. **v_75.** ``OURS`` is at least ``MIN_FOR_BAZAAR_FAQ`` and a, b and c
       see each other at it.
    """
    from socialhome.domain.federation_capabilities import (
        OURS as _OURS,
        FederationCapability as _Cap,
    )

    state = _load()
    if not state:
        raise SystemExit("run 'up' first")
    ask = state.get("bazaar_ask")
    if not ask:
        raise SystemExit("run 'bazaar-ask' first — needs c's listing")
    inst = state["instances"]
    a, b, c = inst["a"], inst["b"], inst["c"]
    post_id = ask["post_id"]
    nonce = secrets.token_hex(3)

    def wait(what: str, check, timeout: float = 45.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if check():
                return
            time.sleep(1.0)
        raise SystemExit(f"bazaar-faq: timed out waiting for {what}")

    def held(label: str) -> list[tuple[str, str]]:
        return [
            (r[0], r[1])
            for r in _rows(
                label,
                "SELECT question, answer FROM bazaar_listing_faq"
                " WHERE listing_post_id = ? AND removed = 0 ORDER BY position",
                (post_id,),
            )
        ]

    def add(question: str, answer: str) -> dict:
        s, entry = _request(
            f"http://127.0.0.1:{c['port']}/api/bazaar/{post_id}/faq",
            token=c["token"],
            method="POST",
            body={"question": question, "answer": answer},
        )
        _must("faq add(c)", s, entry, ok=(201,))
        return entry

    # 1. Two entries by the seller reach every member household.
    keep = add(f"Does the lamp dim? {nonce}", "Yes, three steps.")
    drop = add(f"Is the bulb included? {nonce}", "No.")
    want = [(keep["question"], keep["answer"]), (drop["question"], drop["answer"])]
    for label in ("a", "b", "d"):
        wait(f"{label} to hold both FAQ entries", lambda x=label: held(x) == want)
    print("  c → 2 questions and answers → a, b, d (follower household) ✓")

    # 2. Reworded and removed — everywhere.
    reworded = f"Can the lamp be dimmed? {nonce}"
    s, body = _request(
        f"http://127.0.0.1:{c['port']}/api/bazaar/{post_id}/faq/{keep['entry_id']}",
        token=c["token"],
        method="PATCH",
        body={"question": reworded, "answer": "Yes, three steps."},
    )
    _must("faq edit(c)", s, body)
    s, body = _request(
        f"http://127.0.0.1:{c['port']}/api/bazaar/{post_id}/faq/{drop['entry_id']}",
        token=c["token"],
        method="DELETE",
    )
    _must("faq remove(c)", s, body, ok=(204,))
    for label in ("a", "b", "d"):
        wait(
            f"{label} to take the edit and the removal",
            lambda x=label: held(x) == [(reworded, "Yes, three steps.")],
        )
        gone = _rows(
            label,
            "SELECT 1 FROM bazaar_listing_faq WHERE question = ?",
            (drop["question"],),
        )
        if gone:
            raise SystemExit(f"bazaar-faq: {label} keeps a removed question")
    print("  c rewords one, removes one → a, b, d converge ✓")

    # 3. Only the seller writes.
    s, refused = _request(
        f"http://127.0.0.1:{a['port']}/api/bazaar/{post_id}/faq",
        token=a["token"],
        method="POST",
        body={"question": "Mine?", "answer": "No."},
    )
    if s != 403:
        raise SystemExit(f"bazaar-faq: a (not the seller) could write: {s} {refused!r}")
    print("  a (not the seller) can't write the FAQ ✓")

    # 4. v_75 round-trips between the writer households.
    if _OURS < _Cap.MIN_FOR_BAZAAR_FAQ:
        raise SystemExit(
            f"bazaar-faq: OURS={_OURS} < MIN_FOR_BAZAAR_FAQ={_Cap.MIN_FOR_BAZAAR_FAQ}"
        )
    for label, info in (("a", a), ("b", b), ("c", c)):
        s, conns = _request(
            f"http://127.0.0.1:{info['port']}/api/connections", token=info["token"]
        )
        _must(f"connections({label})", s, conns)
        for row in conns:
            peer = row.get("instance_id")
            if row.get("status") != "confirmed" or peer not in {
                a["instance_id"],
                b["instance_id"],
                c["instance_id"],
            }:
                continue
            pv = int(row.get("proto_version") or 1)
            if pv < _Cap.MIN_FOR_BAZAAR_FAQ:
                raise SystemExit(
                    f"bazaar-faq: {label} sees {peer[:8]} at v{pv} "
                    f"(< {_Cap.MIN_FOR_BAZAAR_FAQ})"
                )
    print(
        f"  a, b, c see each other at >= v{_Cap.MIN_FOR_BAZAAR_FAQ} (OURS={_OURS}) ✓"
    )
    state["bazaar_faq"] = {"entry_id": keep["entry_id"], "question": reworded}
    _save(state)
    print("bazaar-faq: ok")


def cmd_down() -> None:
    state = _load()
    if state.get("gfs_b"):
        cmd_gfs_b_down()
        state = _load()
    gfs = state.get("gfs")
    if gfs:
        try:
            os.killpg(gfs["pid"], signal.SIGTERM)
            print(f"  gfs: SIGTERM pid={gfs['pid']}")
        except ProcessLookupError, PermissionError:
            pass
    for label, info in (state.get("instances") or {}).items():
        try:
            os.killpg(info["pid"], signal.SIGTERM)
            print(f"  {label}: SIGTERM pid={info['pid']}")
        except ProcessLookupError, PermissionError:
            pass
    time.sleep(1)
    if gfs:
        try:
            os.killpg(gfs["pid"], signal.SIGKILL)
        except ProcessLookupError, PermissionError:
            pass
    for label, info in (state.get("instances") or {}).items():
        try:
            os.killpg(info["pid"], signal.SIGKILL)
        except ProcessLookupError, PermissionError:
            pass
    if ROOT.exists():
        shutil.rmtree(ROOT)
    print("down: ok")


# ─── Entry point ───────────────────────────────────────────────────────────


def _accept_remote_invite(state: dict, joiner: str, space_id: str) -> None:
    """Joiner fetches its pending §D1b remote invites and accepts the one
    for ``space_id`` — seats a local space stub + the per-epoch content key."""
    inst = state["instances"][joiner]
    s, invites = _request(
        f"http://127.0.0.1:{inst['port']}/api/remote_invites",
        token=inst["token"],
    )
    _must(f"{joiner}: list remote invites", s, invites)
    rows = invites if isinstance(invites, list) else []
    match = [r for r in rows if r.get("space_id") == space_id]
    if not match:
        raise SystemExit(
            f"owner-offline: {joiner} has no pending invite for {space_id} — "
            f"got {rows!r}",
        )
    token = match[0]["invite_token"]
    s, body = _request(
        f"http://127.0.0.1:{inst['port']}/api/remote_invites/{token}/accept",
        token=inst["token"],
        method="POST",
    )
    _must(f"{joiner}: accept invite", s, body, ok=(204,))


def _verify_routed_moment_reaction(state: dict, moment_id: str) -> list[str]:
    """v_67: d (not paired with c) reacts to c's relayed moment; the
    reaction must reach c two hops back, and its removal too."""
    failures: list[str] = []
    d = state["instances"]["d"]
    d_url = f"http://127.0.0.1:{d['port']}"
    route = _rows("d", "SELECT reaction_route FROM moments WHERE id=?", (moment_id,))
    if not route or not route[0][0]:
        return [
            f"d: no reaction route kept for c's relayed moment {moment_id!r} "
            f"(row={route!r}) — a relay added no return token or c's key-wrap "
            "key did not verify"
        ]
    s, detail = _request(f"{d_url}/api/moments/{moment_id}", token=d["token"])
    reach = ((detail or {}).get("moment") or {}).get("reactions_reach")
    if s != 200 or reach != "author":
        failures.append(
            f"d: c's moment says reactions_reach={reach!r} (HTTP {s}) — "
            "expected 'author' along the route"
        )
    d_user = d.get("user_id")

    def _c_reaction() -> list[tuple]:
        return _rows(
            "c",
            "SELECT emoji FROM moment_reactions WHERE moment_id=? AND reactor_user_id=?",
            (moment_id, d_user),
        )

    s, _ = _request(
        f"{d_url}/api/moments/{moment_id}/reaction",
        token=d["token"],
        method="PUT",
        body={"emoji": "👏"},
    )
    _must("moment react(d)", s, _)
    deadline = time.time() + 30
    while time.time() < deadline and _c_reaction() != [("👏",)]:
        time.sleep(1)
    if _c_reaction() != [("👏",)]:
        _bad = [
            line
            for label in ("a", "b", "c")
            for line in _log_lines_matching(label, "moment_reaction_routed")
            if "refusing" in line
        ]
        failures.append(
            f"c: d's reaction to {moment_id!r} never arrived two hops back "
            f"(row={_c_reaction()!r}"
            + (f", refused: {_bad[-1].strip()[:200]}" if _bad else "")
            + ")"
        )
        return failures
    print("  d's reaction reaches c two hops away (v_67 routed, sealed) ✓")
    s, _ = _request(
        f"{d_url}/api/moments/{moment_id}/reaction",
        token=d["token"],
        method="DELETE",
    )
    _must("moment unreact(d)", s, _)
    deadline = time.time() + 30
    while time.time() < deadline and _c_reaction():
        time.sleep(1)
    if _c_reaction():
        failures.append(f"c: d's removed reaction to {moment_id!r} is still there")
    else:
        print("  d's removal reaches c too ✓")
    return failures


def _backdate_moment(label: str, moment_id: str, *, minutes: int) -> None:
    """Move ``moment_id``'s ``created_at`` on its author household ``label``
    *minutes* into the past, so the v_69 totals checkpoint at +10 min comes
    due at the household's next minute tick instead of in ten minutes. The
    one documented write besides the home-coordinate seed: it changes only
    when the moment counts as made, never what was signed or sent."""
    import sqlite3

    when = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    conn = sqlite3.connect(_instance_dir(label) / "socialhome.db", timeout=10)
    try:
        conn.execute("UPDATE moments SET created_at=? WHERE id=?", (when, moment_id))
        conn.commit()
    finally:
        conn.close()


def _verify_moment_totals(state: dict, moment_id: str) -> list[str]:
    """v_69: d reacts 🔥 to c's moment (it reaches c two hops back, v_67);
    c's +10 min checkpoint is brought forward and its signed totals must
    reach b (paired with c) and d (two hops away, relayed by a v_69 relay),
    and d's API must show them."""
    failures: list[str] = []
    d = state["instances"]["d"]
    d_url = f"http://127.0.0.1:{d['port']}"
    s, _ = _request(
        f"{d_url}/api/moments/{moment_id}/reaction",
        token=d["token"],
        method="PUT",
        body={"emoji": "🔥"},
    )
    _must("moment react 🔥(d)", s, _)
    deadline = time.time() + 30
    held: list[tuple] = []
    while time.time() < deadline:
        held = _rows(
            "c",
            "SELECT emoji FROM moment_reactions WHERE moment_id=? AND reactor_user_id=?",
            (moment_id, d.get("user_id")),
        )
        if held == [("🔥",)]:
            break
        time.sleep(1)
    else:
        return [f"c: d's 🔥 on {moment_id!r} never arrived (row={held!r})"]
    want = _rows(
        "c", "SELECT COUNT(*) FROM moment_reactions WHERE moment_id=?", (moment_id,)
    )[0][0]
    _backdate_moment("c", moment_id, minutes=11)

    def _totals(label: str) -> dict | None:
        rows = _rows(
            label, "SELECT reaction_summary FROM moments WHERE id=?", (moment_id,)
        )
        return json.loads(rows[0][0]) if rows and rows[0][0] else None

    deadline = time.time() + 120
    got: dict[str, dict | None] = {}
    while time.time() < deadline:
        got = {label: _totals(label) for label in ("b", "d")}
        if all(t and t.get("total") == want for t in got.values()):
            break
        time.sleep(2)
    else:
        refused = [
            line.strip()[:200]
            for label in ("b", "d")
            for line in _log_lines_matching(label, "moment_reactions_counted")
            if "refusing" in line
        ]
        return [
            f"c's totals for {moment_id!r} (total {want}) did not reach b and d: "
            f"{got!r}" + (f"; refused: {refused[-1]}" if refused else "")
        ]
    s, detail = _request(f"{d_url}/api/moments/{moment_id}", token=d["token"])
    m = (detail or {}).get("moment") or {}
    # Exactly c's totals: d's own reaction is marked (my_reaction), never
    # added on top — so a re-run of verify (d's 🔥 taken back and made again
    # after c's checkpoint) shows the same count (momentum.md, "API").
    if (
        s != 200
        or m.get("reaction_count") != want
        or m.get("reaction_totals_at") is None
    ):
        failures.append(
            f"d: c's moment shows reaction_count={m.get('reaction_count')!r} "
            f"totals_at={m.get('reaction_totals_at')!r}, expected {want} from c"
        )
    else:
        print(
            f"  v_69 c's signed reaction totals ({want}) reach b and d two hops "
            "away; d's API shows them ✓"
        )
    return failures


def _rows(label: str, sql: str, params: tuple = ()) -> list[tuple]:
    """Run a read-only query against ``label``'s SQLite DB.

    Several steps assert on rows the REST surface doesn't expose (or
    exposes only for local users), so they read the DB directly. Kept
    read-only on purpose — the harness never writes app state behind the
    backend's back except for the documented home-coordinate seed.
    """
    import sqlite3

    conn = sqlite3.connect(_instance_dir(label) / "socialhome.db")
    try:
        return list(conn.execute(sql, params))
    finally:
        conn.close()


def _space_col(label: str, space_id: str, col: str):
    """Read a single column off the local ``spaces`` row for ``space_id``."""
    import sqlite3

    conn = sqlite3.connect(_instance_dir(label) / "socialhome.db")
    try:
        rows = list(conn.execute(f"SELECT {col} FROM spaces WHERE id=?", (space_id,)))
    finally:
        conn.close()
    return rows[0][0] if rows else None


def _remote_member_row(label: str, space_id: str, user_id: str) -> dict | None:
    """Return the ``space_remote_members`` row for ``user_id`` (INCLUDING
    tombstones) on ``label``'s DB, or ``None`` if absent.

    Reads the row by ``(space_id, user_id)`` only — the gossip / convergence
    path keys a removal on the member identity, and the demo asserts on the
    ``tombstoned`` flag a delegated admin's offline ban must flip on the owner.
    """
    import sqlite3

    conn = sqlite3.connect(_instance_dir(label) / "socialhome.db")
    try:
        conn.row_factory = sqlite3.Row
        rows = list(
            conn.execute(
                "SELECT * FROM space_remote_members WHERE space_id=? AND user_id=?",
                (space_id, user_id),
            )
        )
    finally:
        conn.close()
    return dict(rows[0]) if rows else None


@_offline_step
def cmd_owner_offline() -> None:
    """Delegated-admin moderation with the OWNING household offline.

    Proves the keystone of the owner-offline-spaces epic end-to-end across
    live nodes: with the owner process STOPPED, a delegated admin that holds
    the space signing seed performs an authoritative config change, another
    member household converges, and the owner reconciles on restart.

    Topology (all paired by ``cmd_pair`` — needs a↔b and a↔c):
    a = owner, b = delegated admin, c = plain member.

    Sequence:
    1. a creates a private space + enables ``delegated_admin_authority``
       (owner-only flag).
    2. a §D1b-invites b and c; both accept (each seats a local stub + the
       content key).
    3. a promotes b to admin → the host ships ``SPACE_ADMIN_KEY_SHARE`` so b
       receives the space SIGNING SEED.
    4. Assert b now holds the seed (``spaces.identity_private_key`` non-NULL
       on b) — "admin authority = holding the private key" (§4.2.3).
    5. STOP a (SIGTERM the process group) — the owning household is offline.
    6. b renames the space via ``PATCH /api/spaces/{id}`` — b executes
       LOCALLY and AUTHORITY-SIGNS (``_executes_locally_as_delegated_admin``),
       broadcasting ``SPACE_CONFIG_CHANGED`` to every member.
    7. Assert c's local ``spaces.name`` reflects the rename WHILE a is offline
       (c accepts it by verifying the space-authority signature, not
       ``from_instance``).
    8. Restart a; after the outbox/config settle, assert a's ``spaces.name``
       also converges (LWW reconcile on reconnect).

    Run after ``up`` + ``pair``. Re-runnable (each run mints a fresh space +
    a ``time.time_ns()`` rename marker).
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' + 'pair' first")
    a = state["instances"]["a"]
    b = state["instances"]["b"]
    c = state["instances"]["c"]
    marker = f"offline-{time.time_ns()}"

    # 1. a creates a private space + flips on delegated_admin_authority.
    s, space = _request(
        f"http://127.0.0.1:{a['port']}/api/spaces",
        token=a["token"],
        method="POST",
        body={
            "name": "Owner-offline lab",
            "space_type": "private",
            "join_mode": "invite_only",
            "emoji": "🛰️",
        },
    )
    space = _must("a create space", s, space, ok=(201,))
    space_id = space["id"]
    print(f"  a created space {space_id}")

    s, cur = _request(
        f"http://127.0.0.1:{a['port']}/api/spaces/{space_id}", token=a["token"]
    )
    _must("a get space", s, cur)
    feats = dict(cur.get("features") or {})
    feats["delegated_admin_authority"] = True
    s, upd = _request(
        f"http://127.0.0.1:{a['port']}/api/spaces/{space_id}",
        token=a["token"],
        method="PATCH",
        body={"features": feats},
    )
    _must("a enable delegation", s, upd, ok=(200,))
    print("  a enabled delegated_admin_authority ✓")

    # 2. a invites b + c; both accept (seats stub + content key).
    for label, inst in (("b", b), ("c", c)):
        s, inv = _request(
            f"http://127.0.0.1:{a['port']}/api/spaces/{space_id}/remote-invites",
            token=a["token"],
            method="POST",
            body={
                "invitee_instance_id": inst["instance_id"],
                "invitee_user_id": inst["user_id"],
            },
        )
        _must(f"a invite {label}", s, inv, ok=(201,))
    time.sleep(4)  # SPACE_PRIVATE_INVITE federates
    _accept_remote_invite(state, "b", space_id)
    _accept_remote_invite(state, "c", space_id)
    print("  b + c accepted invites (seated + content key) ✓")
    # Let the a↔b capability exchange settle: the seed share is gated on
    # ``peer_supports(b, MIN_FOR_SPACE_ADMIN_KEY_SHARE)`` and is NOT retried if
    # a doesn't yet know b's proto_version when we promote. On a cold boot the
    # INSTANCE_CAPABILITIES_UPDATED handshake can lag, so give it room.
    time.sleep(10)

    # 3. a promotes b to admin → SPACE_ADMIN_KEY_SHARE delivers the seed.
    s, role = _request(
        f"http://127.0.0.1:{a['port']}/api/spaces/{space_id}"
        f"/remote-members/{b['instance_id']}/{b['user_id']}",
        token=a["token"],
        method="PATCH",
        body={"role": "admin"},
    )
    _must("a promote b to admin", s, role, ok=(200,))
    print("  a promoted b to admin (seed share dispatched)")

    # 4. b must now hold the space signing seed (poll — the share federates
    #    asynchronously over the b-pair channel / HTTPS inbox).
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if _space_col("b", space_id, "identity_private_key"):
            break
        time.sleep(1.0)
    if not _space_col("b", space_id, "identity_private_key"):
        raise SystemExit(
            "owner-offline: b did not receive the space signing seed "
            "(spaces.identity_private_key NULL on b after 30s) — "
            "SPACE_ADMIN_KEY_SHARE didn't land",
        )
    print("  b holds the space signing seed ✓ (admin authority = private key)")

    # 4b. Let b's stub catch up to a's config_sequence before b edits. b's
    #     authoritative rename increments from b's local sequence; if that's
    #     stale (behind a's), b's edit lands at a COLLIDING sequence with a's
    #     last edit and loses the ``(config_sequence, author)`` LWW tie-break,
    #     so the owner reverts it on reconnect. A real delegated admin likewise
    #     acts on a synced view. Poll both DBs until b ≥ a (or timeout).
    deadline = time.monotonic() + 25.0
    a_seq = b_seq = None
    while time.monotonic() < deadline:
        a_seq = _space_col("a", space_id, "config_sequence")
        b_seq = _space_col("b", space_id, "config_sequence")
        if a_seq is not None and b_seq is not None and b_seq >= a_seq:
            break
        time.sleep(1.0)
    print(f"  b config_sequence synced to a (a={a_seq}, b={b_seq})")

    # 5. STOP a — the owning household goes offline.
    print(f"  stopping a (pid={a['pid']}) — owner offline")
    try:
        os.killpg(a["pid"], signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and _alive(a["pid"]):
        time.sleep(0.2)
    if _alive(a["pid"]):
        try:
            os.killpg(a["pid"], signal.SIGKILL)
        except ProcessLookupError:
            pass
        time.sleep(0.5)

    # 6. b (delegated admin, holds seed) renames the space offline-of-owner.
    new_name = f"Renamed by delegated admin {marker}"
    s, upd = _request(
        f"http://127.0.0.1:{b['port']}/api/spaces/{space_id}",
        token=b["token"],
        method="PATCH",
        body={"name": new_name},
    )
    _must("b rename space offline-of-owner", s, upd, ok=(200,))
    print(f"  b renamed the space while a offline ({marker}) ✓")

    # 7. c must see the rename despite the owner being offline (poll the
    #    SPACE_CONFIG_CHANGED broadcast b fanned out to every member household).
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if _space_col("c", space_id, "name") == new_name:
            break
        time.sleep(1.0)
    c_name = _space_col("c", space_id, "name")
    if c_name != new_name:
        raise SystemExit(
            f"owner-offline: c's space name is {c_name!r}, expected "
            f"{new_name!r} — delegated-admin config did NOT converge "
            f"offline-of-owner",
        )
    print("  c converged on the delegated-admin rename (owner offline) ✓")

    # 8. Restart a; it must reconcile to the delegated-admin's change (b's
    #    outbox redelivers the authority-signed SPACE_CONFIG_CHANGED once a's
    #    inbox is reachable; a applies it by verifying the signature, LWW).
    new_pid = _spawn("a", a["port"])
    state["instances"]["a"]["pid"] = new_pid
    _wait_ready(a["port"])
    print(f"  a respawned: pid={new_pid}; polling for reconcile…")
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        if _space_col("a", space_id, "name") == new_name:
            break
        time.sleep(2.0)
    a_name = _space_col("a", space_id, "name")
    if a_name != new_name:
        raise SystemExit(
            f"owner-offline: after restart a's space name is {a_name!r}, "
            f"expected {new_name!r} — owner did NOT reconcile the offline "
            f"delegated-admin change",
        )
    print("  a reconciled to the delegated-admin rename on restart ✓")

    state["owner_offline_ran"] = True
    state["owner_offline_space_id"] = space_id
    _save(state)
    print("owner-offline: ok (delegated admin moderates with the owner offline)")


def _poll(what: str, check, *, timeout: float = 45.0, interval: float = 1.0):
    """Poll ``check()`` until it returns a truthy value; return it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = check()
        if got:
            return got
        time.sleep(interval)
    raise SystemExit(f"timed out waiting for {what}")


def cmd_admin_revoke_rotation() -> None:
    """Demoting a delegated admin rotates the space authority key (v_44).

    a = owner, b = delegated admin, c = plain member (all paired by ``pair``).

    1. a creates a private space, turns ``delegated_admin_authority`` on,
       invites b + c, promotes b to admin, and waits until b holds the seed.
    2. a demotes b to member. Assert, on the live wire:
       * a's ``identity_public_key`` moved off K1 and ``authority_key_epoch``
         is a wall-clock-seconds value;
       * c pins a's new key at that epoch and recorded it as its baseline (the
         ``SPACE_AUTHORITY_ROTATED`` bundle was verified and applied once);
       * b pins the new key too and its ``identity_private_key`` is NULL;
       * a rename attempted on b is not executed there any more (b no longer
         signs for the space): neither a nor c changes its name.
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' + 'pair' first")
    a = state["instances"]["a"]
    b = state["instances"]["b"]
    c = state["instances"]["c"]
    a_base = f"http://127.0.0.1:{a['port']}"

    s, space = _request(
        f"{a_base}/api/spaces",
        token=a["token"],
        method="POST",
        body={
            "name": "Revoke lab",
            "space_type": "private",
            "join_mode": "invite_only",
            "emoji": "🔑",
        },
    )
    space_id = _must("a create space", s, space, ok=(201,))["id"]
    s, cur = _request(f"{a_base}/api/spaces/{space_id}", token=a["token"])
    feats = dict(_must("a get space", s, cur).get("features") or {})
    feats["delegated_admin_authority"] = True
    s, upd = _request(
        f"{a_base}/api/spaces/{space_id}",
        token=a["token"],
        method="PATCH",
        body={"features": feats},
    )
    _must("a enable delegation", s, upd, ok=(200,))
    for label, inst in (("b", b), ("c", c)):
        s, inv = _request(
            f"{a_base}/api/spaces/{space_id}/remote-invites",
            token=a["token"],
            method="POST",
            body={
                "invitee_instance_id": inst["instance_id"],
                "invitee_user_id": inst["user_id"],
            },
        )
        _must(f"a invite {label}", s, inv, ok=(201,))
    time.sleep(4)
    _accept_remote_invite(state, "b", space_id)
    _accept_remote_invite(state, "c", space_id)
    time.sleep(10)  # let the capability exchange settle (seed share is gated)
    role_url = (
        f"{a_base}/api/spaces/{space_id}/remote-members/"
        f"{b['instance_id']}/{b['user_id']}"
    )
    s, role = _request(
        role_url, token=a["token"], method="PATCH", body={"role": "admin"}
    )
    _must("a promote b to admin", s, role, ok=(200,))
    _poll(
        "b to hold the space signing seed",
        lambda: _space_col("b", space_id, "identity_private_key"),
        timeout=30.0,
    )
    k1 = _space_col("a", space_id, "identity_public_key")
    print(f"  b holds the seed for K1={k1[:12]}… ✓")

    s, role = _request(
        role_url, token=a["token"], method="PATCH", body={"role": "member"}
    )
    _must("a demote b to member", s, role, ok=(200,))
    k2 = _poll(
        "a to rotate the authority key",
        lambda: (
            _space_col("a", space_id, "identity_public_key")
            if (_space_col("a", space_id, "authority_key_epoch") or 0) > 0
            else None
        ),
        timeout=20.0,
    )
    if k2 == k1:
        raise SystemExit("admin-revoke-rotation: a's pin did not move off K1")
    epoch = _space_col("a", space_id, "authority_key_epoch")
    # The epoch is at least wall-clock seconds (it must outrank an epoch the
    # members hold even after the owner is restored from an old backup).
    if epoch < int(time.time()) - 600:
        raise SystemExit(f"admin-revoke-rotation: epoch {epoch} is not time-based")
    print(f"  a rotated to K2={k2[:12]}… at epoch {epoch} ✓")
    for label in ("c", "b"):
        _poll(
            f"{label} to pin K2 at epoch {epoch}",
            lambda label=label: (
                _space_col(label, space_id, "identity_public_key") == k2
                and _space_col(label, space_id, "authority_key_epoch") == epoch
                and _space_col(label, space_id, "authority_baseline_epoch") == epoch
            ),
        )
        print(f"  {label} applied the owner's cert + baseline (K2, epoch {epoch}) ✓")
    _poll(
        "b's old seed to be cleared",
        lambda: _space_col("b", space_id, "identity_private_key") is None,
        timeout=20.0,
    )
    print("  b's K1 seed is gone ✓")

    a_name = _space_col("a", space_id, "name")
    _request(
        f"http://127.0.0.1:{b['port']}/api/spaces/{space_id}",
        token=b["token"],
        method="PATCH",
        body={"name": f"Hijack {time.time_ns()}"},
    )
    time.sleep(6)
    for label in ("a", "c"):
        if _space_col(label, space_id, "name") != a_name:
            raise SystemExit(
                f"admin-revoke-rotation: b's post-demotion rename reached {label}"
            )
    print("  a rename on b is no longer executed or accepted anywhere ✓")

    state["admin_revoke_rotation_space_id"] = space_id
    _save(state)
    print("admin-revoke-rotation: ok (revoking an admin retires its signing key)")


def _outbox_events(label: str, peer_iid: str) -> list[str]:
    """Pending ``federation_outbox`` event types ``label`` holds for a peer."""
    return [
        r[0]
        for r in _rows(
            label,
            "SELECT event_type FROM federation_outbox"
            " WHERE instance_id=? AND status='pending' ORDER BY created_at",
            (peer_iid,),
        )
    ]


def _content_epoch(label: str, space_id: str) -> int | None:
    rows = _rows(
        label, "SELECT MAX(epoch) FROM space_keys WHERE space_id=?", (space_id,)
    )
    return rows[0][0] if rows else None


@_offline_step
def cmd_rotation_offline_catchup() -> None:
    """A member that was OFFLINE through a rotation catches up from the outbox (v_44).

    a = owner, b = delegated admin, c = plain member. Sibling of
    ``admin-revoke-rotation`` with c down for the whole revocation, modelled
    on ``replay`` / ``owner-offline``:

    1. a creates a private space with ``delegated_admin_authority`` on,
       seats b + c, promotes b to admin and waits until b holds the K1 seed.
    2. Stop c. a demotes b (→ rotation to K2 + a content-key rekey), posts
       once and renames the space. a's outbox must hold the
       ``SPACE_AUTHORITY_ROTATED`` bundle for c — c was down, so the outbox
       is the only way it can ever arrive.
    3. Restart c. Assert c pins K2 at a's epoch with the owner's baseline,
       a's bundle left the outbox (delivered), c holds the post and the
       rename, and c's newest content key is a's post-rotation epoch.
    4. b plays a hostile ex-admin that kept its K1 seed: the harness writes
       the K1 seed (and the admin seat) back into b's DB — the one place the
       harness writes app state, because a revoked household that refuses to
       forget is exactly the threat the rotation answers. b's rename is then
       executed and K1-signed on b, and c must refuse it.
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' + 'pair' first")
    a = state["instances"]["a"]
    b = state["instances"]["b"]
    c = state["instances"]["c"]
    a_base = f"http://127.0.0.1:{a['port']}"
    marker = time.time_ns()

    s, space = _request(
        f"{a_base}/api/spaces",
        token=a["token"],
        method="POST",
        body={
            "name": f"Offline rotation lab {marker}",
            "space_type": "private",
            "join_mode": "invite_only",
            "emoji": "🛰",
        },
    )
    space_id = _must("a create space", s, space, ok=(201,))["id"]
    s, cur = _request(f"{a_base}/api/spaces/{space_id}", token=a["token"])
    feats = dict(_must("a get space", s, cur).get("features") or {})
    feats["delegated_admin_authority"] = True
    s, upd = _request(
        f"{a_base}/api/spaces/{space_id}",
        token=a["token"],
        method="PATCH",
        body={"features": feats},
    )
    _must("a enable delegation", s, upd, ok=(200,))
    for label, inst in (("b", b), ("c", c)):
        s, inv = _request(
            f"{a_base}/api/spaces/{space_id}/remote-invites",
            token=a["token"],
            method="POST",
            body={
                "invitee_instance_id": inst["instance_id"],
                "invitee_user_id": inst["user_id"],
            },
        )
        _must(f"a invite {label}", s, inv, ok=(201,))
    time.sleep(4)
    _accept_remote_invite(state, "b", space_id)
    _accept_remote_invite(state, "c", space_id)
    time.sleep(10)  # let the capability exchange settle (seed share is gated)
    role_url = (
        f"{a_base}/api/spaces/{space_id}/remote-members/"
        f"{b['instance_id']}/{b['user_id']}"
    )
    s, role = _request(
        role_url, token=a["token"], method="PATCH", body={"role": "admin"}
    )
    _must("a promote b to admin", s, role, ok=(200,))
    k1_seed = _poll(
        "b to hold the space signing seed",
        lambda: _space_col("b", space_id, "identity_private_key"),
        timeout=30.0,
    )
    k1 = _space_col("a", space_id, "identity_public_key")
    _poll(
        "c to hold the space content key",
        lambda: _content_epoch("c", space_id) is not None,
        timeout=30.0,
    )
    epoch_before = _content_epoch("c", space_id)
    print(f"  b holds the K1={k1[:12]}… seed; c on content epoch {epoch_before} ✓")

    # 2. c goes offline for the whole revocation.
    _kill_household("c", c)
    s, role = _request(
        role_url, token=a["token"], method="PATCH", body={"role": "member"}
    )
    _must("a demote b to member", s, role, ok=(200,))
    k2 = _poll(
        "a to rotate the authority key",
        lambda: (
            _space_col("a", space_id, "identity_public_key")
            if (_space_col("a", space_id, "authority_key_epoch") or 0) > 0
            else None
        ),
        timeout=20.0,
    )
    if k2 == k1:
        raise SystemExit("rotation-offline-catchup: a's pin did not move off K1")
    epoch = _space_col("a", space_id, "authority_key_epoch")
    content_epoch = _poll(
        "a to rekey the content",
        lambda: (
            _content_epoch("a", space_id)
            if (_content_epoch("a", space_id) or 0) > (epoch_before or 0)
            else None
        ),
        timeout=20.0,
    )
    print(
        f"  c offline; a rotated to K2={k2[:12]}… (epoch {epoch}, content {content_epoch}) ✓"
    )
    content = f"Post while c was offline — {marker}"
    s, post = _request(
        f"{a_base}/api/spaces/{space_id}/posts",
        token=a["token"],
        method="POST",
        body={"type": "text", "content": content},
    )
    post_id = _must("a posts while c offline", s, post, ok=(201,))["id"]
    new_name = f"Renamed offline {marker}"
    s, upd = _request(
        f"{a_base}/api/spaces/{space_id}",
        token=a["token"],
        method="PATCH",
        body={"name": new_name},
    )
    _must("a renames while c offline", s, upd, ok=(200,))
    queued = _poll(
        "a to outbox the rotation bundle for c",
        lambda: (
            q
            if "space_authority_rotated" in (q := _outbox_events("a", c["instance_id"]))
            else None
        ),
        timeout=30.0,
    )
    print(f"  a's outbox holds for c: {', '.join(queued)} ✓")

    # 3. c comes back and catches up from a's outbox.
    time.sleep(4)
    new_pid = _spawn("c", c["port"])
    state["instances"]["c"]["pid"] = new_pid
    _save(state)
    _wait_ready(c["port"])
    print(f"  c respawned: pid={new_pid}")
    _poll(
        f"c to pin K2 at epoch {epoch}",
        lambda: (
            _space_col("c", space_id, "identity_public_key") == k2
            and _space_col("c", space_id, "authority_key_epoch") == epoch
            and _space_col("c", space_id, "authority_baseline_epoch") == epoch
        ),
        timeout=120.0,
        interval=2.0,
    )
    _poll(
        "a's rotation bundle for c to leave the outbox",
        lambda: "space_authority_rotated" not in _outbox_events("a", c["instance_id"]),
        timeout=30.0,
    )
    print(f"  c applied the outboxed bundle: K2 at epoch {epoch}, baseline {epoch} ✓")
    _poll(
        "c to hold a's offline-time rename",
        lambda: _space_col("c", space_id, "name") == new_name,
        timeout=60.0,
        interval=2.0,
    )
    print(f"  c holds the rename ({new_name!r}) ✓")
    _poll(
        "c to hold a's offline-time post",
        lambda: _rows(
            "c",
            "SELECT 1 FROM space_posts WHERE id=? AND space_id=?",
            (post_id, space_id),
        ),
        timeout=60.0,
        interval=2.0,
    )
    s, feed = _request(
        f"http://127.0.0.1:{c['port']}/api/spaces/{space_id}/feed", token=c["token"]
    )
    feed = feed if isinstance(feed, list) else (feed or {}).get("posts") or []
    seen = next((p for p in feed if p.get("id") == post_id), None)
    if seen is None or seen.get("content") != content:
        raise SystemExit(f"rotation-offline-catchup: c cannot read the post ({seen!r})")
    print("  c holds and reads the post ✓")
    if _content_epoch("c", space_id) != content_epoch:
        raise SystemExit(
            f"rotation-offline-catchup: c's content key is epoch "
            f"{_content_epoch('c', space_id)}, a's is {content_epoch}"
        )
    print(f"  c's content key is the post-rotation epoch {content_epoch} ✓")

    # 4. b as a hostile ex-admin that kept K1: put the seed + seat back.
    if _space_col("b", space_id, "identity_private_key") is not None:
        raise SystemExit(
            "rotation-offline-catchup: b still held a seed after the rotation"
        )
    conn = sqlite3.connect(_instance_dir("b") / "socialhome.db", timeout=10)
    try:
        conn.execute(
            "UPDATE spaces SET identity_private_key=? WHERE id=?", (k1_seed, space_id)
        )
        conn.execute(
            "UPDATE space_members SET role='admin' WHERE space_id=? AND user_id=?",
            (space_id, b["user_id"]),
        )
        conn.commit()
    finally:
        conn.close()
    hijack = f"Hijack {marker}"
    # The negative control: a and c must refuse b's K1-signed rename, and
    # each says so at WARNING — expected, for exactly this rename.
    forged = _open_noise_window(
        about="b",
        reason="rotation-offline-catchup: b's K1-signed rename is refused",
        witnesses=("a", "c"),
        named=("authority signature did not verify against the space key",),
        names=[f"SPACE_CONFIG_CHANGED for {space_id} from {b['instance_id']}"],
    )
    s, resp = _request(
        f"http://127.0.0.1:{b['port']}/api/spaces/{space_id}",
        token=b["token"],
        method="PATCH",
        body={"name": hijack},
    )
    if s != 200 or _space_col("b", space_id, "name") != hijack:
        raise SystemExit(
            f"rotation-offline-catchup: b did not execute its K1 rename "
            f"(status={s}, body={resp!r}) — the refusal below would prove nothing"
        )
    print("  b (K1 seed restored) executed + K1-signed a rename locally")
    time.sleep(8)
    for label in ("c", "a"):
        got = _space_col(label, space_id, "name")
        if got != new_name:
            raise SystemExit(
                f"rotation-offline-catchup: {label} accepted b's K1 rename ({got!r})"
            )
    print("  c (and a) refused b's old-key rename ✓")
    _close_noise_windows(forged)

    state["rotation_offline_catchup_space_id"] = space_id
    _save(state)
    print("rotation-offline-catchup: ok (an offline member catches up on the rotation)")


def cmd_gfs_authority_rotate() -> None:
    """The GFS and a subscriber follow an owner-certified rotation (v_44).

    Prereqs: ``gfs-space-post`` (d is a GFS subscriber of a's global space).

    1. a turns ``delegated_admin_authority`` on, then off — the off-flip
       rotates the space authority key unconditionally.
    2. Assert the GFS re-pinned: ``global_spaces.identity_public_key`` is a's
       new key and ``authority_cert`` is stored.
    3. a posts; d (still pinned to the old key) must heal its pin from the
       GFS listing's cert and read the post.
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' first")
    space_id = state.get("gfs_space_id")
    if not space_id or not state.get("gfs_space_post_id"):
        raise SystemExit("run 'gfs-space-post' first")
    a = state["instances"]["a"]
    a_base = f"http://127.0.0.1:{a['port']}"
    before = _space_col("a", space_id, "authority_key_epoch") or 0
    s, cur = _request(f"{a_base}/api/spaces/{space_id}", token=a["token"])
    feats = dict(_must("a get gfs space", s, cur).get("features") or {})
    for flag in (True, False):
        feats["delegated_admin_authority"] = flag
        s, upd = _request(
            f"{a_base}/api/spaces/{space_id}",
            token=a["token"],
            method="PATCH",
            body={"features": feats},
        )
        _must(f"a delegation={flag}", s, upd, ok=(200,))
    k2 = _poll(
        "a to rotate the gfs space's key",
        lambda: (
            _space_col("a", space_id, "identity_public_key")
            if (_space_col("a", space_id, "authority_key_epoch") or 0) > before
            else None
        ),
        timeout=20.0,
    )
    _poll(
        "the GFS to re-pin from the owner's cert",
        lambda: _gfs_rows(
            "SELECT 1 FROM global_spaces WHERE space_id=? AND identity_public_key=?"
            " AND authority_cert IS NOT NULL",
            (space_id, k2),
        ),
        timeout=30.0,
    )
    print(f"  GFS re-pinned {space_id} to K2={k2[:12]}… from the owner cert ✓")
    time.sleep(8)  # the K2-signed subscriber key re-seal reaches d
    content = f"Post after the authority rotation — {time.time_ns()}"
    s, post = _request(
        f"{a_base}/api/spaces/{space_id}/posts",
        token=a["token"],
        method="POST",
        body={"type": "text", "content": content},
    )
    post_id = _must("a posts after rotation", s, post, ok=(201,))["id"]
    seen = _await_space_post(state, "d", space_id, post_id)
    if seen.get("content") != content:
        raise SystemExit("gfs-authority-rotate: d did not read the post-rotation post")
    if _space_col("d", space_id, "identity_public_key") != k2:
        raise SystemExit("gfs-authority-rotate: d did not heal its pin to K2")
    print("  d healed its pin from the GFS listing and reads the K2-relayed post ✓")
    print("gfs-authority-rotate: ok (the GFS and a subscriber follow the rotation)")


@_offline_step
def cmd_owner_offline_ban() -> None:
    """Delegated-admin offline-of-owner BAN converges (covers the #618 path).

    Sibling of :func:`cmd_owner_offline`, but exercises a ROSTER mutation
    (a removal/ban) rather than a config edit. This is the path the #618 bug
    lived on: a delegated admin's offline ban gossips a ``SPACE_MEMBER_LEFT``
    tombstone whose ``member_version`` is sourced from the space's dedicated
    ``roster_sequence``. Pre-#618 that sequence wasn't anchored above the
    member's last-seen version, so other households (which held the victim at
    a HIGH ``member_version`` from a real seat) would DROP the tombstone as
    stale via the version-guarded CRDT merge — the banned member stayed live.

    Topology (all paired by ``cmd_pair`` — needs a↔b and a↔c):
    a = owner / host, b = delegated admin + seed-holder, c = the member banned.

    Sequence:
    1. a creates a private space + enables ``delegated_admin_authority``.
    2. a §D1b-invites b AND c; both accept (seats a local stub + content key).
    3. a promotes b to admin → ``SPACE_ADMIN_KEY_SHARE`` ships the signing seed;
       poll until b holds it (``spaces.identity_private_key`` non-NULL on b).
    4. Settle until a, b AND c all agree c is a LIVE member at a real
       ``member_version`` in their ``space_remote_members`` roster. This is the
       #618 pre-condition: other households hold c at a high version that an
       un-anchored ban gossip would fail to beat. Print the converged state.
    5. STOP a (SIGTERM the process group) — the owning household is offline.
    6. b bans c offline-of-owner via
       ``DELETE /api/spaces/{id}/remote-members/{c_inst}/{c_user}`` —
       :meth:`SpaceService.remove_remote_member`. b holds the seed, so the
       ``SPACE_MEMBER_LEFT`` roster gossip it fans out to every member
       household (incl. the offline host's ``space_instances`` row) is
       space-authority-signed; the per-member ``member_version`` is anchored on
       ``roster_sequence`` (the #618 fix).
    7. Restart a; after the outbox/redelivery window, assert a's
       ``space_remote_members`` row for c is ``tombstoned=1`` — a applied b's
       offline ban gossip on reconnect. Pre-#618, a would have dropped it as
       stale and c would still be a live member.
       Secondary: c's own household sees itself removed from the space
       (``SPACE_REMOTE_MEMBER_REMOVED`` cascaded the local stub away).

    Run after ``up`` + ``pair``. Re-runnable (each run mints a fresh space +
    a ``time.time_ns()`` marker so a re-run never collides with a prior space).
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' + 'pair' first")
    a = state["instances"]["a"]
    b = state["instances"]["b"]
    c = state["instances"]["c"]
    marker = f"offline-ban-{time.time_ns()}"

    # 1. a creates a private space + flips on delegated_admin_authority.
    s, space = _request(
        f"http://127.0.0.1:{a['port']}/api/spaces",
        token=a["token"],
        method="POST",
        body={
            "name": f"Owner-offline ban lab {marker}",
            "space_type": "private",
            "join_mode": "invite_only",
            "emoji": "🚫",
        },
    )
    space = _must("a create space", s, space, ok=(201,))
    space_id = space["id"]
    print(f"  a created space {space_id}")

    s, cur = _request(
        f"http://127.0.0.1:{a['port']}/api/spaces/{space_id}", token=a["token"]
    )
    _must("a get space", s, cur)
    feats = dict(cur.get("features") or {})
    feats["delegated_admin_authority"] = True
    s, upd = _request(
        f"http://127.0.0.1:{a['port']}/api/spaces/{space_id}",
        token=a["token"],
        method="PATCH",
        body={"features": feats},
    )
    _must("a enable delegation", s, upd, ok=(200,))
    print("  a enabled delegated_admin_authority ✓")

    # 2. a invites b + c; both accept (seats stub + content key).
    for label, inst in (("b", b), ("c", c)):
        s, inv = _request(
            f"http://127.0.0.1:{a['port']}/api/spaces/{space_id}/remote-invites",
            token=a["token"],
            method="POST",
            body={
                "invitee_instance_id": inst["instance_id"],
                "invitee_user_id": inst["user_id"],
            },
        )
        _must(f"a invite {label}", s, inv, ok=(201,))
    time.sleep(4)  # SPACE_PRIVATE_INVITE federates
    _accept_remote_invite(state, "b", space_id)
    _accept_remote_invite(state, "c", space_id)
    print("  b + c accepted invites (seated + content key) ✓")
    # Let the a↔b capability exchange settle: the seed share is gated on
    # ``peer_supports(b, MIN_FOR_SPACE_ADMIN_KEY_SHARE)`` and is NOT retried if
    # a doesn't yet know b's proto_version when we promote. On a cold boot the
    # INSTANCE_CAPABILITIES_UPDATED handshake can lag, so give it room.
    time.sleep(10)

    # 3. a promotes b to admin → SPACE_ADMIN_KEY_SHARE delivers the seed.
    s, role = _request(
        f"http://127.0.0.1:{a['port']}/api/spaces/{space_id}"
        f"/remote-members/{b['instance_id']}/{b['user_id']}",
        token=a["token"],
        method="PATCH",
        body={"role": "admin"},
    )
    _must("a promote b to admin", s, role, ok=(200,))
    print("  a promoted b to admin (seed share dispatched)")

    # 3b. b must now hold the space signing seed (poll — the share federates
    #     asynchronously). Without the seed b can't sign the offline ban gossip
    #     and the roster never converges.
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if _space_col("b", space_id, "identity_private_key"):
            break
        time.sleep(1.0)
    if not _space_col("b", space_id, "identity_private_key"):
        raise SystemExit(
            "owner-offline-ban: b did not receive the space signing seed "
            "(spaces.identity_private_key NULL on b after 30s) — "
            "SPACE_ADMIN_KEY_SHARE didn't land",
        )
    print("  b holds the space signing seed ✓ (admin authority = private key)")

    # 4. Settle until a, b AND c all agree c is a LIVE member of the space.
    #    This is the #618 pre-condition: every household must hold c at a real
    #    (non-zero, non-tombstoned) ``member_version`` in space_remote_members,
    #    so that an un-anchored ban gossip would have been DROPPED as stale.
    #    a learns c via the invite-accept seat; b + c learn c via the
    #    authority-signed SPACE_MEMBER_JOINED roster gossip a fanned out.
    deadline = time.monotonic() + 40.0
    rows: dict[str, dict | None] = {}
    while time.monotonic() < deadline:
        rows = {
            who: _remote_member_row(who, space_id, c["user_id"])
            for who in ("a", "b", "c")
        }
        live = {
            who: (
                r is not None
                and int(r.get("tombstoned") or 0) == 0
                and int(r.get("member_version") or 0) >= 0
            )
            for who, r in rows.items()
        }
        if all(live.values()):
            break
        time.sleep(2.0)
    missing = [who for who, r in rows.items() if r is None]
    tombstoned_pre = [
        who
        for who, r in rows.items()
        if r is not None and int(r.get("tombstoned") or 0)
    ]
    if missing or tombstoned_pre:
        raise SystemExit(
            "owner-offline-ban: roster did not converge on c as a live member "
            f"before the ban — missing={missing!r} tombstoned={tombstoned_pre!r} "
            f"rows={rows!r}",
        )
    versions = {who: int(r["member_version"]) for who, r in rows.items()}  # type: ignore[index]
    print(
        f"  a, b, c all agree c is a live member "
        f"(member_version a={versions['a']}, b={versions['b']}, c={versions['c']}) ✓"
    )

    # 5. STOP a — the owning household goes offline.
    print(f"  stopping a (pid={a['pid']}) — owner offline")
    try:
        os.killpg(a["pid"], signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and _alive(a["pid"]):
        time.sleep(0.2)
    if _alive(a["pid"]):
        try:
            os.killpg(a["pid"], signal.SIGKILL)
        except ProcessLookupError:
            pass
        time.sleep(0.5)

    # 6. b (delegated admin, holds seed) BANS c offline-of-owner. c is a remote
    #    member on b's stub, so the kick verb is the remote-member DELETE — it
    #    tombstones c locally + fans an authority-signed SPACE_MEMBER_LEFT
    #    roster gossip out to every member household (incl. a's offline inbox,
    #    where the outbox redelivers it on reconnect).
    s, resp = _request(
        f"http://127.0.0.1:{b['port']}/api/spaces/{space_id}"
        f"/remote-members/{c['instance_id']}/{c['user_id']}",
        token=b["token"],
        method="DELETE",
    )
    _must("b bans c offline-of-owner", s, resp, ok=(200,))
    print(f"  b banned c while a offline ({marker}) ✓")

    # 6b. b's own roster must reflect the tombstone immediately (local write).
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        r = _remote_member_row("b", space_id, c["user_id"])
        if r is not None and int(r.get("tombstoned") or 0) == 1:
            break
        time.sleep(1.0)
    rb = _remote_member_row("b", space_id, c["user_id"])
    if rb is None or int(rb.get("tombstoned") or 0) != 1:
        raise SystemExit(
            f"owner-offline-ban: b's local roster did not tombstone c — row={rb!r}",
        )
    print("  b's local roster shows c tombstoned ✓")

    # 7. Restart a; after the outbox redelivery window a must apply b's
    #    authority-signed SPACE_MEMBER_LEFT and tombstone c. Pre-#618 a held c
    #    at a HIGH member_version and would DROP the un-anchored ban as stale —
    #    c would stay a live member. Post-#618 the ban's roster_sequence is
    #    anchored above c's last-seen version, so the merge applies it.
    new_pid = _spawn("a", a["port"])
    state["instances"]["a"]["pid"] = new_pid
    _wait_ready(a["port"])
    print(f"  a respawned: pid={new_pid}; polling for ban convergence…")
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        r = _remote_member_row("a", space_id, c["user_id"])
        if r is not None and int(r.get("tombstoned") or 0) == 1:
            break
        # A hard-DELETE of the row would also satisfy "no longer a live member",
        # but the convergence primitive RETAINS the row tombstoned (so a
        # replayed JOIN can't resurrect c) — so an absent row is unexpected.
        time.sleep(2.0)
    ra = _remote_member_row("a", space_id, c["user_id"])
    a_live = ra is not None and int(ra.get("tombstoned") or 0) == 0
    if ra is None or a_live:
        raise SystemExit(
            f"owner-offline-ban: after restart a still has c as a LIVE member "
            f"(row={ra!r}) — the owner DROPPED the delegated-admin offline ban "
            f"as stale (the #618 regression: un-anchored roster_sequence)",
        )
    print(
        f"  a applied the offline ban on reconnect — c tombstoned "
        f"(member_version={ra.get('member_version')}) ✓ [#618 proof]"
    )

    # 7b. Secondary: c's own household should see itself removed from the space
    #     (SPACE_REMOTE_MEMBER_REMOVED cascades the local stub away). This is a
    #     best-effort assertion — the direct-delivery envelope to c only lands
    #     if a relayed it on reconnect, so a miss is reported, not fatal.
    c_space_name = _space_col("c", space_id, "name")
    if c_space_name is None:
        print("  c's local space stub removed (saw itself banned) ✓ [secondary]")
    else:
        print(
            "  note: c still holds a local stub for the space "
            f"(name={c_space_name!r}) — direct removal envelope not yet "
            "delivered (secondary check, non-fatal)"
        )

    state["owner_offline_ban_ran"] = True
    state["owner_offline_ban_space_id"] = space_id
    _save(state)
    print("owner-offline-ban: ok")


def _pairing_call(label: str, url: str, **kw) -> tuple[int, Any]:
    """``_request`` that waits out the per-user ``/api/pairing`` bucket.

    Every ``/api/pairing/*`` write shares one 5 / 60 s bucket per user, so a
    step that unpairs and re-pairs on the heels of an earlier pairing step
    can hit 429. Wait one window and retry once, rather than failing on a
    rate limit the step isn't testing.
    """
    s, body = _request(url, **kw)
    if s == 429:
        print(f"  {label}: /api/pairing rate-limited — waiting out the window")
        time.sleep(61)
        s, body = _request(url, **kw)
    return s, body


def _peer_ids(info: dict, *, confirmed_only: bool = False) -> set[str]:
    s, conns = _request(
        f"http://127.0.0.1:{info['port']}/api/connections",
        token=info["token"],
    )
    _must("connections", s, conns)
    return {
        c["instance_id"]
        for c in conns
        if not confirmed_only or c["status"] == "confirmed"
    }


def _repair_a_c(a: dict, c: dict) -> None:
    """Re-pair a↔c (QR flow) so the topology later steps assume is back."""
    time.sleep(2)
    s, qr = _pairing_call(
        "a",
        f"http://127.0.0.1:{a['port']}/api/pairing/initiate",
        token=a["token"],
        method="POST",
    )
    _must("re-pair initiate(a)", s, qr, ok=(201,))
    s, ack = _pairing_call(
        "c",
        f"http://127.0.0.1:{c['port']}/api/pairing/accept",
        token=c["token"],
        method="POST",
        body=qr,
    )
    _must("re-pair accept(c)", s, ack)
    s, conf = _pairing_call(
        "a",
        f"http://127.0.0.1:{a['port']}/api/pairing/confirm",
        token=a["token"],
        method="POST",
        body={"token": ack["token"], "verification_code": ack["verification_code"]},
    )
    _must("re-pair confirm(a)", s, conf)
    deadline = time.monotonic() + 20.0

    def _repaired() -> bool:
        return c["instance_id"] in _peer_ids(a, confirmed_only=True) and (
            a["instance_id"] in _peer_ids(c, confirmed_only=True)
        )

    while time.monotonic() < deadline and not _repaired():
        time.sleep(1.0)
    if not _repaired():
        raise SystemExit("unpair: re-pairing a↔c did not land on both sides")
    print("  a↔c re-paired ✓")


def _kill_household(label: str, info: dict) -> None:
    """SIGTERM (then SIGKILL) one household's process group."""
    _open_offline_window(label)
    print(f"  killing {label} (pid={info['pid']}) to simulate an offline peer")
    try:
        os.killpg(info["pid"], signal.SIGTERM)
    except ProcessLookupError:
        print(f"  {label} was already gone")
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and _alive(info["pid"]):
        time.sleep(0.2)
    if _alive(info["pid"]):
        try:
            os.killpg(info["pid"], signal.SIGKILL)
        except ProcessLookupError:
            pass
        time.sleep(0.5)


@_offline_step
def cmd_unpair_offline() -> None:
    """Unpair c while it is OFFLINE; c still drops a once it is back.

    Pre-fix a sent one UNPAIR (5 s budget), then deleted c's row — so a c
    that was down at that moment never learned and kept a forever. Now a
    keeps c as an ``unpairing`` tombstone (hidden from its connections,
    refusing everything c sends) with one UNPAIR queued in the outbox:

    1. stop c; a unpairs c → ``peer_notified: false``, c gone from a's
       list at once, a holds a tombstone + exactly one pending UNPAIR;
    2. restart c (it still thinks it is paired) → c's first envelope is
       refused and pulls a's UNPAIR forward; c drops a;
    3. a purges the tombstone and queues nothing for c;
    4. re-pair a↔c (a tombstone never blocks a fresh pairing).
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' + 'pair' first")
    a, c = state["instances"]["a"], state["instances"]["c"]
    if c["instance_id"] not in _peer_ids(a) or a["instance_id"] not in _peer_ids(c):
        raise SystemExit("unpair-offline: a↔c must be paired first (run 'pair')")

    _kill_household("c", c)
    started = time.monotonic()
    s, resp = _pairing_call(
        "a",
        f"http://127.0.0.1:{a['port']}/api/pairing/connections/{c['instance_id']}",
        token=a["token"],
        method="DELETE",
    )
    _must("a unpairs offline c", s, resp, ok=(200,))
    if resp.get("peer_notified") is not False:
        raise SystemExit(f"unpair-offline: c was down yet notified? {resp!r}")
    print(f"  a unpaired offline c in {time.monotonic() - started:.1f}s ✓")
    if c["instance_id"] in _peer_ids(a):
        raise SystemExit("unpair-offline: a still lists c after unpairing it")
    status = _rows(
        "a", "SELECT status FROM remote_instances WHERE id=?", (c["instance_id"],)
    )
    if [r[0] for r in status] != ["unpairing"]:
        raise SystemExit(f"unpair-offline: expected a tombstone on a, got {status!r}")
    queued = _rows(
        "a",
        "SELECT event_type FROM federation_outbox"
        " WHERE instance_id=? AND status='pending'",
        (c["instance_id"],),
    )
    if [r[0] for r in queued] != ["unpair"]:
        raise SystemExit(f"unpair-offline: a should queue one UNPAIR, has {queued!r}")
    print("  a holds an 'unpairing' tombstone + one queued UNPAIR ✓")

    # Let a's outbox fail an attempt or two against the dead c.
    time.sleep(8)
    new_pid = _spawn("c", c["port"])
    state["instances"]["c"]["pid"] = new_pid
    _save(state)
    _wait_ready(c["port"])
    print(f"  c respawned: pid={new_pid}")

    deadline = time.monotonic() + 180.0
    while time.monotonic() < deadline and a["instance_id"] in _peer_ids(c):
        time.sleep(2.0)
    if a["instance_id"] in _peer_ids(c):
        raise SystemExit("unpair-offline: c still lists a 180s after coming back")
    print(f"  c dropped a after reconnect ({time.monotonic() - started:.0f}s total) ✓")

    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline and _rows(
        "a", "SELECT 1 FROM remote_instances WHERE id=?", (c["instance_id"],)
    ):
        time.sleep(1.0)
    if _rows("a", "SELECT 1 FROM remote_instances WHERE id=?", (c["instance_id"],)):
        raise SystemExit("unpair-offline: a kept its tombstone after delivery")
    for label, peer_iid in (("a", c["instance_id"]), ("c", a["instance_id"])):
        n = _rows(
            label,
            "SELECT COUNT(*) FROM federation_outbox"
            " WHERE instance_id=? AND status='pending'",
            (peer_iid,),
        )[0][0]
        if n:
            raise SystemExit(f"unpair-offline: {label} still queues {n} envelope(s)")
    print("  a purged its tombstone; nothing queued either way ✓")

    _repair_a_c(a, c)
    print("unpair-offline: ok")


def cmd_unpair() -> None:
    """Unpair a↔c from a's side; BOTH households drop the pairing.

    a's ``DELETE /api/pairing/connections/{c}`` must send c a signed
    ``UNPAIR`` before a forgets c's keys — so c's connections list loses a
    too (pre-fix c kept a's row forever and kept retrying deliveries).
    Shared space membership is NOT part of the pairing: every
    ``space_instances`` row either side held before the unpair is still
    there after it. Finishes by re-pairing a↔c so the topology the rest
    of the chain assumes is restored.
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' + 'pair' first")
    a, c = state["instances"]["a"], state["instances"]["c"]
    if c["instance_id"] not in _peer_ids(a) or a["instance_id"] not in _peer_ids(c):
        raise SystemExit("unpair: a↔c must be paired first (run 'pair')")

    def _space_rows(label: str, peer_iid: str) -> set[str]:
        return {
            r[0]
            for r in _rows(
                label,
                "SELECT space_id FROM space_instances WHERE instance_id=?",
                (peer_iid,),
            )
        }

    spaces_on_a = _space_rows("a", c["instance_id"])
    spaces_on_c = _space_rows("c", a["instance_id"])

    started = time.monotonic()
    a_log_at, c_log_at = _log_size("a"), _log_size("c")
    s, resp = _pairing_call(
        "a",
        f"http://127.0.0.1:{a['port']}/api/pairing/connections/{c['instance_id']}",
        token=a["token"],
        method="DELETE",
    )
    _must("a unpairs c", s, resp, ok=(200,))
    if resp.get("peer_notified") is not True:
        # a's own account of why the first notify did not land (timeout,
        # HTTP status, RTC fallback) plus c's inbox view of the same window.
        evidence = [
            f"    a: {line}"
            for line in _log_lines_matching("a", c["instance_id"], offset=a_log_at)
        ] + [
            f"    c: {line}"
            for line in _log_lines_matching("c", "federation", offset=c_log_at)
        ]
        raise SystemExit(
            f"unpair: a could not tell c after "
            f"{time.monotonic() - started:.1f}s (response={resp!r})\n"
            + "\n".join(evidence[-40:])
        )
    print(f"  a unpaired c in {time.monotonic() - started:.1f}s (peer_notified) ✓")

    if c["instance_id"] in _peer_ids(a):
        raise SystemExit("unpair: a still lists c after its own unpair")
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline and a["instance_id"] in _peer_ids(c):
        time.sleep(1.0)
    if a["instance_id"] in _peer_ids(c):
        raise SystemExit(
            "unpair: c still lists a 20s after a unpaired — the UNPAIR never "
            "reached c (or c ignored it)"
        )
    print("  c dropped a on the inbound UNPAIR ✓")

    for label, peer_iid in (("a", c["instance_id"]), ("c", a["instance_id"])):
        queued = _rows(
            label,
            "SELECT COUNT(*) FROM federation_outbox WHERE instance_id=?",
            (peer_iid,),
        )[0][0]
        if queued:
            raise SystemExit(f"unpair: {label} still queues {queued} envelope(s)")
    print("  neither side queues envelopes for the other ✓")

    if _space_rows("a", c["instance_id"]) != spaces_on_a or (
        _space_rows("c", a["instance_id"]) != spaces_on_c
    ):
        raise SystemExit(
            "unpair: shared space membership changed — the pairing and the "
            "space are separate relationships"
        )
    print(f"  shared spaces kept (a: {len(spaces_on_a)}, c: {len(spaces_on_c)}) ✓")

    _repair_a_c(a, c)
    print("unpair: ok")


# ─── Step: group-dm (v_37 cross-household group conversations) ───────────


def _conv_ids(label: str, info: dict) -> dict[str, dict]:
    s, rows = _request(
        f"http://127.0.0.1:{info['port']}/api/conversations", token=info["token"]
    )
    _must(f"conversations({label})", s, rows)
    return {r["id"]: r for r in rows}


def _message_texts(label: str, conv_id: str) -> set[str]:
    return {
        r[0]
        for r in _rows(
            label,
            "SELECT content FROM conversation_messages WHERE conversation_id=?",
            (conv_id,),
        )
    }


def _wait_for(what: str, check, *, timeout: float = 45.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if check():
            return
        time.sleep(1.0)
    raise SystemExit(f"group-dm: timed out waiting for {what}")


def _send_group(label: str, info: dict, conv_id: str, text: str) -> None:
    s, body = _request(
        f"http://127.0.0.1:{info['port']}/api/conversations/{conv_id}/messages",
        token=info["token"],
        method="POST",
        body={"content": text},
    )
    _must(f"send({label})", s, body, ok=(200, 201))


def cmd_group_dm() -> None:
    """A group conversation spanning households a, b and c (v_37).

    1. a (the authority) creates a group with b's and c's admins; b and c
       see it (``managed_here`` false) and every household's message
       reaches the other two.
    2. d — paired with a and b, never a member — holds no row and no text.
    3. a adds d's admin (when a↔d is paired): d catches up on the history.
    4. a removes c: c drops the group, and a message sent after that never
       reaches c. b leaves: a's roster no longer lists b.
    5. A second group on b with c and d — who are NOT paired with each
       other — proves the mesh leg: c's message reaches d E2E-sealed, and
       a (a possible relay, never a member) stores no text of it.
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' + 'pair' first")
    inst = state["instances"]
    a, b, c, d = inst["a"], inst["b"], inst["c"], inst["d"]
    tag = secrets.token_hex(3)

    s, conv = _request(
        f"http://127.0.0.1:{a['port']}/api/conversations/group",
        token=a["token"],
        method="POST",
        body={
            "members": [],
            "member_user_ids": [b["user_id"], c["user_id"]],
            "name": f"Demo crew {tag}",
        },
    )
    conv = _must("create group (a)", s, conv, ok=(201,))
    gid = conv["id"]
    print(f"  a created group {gid[:12]}… with b + c")
    for label in ("b", "c"):
        _wait_for(
            f"{label} to be seated in the group",
            lambda label=label: gid in _conv_ids(label, inst[label]),
        )
        row = _conv_ids(label, inst[label])[gid]
        if row["type"] != "group_dm" or row["managed_here"] is not False:
            raise SystemExit(f"group-dm: {label} sees {row} — expected a remote group")
    print("  b and c are seated (authority = a) ✓")

    texts = {label: f"[group {tag}] hello from {label}" for label in ("a", "b", "c")}
    for label, text in texts.items():
        _send_group(label, inst[label], gid, text)
    for label in ("a", "b", "c"):
        _wait_for(
            f"{label} to hold every member's message",
            lambda label=label: set(texts.values()) <= _message_texts(label, gid),
        )
    print("  every member household's message reached the other two ✓")

    if _rows("d", "SELECT 1 FROM conversations WHERE id=?", (gid,)):
        raise SystemExit("group-dm: non-member d holds the group row")
    leaked = _rows(
        "d",
        "SELECT content FROM conversation_messages WHERE content LIKE ?",
        (f"%[group {tag}]%",),
    )
    if leaked:
        raise SystemExit(f"group-dm: non-member d holds group content {leaked}")
    print("  non-member d holds neither the group nor any of its text ✓")

    a_peers = _rows(
        "a",
        "SELECT 1 FROM remote_instances WHERE id=? AND status='confirmed'",
        (d["instance_id"],),
    )
    if a_peers:
        s, body = _request(
            f"http://127.0.0.1:{a['port']}/api/conversations/{gid}/members",
            token=a["token"],
            method="POST",
            body={"user_ids": [d["user_id"]]},
        )
        _must("add d (a)", s, body, ok=(201,))
        _wait_for("d to be seated", lambda: gid in _conv_ids("d", d))
        _wait_for(
            "d to catch up on the history from a",
            lambda: set(texts.values()) <= _message_texts("d", gid),
            timeout=90.0,
        )
        print("  a added d's admin; d caught up on the whole history ✓")
    else:
        print("  a↔d not paired (run relay-pair first) — skipping the add")

    s, body = _request(
        f"http://127.0.0.1:{a['port']}/api/conversations/{gid}/members/{c['user_id']}",
        token=a["token"],
        method="DELETE",
    )
    _must("remove c (a)", s, body)
    _wait_for("c to drop the group", lambda: gid not in _conv_ids("c", c))
    after = f"[group {tag}] after carol left"
    _send_group("a", a, gid, after)
    _wait_for(
        "b to get the post-removal message", lambda: after in _message_texts("b", gid)
    )
    time.sleep(3)
    if after in _message_texts("c", gid):
        raise SystemExit("group-dm: removed c still received a group message")
    print("  a removed c: c dropped the group and gets nothing new ✓")

    s, body = _request(
        f"http://127.0.0.1:{b['port']}/api/conversations/{gid}/leave",
        token=b["token"],
        method="POST",
    )
    _must("leave (b)", s, body)

    def _b_gone() -> bool:
        s, rows = _request(
            f"http://127.0.0.1:{a['port']}/api/conversations/{gid}/members",
            token=a["token"],
        )
        return s == 200 and b["user_id"] not in {r["user_id"] for r in rows}

    _wait_for("a's roster to drop b after DM_GROUP_LEAVE", _b_gone)
    print("  b left: its DM_GROUP_LEAVE reached the authority, a's roster updated ✓")

    # ── Mesh leg: b's group with c and d (c and d never paired). ──
    c_d_paired = _rows(
        "c",
        "SELECT 1 FROM remote_instances WHERE id=? AND status='confirmed'",
        (d["instance_id"],),
    )
    if c_d_paired:
        print("  c↔d are paired here — skipping the mesh-leg check")
    else:
        s, conv2 = _request(
            f"http://127.0.0.1:{b['port']}/api/conversations/group",
            token=b["token"],
            method="POST",
            body={"member_user_ids": [c["user_id"], d["user_id"]], "name": "Mesh"},
        )
        gid2 = _must("create mesh group (b)", s, conv2, ok=(201,))["id"]
        for label in ("c", "d"):
            _wait_for(
                f"{label} to be seated in b's group",
                lambda label=label: gid2 in _conv_ids(label, inst[label]),
            )
        sealed = f"[group {tag}] c to d across the mesh"
        _send_group("c", c, gid2, sealed)
        _wait_for(
            "d to receive c's message over the mesh",
            lambda: sealed in _message_texts("d", gid2),
            timeout=90.0,
        )
        relay_copy = _rows(
            "a",
            "SELECT 1 FROM conversation_messages WHERE content=?",
            (sealed,),
        )
        if relay_copy:
            raise SystemExit("group-dm: non-member a stored the mesh-routed text")
        print(
            "  c → d (never paired) arrived over the E2E-sealed mesh; "
            "non-member a stored nothing ✓"
        )
    print("group-dm: ok")


def cmd_space_report() -> None:
    """Space-scoped reports (v_45): a space's moderators triage its reports.

    Beta (b) hosts a fresh space ("Report club") with Alice (a), Carol (c)
    and Dave (d) as remote members and promotes Alice to ``moderator``.
    Dave (d, a plain member) posts in it.

    1. **Carol reports the post on her own household (c)**: ``POST
       /api/reports`` answers ``space_id`` set — the space's moderators,
       not c's household admin, triage it — and c sends ``SPACE_REPORT``
       to the host b and the moderator household a only.
    2. **a's and b's space report queues** (``GET
       /api/spaces/{id}/reports``) list it; no household admin queue
       (``/api/admin/reports``) does.
    3. **d — a plain member household — never received it**: no
       ``content_reports`` row for the space in d's DB, and the report's
       target never appears in d's log after the post landed.
    4. **Alice resolves it on a**: ``SPACE_REPORT_DECIDED`` reaches b and
       b's copy reads ``resolved`` (b's queue is empty).
    5. **Carol hears back (v_71)**: the host b — not a — sends
       ``SPACE_REPORT_ANSWERED`` to c only; c's copy reads ``resolved`` and
       Carol has exactly one ``report_answer`` row ("Your report in Report
       club was reviewed") naming nobody; d's log never names it.

    Beta then demotes Alice; the space stays (its own, so no earlier
    step's assertions change).
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' + 'pair' + 'relay-pair' first")
    inst = state["instances"]
    a, b, c = inst["a"], inst["b"], inst["c"]
    b_base = f"http://127.0.0.1:{b['port']}"
    s, space = _request(
        f"{b_base}/api/spaces",
        token=b["token"],
        method="POST",
        body={"name": "Report club", "join_mode": "invite_only"},
    )
    space_id = _must("space create(b)", s, space, ok=(201,))["id"]
    print(f"  b: space {space_id[:8]} created")
    for label in ("a", "c", "d"):
        guest = inst[label]
        s, inv = _request(
            f"{b_base}/api/spaces/{space_id}/remote-invites",
            token=b["token"],
            method="POST",
            body={
                "invitee_instance_id": guest["instance_id"],
                "invitee_user_id": guest["user_id"],
            },
        )
        _must(f"remote-invite({label})", s, inv, ok=(201,))

    def _invite_token(label: str) -> str | None:
        guest = inst[label]
        s, invites = _request(
            f"http://127.0.0.1:{guest['port']}/api/remote_invites",
            token=guest["token"],
        )
        items = (
            invites if isinstance(invites, list) else (invites or {}).get("items") or []
        )
        hit = next((i for i in items if i.get("space_id") == space_id), None)
        return hit["invite_token"] if s == 200 and hit else None

    for label in ("a", "c", "d"):
        _wait_for(
            f"{label} to receive b's invite",
            lambda label=label: _invite_token(label) is not None,
            timeout=60.0,
        )
        token = _invite_token(label)
        guest = inst[label]
        s, r = _request(
            f"http://127.0.0.1:{guest['port']}/api/remote_invites/{token}/accept",
            token=guest["token"],
            method="POST",
        )
        _must(f"accept-invite({label})", s, r, ok=(204,))

    def _seated(label: str) -> bool:
        return bool(
            _rows(
                "b",
                "SELECT 1 FROM space_remote_members WHERE space_id=? AND user_id=?"
                " AND tombstoned=0",
                (space_id, inst[label]["user_id"]),
            )
        )

    for label in ("a", "c", "d"):
        _wait_for(
            f"b to seat {label}", lambda label=label: _seated(label), timeout=60.0
        )
    print("  a, c and d seated in the Report club")

    s, body = _request(
        f"{b_base}/api/spaces/{space_id}/remote-members/{a['instance_id']}/{a['user_id']}",
        token=b["token"],
        method="PATCH",
        body={"role": "moderator"},
    )
    _must("promote alice to moderator", s, body)

    def _local_role(label: str) -> str | None:
        rows = _rows(
            label,
            "SELECT role FROM space_members WHERE space_id=? AND user_id=?",
            (space_id, inst[label]["user_id"]),
        )
        return rows[0][0] if rows else None

    _wait_for(
        "a to hold alice's moderator seat", lambda: _local_role("a") == "moderator"
    )
    # c must also know a reviews the space (the roster mirror carries it).
    _wait_for(
        "c to mirror alice as moderator",
        lambda: bool(
            _rows(
                "c",
                "SELECT 1 FROM space_remote_members WHERE space_id=? AND user_id=?"
                " AND role='moderator' AND tombstoned=0",
                (space_id, a["user_id"]),
            )
        ),
        timeout=60.0,
    )

    # Dave (d, a plain member) writes the reported post. Not b's owner: the
    # author is the report's SUBJECT and never sees it in their own queue
    # (and a's moderator is other authority, so no anonymous fallback) —
    # b's queue as Bob would rightly stay empty.
    d = inst["d"]
    s, post = _request(
        f"http://127.0.0.1:{d['port']}/api/spaces/{space_id}/posts",
        token=d["token"],
        method="POST",
        body={"type": "text", "content": f"Report me {int(time.time())}"},
    )
    post_id = _must("d posts", s, post, ok=(201,))["id"]

    def _holds_post(label: str) -> bool:
        return bool(_rows(label, "SELECT 1 FROM space_posts WHERE id=?", (post_id,)))

    for label in ("a", "b", "c"):
        _wait_for(
            f"{label} to hold d's post",
            lambda label=label: _holds_post(label),
            timeout=60.0,
        )
    d_log_before = _log_size("d")

    s, filed = _request(
        f"http://127.0.0.1:{c['port']}/api/reports",
        token=c["token"],
        method="POST",
        body={"target_type": "post", "target_id": post_id, "category": "spam"},
    )
    filed = _must("c: carol reports dave's post", s, filed, ok=(201,))
    if filed.get("space_id") != space_id:
        raise SystemExit(
            f"space-report: c's report -> {filed!r} (expected space_id {space_id} "
            "— the space's moderators triage it)"
        )
    print("  c: Carol reported the post — space-scoped ✓")

    def _queue(label: str) -> list:
        v = inst[label]
        s, rows = _request(
            f"http://127.0.0.1:{v['port']}/api/spaces/{space_id}/reports",
            token=v["token"],
        )
        return [r for r in rows if r.get("target_id") == post_id] if s == 200 else []

    _wait_for(
        "a's space report queue to list it", lambda: bool(_queue("a")), timeout=60.0
    )
    _wait_for(
        "b's space report queue to list it", lambda: bool(_queue("b")), timeout=60.0
    )
    print("  a (moderator household) and b (host) list the report ✓")
    for label in ("a", "b", "c"):
        v = inst[label]
        s, rows = _request(
            f"http://127.0.0.1:{v['port']}/api/admin/reports", token=v["token"]
        )
        if s == 200 and any(r.get("target_id") == post_id for r in rows):
            raise SystemExit(f"space-report: {label}'s household admin queue lists it")
    print("  no household admin queue lists it ✓")

    if _rows("d", "SELECT 1 FROM content_reports WHERE target_id=?", (post_id,)):
        raise SystemExit("space-report: plain member household d stores the report")
    if _log_contains("d", "space_report", offset=d_log_before):
        raise SystemExit("space-report: d's log names a SPACE_REPORT")
    print("  d (plain member) never received the report ✓")

    rid = _queue("a")[0]["id"]
    s, body = _request(
        f"http://127.0.0.1:{a['port']}/api/spaces/{space_id}/reports/{rid}/resolve",
        token=a["token"],
        method="POST",
        body={},
    )
    _must("a: alice resolves the report", s, body)

    def _b_status() -> str | None:
        rows = _rows(
            "b",
            "SELECT status FROM content_reports WHERE space_id=? AND target_id=?",
            (space_id, post_id),
        )
        return rows[0][0] if rows else None

    _wait_for(
        "b's copy to read resolved", lambda: _b_status() == "resolved", timeout=60.0
    )
    if _queue("b"):
        raise SystemExit("space-report: b still lists the resolved report")
    print("  b: resolved by Alice's decision on a (SPACE_REPORT_DECIDED) ✓")

    # v_71: the host b — never a — tells Carol's household c it was
    # reviewed (SPACE_REPORT_ANSWERED). c closes its copy and Carol gets one
    # "was reviewed" row naming nobody; d (plain member) is never sent it.
    def _c_status() -> str | None:
        rows = _rows(
            "c",
            "SELECT status FROM content_reports WHERE space_id=? AND target_id=?",
            (space_id, post_id),
        )
        return rows[0][0] if rows else None

    def _c_answers() -> list:
        return _rows(
            "c",
            "SELECT title, params FROM notifications WHERE user_id=?"
            " AND type='report_answer' AND space_id=?",
            (c["user_id"], space_id),
        )

    _wait_for(
        "c's copy to be answered (SPACE_REPORT_ANSWERED from b)",
        lambda: _c_status() == "resolved" and bool(_c_answers()),
        timeout=60.0,
    )
    answers = _c_answers()
    if len(answers) != 1 or answers[0][0] != "Your report in Report club was reviewed":
        raise SystemExit(f"space-report: Carol's report_answer rows -> {answers!r}")
    params = answers[0][1] or ""
    for who in (a["user_id"], b["user_id"], d["user_id"]):
        if who in params:
            raise SystemExit(
                f"space-report: Carol's answer names {who} (moderator / host / "
                "subject must stay out of it)"
            )
    if _log_contains("d", "space_report_answered", offset=d_log_before):
        raise SystemExit("space-report: d's log names a SPACE_REPORT_ANSWERED")
    print(
        "  c: Carol hears her report was reviewed (SPACE_REPORT_ANSWERED from "
        "the host b, naming nobody); d never sent it ✓"
    )

    _request(
        f"{b_base}/api/spaces/{space_id}/remote-members/{a['instance_id']}/{a['user_id']}",
        token=b["token"],
        method="PATCH",
        body={"role": "member"},
    )
    state["space_report_space_id"] = space_id
    _save(state)
    print("space-report: ok")


def cmd_federated_moderation() -> None:
    """Federated moderation (v_43): "Reviewed" across households.

    Beta (b) hosts a fresh space ("Reviewed club") with Alice (a), Carol
    (c) and Dave (d) as remote members. Beta promotes Carol to
    ``moderator`` and sets ``tasks_access`` to ``moderated``.

    1. **Alice submits** a task on her own household: a answers 202
       queued and sends ``SPACE_MODERATION_SUBMITTED`` to the host b and
       the moderator household c only.
    2. **Carol approves on her own household** (c's Moderation tab — no
       ``NOT_HOST`` any more): c answers ``publishing`` and sends
       ``SPACE_MODERATION_DECIDED{approved}`` to the host b alone; b applies
       the task from its own copy and federates ``SPACE_TASK_CREATED`` as
       Alice's with the approval block, then announces the decision to a
       and c.
    3. a, b, c **and** d all hold the task, created by Alice; a's own
       queue row reads ``approved``.
    4. **d — a plain member household — never received the pending
       item**: no ``space_moderation_queue`` row for it in d's DB, and the
       item id never appears in d's log.

    Beta then resets the level, demotes Carol and the space stays (its
    own, so no earlier step's assertions change).
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' + 'pair' + 'relay-pair' first")
    inst = state["instances"]
    a, b, c = inst["a"], inst["b"], inst["c"]
    b_base = f"http://127.0.0.1:{b['port']}"
    s, space = _request(
        f"{b_base}/api/spaces",
        token=b["token"],
        method="POST",
        body={"name": "Reviewed club", "join_mode": "invite_only"},
    )
    space_id = _must("space create(b)", s, space, ok=(201,))["id"]
    print(f"  b: space {space_id[:8]} created")
    d_log_before = _log_size("d")
    for label in ("a", "c", "d"):
        guest = inst[label]
        s, inv = _request(
            f"{b_base}/api/spaces/{space_id}/remote-invites",
            token=b["token"],
            method="POST",
            body={
                "invitee_instance_id": guest["instance_id"],
                "invitee_user_id": guest["user_id"],
            },
        )
        _must(f"remote-invite({label})", s, inv, ok=(201,))

    def _invite_token(label: str) -> str | None:
        guest = inst[label]
        s, invites = _request(
            f"http://127.0.0.1:{guest['port']}/api/remote_invites",
            token=guest["token"],
        )
        items = (
            invites if isinstance(invites, list) else (invites or {}).get("items") or []
        )
        hit = next((i for i in items if i.get("space_id") == space_id), None)
        return hit["invite_token"] if s == 200 and hit else None

    for label in ("a", "c", "d"):
        _wait_for(
            f"{label} to receive b's invite",
            lambda label=label: _invite_token(label) is not None,
            timeout=60.0,
        )
        token = _invite_token(label)
        guest = inst[label]
        s, r = _request(
            f"http://127.0.0.1:{guest['port']}/api/remote_invites/{token}/accept",
            token=guest["token"],
            method="POST",
        )
        _must(f"accept-invite({label})", s, r, ok=(204,))

    def _seated(label: str) -> bool:
        rows = _rows(
            "b",
            "SELECT 1 FROM space_remote_members WHERE space_id=? AND user_id=?"
            " AND tombstoned=0",
            (space_id, inst[label]["user_id"]),
        )
        return bool(rows)

    for label in ("a", "c", "d"):
        _wait_for(
            f"b to seat {label}", lambda label=label: _seated(label), timeout=60.0
        )
    print("  a, c and d seated in the Reviewed club")

    s, body = _request(
        f"{b_base}/api/spaces/{space_id}/remote-members/{c['instance_id']}/{c['user_id']}",
        token=b["token"],
        method="PATCH",
        body={"role": "moderator"},
    )
    _must("promote carol to moderator", s, body)

    def _local_role(label: str) -> str | None:
        rows = _rows(
            label,
            "SELECT role FROM space_members WHERE space_id=? AND user_id=?",
            (space_id, inst[label]["user_id"]),
        )
        return rows[0][0] if rows else None

    _wait_for(
        "c to hold carol's moderator seat", lambda: _local_role("c") == "moderator"
    )
    s, lst = _request(
        f"{b_base}/api/spaces/{space_id}/tasks/lists",
        token=b["token"],
        method="POST",
        body={"name": "Club chores"},
    )
    list_id = _must("task list create(b)", s, lst, ok=(201,))["id"]
    s, body = _request(
        f"{b_base}/api/spaces/{space_id}",
        token=b["token"],
        method="PATCH",
        body={"features": {"tasks_access": "moderated"}},
    )
    _must("b: tasks_access=moderated (every household on v_43)", s, body)

    def _level(label: str) -> str | None:
        v = inst[label]
        s, sp = _request(
            f"http://127.0.0.1:{v['port']}/api/spaces/{space_id}", token=v["token"]
        )
        return (
            ((sp or {}).get("features") or {}).get("tasks_access") if s == 200 else None
        )

    for label in ("a", "c"):
        _wait_for(
            f"{label} to see tasks reviewed",
            lambda label=label: _level(label) == "moderated",
        )

    def _has_list(label: str) -> bool:
        v = inst[label]
        s, lists = _request(
            f"http://127.0.0.1:{v['port']}/api/spaces/{space_id}/tasks/lists",
            token=v["token"],
        )
        return s == 200 and any(row.get("id") == list_id for row in lists)

    for label in ("a", "c", "d"):
        _wait_for(f"{label} to hold the list", lambda label=label: _has_list(label))

    title = f"Bring snacks {int(time.time())}"
    s, queued = _request(
        f"http://127.0.0.1:{a['port']}/api/spaces/{space_id}/tasks/lists/{list_id}/tasks",
        token=a["token"],
        method="POST",
        body={"title": title},
    )
    if s != 202 or not (queued or {}).get("queued"):
        raise SystemExit(
            f"federated-moderation: a's member task -> {s} {queued!r} "
            "(expected 202 queued — v_43 stub submit)"
        )
    item_id = queued["item_id"]
    print(f"  a: Alice's task queued for review (item {item_id[:8]})")

    def _queue_has(label: str) -> bool:
        v = inst[label]
        s, items = _request(
            f"http://127.0.0.1:{v['port']}/api/spaces/{space_id}/moderation",
            token=v["token"],
        )
        return s == 200 and any(i.get("id") == item_id for i in items)

    _wait_for(
        "c's Moderation tab to list the item", lambda: _queue_has("c"), timeout=60.0
    )
    _wait_for(
        "b's Moderation tab to list the item", lambda: _queue_has("b"), timeout=60.0
    )
    print("  b (host) and c (moderator household) hold the pending item ✓")

    s, body = _request(
        f"http://127.0.0.1:{c['port']}/api/spaces/{space_id}/moderation/{item_id}/approve",
        token=c["token"],
        method="POST",
        body={},
    )
    _must("c: carol approves on her own household", s, body)
    if body.get("status") != "publishing":
        raise SystemExit(
            f"federated-moderation: c's approve -> {body!r} (expected status "
            "'publishing' — only the host applies an item)"
        )
    print("  c: Carol approved on Gamma → handed to the host b (publishing)")

    def _task_by(label: str) -> str | None:
        v = inst[label]
        s, rows = _request(
            f"http://127.0.0.1:{v['port']}/api/spaces/{space_id}/tasks/lists/{list_id}/tasks",
            token=v["token"],
        )
        hit = (
            next((t for t in rows if t.get("title") == title), None)
            if s == 200
            else None
        )
        return hit.get("created_by") if hit else None

    for label in ("a", "b", "c", "d"):
        _wait_for(
            f"{label} to hold the approved task",
            lambda label=label: _task_by(label) == a["user_id"],
            timeout=90.0,
        )
        print(f"  {label} holds Alice's task (created_by Alice) ✓")

    def _status(label: str) -> str | None:
        rows = _rows(
            label, "SELECT status FROM space_moderation_queue WHERE id=?", (item_id,)
        )
        return rows[0][0] if rows else None

    _wait_for("a's own queue row to read approved", lambda: _status("a") == "approved")
    print("  a's queue row: approved ✓")
    if _rows("d", "SELECT 1 FROM space_moderation_queue WHERE id=?", (item_id,)):
        raise SystemExit(
            "federated-moderation: plain member household d holds the item"
        )
    if _log_contains("d", item_id, offset=d_log_before):
        raise SystemExit("federated-moderation: d's log mentions the pending item")
    print("  d (plain member) never received the pending item ✓")

    _request(
        f"{b_base}/api/spaces/{space_id}",
        token=b["token"],
        method="PATCH",
        body={"features": {"tasks_access": "open"}},
    )
    _request(
        f"{b_base}/api/spaces/{space_id}/remote-members/{c['instance_id']}/{c['user_id']}",
        token=b["token"],
        method="PATCH",
        body={"role": "member"},
    )
    state["federated_moderation_space_id"] = space_id
    _save(state)
    print("federated-moderation: ok")


def cmd_forwarded_role_change() -> None:
    """Forwarded role change (v_47): an admin on a member household changes
    a role in a space hosted elsewhere.

    Beta (b) hosts a fresh "Roles club" with delegated admin authority on,
    seats Alice (a), Carol (c) and Dave (d), and makes Alice an admin.

    1. **Alice (on a) promotes Carol (on c) to moderator.** a's route
       answers 202 ``{forwarded: true}`` — a's stub is not touched; it ships
       ``SPACE_REMOTE_ADMIN_ACTION{set_member_role}`` to the host b.
    2. b re-checks Alice's live admin seat and the role matrix with its own
       roster, applies it and federates the role: **a, b, c and d all see
       Carol as moderator** (c in ``space_members``, the others in
       ``space_remote_members``).
    3. **The new moderator can't drive a role change**: Carol's attempt to
       promote Dave from c answers 403 and nothing is forwarded; Dave stays
       a member on b.
    4. **An admin still can't make an admin**: Alice's try to make Dave an
       admin answers 403 on a.

    The space is this step's own, so no earlier assertion changes.
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' + 'pair' + 'relay-pair' first")
    inst = state["instances"]
    a, b, c, d = inst["a"], inst["b"], inst["c"], inst["d"]
    b_base = f"http://127.0.0.1:{b['port']}"
    a_base = f"http://127.0.0.1:{a['port']}"
    c_base = f"http://127.0.0.1:{c['port']}"
    s, space = _request(
        f"{b_base}/api/spaces",
        token=b["token"],
        method="POST",
        body={"name": "Roles club", "join_mode": "invite_only"},
    )
    space_id = _must("space create(b)", s, space, ok=(201,))["id"]
    print(f"  b: space {space_id[:8]} created")
    s, body = _request(
        f"{b_base}/api/spaces/{space_id}",
        token=b["token"],
        method="PATCH",
        body={"features": {"delegated_admin_authority": True}},
    )
    _must("b: delegated admin authority on", s, body)
    for label in ("a", "c", "d"):
        guest = inst[label]
        s, inv = _request(
            f"{b_base}/api/spaces/{space_id}/remote-invites",
            token=b["token"],
            method="POST",
            body={
                "invitee_instance_id": guest["instance_id"],
                "invitee_user_id": guest["user_id"],
            },
        )
        _must(f"remote-invite({label})", s, inv, ok=(201,))

    def _invite_token(label: str) -> str | None:
        guest = inst[label]
        s, invites = _request(
            f"http://127.0.0.1:{guest['port']}/api/remote_invites",
            token=guest["token"],
        )
        items = (
            invites if isinstance(invites, list) else (invites or {}).get("items") or []
        )
        hit = next((i for i in items if i.get("space_id") == space_id), None)
        return hit["invite_token"] if s == 200 and hit else None

    for label in ("a", "c", "d"):
        _wait_for(
            f"{label} to receive b's invite",
            lambda label=label: _invite_token(label) is not None,
            timeout=60.0,
        )
        guest = inst[label]
        s, r = _request(
            f"http://127.0.0.1:{guest['port']}/api/remote_invites/"
            f"{_invite_token(label)}/accept",
            token=guest["token"],
            method="POST",
        )
        _must(f"accept-invite({label})", s, r, ok=(204,))

    def _role_on(viewer: str, label: str) -> str | None:
        """``label``'s role in the Roles club as ``viewer``'s DB holds it."""
        uid = inst[label]["user_id"]
        if viewer == label:
            rows = _rows(
                viewer,
                "SELECT role FROM space_members WHERE space_id=? AND user_id=?",
                (space_id, uid),
            )
        else:
            rows = _rows(
                viewer,
                "SELECT role FROM space_remote_members WHERE space_id=?"
                " AND user_id=? AND tombstoned=0",
                (space_id, uid),
            )
        return rows[0][0] if rows else None

    for label in ("a", "c", "d"):
        _wait_for(
            f"b to seat {label}",
            lambda label=label: _role_on("b", label) == "member",
            timeout=60.0,
        )
    s, body = _request(
        f"{b_base}/api/spaces/{space_id}/remote-members/{a['instance_id']}/{a['user_id']}",
        token=b["token"],
        method="PATCH",
        body={"role": "admin"},
    )
    _must("b: make alice an admin", s, body)
    _wait_for("a to hold alice's admin seat", lambda: _role_on("a", "a") == "admin")
    # a's stub needs the roster mirror rows it will name as targets.
    for label in ("c", "d"):
        _wait_for(
            f"a to mirror {label}'s seat",
            lambda label=label: _role_on("a", label) == "member",
            timeout=60.0,
        )
    print("  a, c and d seated; alice (a) is an admin")

    # 1. Alice forwards a promotion from her own household.
    s, body = _request(
        f"{a_base}/api/spaces/{space_id}/remote-members/{c['instance_id']}/{c['user_id']}",
        token=a["token"],
        method="PATCH",
        body={"role": "moderator"},
    )
    _must("a: promote carol to moderator (forwarded)", s, body, ok=(202,))
    if body.get("forwarded") is not True:
        raise SystemExit(f"forwarded-role-change: a answered {body!r}, not forwarded")
    print("  a: 202 forwarded to the host b")

    # 2. The host applies it; everyone sees the moderator.
    for viewer in ("b", "a", "c", "d"):
        _wait_for(
            f"{viewer} to see carol as moderator",
            lambda viewer=viewer: _role_on(viewer, "c") == "moderator",
            timeout=60.0,
        )
    print("  a, b, c and d all see carol as moderator ✓")

    # 3. A moderator can't drive a role change.
    _wait_for(
        "c to mirror dave's seat", lambda: _role_on("c", "d") == "member", timeout=60.0
    )
    s, body = _request(
        f"{c_base}/api/spaces/{space_id}/remote-members/{d['instance_id']}/{d['user_id']}",
        token=c["token"],
        method="PATCH",
        body={"role": "moderator"},
    )
    if s != 403:
        raise SystemExit(
            f"forwarded-role-change: moderator carol's role change -> {s} {body!r} "
            "(expected 403)"
        )
    # 4. An admin can't make an admin, forwarded or not.
    s, body = _request(
        f"{a_base}/api/spaces/{space_id}/remote-members/{d['instance_id']}/{d['user_id']}",
        token=a["token"],
        method="PATCH",
        body={"role": "admin"},
    )
    if s != 403:
        raise SystemExit(
            f"forwarded-role-change: admin alice making dave an admin -> {s} {body!r} "
            "(expected 403)"
        )
    time.sleep(3.0)  # give a stray forward the time it would need to land
    if _role_on("b", "d") != "member":
        raise SystemExit(
            f"forwarded-role-change: dave is {_role_on('b', 'd')!r} on the host"
        )
    print("  carol (moderator) and an admin->admin grant both refused ✓")
    state["forwarded_role_change_space_id"] = space_id
    _save(state)
    print("forwarded-role-change: ok")


@_offline_step
def cmd_page_concurrent_edit() -> None:
    """Host-sequenced space pages: one order, one state everywhere (v_48).

    Beta (b) hosts a fresh "Wiki club" and seats Alice (a) and Carol (c);
    b is the page's only sequencer. Alice creates a page with three
    paragraphs; once b and c hold it:

    1. **Concurrent edits.** At the same time (three threads) a edits
       paragraph 1, c edits paragraph 3 and the host b appends a paragraph.
       a and c propose to b alone; b merges each proposal against the
       version it was made from and broadcasts the canonical version with
       its ``seq``. **a, b and c end on the same ``seq`` and the same body**
       holding all three edits, with no conflict.
    2. **Same paragraph.** a and c both rewrite paragraph 2 at once. The
       host keeps the first and makes the second a conflict side: **all
       three hold the identical conflict** (same side hashes, same
       ``current_hash``). A conflict never blocks edits: a ``PATCH`` on a
       conflicted page answers 200. Both edits start from the same settled
       version, so a conflict forms whichever order the host sees them in.
    3. **Resolution.** a resolves with ``resolution: "side"`` + ``sides``;
       it is a proposal like any edit, the host retires every side and
       broadcasts: a, b and c converge on one ``seq``, no conflict left.
    4. **A missed delete (migration 0073 tombstones).** a creates a second
       page; c goes offline; b deletes it and the delete addressed to c is
       LOST (its outbox rows dropped, as if the envelope never made it).
       c comes back still holding the page. c's start-up §25.6 sync pulls
       the host's ``pages_deleted`` stream, so c tombstones it; b then
       pulls c's chunk ("Sync now") and a and b never see the page again —
       no household resurrects it.

    The space is this step's own, so no earlier assertion changes.
    """
    state = _load()
    if not state:
        raise SystemExit("run 'up' + 'pair' first")
    inst = state["instances"]
    a, b, c = inst["a"], inst["b"], inst["c"]
    base_of = {k: f"http://127.0.0.1:{inst[k]['port']}" for k in ("a", "b", "c")}
    b_base = base_of["b"]
    s, space = _request(
        f"{b_base}/api/spaces",
        token=b["token"],
        method="POST",
        body={"name": "Wiki club", "join_mode": "invite_only"},
    )
    space_id = _must("space create(b)", s, space, ok=(201,))["id"]
    print(f"  b: space {space_id[:8]} created")

    for label, who in (("a", a), ("c", c)):
        s, inv = _request(
            f"{b_base}/api/spaces/{space_id}/remote-invites",
            token=b["token"],
            method="POST",
            body={
                "invitee_instance_id": who["instance_id"],
                "invitee_user_id": who["user_id"],
            },
        )
        _must(f"remote-invite({label})", s, inv, ok=(201,))
        base = base_of[label]

        def _invite_token(base=base, who=who) -> str | None:
            s, invites = _request(f"{base}/api/remote_invites", token=who["token"])
            items = (
                invites
                if isinstance(invites, list)
                else (invites or {}).get("items") or []
            )
            hit = next((i for i in items if i.get("space_id") == space_id), None)
            return hit["invite_token"] if s == 200 and hit else None

        _wait_for(
            f"{label} to receive b's invite",
            lambda f=_invite_token: f() is not None,
            timeout=60.0,
        )
        s, r = _request(
            f"{base}/api/remote_invites/{_invite_token()}/accept",
            token=who["token"],
            method="POST",
        )
        _must(f"accept-invite({label})", s, r, ok=(204,))
        _wait_for(
            f"b to seat {label}",
            lambda who=who: bool(
                _rows(
                    "b",
                    "SELECT 1 FROM space_remote_members WHERE space_id=? AND user_id=?"
                    " AND tombstoned=0",
                    (space_id, who["user_id"]),
                )
            ),
            timeout=60.0,
        )

    houses = {k: (base_of[k], inst[k]["token"]) for k in ("a", "b", "c")}
    pages = f"/api/spaces/{space_id}/pages"
    s, page = _request(
        f"{base_of['a']}{pages}",
        token=a["token"],
        method="POST",
        body={"title": "Rules", "content": "Para one.\n\nPara two.\n\nPara three."},
    )
    page_id = _must("page create(a)", s, page, ok=(201,))["id"]

    # Every ``/api/spaces/...`` request of one user shares ONE rate-limit
    # bucket (60 / 60 s, keyed on the first two path segments). So this step
    # polls every 3 s (3 reads per poll, one per household = 20 reads / min
    # per household's admin, well under 60 with the few writes), and a read
    # that still meets a 429 backs off (``Retry-After``) instead of passing
    # ``None`` along.
    poll_s = 3.0

    def _http(label: str, method: str, path: str, body: dict | None = None):
        base, token = houses[label]
        for _attempt in range(10):
            req = urllib.request.Request(
                f"{base}{path}",
                data=json.dumps(body).encode() if body is not None else None,
                method=method,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {token}",
                    **(
                        {"Content-Type": "application/json"} if body is not None else {}
                    ),
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=15.0) as r:
                    raw = r.read().decode() or "{}"
                    return r.status, json.loads(raw)
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode()
                if exc.code == 429:
                    try:
                        wait = float(exc.headers.get("Retry-After") or 5)
                    except ValueError:
                        wait = 5.0
                    time.sleep(min(max(wait, 1.0), 30.0))
                    continue
                try:
                    return exc.code, json.loads(raw)
                except ValueError:
                    return exc.code, {"_raw": raw}
        raise SystemExit(f"page-concurrent-edit: {label} {method} {path} still 429")

    def _get(label: str) -> dict:
        st, body = _http(label, "GET", f"{pages}/{page_id}")
        if st != 200:  # once every household holds the page, never 404
            raise SystemExit(
                f"page-concurrent-edit: reading the page on {label} answered {st} {body!r}"
            )
        return body

    def _all() -> dict[str, dict]:
        return {k: _get(k) for k in houses}

    def _poll(what: str, check, *, timeout: float) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if check():
                return
            time.sleep(poll_s)
        raise SystemExit(f"page-concurrent-edit: timed out waiting for {what}")

    def _arrived(label: str) -> bool:
        # The list, not the detail: a detail read before the host's version
        # arrives is a 404 the ``verify`` log audit would flag.
        st, body = _http(label, "GET", pages)
        if st != 200:
            raise SystemExit(
                f"page-concurrent-edit: listing on {label} answered {st} {body!r}"
            )
        hit = next((p for p in body if p.get("id") == page_id), None)
        return bool(hit) and hit.get("seq", 0) >= 1 and not hit.get("pending")

    _poll(
        "every household to hold the host-sequenced page",
        lambda: all(_arrived(k) for k in houses),
        timeout=90.0,
    )
    print(f"  a: page {page_id[:8]} created, sequenced by b, held by a/b/c")

    def _patch_all(edits: dict[str, str]) -> dict[str, int]:
        statuses: dict[str, int] = {}

        def _one(label: str, content: str) -> None:
            st, _b = _http(label, "PATCH", f"{pages}/{page_id}", {"content": content})
            statuses[label] = st

        threads = [threading.Thread(target=_one, args=kv) for kv in edits.items()]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return statuses

    def _same(check) -> bool:
        ps = _all()
        vals = list(ps.values())
        first = vals[0]
        return all(
            p["seq"] == first["seq"]
            and p["title"] == first["title"]
            and p["content"] == first["content"]
            and not p.get("pending")
            for p in vals
        ) and check(ps)

    # 1. Concurrent edits from two members and the host → one seq, one body.
    statuses = _patch_all(
        {
            "a": "Para one, by Alice.\n\nPara two.\n\nPara three.",
            "c": "Para one.\n\nPara two.\n\nPara three, by Carol.",
            "b": "Para one.\n\nPara two.\n\nPara three.\n\nAppended by Beta.",
        }
    )
    if any(st != 200 for st in statuses.values()):
        raise SystemExit(f"page-concurrent-edit: edits answered {statuses}")
    _poll(
        "a, b and c to hold the same seq and merged body",
        lambda: _same(
            lambda ps: (
                all(
                    w in ps["b"]["content"] for w in ("by Alice", "by Carol", "by Beta")
                )
                and not any(p.get("conflict") for p in ps.values())
            )
        ),
        timeout=90.0,
    )
    print(f"  concurrent edits: a/b/c on seq {_get('b')['seq']} with one merged body ✓")

    # 2. Same paragraph → the identical conflict on all three.
    #
    # Always a conflict, whatever order the host sees them in: both drafts
    # are made from the SAME settled version (seq N — the reads happen before
    # either PATCH, and both members hold seq N after step 1). The first
    # proposal the host takes is based on its current version → it
    # fast-forwards (seq N+1). The second is based on seq N too, whose body
    # the host finds in its history → a three-way merge in which both sides
    # rewrote paragraph two differently → a conflict side. No rebase can
    # intervene: a draft is rebased only on its OWN earlier proposal (when a
    # household keeps editing while one is outstanding), never on another
    # household's version.
    def _conflicted(ps: dict) -> bool:
        cs = [p.get("conflict") for p in ps.values()]
        if not all(cs):
            return False
        key = lambda cf: (cf["current_hash"], sorted(x["hash"] for x in cf["sides"]))  # noqa: E731
        return all(key(cf) == key(cs[0]) for cf in cs)

    settled = _all()
    if len({p["seq"] for p in settled.values()}) != 1:
        raise SystemExit(f"page-concurrent-edit: not settled before step 2: {settled}")
    statuses = _patch_all(
        {
            "a": settled["a"]["content"].replace("Para two.", "Two by Alice."),
            "c": settled["c"]["content"].replace("Para two.", "Two by Carol."),
        }
    )
    if any(st != 200 for st in statuses.values()):
        raise SystemExit(
            f"page-concurrent-edit: same-paragraph edits answered {statuses}"
        )
    _poll(
        "a, b and c to hold the same conflict", lambda: _same(_conflicted), timeout=90.0
    )
    print("  same paragraph: a/b/c hold the identical conflict ✓")
    current = _get("b")
    st, body = _http(
        "b",
        "PATCH",
        f"{pages}/{page_id}",
        {"content": current["content"] + "\n\nEdited while conflicted."},
    )
    if st != 200:
        raise SystemExit(
            f"page-concurrent-edit: PATCH while conflicted answered {st} {body!r}"
        )
    _poll(
        "the edit to reach a and c",
        lambda: _same(
            lambda ps: (
                _conflicted(ps) and "Edited while conflicted." in ps["b"]["content"]
            )
        ),
        timeout=90.0,
    )
    print("  PATCH on a conflicted page: 200, the conflict stays ✓")

    # 3. a resolves by side; everyone converges, no conflict left.
    conflict = _get("a")["conflict"]
    keep = conflict["sides"][0]
    s, body = _http(
        "a",
        "POST",
        f"{pages}/{page_id}/resolve-conflict",
        {
            "resolution": "side",
            "side": keep["hash"],
            "sides": [x["hash"] for x in conflict["sides"]],
        },
    )
    _must("a: resolve-conflict", s, body)
    _poll(
        "a, b and c to converge on the resolution",
        lambda: _same(
            lambda ps: (
                not any(p.get("conflict") for p in ps.values())
                and ps["b"]["content"] == keep["content"]
            )
        ),
        timeout=90.0,
    )
    print(f"  resolution: a/b/c on seq {_get('b')['seq']}, no conflict left ✓")

    # 4. A delete c misses cannot come back, and c heals by sync.
    st, old = _http("a", "POST", pages, {"title": "Old notes", "content": "Bye."})
    if st != 201:
        raise SystemExit(f"page-concurrent-edit: second page answered {st} {old!r}")
    old_id = old["id"]

    def _listed(label: str, pid: str) -> bool:
        st, body = _http(label, "GET", pages)
        if st != 200:
            raise SystemExit(
                f"page-concurrent-edit: listing on {label} answered {st} {body!r}"
            )
        hit = next((p for p in body if p.get("id") == pid), None)
        return bool(hit) and hit.get("seq", 0) >= 1 and not hit.get("pending")

    def _tombstoned(label: str, pid: str) -> bool:
        return bool(
            _rows(
                label,
                "SELECT 1 FROM space_pages WHERE id=? AND deleted_at IS NOT NULL"
                " AND content='' AND title=''",
                (pid,),
            )
        )

    _poll(
        "every household to hold the second page",
        lambda: all(_listed(k, old_id) for k in houses),
        timeout=90.0,
    )
    _kill_household("c", c)
    t0 = _rows("b", "SELECT datetime('now', '-2 seconds')")[0][0]
    st, body = _http("b", "DELETE", f"{pages}/{old_id}")
    if st != 200:
        raise SystemExit(f"page-concurrent-edit: delete answered {st} {body!r}")
    _poll(
        "a and b to tombstone the deleted page",
        lambda: all(_tombstoned(k, old_id) for k in ("a", "b")),
        timeout=60.0,
    )
    # Lose the delete addressed to c: whatever a or b queued for c since
    # (the direct envelope or a mesh-routed copy) is dropped.
    lost = 0
    for label in ("a", "b"):
        con = sqlite3.connect(_instance_dir(label) / "socialhome.db", timeout=10)
        try:
            cur = con.execute(
                "DELETE FROM federation_outbox WHERE instance_id=? AND status='pending'"
                " AND event_type IN ('space_page_deleted', 'space_routed')"
                " AND created_at >= ?",
                (c["instance_id"], t0),
            )
            lost += cur.rowcount
            con.commit()
        finally:
            con.close()
    if not _rows(
        "c", "SELECT 1 FROM space_pages WHERE id=? AND deleted_at IS NULL", (old_id,)
    ):
        raise SystemExit("page-concurrent-edit: offline c no longer holds the page?")
    print(f"  c offline missed b's delete ({lost} queued envelope(s) dropped)")
    new_pid = _spawn("c", c["port"])
    state["instances"]["c"]["pid"] = new_pid
    _save(state)
    _wait_ready(c["port"])
    print(f"  c respawned: pid={new_pid}")
    _poll(
        "c to tombstone the page from the host's pages_deleted stream",
        lambda: _tombstoned("c", old_id),
        timeout=240.0,
    )
    print("  c caught up by sync: the page is a blank tombstone on c ✓")
    st, body = _http("b", "POST", f"/api/spaces/{space_id}/sync")
    if st != 202:
        raise SystemExit(f"page-concurrent-edit: sync now on b answered {st} {body!r}")
    time.sleep(10.0)
    for label in houses:
        if _listed(label, old_id) or not _tombstoned(label, old_id):
            raise SystemExit(
                f"page-concurrent-edit: the deleted page came back on {label}"
            )
    print("  no household resurrected the deleted page ✓")
    state["page_concurrent_edit_space_id"] = space_id
    _save(state)
    print("page-concurrent-edit: ok")


# ─── Step: local-community ─────────────────────────────────────────────────
#
# A public space bound to an area (#1057): every join carries the joiner's
# household home rounded to 2 dp (~1 km), only with consent, sealed to the
# host, never stored. Host d. The homes below are the step's own and are put
# back afterwards (the documented home-coordinate seed exception — written
# straight into ``instance_identity``; no broadcast, so peers' rows keep the
# ``_SEED_COORDS`` the rest of the run asserts on). Each 4 dp home is chosen
# so its 2 dp join point differs from it: a match on the 2 dp value in a
# relay's or the host's DB is then the join point and nothing else.

#: The community: centred on d's home, 5 km.
_LC_CENTRE = (48.1371, 11.5754)
_LC_RADIUS_KM = 5.0
#: label → (home, the verdict its ~1 km cell must get). ``a`` moves from
#: outside (its seed, Berlin) to the edge between two asks.
_LC_HOMES: dict[str, tuple[tuple[float, float], str]] = {
    "b": ((48.1512, 11.5803), "inside"),  # paired with d
    "c": ((48.1288, 11.5583), "inside"),  # mesh-only: c ↔ (a|b) ↔ d
    "e": ((48.1405, 11.6012), "inside"),  # GFS only (§D2b)
}
_LC_EDGE_A = ((48.1814, 11.5751), "edge")  # ~4.8 km north: the cell straddles the rim


def _set_home(label: str, home: tuple[float, float] | None) -> None:
    """The household's own home, written the way ``_seed_home_coords`` does."""
    con = sqlite3.connect(str(_instance_dir(label) / "socialhome.db"), timeout=10)
    try:
        con.execute(
            "UPDATE instance_identity SET home_lat = ?, home_lon = ? WHERE id = 'self'",
            (home[0], home[1]) if home else (None, None),
        )
        con.commit()
    finally:
        con.close()


def _home_of(label: str) -> tuple[float, float] | None:
    row = _rows(
        label, "SELECT home_lat, home_lon FROM instance_identity WHERE id='self'"
    )
    if not row or row[0][0] is None:
        return None
    return (row[0][0], row[0][1])


def _db_mentions(
    path: Path, needles: list[str], points: list[tuple[float, float]]
) -> list[str]:
    """Every place a SQLite DB holds one of ``needles`` (in any text value)
    or one of ``points`` (a lat and a lon REAL in the same row). Read-only.
    Used to prove a join point was never stored — on the host, on a relay,
    on the GFS."""
    hits: list[str] = []
    con = sqlite3.connect(str(path), timeout=10)
    try:
        tables = [
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        for table in tables:
            # Table names come from sqlite_master, never from input.
            for row in con.execute(f'SELECT * FROM "{table}"'):
                texts = [v for v in row if isinstance(v, str)]
                for needle in needles:
                    if any(needle in v for v in texts):
                        hits.append(f"{table}: text holds {needle!r}")
                reals = {v for v in row if isinstance(v, float)}
                for lat, lon in points:
                    if lat in reals and lon in reals:
                        hits.append(f"{table}: a row holds the point {lat}, {lon}")
    finally:
        con.close()
    return sorted(set(hits))


def _point_needles(point: tuple[float, float]) -> list[str]:
    """How a 2 dp point reads as JSON text (``"lat": 48.13``)."""
    return [f'{point[0]}, "lon": {point[1]}', f'{point[0]},"lon":{point[1]}']


def _lc_err(body: Any) -> str | None:
    err = body.get("error") if isinstance(body, dict) else None
    return err.get("code") if isinstance(err, dict) else None


def _lc_join(state: dict, label: str, body: dict) -> tuple[int, Any]:
    return _as(state, label, "POST", "/api/spaces/join", body)


def _lc_seated(host: str, space_id: str, user_id: str) -> bool:
    return bool(
        _rows(
            host,
            "SELECT 1 FROM space_remote_members WHERE space_id=? AND user_id=?",
            (space_id, user_id),
        )
    )


def _lc_member_here(label: str, space_id: str, user_id: str) -> bool:
    """The joiner's own side of a seat: its household holds the space and
    the person as a member — what makes the space show up for them."""
    return bool(
        _rows(
            label,
            "SELECT 1 FROM space_members m JOIN spaces s ON s.id = m.space_id"
            " WHERE m.space_id=? AND m.user_id=?",
            (space_id, user_id),
        )
    )


def _lc_both_sides(state: dict, label: str, space_id: str, *, timeout: float) -> None:
    """Wait until ``label``'s person is seated on the host d AND in the
    space on ``label``'s own household."""
    uid = state["instances"][label]["user_id"]
    _poll(
        f"{label} to be seated on d",
        lambda: _lc_seated("d", space_id, uid),
        timeout=timeout,
    )
    _poll(
        f"{label}'s own household to hold the space",
        lambda: _lc_member_here(label, space_id, uid),
        timeout=timeout,
    )


def _lc_pending(host: str, space_id: str, user_id: str) -> list[tuple]:
    return _rows(
        host,
        "SELECT id, area_verdict FROM space_join_requests"
        " WHERE space_id=? AND user_id=? AND status='pending'",
        (space_id, user_id),
    )


def _cors_probe(
    url: str, *, method: str = "GET", origin: str = "https://viewer.example"
) -> tuple[int, dict[str, str]]:
    """``url`` with an ``Origin`` header (and the preflight headers for
    ``OPTIONS``): its status and response headers, lower-cased."""
    headers = {"Origin": origin}
    if method == "OPTIONS":
        headers["Access-Control-Request-Method"] = "POST"
        headers["Access-Control-Request-Headers"] = "content-type"
    req = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}


def cmd_gfs_highlight_link() -> None:
    """A public highlight link opens the PINNED viewer, not a GFS page.

    Prereqs: ``up`` + ``gfs-up`` + ``gfs-pair`` (a on the GFS).

    a publishes today's highlight on its GFS. Asserts:

    * the link is ``{public_viewer_base_url}/v/<hash>/#g=…&i=…&h=…&t=…&k=…``
      — the shipped default base URL and the ``v/<hash>/`` of the committed
      ``socialhome/pinned_viewer.json``; ``g`` is the GFS address a talks
      to, ``i`` / ``h`` / ``t`` its instance, highlight and the token the
      GFS minted, ``k`` the 32-byte secret (43 base64url chars);
    * the GFS never saw the secret (its log from the step's start);
    * the GFS answers the pinned page cross-origin on exactly the anonymous
      viewer routes: ``Access-Control-Allow-Origin: *`` on the ice-servers
      GET and the offer preflight (204), none on ``/gfs/info`` or the signed
      ``/gfs/highlight_rtc/answer``.

    Then unpublishes, so later steps see no public link.
    """
    state = _load()
    if not state or not _gfs_alive(state):
        raise SystemExit("run 'up' + 'gfs-up' + 'gfs-pair' first")
    a = state["instances"]["a"]
    a_url = f"http://127.0.0.1:{a['port']}"
    gfs = f"http://127.0.0.1:{GFS_PORT}"
    a_gfs = state["gfs"]["pairings"]["a"]["id"]
    log_offset = _gfs_log_size()

    s, created = _request(
        f"{a_url}/api/highlights/frames",
        token=a["token"],
        method="POST",
        body={
            "media_url": _upload_demo_image(a_url, token=a["token"], label="a"),
            "frame_type": "image",
            "caption_text": "[a] highlight — shared by a public link",
            "audience_kind": "all_paired",
        },
    )
    created = _must("gfs-highlight-link: frame(a)", s, created, ok=(201,))
    highlight_id = created["highlight"]["id"]

    s, pub = _request(
        f"{a_url}/api/highlights/{highlight_id}/publish",
        token=a["token"],
        method="POST",
        body={"gfs_id": a_gfs},
    )
    pub = _must("gfs-highlight-link: publish(a)", s, pub, ok=(201,))
    link, token = pub["url"], pub["token"]
    manifest = json.loads((REPO_ROOT / "socialhome" / "pinned_viewer.json").read_text())
    page, _, fragment = link.partition("#")
    expected_page = f"https://viewer.social-home.io/{manifest['path']}"
    if page != expected_page:
        raise SystemExit(
            f"gfs-highlight-link: link opens {page!r}, expected the pinned "
            f"viewer {expected_page!r}"
        )
    params = dict(urllib.parse.parse_qsl(fragment))
    expected = {"g": gfs, "i": a["instance_id"], "h": highlight_id, "t": token}
    if {k: params.get(k) for k in expected} != expected:
        raise SystemExit(
            f"gfs-highlight-link: fragment {params!r} does not carry {expected!r}"
        )
    secret = params.get("k", "")
    if len(secret) != 43:
        raise SystemExit(f"gfs-highlight-link: no 32-byte secret in {fragment!r}")
    print(f"  a: link opens the pinned viewer {manifest['path']} ✓")

    leaked = _gfs_log_lines_matching(secret, offset=log_offset)
    if leaked:
        raise SystemExit(f"gfs-highlight-link: the GFS log holds the secret: {leaked}")
    print("  gfs never saw the link secret ✓")

    s, h = _cors_probe(f"{gfs}/gfs/highlights/ice-servers")
    if s != 200 or h.get("access-control-allow-origin") != "*":
        raise SystemExit(f"gfs-highlight-link: ice-servers CORS: HTTP {s} {h!r}")
    s, h = _cors_probe(f"{gfs}/gfs/highlight_rtc/offer", method="OPTIONS")
    if (
        s != 204
        or h.get("access-control-allow-origin") != "*"
        or "access-control-allow-credentials" in h
    ):
        raise SystemExit(f"gfs-highlight-link: offer preflight: HTTP {s} {h!r}")
    for path, method in (
        ("/gfs/info", "GET"),
        ("/gfs/highlight_rtc/answer", "OPTIONS"),
    ):
        s, h = _cors_probe(f"{gfs}{path}", method=method)
        if any(k.startswith("access-control-") for k in h):
            raise SystemExit(f"gfs-highlight-link: CORS on {method} {path}: {h!r}")
    print("  gfs: CORS on the anonymous viewer routes only ✓")

    s, out = _request(
        f"{a_url}/api/highlights/{highlight_id}/publish",
        token=a["token"],
        method="DELETE",
    )
    _must("gfs-highlight-link: unpublish(a)", s, out, ok=(200, 204))
    print("gfs-highlight-link: ok")


def cmd_gfs_public_pages() -> None:
    """A GFS's space page and a person's profile open in the pinned viewer.

    Prereqs: ``up`` + ``gfs-up`` + ``gfs-pair`` + ``gfs-moment-profile``
    (Noor Haddad registered for public moments); ``gfs-traffic`` for a
    listed space (without it the space page's not-found state is checked).

    For each page (``/spaces/{id}``, ``/moments/{user}``) asserts the
    hand-off page (link-preview tags + a refresh) to the pinned viewer with
    everything in the fragment and that the viewer's
    JSON read answers cross-origin with what the page shows; a page that
    isn't listed is one ``404 {server_name}``. ``gfs-invite-link`` covers
    ``/join/{token}`` the same way.
    """
    state = _load()
    if not state or not _gfs_alive(state):
        raise SystemExit("run 'up' + 'gfs-up' + 'gfs-pair' first")
    gfs = f"http://127.0.0.1:{GFS_PORT}"

    space_id = state.get("gfs_space_id")
    if not space_id:
        # ``gfs-traffic`` lists one without recording it: take the directory's.
        s, listing = _request(f"{gfs}/gfs/spaces")
        rows = (
            listing if isinstance(listing, list) else (listing or {}).get("spaces", [])
        )
        space_id = next((r.get("space_id") for r in rows if r.get("space_id")), None)
    if space_id:
        s, page = _open_through_viewer(
            f"{gfs}/spaces/{space_id}",
            view="space",
            key="s",
            value=space_id,
            what="gfs-public-pages: space",
        )
        space = page.get("space") or {}
        if s != 200 or space.get("space_id") != space_id or not space.get("name"):
            raise SystemExit(f"gfs-public-pages: space page JSON {s} {page!r}")
        for field in ("cover", "icon"):
            if space.get(field) is not None and not str(space[field]).startswith(
                "data:image/"
            ):
                raise SystemExit(f"gfs-public-pages: {field} is not a data: image")
        print(f"  space page: {space['name']!r}, {space['member_count']} member(s) ✓")
    s, page = _open_through_viewer(
        f"{gfs}/spaces/sp-not-listed",
        view="space",
        key="s",
        value="sp-not-listed",
        what="gfs-public-pages: unlisted space",
    )
    if s != 404 or set(page) != {"server_name"}:
        raise SystemExit(f"gfs-public-pages: unlisted space answered {s} {page!r}")
    print("  an unlisted space is one 404 {server_name} ✓")

    s, directory = _request(f"{gfs}/gfs/moments/users?q=noor")
    _must("gfs-public-pages: directory", s, directory)
    noor = next(
        (u for u in directory.get("users", []) if u.get("username") == "noor"), None
    )
    if noor is None:
        raise SystemExit("gfs-public-pages: run 'gfs-moment-profile' first (no Noor)")
    s, page = _open_through_viewer(
        f"{gfs}/moments/{noor['user_id']}",
        view="person",
        key="u",
        value=noor["user_id"],
        what="gfs-public-pages: profile",
    )
    person = page.get("person") or {}
    if s != 200 or person.get("display_name") != noor["display_name"]:
        raise SystemExit(f"gfs-public-pages: profile JSON {s} {page!r}")
    if not str(person.get("picture") or "").startswith("data:image/"):
        raise SystemExit("gfs-public-pages: the profile carries no picture")
    if not page.get("gfs_id"):
        raise SystemExit("gfs-public-pages: no gfs_id for the Follow button")
    print(f"  profile: {person['display_name']!r} with picture and Follow ✓")
    print("gfs-public-pages: ok")


def cmd_gfs_moment_late_delete() -> None:
    """v_66: a public moment's delete leaves its signed certificate on the GFS.

    Prereqs: ``gfs-moment-profile`` (Noor on a publishes public moments, e
    follows her through the GFS). Noor posts a public reply under her moment;
    e gets it; Noor deletes it. Asserts:

    * e drops the reply (the live delete);
    * the GFS keeps exactly one certificate for it — the delete's origin
      signature, naming the moment, Noor and a's household, and nothing
      else (no content) — and refused nothing;
    * the reply's text is in neither the GFS log nor its database.

    ``verify`` then checks e still holds the tombstone and the GFS the
    certificate (what a follower away for days would be handed on its next
    reconnect).
    """
    state = _load()
    gmp = state.get("gfs_moment_profile") if state else None
    if not state or not gmp or not _gfs_alive(state):
        raise SystemExit("gfs-moment-late-delete: run 'gfs-moment-profile' first")
    a = state["instances"]["a"]
    e = state["instances"]["e"]
    a_url = f"http://127.0.0.1:{a['port']}"
    e_url = f"http://127.0.0.1:{e['port']}"
    _wait_for_gfs_ws(e["instance_id"])
    gfs_off = _gfs_log_size()
    s, login = _request(
        f"{a_url}/api/auth/token",
        method="POST",
        body={"username": "noor", "password": "harness-pwd-noor"},
    )
    _must("gfs-moment-late-delete: noor login(a)", s, login)
    tok = login["token"]
    content = f"🍯 reply to be deleted {int(time.time())}"
    s, reply = _request(
        f"{a_url}/api/moments",
        token=tok,
        method="POST",
        body={
            "content": content,
            "is_public": True,
            "parent_moment_id": gmp["moment_id"],
        },
    )
    _must("gfs-moment-late-delete: noor public reply", s, reply, ok=(201,))
    reply_id = reply["id"]
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        s, _ = _request(f"{e_url}/api/moments/{reply_id}", token=e["token"])
        if s == 200:
            break
        time.sleep(2)
    else:
        raise SystemExit(f"gfs-moment-late-delete: e never got reply {reply_id}")
    s, out = _request(f"{a_url}/api/moments/{reply_id}", token=tok, method="DELETE")
    _must("gfs-moment-late-delete: noor deletes the reply", s, out, ok=(200, 204))
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        s, _ = _request(f"{e_url}/api/moments/{reply_id}", token=e["token"])
        if s == 404:
            break
        time.sleep(2)
    else:
        raise SystemExit(f"gfs-moment-late-delete: e still shows {reply_id} (HTTP {s})")
    print("  e dropped the deleted public reply ✓")
    rows: list = []
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        rows = _gfs_rows(
            "SELECT author_user_id, cert FROM gfs_moment_delete_log WHERE moment_id = ?",
            (reply_id,),
        )
        if rows:
            break
        time.sleep(1)
    if len(rows) != 1 or rows[0][0] != gmp["user_id"]:
        raise SystemExit(f"gfs-moment-late-delete: GFS certificate rows {rows!r}")
    cert = json.loads(rows[0][1])
    want = {
        "moment_id",
        "author_user_id",
        "origin_instance_id",
        "occurred_at",
        "origin_identity_pk",
        "origin_sig_suite",
        "origin_sig",
    }
    if (
        set(cert) != want
        or cert["origin_instance_id"] != a["instance_id"]
        or cert["origin_sig_suite"] != "ed25519"
    ):
        raise SystemExit(f"gfs-moment-late-delete: the certificate is {cert!r}")
    refused = _gfs_log_lines_matching("delete_cert: refused", offset=gfs_off)
    if refused:
        raise SystemExit(f"gfs-moment-late-delete: the GFS refused {refused[:2]!r}")
    if _gfs_log_lines_matching(content, offset=gfs_off):
        raise SystemExit("gfs-moment-late-delete: the GFS log shows the reply text")
    for db_file in ("gfs.db", "gfs.db-wal"):
        try:
            raw = (GFS_DIR / db_file).read_bytes()
        except OSError:
            continue
        if content.encode() in raw:
            raise SystemExit(f"gfs-moment-late-delete: the GFS's {db_file} holds it")
    print(
        "  the GFS keeps the delete's signed certificate (no content) for "
        "followers away when it went out ✓"
    )
    state["gfs_moment_late_delete"] = {"moment_id": reply_id, "user_id": gmp["user_id"]}
    _save(state)


def cmd_gfs_moment_profile() -> None:
    """A public moment from a stranger reads by name, picture and bio.

    Prereqs: ``up`` + ``pair`` + ``gfs-up`` + ``gfs-pair`` (a and e on the
    GFS; e is paired with nobody). a new user on a (Noor) uploads a picture and
    registers for public moments on the GFS (the directory gets the name, bio and
    picture); e's user follows them there; Noor posts a public moment.

    Asserts on e, which knows a's user ONLY through the GFS:

    * ``GET /api/moments/{id}`` names the author with a's display name and
      bio — never "Someone", never the id — and a picture served by e's own
      proxy (``api/gfs/{gfs_id}/moments/users/{user_id}/picture``), which
      returns the image bytes;
    * e's follow row holds the profile the signed envelope carried.

    And the GFS learned nothing new: its directory row already held this
    bio and the picture whose digest the envelope names.

    v_62 — **encrypted from the GFS**: a minted its author's moment content
    key, sealed it to e alone (e holds the delivered epoch a minted), and
    neither the GFS log nor the GFS database contains the moment's text or
    id; the GFS logged the sealed key it routed and refused nothing.
    """
    state = _load()
    if not state or not _gfs_alive(state):
        raise SystemExit("run 'up' + 'pair' + 'gfs-up' + 'gfs-pair' first")
    a = state["instances"]["a"]
    e = state["instances"]["e"]
    a_url = f"http://127.0.0.1:{a['port']}"
    e_url = f"http://127.0.0.1:{e['port']}"
    a_gfs = state["gfs"]["pairings"]["a"]["id"]
    e_gfs = state["gfs"]["pairings"]["e"]["id"]
    _wait_for_gfs_ws(e["instance_id"])
    gfs_off = _gfs_log_size()

    # A user of its own on a: its first moment is never rate limited (one
    # per 15 min per author), and a's own user stays as earlier steps left it.
    name, bio = "Noor Haddad", "Beekeeper on a rooftop. Mostly bees."
    s, prov = _request(
        f"{a_url}/api/admin/users",
        token=a["token"],
        method="POST",
        body={
            "username": "noor",
            "password": "harness-pwd-noor",
            "display_name": name,
            "is_admin": False,
        },
    )
    if s not in (201, 409):
        _must("gfs-moment-profile: provision noor(a)", s, prov, ok=(201,))
    s, login = _request(
        f"{a_url}/api/auth/token",
        method="POST",
        body={"username": "noor", "password": "harness-pwd-noor"},
    )
    _must("gfs-moment-profile: noor login(a)", s, login)
    tok = login["token"]
    s, me = _request(
        f"{a_url}/api/me",
        token=tok,
        method="PATCH",
        body={"display_name": name, "bio": bio},
    )
    _must("gfs-moment-profile: noor profile", s, me)
    author_uid = me["user_id"]
    s, pic = _upload_file(
        f"{a_url}/api/me/picture",
        token=tok,
        filename="noor.webp",
        content=_make_demo_webp(),
        content_type="image/webp",
    )
    _must("gfs-moment-profile: noor picture", s, pic)
    s, reg = _request(
        f"{a_url}/api/moments/public/registrations",
        token=tok,
        method="POST",
        body={"gfs_id": a_gfs, "default_share": True},
    )
    _must("gfs-moment-profile: noor register", s, reg, ok=(201,))
    s, fol = _request(
        f"{e_url}/api/moments/public/follows",
        token=e["token"],
        method="POST",
        body={"gfs_id": e_gfs, "followed_user_id": author_uid},
    )
    _must("gfs-moment-profile: e follow", s, fol, ok=(201,))

    content = f"🐝 public moment from {a['name']} {int(time.time())}"
    # v_63: with a picture — it must reach e as an encrypted blob.
    picture_url = _upload_demo_image(a_url, token=tok, label="a")
    s, m = _request(
        f"{a_url}/api/moments",
        token=tok,
        method="POST",
        body={
            "content": content,
            "is_public": True,
            "media_url": picture_url,
            "media_type": "image",
        },
    )
    _must("gfs-moment-profile: noor public moment", s, m, ok=(201,))
    moment_id = m["id"]

    author: dict = {}
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        s, body = _request(f"{e_url}/api/moments/{moment_id}", token=e["token"])
        if s == 200:
            author = body["moment"].get("author") or {}
            if author.get("display_name"):
                break
        time.sleep(2)
    else:
        raise SystemExit(
            f"gfs-moment-profile: e never named the author of {moment_id} "
            f"(last HTTP {s}, author={author!r})"
        )
    if author.get("display_name") != name:
        raise SystemExit(
            f"gfs-moment-profile: e names a's user {author!r}, expected {name!r}"
        )
    if author.get("bio") != bio:
        raise SystemExit(
            f"gfs-moment-profile: e shows bio {author.get('bio')!r}, expected {bio!r}"
        )
    picture = author.get("picture_url") or ""
    if not picture.startswith(f"api/gfs/{e_gfs}/moments/users/{author_uid}/picture?v="):
        raise SystemExit(f"gfs-moment-profile: e's picture URL is {picture!r}")
    if author_uid in (author.get("display_name") or ""):
        raise SystemExit("gfs-moment-profile: the author's id shows as their name")
    s, raw = _http_get_bytes(f"{e_url}/{picture}")
    if s != 200 or not raw.startswith(b"RIFF"):
        raise SystemExit(
            f"gfs-moment-profile: e's picture proxy → HTTP {s} ({raw[:16]!r})"
        )

    rows = _gfs_rows(
        "SELECT bio, picture_digest FROM gfs_user_registrations WHERE user_id = ?",
        (author_uid,),
    )
    if (
        not rows
        or rows[0][0] != bio
        or not (rows[0][1] or "").startswith(picture.split("?v=")[1].split("&")[0])
    ):
        raise SystemExit(
            f"gfs-moment-profile: the GFS directory row {rows!r} does not already "
            "hold the bio + picture the envelope carried"
        )
    # v_62 — the key: minted on a, sealed to e alone, never the GFS's.
    own = _rows(
        "a",
        "SELECT key_gen FROM moment_public_keys WHERE author_user_id = ? AND is_own = 1",
        (author_uid,),
    )
    got = _rows(
        "e",
        "SELECT key_gen FROM moment_public_keys WHERE author_user_id = ? AND is_own = 0",
        (author_uid,),
    )
    if not own or not got or own[-1][0] != got[-1][0]:
        raise SystemExit(
            f"gfs-moment-profile: a minted epochs {own!r} but e holds {got!r}"
        )
    print(f"  e holds a's moment key generation {got[-1][0]} (sealed to e alone) ✓")
    routed = _gfs_log_lines_matching("sealed key(s) delivered", offset=gfs_off)
    if not routed:
        raise SystemExit("gfs-moment-profile: the GFS never routed a sealed key")
    mail = _gfs_rows(
        "SELECT key_gen, delivered FROM gfs_moment_key_mailbox "
        "WHERE author_user_id = ? AND follower_instance_id = ?",
        (author_uid, e["instance_id"]),
    )
    if not mail or mail[0][0] != got[-1][0] or mail[0][1] != 1:
        raise SystemExit(
            f"gfs-moment-profile: the GFS mailbox row for e is {mail!r} "
            "(expected the delivered sealed key of that generation)"
        )
    refused = _gfs_log_lines_matching("encryption_required", offset=gfs_off)
    if refused:
        raise SystemExit(f"gfs-moment-profile: the GFS refused {refused[:2]!r}")
    # The moment id is a routing field now (bound into the AEAD); the
    # content never shows.
    for secret in (content,):
        if _gfs_log_lines_matching(secret, offset=gfs_off):
            raise SystemExit(
                f"gfs-moment-profile: the GFS log shows {secret!r} in the clear"
            )
        for db_file in ("gfs.db", "gfs.db-wal"):
            try:
                raw = (GFS_DIR / db_file).read_bytes()
            except OSError:
                continue
            if secret.encode() in raw:
                raise SystemExit(
                    f"gfs-moment-profile: the GFS's {db_file} holds {secret!r}"
                )
    print("  the GFS log and database hold no moment text ✓")

    # v_63 — the picture: e keeps it from an encrypted blob, and the GFS
    # holds that blob only as ciphertext.
    media_url = ""
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        s, body = _request(f"{e_url}/api/moments/{moment_id}", token=e["token"])
        media_url = (
            (body.get("moment") or {}).get("media_url") or "" if s == 200 else ""
        )
        if media_url:
            break
        time.sleep(2)
    if "api/media/gm-" not in media_url:
        raise SystemExit(
            f"gfs-moment-profile: e's moment has no kept picture ({media_url!r})"
        )
    s, raw = _http_get_bytes(f"{e_url}/{media_url}")
    if s != 200 or not raw.startswith(b"RIFF"):
        raise SystemExit(
            f"gfs-moment-profile: e's kept picture → HTTP {s} ({raw[:12]!r})"
        )
    blobs = list((GFS_DIR / "media" / "moments").glob("[0-9a-f]*"))
    if not blobs:
        raise SystemExit("gfs-moment-profile: the GFS holds no moment blob")
    for path in blobs:
        if b"WEBP" in path.read_bytes():
            raise SystemExit("gfs-moment-profile: a GFS blob holds a readable WebP")
    print(
        f"  e keeps the picture from one encrypted GFS blob ({len(blobs)} on the GFS, none readable) ✓"
    )

    # Share link — a stranger opens this one moment from GFS ciphertext
    # with the item key in the fragment; the GFS never sees that key.
    link_off = _gfs_log_size()
    s, share = _request(
        f"{a_url}/api/moments/{moment_id}/share-link",
        token=tok,
        method="POST",
        body={"gfs_id": a_gfs},
    )
    _must("gfs-moment-profile: share link", s, share)
    url = share.get("url") or ""
    base, _, fragment = url.partition("#")
    frag = dict(urllib.parse.parse_qsl(fragment))
    if (
        "/v/" not in base
        or frag.get("a") != author_uid
        or frag.get("m") != moment_id
        or len(frag.get("k") or "") != 43
        or len(frag.get("b") or "") != 64
        or frag.get("g") != f"http://127.0.0.1:{GFS_PORT}"
    ):
        raise SystemExit(f"gfs-moment-profile: share link has the wrong shape: {url!r}")
    s, blob = _http_get_bytes(
        f"http://127.0.0.1:{GFS_PORT}/gfs/moments/media/{frag['b']}"
    )
    if s != 200 or content.encode() in blob:
        raise SystemExit(f"gfs-moment-profile: share copy → HTTP {s} (or readable)")
    opened = subprocess.run(
        [sys.executable, "-c", _OPEN_SHARE_SNIPPET, frag["k"], author_uid, moment_id],
        input=blob,
        capture_output=True,
        cwd=REPO_ROOT,
        timeout=30,
    )
    if opened.returncode != 0:
        raise SystemExit(
            f"gfs-moment-profile: the share copy doesn't open with k: "
            f"{opened.stderr.decode()[-400:]}"
        )
    body = json.loads(opened.stdout)
    if body.get("content") != content or body.get("author_name") != name:
        raise SystemExit(f"gfs-moment-profile: the share copy opened to {body!r}")
    media_blob = ((body.get("media") or {}).get("blob_id")) or ""
    s, _ = _http_get_bytes(
        f"http://127.0.0.1:{GFS_PORT}/gfs/moments/media/{media_blob}"
    )
    if not media_blob or s != 200:
        raise SystemExit(f"gfs-moment-profile: the shared picture blob → HTTP {s}")
    if _gfs_log_lines_matching(frag["k"], offset=link_off):
        raise SystemExit("gfs-moment-profile: the GFS log shows the link's key")
    s, profile = _open_through_viewer(
        f"http://127.0.0.1:{GFS_PORT}/moments/{author_uid}",
        view="person",
        key="u",
        value=author_uid,
        what="gfs-moment-profile",
    )
    page = json.dumps(profile).encode()
    if s != 200 or content.encode() in page or b"moment_rtc" in page:
        raise SystemExit(
            f"gfs-moment-profile: /moments/{author_uid} → HTTP {s} shows moments"
        )
    print(
        "  the share link opens the moment from GFS ciphertext with its item key; "
        "the GFS never logged the key, and the profile page shows no moments ✓"
    )

    # v_68 — a reaction from e reaches a through the GFS.
    _gfs_moment_reaction(state, moment_id, author_uid)

    state["gfs_moment_profile"] = {
        "moment_id": moment_id,
        "user_id": author_uid,
        "display_name": name,
        "bio": bio,
        "gfs_id": e_gfs,
        "reaction": "🔥",
    }
    _save(state)
    print(
        f"gfs-moment-profile: ok (e names {name!r} with bio + GFS-mirrored "
        "picture; the GFS directory already held both; the moment reached e "
        "encrypted from the GFS)"
    )


def _gfs_moment_reaction(state: dict, moment_id: str, author_uid: str) -> None:
    """v_68: e (paired with nobody, follows Noor through the GFS) reacts to
    her public moment. The reaction is sealed to a's key-wrap key (named in
    the signed moment), posted to the GFS the moment came through, and
    pushed to a only. a must hold it within 30 s, then drop e's removal; the
    GFS log and database must hold neither the emoji nor the reactor."""
    a, e = state["instances"]["a"], state["instances"]["e"]
    e_url = f"http://127.0.0.1:{e['port']}"
    e_user = e.get("user_id")
    kw = _rows(
        "e",
        "SELECT followed_keywrap_pk, followed_keywrap_suite"
        " FROM moment_public_follows WHERE followed_user_id = ?",
        (author_uid,),
    )
    if not kw or not kw[0][0] or kw[0][1] != "x25519":
        raise SystemExit(
            f"gfs-moment-profile: e kept no key-wrap key for a's author ({kw!r})"
        )
    s, detail = _request(f"{e_url}/api/moments/{moment_id}", token=e["token"])
    reach = ((detail or {}).get("moment") or {}).get("reactions_reach")
    if s != 200 or reach != "author":
        raise SystemExit(
            f"gfs-moment-profile: e's moment says reactions_reach={reach!r} "
            f"(HTTP {s}) — expected 'author' through the GFS"
        )
    gfs_off = _gfs_log_size()

    def _a_holds() -> list[tuple]:
        return _rows(
            "a",
            "SELECT emoji FROM moment_reactions"
            " WHERE moment_id = ? AND reactor_user_id = ?",
            (moment_id, e_user),
        )

    def _wait(want: list[tuple], what: str) -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if _a_holds() == want:
                return
            time.sleep(1)
        refused = _log_lines_matching("a", "moment_reaction via GFS")
        raise SystemExit(
            f"gfs-moment-profile: a holds {_a_holds()!r} after e's {what}, "
            f"expected {want!r}; a refused: {refused[-2:]!r}"
        )

    for emoji, what in (("👏", "reaction"), (None, "removal"), ("🔥", "reaction")):
        if emoji is None:
            s, body = _request(
                f"{e_url}/api/moments/{moment_id}/reaction",
                token=e["token"],
                method="DELETE",
            )
            want: list[tuple] = []
        else:
            s, body = _request(
                f"{e_url}/api/moments/{moment_id}/reaction",
                token=e["token"],
                method="PUT",
                body={"emoji": emoji},
            )
            want = [(emoji,)]
        _must(f"gfs-moment-profile: e {what}", s, body, ok=(200, 204))
        _wait(want, what)
    # v_69 — a's totals reach e through the GFS: the moment's +10 min
    # checkpoint is brought forward (the moment is backdated on a), a's
    # minute tick seals the totals under today's step and the GFS pushes them
    # to e (connected, declared ``ops: ["counts"]``) — never queued.
    _backdate_moment("a", moment_id, minutes=11)
    deadline = time.monotonic() + 120
    totals = None
    while time.monotonic() < deadline:
        rows = _rows("e", "SELECT reaction_summary FROM moments WHERE id = ?", (moment_id,))
        totals = json.loads(rows[0][0]) if rows and rows[0][0] else None
        if totals and totals.get("counts") == {"🔥": 1} and totals.get("total") == 1:
            break
        time.sleep(2)
    else:
        sent = _log_lines_matching("a", "moment totals")
        raise SystemExit(
            f"gfs-moment-profile: e holds totals {totals!r} for a's moment, "
            f"expected 🔥 1; a: {sent[-2:]!r}"
        )
    s, detail = _request(f"{e_url}/api/moments/{moment_id}", token=e["token"])
    m = (detail or {}).get("moment") or {}
    if s != 200 or m.get("reaction_count") != 1 or m.get("reaction_totals_at") is None:
        raise SystemExit(
            f"gfs-moment-profile: e's API shows reaction_count="
            f"{m.get('reaction_count')!r} totals_at={m.get('reaction_totals_at')!r}"
        )
    print("  v_69 a's reaction totals reached e through the GFS, sealed ✓")
    for secret in ("👏", "🔥", e_user or "-", moment_id):
        if _gfs_log_lines_matching(secret, offset=gfs_off):
            raise SystemExit(f"gfs-moment-profile: the GFS log names {secret!r}")
    for db_file in ("gfs.db", "gfs.db-wal"):
        try:
            raw = (GFS_DIR / db_file).read_bytes()
        except OSError:
            continue
        for secret in ("👏", "🔥"):
            if secret.encode() in raw:
                raise SystemExit(
                    f"gfs-moment-profile: the GFS's {db_file} holds {secret!r}"
                )
    if _rows("e", "SELECT 1 FROM remote_instances WHERE id = ?", (a["instance_id"],)):
        raise SystemExit("gfs-moment-profile: e and a became paired")
    print(
        "  v_68 e's reaction (and its removal) reached a through the GFS, sealed "
        "— the GFS holds no emoji, a and e still unpaired ✓"
    )


def cmd_local_community() -> None:
    """A local community on the real wire (#1057): who gets in, and that the
    ~1 km point a join carries is sealed end to end and never stored.

    Prereqs: ``up`` + ``pair`` + ``relay-pair`` (a ↔ d) + ``gfs-up`` +
    ``gfs-pair`` (d and e on the GFS). Host d; the community is public,
    open, centred on d's home with a 5 km radius.

    a. **Paired, inside** — b asks to join (``/api/public_spaces/{id}/
       join-request``). Without consent the host drops it (nothing filed);
       with ``share_location`` d files it as ``inside`` (a paired
       household's ask is always reviewed), d approves, b is seated.
    b. **Mesh, inside** — c (not paired with d) redeems d's link over the
       mesh: ``LOCATION_REQUIRED`` without consent, seated with it. Neither
       possible relay (a, b) dispatched the inner redeem, logged a join
       location, or stored the point.
    c. **Outside** — a (Berlin) redeems a link: ``OUTSIDE_AREA``, nothing
       filed, not seated.
    d. **Edge** — a moves to ~4.8 km north and redeems again:
       ``JOIN_REQUEST_SENT``; d holds a pending ``edge`` request ("Near the
       edge"); d approves; a is seated with no second check.
    e. **GFS** — e (paired with nobody) redeems a GFS-published link through
       the §D2b sealed bootstrap: ``LOCATION_REQUIRED``, then seated; the
       GFS log and DB hold no join location and not the point.

    Throughout: d (the host) stores none of the points — only the verdict
    on a pending request.
    """
    from socialhome.domain.local_community import (
        LocalArea,
        coarse_join_point,
        join_verdict,
    )

    state = _load()
    if not state or not _gfs_alive(state):
        raise SystemExit(
            "run 'up' + 'pair' + 'relay-pair' + 'gfs-up' + 'gfs-pair' first"
        )
    inst = state["instances"]
    d = inst["d"]
    d_iid = d["instance_id"]
    gfs_conn = ((state.get("gfs") or {}).get("pairings") or {}).get("d", {}).get("id")
    if not gfs_conn:
        raise SystemExit("run 'gfs-pair' first (d is not a client of the GFS)")
    for label, want_paired in (("a", True), ("b", True), ("c", False), ("e", False)):
        row = _instance_row(label, d_iid)
        # A §D2b seat (``space_session``, an earlier run's) is not a pair.
        paired = (
            row is not None
            and row.get("status") == "confirmed"
            and row.get("source") != "space_session"
        )
        if paired != want_paired:
            raise SystemExit(
                f"local-community: {label} {'is' if paired else 'is not'} paired with d — "
                "the step needs b and a paired, c mesh-only and e GFS-only"
            )
    # c is on no GFS, so its only way to d is the mesh; e is paired with
    # nobody, so its only way is the GFS relay.
    if _rows("c", "SELECT 1 FROM gfs_connections"):
        raise SystemExit(
            "local-community: c is a GFS client — its redeem could skip the mesh"
        )
    if _rows(
        "e",
        "SELECT 1 FROM remote_instances WHERE status='confirmed' AND source != 'space_session'",
    ):
        raise SystemExit(
            "local-community: e has a paired household — it is no longer GFS-only"
        )

    area = LocalArea(lat=_LC_CENTRE[0], lon=_LC_CENTRE[1], radius_km=_LC_RADIUS_KM)
    points: dict[str, tuple[float, float]] = {}
    for label, (home, want) in {**_LC_HOMES, "a": _LC_EDGE_A}.items():
        point = coarse_join_point(*home)
        got = str(join_verdict(area, point))
        if got != want:
            raise SystemExit(
                f"local-community: {label}'s home {home} → cell {point} is {got!r}, "
                f"the step needs {want!r} (fix _LC_HOMES)"
            )
        points[label] = point
    berlin = coarse_join_point(*_SEED_COORDS["a"])
    if str(join_verdict(area, berlin)) != "outside":
        raise SystemExit("local-community: a's seed home is not outside the area?")
    points["a_outside"] = berlin
    saved = {label: _home_of(label) for label in ("a", "b", "c", "d", "e")}
    every_point = list(points.values())
    every_needle = ["join_location"] + [
        n for p in every_point for n in _point_needles(p)
    ]

    try:
        _set_home("d", _LC_CENTRE)
        for label, (home, _want) in _LC_HOMES.items():
            _set_home(label, home)
        _set_home("a", _SEED_COORDS["a"])

        st, space = _as(
            state,
            "d",
            "POST",
            "/api/spaces",
            {
                "name": "Delta Neighbourhood",
                "space_type": "public",
                "join_mode": "open",
                "emoji": "🏘",
                "lat": _LC_CENTRE[0],
                "lon": _LC_CENTRE[1],
                "radius_km": _LC_RADIUS_KM,
            },
        )
        space = _must("local-community: d creates the community", st, space, ok=(201,))
        space_id = space["id"]
        print(
            f"  d hosts local community {space_id} ({_LC_RADIUS_KM:g} km around d's home)"
        )

        # ── a. paired, inside ─────────────────────────────────────────────
        b = inst["b"]
        jr = f"/api/public_spaces/{space_id}/join-request"
        st, body = _as(state, "b", "POST", jr, {"host_instance_id": d_iid})
        _must("local-community: b asks without consent", st, body, ok=(202,))
        time.sleep(6.0)
        if _lc_pending("d", space_id, b["user_id"]):
            raise SystemExit(
                "local-community: d filed b's request although it carried no "
                "location — the host must drop a join with no point (LOCATION_REQUIRED)"
            )
        print("  b without consent: d dropped the request (no point → no decision) ✓")
        st, body = _as(
            state, "b", "POST", jr, {"host_instance_id": d_iid, "share_location": True}
        )
        _must("local-community: b asks with consent", st, body, ok=(202,))
        pending = _poll(
            "d to file b's request",
            lambda: _lc_pending("d", space_id, b["user_id"]),
            timeout=60.0,
        )
        rid, verdict = pending[0]
        if verdict != "inside":
            raise SystemExit(
                f"local-community: b's request is {verdict!r}, expected 'inside'"
            )
        print("  b with consent: d filed it as 'inside' ✓")
        st, body = _as(
            state, "d", "POST", f"/api/spaces/{space_id}/join-requests/{rid}/approve"
        )
        _must("local-community: d approves b", st, body)
        _lc_both_sides(state, "b", space_id, timeout=90.0)
        print("  d approved: b seated — on d and on b ✓")

        # ── b. mesh, inside ───────────────────────────────────────────────
        st, link = _as(
            state, "d", "POST", f"/api/spaces/{space_id}/invite-tokens", {"uses": 1}
        )
        link = _must("local-community: d mints a link for c", st, link, ok=(201,))
        offs = {r: _log_size(r) for r in ("a", "b")}
        redeem = {"token": link["token"], "issuer_instance_id": d_iid}
        st, body = _lc_join(state, "c", redeem)
        if st != 403 or _lc_err(body) != "LOCATION_REQUIRED":
            raise SystemExit(
                f"local-community: c's redeem without consent answered {st} {body!r}, "
                "expected 403 LOCATION_REQUIRED"
            )
        print(
            "  c (mesh) without consent: 403 LOCATION_REQUIRED, the use handed back ✓"
        )
        st, body = _lc_join(state, "c", {**redeem, "share_location": True})
        _must(
            "local-community: c redeems over the mesh with consent",
            st,
            body,
            ok=(200, 201),
        )
        _lc_both_sides(state, "c", space_id, timeout=10.0)
        print("  c (mesh) with consent: seated — on d and on c ✓")
        relayed = []
        for relay, off in offs.items():
            if _log_contains(relay, "SPACE_INVITE_TOKEN_REDEEM", offset=off):
                raise SystemExit(
                    f"local-community: relay {relay} dispatched the inner redeem — the "
                    "join (and its point) must ride sealed inside SPACE_ROUTED"
                )
            for needle in every_needle:
                if _log_contains(relay, needle, offset=off):
                    raise SystemExit(
                        f"local-community: relay {relay}'s log shows {needle!r}"
                    )
            hits = _db_mentions(
                _instance_dir(relay) / "socialhome.db", every_needle, every_point
            )
            if hits:
                raise SystemExit(
                    f"local-community: relay {relay} stored the point: {hits}"
                )
            if _log_contains(relay, "SPACE_ROUTED", offset=off):
                relayed.append(relay)
        print(
            f"  relays {sorted(offs)} never unsealed it, logged or stored a point ✓"
            + (f" (SPACE_ROUTED seen on {relayed})" if relayed else "")
        )

        # ── c. outside ────────────────────────────────────────────────────
        a = inst["a"]
        st, link = _as(
            state, "d", "POST", f"/api/spaces/{space_id}/invite-tokens", {"uses": 1}
        )
        link = _must("local-community: d mints a link for a", st, link, ok=(201,))
        a_redeem = {
            "token": link["token"],
            "issuer_instance_id": d_iid,
            "share_location": True,
        }
        st, body = _lc_join(state, "a", a_redeem)
        if st != 403 or _lc_err(body) != "OUTSIDE_AREA":
            raise SystemExit(
                f"local-community: a (Berlin) answered {st} {body!r}, expected 403 OUTSIDE_AREA"
            )
        if _lc_seated("d", space_id, a["user_id"]) or _lc_pending(
            "d", space_id, a["user_id"]
        ):
            raise SystemExit(
                "local-community: an outside household was seated or filed"
            )
        print("  a outside: 403 OUTSIDE_AREA, nothing filed ✓")

        # ── d. edge → admin review ────────────────────────────────────────
        _set_home("a", _LC_EDGE_A[0])
        st, body = _lc_join(state, "a", a_redeem)
        if st != 403 or _lc_err(body) != "JOIN_REQUEST_SENT":
            raise SystemExit(
                f"local-community: a near the edge answered {st} {body!r}, "
                "expected 403 JOIN_REQUEST_SENT"
            )
        pending = _lc_pending("d", space_id, a["user_id"])
        if not pending or pending[0][1] != "edge":
            raise SystemExit(
                f"local-community: d holds {pending!r} for a, expected one 'edge' request"
            )
        st, listed = _as(state, "d", "GET", f"/api/spaces/{space_id}/join-requests")
        listed = _must("local-community: d lists its join requests", st, listed)
        mine = [r for r in listed if r.get("id") == pending[0][0]]
        if not mine or mine[0].get("area_verdict") != "edge":
            raise SystemExit(f"local-community: d's admin queue shows {listed!r}")
        leaked = [k for k in ("lat", "lon", "join_location") if k in mine[0]]
        if leaked:
            raise SystemExit(f"local-community: the admin queue carries {leaked}")
        print(
            "  a near the edge: 403 JOIN_REQUEST_SENT, an 'edge' request in d's queue ✓"
        )
        st, body = _as(
            state,
            "d",
            "POST",
            f"/api/spaces/{space_id}/join-requests/{pending[0][0]}/approve",
        )
        _must("local-community: d approves a", st, body)
        _lc_both_sides(state, "a", space_id, timeout=90.0)
        print("  d approved: a seated — on d and on a, no second check ✓")

        # ── e. GFS (§D2b) ─────────────────────────────────────────────────
        e = inst["e"]
        # A local community is public, and public is never on a GFS: its link
        # is not parked on a GFS /join page — the code is handed over (chat,
        # mail) and the redeem still travels sealed through the GFS relay.
        st, link = _as(
            state,
            "d",
            "POST",
            f"/api/spaces/{space_id}/invite-tokens",
            {"role": "member", "uses": 1},
        )
        link = _must("local-community: d mints a link for e", st, link, ok=(201,))
        code = link.get("code")
        if not code:
            raise SystemExit(f"local-community: the link carries no code: {link!r}")
        e_body = _join_body_from_blob(_decode_invite_blob(code))
        if "issuer_keywrap_pk" not in e_body:
            raise SystemExit(
                f"local-community: the code carries no §D2b bootstrap block: {e_body!r}"
            )
        # Both ends of the relay must be on their GFS socket: an envelope for
        # a household that isn't is queued, and the redeem gives up first.
        _wait_for_gfs_ws(d_iid)
        _wait_for_gfs_ws(e["instance_id"])
        gfs_off = _gfs_log_size()
        st, body = _lc_join(state, "e", e_body)
        if st != 403 or _lc_err(body) != "LOCATION_REQUIRED":
            raise SystemExit(
                f"local-community: e's redeem without consent answered {st} {body!r}, "
                "expected 403 LOCATION_REQUIRED"
            )
        print("  e (GFS) without consent: 403 LOCATION_REQUIRED ✓")
        st, body = _lc_join(state, "e", {**e_body, "share_location": True})
        _must(
            "local-community: e redeems via the GFS with consent",
            st,
            body,
            ok=(200, 201),
        )
        _lc_both_sides(state, "e", space_id, timeout=10.0)
        seat = _instance_row("e", d_iid) or {}
        if seat.get("source") != "space_session":
            raise SystemExit(
                f"local-community: e's row for d is {seat!r}, not a §D2b seat"
            )
        if not _gfs_log_lines_matching("gfs.envelope:", offset=gfs_off):
            raise SystemExit(
                "local-community: the GFS relayed no envelope for e's redeem"
            )
        for needle in every_needle:
            if _gfs_log_lines_matching(needle, offset=gfs_off):
                raise SystemExit(f"local-community: the GFS log shows {needle!r}")
        hits = _db_mentions(GFS_DIR / "gfs.db", every_needle, every_point)
        if hits:
            raise SystemExit(f"local-community: the GFS stored the point: {hits}")
        print("  e (GFS) with consent: seated; the GFS saw no join location ✓")

        # ── the host keeps no point ───────────────────────────────────────
        joiners = [points[k] for k in ("a", "b", "c", "e")] + [berlin]
        hits = _db_mentions(_instance_dir("d") / "socialhome.db", every_needle, joiners)
        if hits:
            raise SystemExit(f"local-community: d stored a join point: {hits}")
        print("  d kept no join point — only the verdicts ✓")
    finally:
        for label, home in saved.items():
            _set_home(label, home)

    state["local_community"] = {
        "space_id": space_id,
        "seated": {k: inst[k]["user_id"] for k in ("a", "b", "c", "e")},
    }
    _save(state)
    print("local-community: ok")


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    cmd = sys.argv[1]
    if cmd == "all":
        # The full path: pair the inner ring → traffic + invites →
        # accept invites + RSVP-on-event (cmd_calendar) → assertions →
        # transitive auto-pair via the trust relay (cmd_relay_pair).
        # The rest of the GFS surface (``gfs-*``) stays opt-in; ``all``
        # only brings the GFS up at the very end, for ``local-community``.
        cmd_up()
        cmd_pair()
        cmd_traffic()
        time.sleep(5)  # let federation settle before assertions
        cmd_calendar()
        cmd_verify()
        cmd_relay_pair()
        # ``visibility`` toggles a local user hidden from a peer and
        # asserts the peer's ``/api/friends`` mirror tracks the
        # change. While the user is hidden the step also fires a DM,
        # a moment, and an ``all_paired`` highlight and asserts none
        # reach the blocked peer (DM_MESSAGE / MOMENT_CREATED /
        # HIGHLIGHT_* gates) while a positive control on Gamma
        # confirms the filter is per-peer rather than global.
        cmd_visibility()
        # ``invite-redeem`` exercises the
        # ``SPACE_INVITE_TOKEN_REDEEM`` federation flow: Carol mints
        # a token on c, Alice pastes it on a, a's /api/spaces/join
        # routes the redeem over federation, c seats Alice as a
        # remote space member, c's next space post reaches a. Runs
        # after ``visibility`` so the direct a↔c pair is still
        # confirmed and not in any partial-hide state.
        cmd_invite_redeem()
        # ``invite-redeem-routed`` exercises PR 2's mesh path: c
        # wants to join a space hosted on d but is NOT directly paired
        # with d (only c↔b↔d exists). The receiver-side coordinator
        # discovers a route via b and ships the redeem inside a
        # ``SPACE_ROUTED`` envelope sealed under d's per-route
        # ephemeral; b forwards the opaque blob without decrypting.
        # Asserts d seated c as a remote member AND b never dispatched
        # the inner SPACE_INVITE_TOKEN_REDEEM (i.e. relays cannot
        # read content).
        cmd_invite_redeem_routed()
        # ``remote-invite-routed`` exercises PR 3's mesh-enabled
        # SpaceService outbound: c (admin) invites dave on d via the
        # mesh because c↔d isn't paired. The accept leg also
        # mesh-routes back. Asserts dave seated AND that b never saw
        # the inner SPACE_PRIVATE_INVITE / _ACCEPT (encryption
        # invariant for the admin-initiated flow too).
        cmd_remote_invite_routed()
        # ``space-post-routed`` validates the broader mesh surface:
        # c posts in the mesh-private space we just seeded. The new
        # ``broadcast_to_space_members`` lists every member instance
        # (no CONFIRMED filter) and ships each via
        # ``send_with_mesh_fallback`` — so the SPACE_POST_CREATED
        # envelope routes to d via SPACE_ROUTED through b, end-to-end
        # encrypted. Asserts d.space_posts contains the post AND
        # b's log never decrypted the inner event.
        cmd_space_post_routed()
        # ``mesh-member-cert`` (migration 0078): the members those two
        # mesh joins seated (d in c's space, c in d's) told their host their
        # version over the mesh, and each now holds a verifying writer cert
        # under its own identity key — delivered sealed over SPACE_ROUTED.
        cmd_mesh_member_cert()
        # ``space-media-blob`` validates that picture/video bytes
        # posted in a space ACTUALLY reach remote member households —
        # SPACE_POST_CREATED only carries the URL string, so without
        # the SpaceMediaSyncService outbox the receiver's
        # ``<img src>`` 404s on the relative URL. c uploads a WebP,
        # posts it in c's mesh-private space, and after settle d's
        # media path must contain the same bytes under the same
        # filename.
        cmd_space_media_blob()
        # ``space-gallery-media-blob`` covers the same federation gap
        # for the gallery surface — items uploaded into a per-space
        # album ship their thumbnail + full bytes to remote members
        # via the same shared media outbox. Without this the gallery
        # thumbnails on remote households render as broken images.
        cmd_space_gallery_media_blob()
        # ``space-sync-catchup-media`` proves the §25.6 catch-up path:
        # c populates a fresh mesh-private space with a post + gallery
        # item BEFORE inviting dave, dave joins, and dave's first
        # ``stream_initial`` ships the historical media bytes via the
        # same outbox the realtime path uses. Without this, a newcomer
        # joining a long-running space sees post/gallery rows but
        # broken ``<img src>`` tags.
        cmd_space_sync_catchup_media()
        # ``sync-https-fallback`` proves the Part C wiring: dave
        # restarts with ``SH_FORCE_SYNC_HTTPS=1`` so the scheduler
        # asks for ``prefer_direct=False`` syncs. c streams chunks
        # via ``SPACE_SYNC_CHUNK`` federation events instead of the
        # DataChannel, and dave's feed catches up.
        cmd_sync_https_fallback()
        # ``admin-promote-kick`` exercises the cross-household admin
        # promotion path (#114, v_8+) AND the v_28 SPACE_ROUTE_STALE
        # nack: c warms its mesh route to d, d is killed, c promotes
        # dave via PATCH /api/spaces/{id}/remote-members/{instance}/
        # {user} while d is down (sealing under d's now-dead ephemeral
        # key; b's outbox holds the envelope), d is respawned, and the
        # redelivered stale envelope is nacked back to c, which
        # rediscovers and retransmits — d's stub then reflects the new
        # role. The kick half (SPACE_REMOTE_ADMIN_KICK, v_9+) is covered
        # by unit tests; the demo focuses on the wire-level round-trip.
        cmd_admin_promote_kick()
        # ``app-session`` exercises the PR4 cross-household app
        # federation bridge: opens an APP_SESSION from a to b,
        # sends an APP_MESSAGE, and asserts both REST calls return
        # 2xx.  The step skips gracefully when no common installed
        # app is present (unit tests cover the full delivery path).
        cmd_app_session()
        # ``remote-invite-decline`` covers the DECLINE leg that the
        # earlier accept-only flows never hit — c invites alice
        # (direct pair), alice declines, c's invitation row is
        # marked declined and the user is NOT seated.
        cmd_remote_invite_decline()
        # ``group-dm`` (v_37): a group on a with b + c, messages from each
        # household reach the others, non-member d holds nothing, a adds d
        # (history catch-up) and removes c, b leaves; then a group on b
        # with c + d (never paired) proves the E2E-sealed mesh leg.
        cmd_group_dm()
        # ``federated-moderation`` (v_43): a member on a submits a task in
        # a Reviewed space hosted by b; a moderator on c approves on c; a,
        # b, c and d all hold the task, and plain-member household d never
        # received the pending item.
        cmd_federated_moderation()
        # ``space-report`` (v_45): a member on c reports a post in a space
        # hosted by b; the moderator on a sees it, plain-member household d
        # never receives it, and a's resolve clears it on b.
        cmd_space_report()
        # ``forwarded-role-change`` (v_47): an admin on a promotes a member
        # of c to moderator in a space hosted by b — a answers 202
        # forwarded, the host applies it and a, b, c, d all see the
        # moderator; the new moderator's own role change is refused.
        cmd_forwarded_role_change()
        # ``page-concurrent-edit`` (v_48 host-sequenced pages): in a space
        # hosted by b, a and c propose concurrent edits while b appends — all
        # three end on one seq and body; same-paragraph edits leave the
        # identical conflict everywhere (edits still allowed); a resolves by
        # side and everyone converges. Then c misses a page delete while
        # offline and heals from the host's tombstones without bringing
        # the page back anywhere (migration 0073).
        cmd_page_concurrent_edit()
        # ``admin-revoke-rotation`` (v_44): a demotes delegated admin b; a
        # rotates the space authority key, c and b re-pin from the owner's
        # cert, b's old seed is cleared and b can no longer sign.
        cmd_admin_revoke_rotation()
        # ``rotation-offline-catchup`` (v_44): c is DOWN while a demotes b
        # (rotation), posts and renames; on restart c applies the outboxed
        # bundle (K2 pin + baseline), holds the post, the rename and the
        # post-rotation content key, and refuses b's K1-signed rename.
        cmd_rotation_offline_catchup()
        # ``space-chat`` (v_55): in Beta's space (owner b, members a + c) d
        # joins as a follower household; a posts, c replies, b edits, a
        # reacts and b (owner) deletes c's reply — a, b and c converge and
        # d holds nothing. Late in the chain: it seats d as a follower in
        # the space the earlier steps assert member counts on.
        cmd_space_chat()
        # ``bazaar-ask`` (v_74): in the same space b turns the Bazaar on, c
        # lists a lamp, a (paired with c) asks and lands in the normal DM,
        # d (follower) is refused; a, b, c see each other at >= v_74.
        cmd_bazaar_ask()
        # ``bazaar-faq`` (v_75): c adds questions and answers to the lamp,
        # rewords one and removes one — a, b and d (follower household)
        # converge; a (not the seller) is refused.
        cmd_bazaar_faq()
        # ``app-group`` (v_76): a opens a group table with b's and c's
        # admins, both join, a starts and closes it — all three converge.
        cmd_app_group()
        # ``app-draw`` (v_78): a's admin, dora (on a), b's and c's admins
        # draw names with one pair apart — one circle, every result only on
        # its own sheet, no wish list in plaintext on any disk.
        cmd_app_draw()
        # ``space-table`` (v_77): in c's space from remote-invite-routed, c
        # opens a two-player table and d (mesh only, via b) joins it — it
        # starts at once, d's move reaches c, and a and b hold nothing.
        cmd_space_table()
        # ``highlight-audience`` (v_70): a narrows today's highlight to b's
        # household — c deletes it and remembers the removal, a narrowed
        # frame reaches b only — then widens back and c gets the day again.
        # Before ``replay``: it ends at all_paired, which replay's frame
        # names.
        cmd_highlight_audience()
        # ``replay`` exercises the §24 outbox redelivery path by
        # killing Carol, posting a highlight from Alpha, restarting
        # Carol, and asserting the queued envelope flushes after the
        # backoff window. Runs last so the kill-restart cycle can't
        # destabilise the earlier topology assertions.
        cmd_replay()
        # ``unpair`` tears a↔c down from a's side and asserts BOTH
        # households dropped the pairing (a's signed UNPAIR reached c),
        # neither queues envelopes for the other, and shared space
        # membership is untouched — then re-pairs a↔c. Runs last: it
        # churns a pairing every earlier step relies on.
        cmd_unpair()
        # ``unpair-offline`` does the same with c DOWN: a keeps an
        # ``unpairing`` tombstone + queued UNPAIR, and c still drops a once
        # it restarts. Then re-pairs a↔c.
        cmd_unpair_offline()
        # ``local-community`` (#1057): who gets into a local community —
        # paired, mesh-only, outside, edge and GFS-only (§D2b) joiners —
        # and that the ~1 km join point is never stored. Its GFS case needs
        # the GFS, so it runs last, after ``gfs-up`` + ``gfs-pair``.
        cmd_gfs_up()
        cmd_gfs_pair()
        cmd_local_community()
        # ``gfs-moment-profile``: e (paired with nobody) follows a's user's
        # public moments through the GFS and sees them by name, picture
        # and bio — from the directory and the signed envelope.
        cmd_gfs_moment_profile()
        # ``gfs-highlight-link``: a public highlight link opens the pinned
        # viewer (a page no GFS serves), the GFS never sees its secret and
        # answers CORS on the anonymous viewer routes only.
        cmd_gfs_highlight_link()
        # ``gfs-public-pages``: the GFS's space page and Noor's profile open
        # in the pinned viewer (preview + refresh, all in the fragment) and its
        # JSON answers cross-origin.
        cmd_gfs_public_pages()
        # ``gfs-moment-late-delete`` (v_66): a public reply's delete leaves its
        # signed certificate on the GFS (no content) and e drops the reply.
        cmd_gfs_moment_late_delete()
        # The closing check: every step's state still holds, and the log
        # audit is clean — offline steps' lines only inside their windows.
        cmd_verify()
        return
    fn = globals().get(f"cmd_{cmd.replace('-', '_')}")
    if fn is None:
        raise SystemExit(f"unknown subcommand: {cmd!r}")
    try:
        fn()
    finally:
        # An offline step's windows close OFFLINE_WINDOW_TAIL_S after it.
        for t in _NOISE_TAILS:
            t.join()


if __name__ == "__main__":
    main()
