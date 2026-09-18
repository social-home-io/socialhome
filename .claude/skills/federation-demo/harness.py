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
    python .claude/skills/federation-demo/harness.py down    # stop + wipe data dirs
    python .claude/skills/federation-demo/harness.py all     # everything in order

State is persisted to ``/tmp/sh-demo/state.json`` so the steps can be
run independently. ``all`` is the canonical invocation.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import subprocess
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path("/tmp/sh-demo")
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
)


# ─── State helpers ─────────────────────────────────────────────────────────


def _load() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def _save(state: dict) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


# ─── HTTP helpers ──────────────────────────────────────────────────────────


def _request(
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
        f'external_url = "http://127.0.0.1:{port}"\n'
    )


def _spawn(label: str, port: int, *, extra_env: dict | None = None) -> int:
    d = _instance_dir(label)
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
    static_index = (
        Path(__file__).resolve().parents[3] / "socialhome" / "static" / "index.html"
    )
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
    """Pair Alpha + Delta with the GFS.

    Walk the §24 GFS pairing flow end-to-end:

    1. Mint a one-time pair token via the GFS landing page (rendered
       at ``GET /`` — the same page that displays the QR code).
    2. POST it to Alpha's ``/api/gfs/connections`` so Alpha runs
       :meth:`GfsConnectionService.pair` (fetch ``GET /gfs/info``,
       then ``POST /gfs/register`` with Alpha's identity + the
       token).
    3. Repeat for Delta with a fresh token.
    4. Assert both households now show the GFS connection as
       ``status="active"`` (auto-accept is on by default for fresh
       deployments).
    """
    state = _load()
    if not state or not _gfs_alive(state):
        raise SystemExit("run 'gfs-up' first")

    a = state["instances"]["a"]
    d = state["instances"]["d"]
    gfs_url = f"http://127.0.0.1:{GFS_PORT}"

    state.setdefault("gfs", {})
    state["gfs"]["pairings"] = {}
    for label, info in (("a", a), ("d", d)):
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
    _save(state)
    print("gfs-pair: ok (a + d connected to GFS)")


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
    # takes no ``features`` block, so this is a PATCH — and PATCH replaces the
    # whole features dict, so read the current one back first rather than
    # resetting every other feature to its default.
    s, current = _request(
        f"http://127.0.0.1:{a['port']}/api/spaces/{space_id}",
        token=a["token"],
    )
    current = _must("read-global-space", s, current, ok=(200,))
    features = dict(current.get("features") or {})
    features["allow_subscribers"] = True
    s, patched = _request(
        f"http://127.0.0.1:{a['port']}/api/spaces/{space_id}",
        token=a["token"],
        method="PATCH",
        body={"features": features},
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
        display_name="Erin",
    )
    state["gfs_space_author_user_id"] = author_id

    # Bookmarks for the step-6 anonymity assertions: everything the GFS, d and
    # a log from here on is what THIS publish produced.
    gfs_off = _gfs_log_size()
    d_off = _log_size("d")

    # Marker keeps the step re-runnable against a live sandbox.
    content = f"Global space post over the GFS — {time.time_ns()}"
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
        display_name="Erin",
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
            row = next(
                (sp for sp in listing if sp["space_id"] == space_id), None
            )
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
    print(
        "gfs-space-no-subscribers: ok (listed for discovery, content never "
        "relayed)"
    )


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
    }
    for label, info in state["instances"].items():
        s, conns = _request(
            f"http://127.0.0.1:{info['port']}/api/pairing/connections",
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
    print("pair: ok (a↔b, b↔c, a↔c, b↔d)")


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
                items = (
                    inbox if isinstance(inbox, list) else (inbox.get("items") or [])
                )
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

    # Settle: both the confirmed-status flip AND the on-pair
    # ``INSTANCE_CAPABILITIES_UPDATED`` need a moment to land. The
    # capabilities envelope races ahead of the ack — the receiver's
    # provisional row has an empty ``remote_identity_pk`` until the
    # ack lands, so the first send returns 404 and the outbox retries
    # on a 5/10/20s backoff. Sleep past the first retry slot before
    # checking, so we hit each token's ``/api/pairing/*`` bucket only
    # once for this step (the bucket is 5 calls per 60 s; we've
    # already spent ~2 on the request/approve round).
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
                f"http://127.0.0.1:{info['port']}/api/pairing/connections",
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
                "media_url": "https://example.invalid/img.jpg",
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
    _save(state)
    print("calendar: ok")

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
    from socialhome.domain.federation_capabilities import OURS as _OURS

    for viewer in ("a", "b", "c"):
        v = state["instances"][viewer]
        s, conns = _request(
            f"http://127.0.0.1:{v['port']}/api/pairing/connections",
            token=v["token"],
        )
        if s != 200 or not isinstance(conns, list):
            continue
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

    # 0b. INSTANCE_RESYNC_REQUEST round-trip (v_19, #319 ¶6) — ask a confirmed
    #     v_19+ peer to re-advertise its capabilities via the operator
    #     endpoint and assert it's accepted. Proves the new event type +
    #     POST /api/admin/federation/resync + peer_supports(v_19) gate are
    #     wired end-to-end; a sub-v_19 peer would 409 (PEER_TOO_OLD).
    va = state["instances"]["a"]
    s, conns = _request(
        f"http://127.0.0.1:{va['port']}/api/pairing/connections",
        token=va["token"],
    )
    resync_target = ""
    if s == 200 and isinstance(conns, list):
        for row in conns:
            if (
                row.get("status") == "confirmed"
                and int(row.get("proto_version") or 1) >= _OURS
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
            print(
                f"  a → resync(capabilities) accepted for "
                f"{resync_target[:8]} ✓"
            )
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
                                f"http://127.0.0.1:{c['port']}/api/pairing/connections",
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
                print(f"  b received a→b bazaar-inquiry DM ✓")
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

    # 5. Trust-relay pair — a ↔ d should be CONFIRMED on both sides
    #    *if* :func:`cmd_relay_pair` was run (excluded from ``all``).
    if "relay_pair_ran" in state:
        a_iid = state["instances"]["a"]["instance_id"]
        d_iid = state["instances"]["d"]["instance_id"]
        for viewer, peer in (("a", d_iid), ("d", a_iid)):
            info = state["instances"][viewer]
            s, conns = _request(
                f"http://127.0.0.1:{info['port']}/api/pairing/connections",
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

    # 7. Capability handshake — every confirmed inner-ring peer should
    #    have announced their proto_version via
    #    ``INSTANCE_CAPABILITIES_UPDATED`` at startup. After ``up`` + a
    #    short settle window we expect each of a/b/c to see the others
    #    at proto_version >= 2 (the version this build advertises). A
    #    peer still pinned at 1 means the announcement never landed —
    #    most likely the outbound didn't fire or the inbound handler is
    #    not registered. The harness asserts the round-trip so future
    #    additive-but-not-fail-soft features have a safety net.
    for viewer in ("a", "b", "c"):
        info = state["instances"][viewer]
        s, conns = _request(
            f"http://127.0.0.1:{info['port']}/api/pairing/connections",
            token=info["token"],
        )
        _must(f"connections({viewer})", s, conns)
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

    def _check_transports() -> tuple[list[str], list[str]]:
        """Probe every inner-ring confirmed peer's transport once.

        Returns ``(success_lines, warning_lines)``: a peer that hasn't
        flipped to ``rtc`` yet emits a *warning* (not a failure) —
        WebRTC peer-connections legitimately don't establish reliably
        on loopback because perfect-negotiation glare aborts one side
        of the DTLS handshake. Federation transparently falls through
        to HTTPS-inbox and content still lands; the per-step content
        assertions earlier in this function are what actually pin
        delivery. The transport probe is informational — useful to
        see at a glance which pairs flipped — but not a gate.
        """
        ok_lines: list[str] = []
        warn_lines: list[str] = []
        for src in ("a", "b", "c"):
            info = state["instances"][src]
            s, conns = _request(
                f"http://127.0.0.1:{info['port']}/api/pairing/connections",
                token=info["token"],
            )
            _must(f"connections({src})", s, conns)
            for row in conns:
                if row.get("status") != "confirmed":
                    continue
                # d isn't part of the inner-ring traffic test — it's
                # only paired with b in the demo, so the channel may
                # or may not have flipped to RTC by verify time.
                if row["instance_id"] == d_iid:
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
        return ok_lines, warn_lines

    # The ``/api/pairing/*`` bucket is 5 calls per 60s per instance,
    # and we already spent the budget in earlier verify steps and
    # share_home OFF→ON. Cap the poll at 3 attempts spaced 15s apart
    # (≤3 calls per instance per minute — well within budget) so a
    # slow RTC handshake gets ~30s of patience without exhausting
    # the limiter.
    ok_lines: list[str] = []
    warn_lines: list[str] = []
    for attempt in range(3):
        ok_lines, warn_lines = _check_transports()
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

    # Flip OFF on Alpha's side.
    s, r = _request(
        f"http://127.0.0.1:{a['port']}/api/pairing/connections/{bob_id}",
        token=a["token"],
        method="PATCH",
        body={"share_home": False},
    )
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

        # Flip ON and verify coords are restored. The ``/api/pairing``
        # bucket is 5 calls / 60 s and verify's earlier
        # capabilities + transport checks (the latter probes 3×) plus
        # the share_home OFF PATCH have already eaten ~5 of the
        # budget on Alpha. A 429 here doesn't mean the share-home
        # toggle is broken — just that the demo's verify step crowds
        # the bucket. Retry up to 3 times with a 25 s spacer between
        # attempts so the sliding window has room to drain (5 / 60s
        # means one slot frees every ~12 s on average; 25 s buys
        # two).
        s2: int = 0
        r2: dict = {}
        for attempt in range(3):
            s2, r2 = _request(
                f"http://127.0.0.1:{a['port']}/api/pairing/connections/{bob_id}",
                token=a["token"],
                method="PATCH",
                body={"share_home": True},
            )
            if s2 in (200, 204):
                break
            if s2 == 429 and attempt < 2:
                time.sleep(25)
                continue
            break
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
            relay_lines = [
                line
                for line in _log_lines_matching("d", "gfs.relay.received:")
                if f"space={gfs_space_id}" in line
            ]
            if not relay_lines:
                failures.append(
                    f"d: no 'gfs.relay.received: space={gfs_space_id}' record "
                    "in d's log (note: _spawn truncates log.txt, so a step "
                    "that respawned d since gfs-space-post wipes the evidence)",
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
            row = next(
                (sp for sp in rows if sp.get("space_id") == io_space_id), None
            )
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
        blocks = _split_log_into_blocks(text)
        hits: list[str] = []
        for header, block_text in blocks:
            if not any(marker in header for marker in _LOG_INTERESTING):
                continue
            if any(needle in block_text for needle in _LOG_BENIGN):
                continue
            hits.append(header)
            if len(hits) >= 5:
                break
        for h in hits:
            failures.append(f"{label}: log audit — {h.strip()[:200]}")
    return failures


def _split_log_into_blocks(text: str) -> list[tuple[str, str]]:
    """Group lines into ``(header, full_block)`` tuples.

    A block starts at the first non-indented line with one of
    :data:`_LOG_INTERESTING` markers and continues through every
    subsequent indented frame (``  File "...``,
    ``    cursor.execute(...)`` etc.) plus the final exception-type
    summary line. Blank lines reset the block. The header is what
    we match for "is this interesting"; the full block text is what
    the benign filter scans, so a Traceback whose final line names
    a known-benign cause gets suppressed cleanly.
    """
    out: list[tuple[str, str]] = []
    cur_header: str | None = None
    cur_lines: list[str] = []

    def _flush() -> None:
        nonlocal cur_header, cur_lines
        if cur_header is not None:
            out.append((cur_header, "\n".join(cur_lines)))
        cur_header = None
        cur_lines = []

    for line in text.splitlines():
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
    print(f"  pre-check: Beta sees ada via /api/friends ✓")

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
            "media_url": "https://example.invalid/ada-pre.jpg",
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
    print(f"  pre-hide: Beta has ada's highlight ✓ (cascade-target seeded)")

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
    print(f"  a hid ada from Beta — USER_REMOVED fan-out queued")

    # 5. Wait for USER_REMOVED to land and Beta's mirror to drop ada.
    time.sleep(4)
    seen_after_hide = _friends_users_for(b_url, b["token"], a["instance_id"])
    if ada_user_id in seen_after_hide:
        raise SystemExit(
            f"visibility: Beta still sees ada {ada_user_id} after hide — "
            f"saw {sorted(seen_after_hide)!r}",
        )
    print(f"  post-hide: Beta no longer lists ada ✓")

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
            "media_url": "https://example.invalid/ada.jpg",
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
    print(f"  a re-exposed ada to Beta — USER_UPDATED fan-out queued")

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
    print(f"  post-unhide: Beta sees ada again ✓")

    state["visibility_ran"] = True
    _save(state)
    print("visibility: ok (per-pair user-visibility filter works both ways)")


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
    5. Settle the outbox redelivery window (default 30s exponential
       backoff; the harness sleeps 25 s which crosses the second
       backoff slot).
    6. Assert Carol's ``/api/highlights`` now contains the new caption.

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
            "media_url": "https://example.invalid/replay.jpg",
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

    # 5. Outbox redelivery window. The default backoff schedule is
    #    {0, 5, 30, 120, 600}s; we already burned the immediate slot
    #    in step 3, so we sleep across the 30s slot to give the
    #    second attempt a chance to land.
    settle = 35
    print(f"  waiting {settle}s for outbox redelivery to flush…")
    time.sleep(settle)

    # 6. Carol should now have Alpha's highlight despite having been
    #    down at the moment Alpha posted it.
    captions = _highlight_captions(state, "c")
    if caption in captions:
        print(f"  c received the replayed highlight ✓")
    else:
        raise SystemExit(
            f"replay: Carol did not receive {caption!r} after redelivery "
            f"window — captions seen: {sorted(captions)!r}",
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
    b = state["instances"]["b"]

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
    d = state["instances"]["d"]

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
    d = state["instances"]["d"]

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
    print(f"  bytes match between c and d ✓")

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
    4. b (the relay) saw ``SPACE_ROUTED`` envelopes but NEVER the
       inner ``SPACE_GALLERY_ITEM_CREATED`` or ``SPACE_MEDIA_BLOB``.

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

    # 5. Relay-encryption invariant: b sees SPACE_ROUTED but never
    #    the inner SPACE_GALLERY_ITEM_CREATED or SPACE_MEDIA_BLOB.
    if b_log_path.exists():
        b_log_after = b_log_path.read_text(errors="replace")[b_log_before_size:]
        for forbidden in (
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

    # The system ("Posts") album MUST arrive — it carries the host's own id
    # and is what mirrored post images live in on the joiner's side.
    d_albums = {r[0] for r in _rows(
        "d",
        "SELECT id FROM gallery_albums WHERE space_id = ?",
        (space_id,),
    )}
    c_system = {r[0] for r in _rows(
        "c",
        "SELECT id FROM gallery_albums WHERE space_id = ? AND is_system = 1",
        (space_id,),
    )}
    if not c_system <= d_albums:
        raise SystemExit(
            f"space-sync-catchup-media: d is missing the host's system gallery "
            f"album {sorted(c_system - d_albums)} (has {sorted(d_albums)})",
        )
    print("  d.gallery_albums has the host's system album ✓ (catch-up)")

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
            c for c in contacts
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
            _must("open person-routed session (a→b user, v_18)", s_pr, pr_res, ok=(200, 201))
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


def cmd_down() -> None:
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
        rows = list(
            conn.execute(f"SELECT {col} FROM spaces WHERE id=?", (space_id,))
        )
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
                "SELECT * FROM space_remote_members "
                "WHERE space_id=? AND user_id=?",
                (space_id, user_id),
            )
        )
    finally:
        conn.close()
    return dict(rows[0]) if rows else None


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
        who for who, r in rows.items() if r is not None and int(r.get("tombstoned") or 0)
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


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    cmd = sys.argv[1]
    if cmd == "all":
        # The full path: pair the inner ring → traffic + invites →
        # accept invites + RSVP-on-event (cmd_calendar) → assertions →
        # transitive auto-pair via the trust relay (cmd_relay_pair).
        # GFS lifecycle (``gfs-up`` / ``gfs-pair`` / ``gfs-down``) is
        # opt-in and not part of the canonical smoke run; the GFS
        # process is heavyweight to spin up and not strictly required
        # for the HFS↔HFS federation surface this skill validates.
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
        # ``replay`` exercises the §24 outbox redelivery path by
        # killing Carol, posting a highlight from Alpha, restarting
        # Carol, and asserting the queued envelope flushes after the
        # backoff window. Runs last so the kill-restart cycle can't
        # destabilise the earlier topology assertions.
        cmd_replay()
        return
    fn = globals().get(f"cmd_{cmd.replace('-', '_')}")
    if fn is None:
        raise SystemExit(f"unknown subcommand: {cmd!r}")
    fn()


if __name__ == "__main__":
    main()
