"""Regression tests for the federation-demo log-audit helpers.

These pin the block-splitter + benign-needle invariants that
``cmd_verify`` uses to assert all four backends finished a clean run.
Two real bugs surfaced here:

1. ``_looks_like_exc_summary`` mis-classified standard Python logging
   records (``ERROR:logger:msg``) as exception-tail continuations,
   so the whole log collapsed into one big block and the audit
   couldn't see anything individually.
2. Several known-benign warning patterns (DTLS handshake timeouts on
   loopback, outbox terminal HTTP 410 drops, HTTPS-inbox transient
   failures during the inner-ring settle window) weren't in
   ``_LOG_BENIGN`` so they would have been (mis-)reported as audit
   failures if the splitter had been working.

The tests load the audit helpers directly from
``.claude/skills/federation-demo/harness.py`` — they're standalone
functions with no demo-runtime dependencies."""

from __future__ import annotations

import importlib.util
import ipaddress
import sys
from pathlib import Path

import pytest

from socialhome import outbound_fetch, peer_url
from socialhome.domain import gfs_server

# Load the harness module by path — it lives outside the ``socialhome``
# package and pytest's import machinery doesn't pick it up via normal
# imports.
HARNESS_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / ".claude"
    / "skills"
    / "federation-demo"
    / "harness.py"
)


@pytest.fixture(scope="module")
def harness():
    spec = importlib.util.spec_from_file_location(
        "federation_demo_harness",
        str(HARNESS_PATH),
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["federation_demo_harness"] = mod
    spec.loader.exec_module(mod)
    return mod


# ── _looks_like_exc_summary ────────────────────────────────────────────


def test_exc_summary_recognises_bare_exception_lines(harness):
    """``ValueError: bad input`` style summaries fold onto the
    preceding traceback so the benign filter can scan them."""
    assert harness._looks_like_exc_summary("ValueError: bad input")
    assert harness._looks_like_exc_summary("KeyError: 'missing'")


def test_exc_summary_recognises_dotted_exception_lines(harness):
    """``mod.sub.MyError: detail`` — dotted forms are valid too
    (third-party library exceptions, e.g. ``sqlite3.IntegrityError``).
    """
    assert harness._looks_like_exc_summary("sqlite3.IntegrityError: FK fail")
    assert harness._looks_like_exc_summary("aiohttp.ClientError: timeout")


def test_exc_summary_rejects_logging_level_prefixes(harness):
    """REGRESSION: ``ERROR:logger:msg`` / ``WARNING:...`` etc. are
    Python logging record headers, not exception summaries.
    Pre-fix the splitter folded every ERROR / WARNING / INFO line
    into the FIRST block, leaving the audit blind to per-line
    warnings."""
    assert not harness._looks_like_exc_summary(
        "ERROR:aiolibdatachannel:rtc::impl::DtlsTransport: DTLS handshake failed"
    )
    assert not harness._looks_like_exc_summary(
        "WARNING:socialhome.federation.transport:HTTPS-inbox send to x failed: "
    )
    assert not harness._looks_like_exc_summary("INFO:socialhome.app:startup complete")
    assert not harness._looks_like_exc_summary("CRITICAL:foo:bar")
    assert not harness._looks_like_exc_summary("DEBUG:trace:hi")


def test_exc_summary_rejects_lowercase_first_char(harness):
    """``value error: x`` shouldn't match — bare type names start
    capitalised by Python convention."""
    assert not harness._looks_like_exc_summary("value error: oops")


def test_exc_summary_rejects_lines_with_whitespace_in_head(harness):
    """``some message: detail`` — head has whitespace, not an
    exception summary."""
    assert not harness._looks_like_exc_summary("hello world: bar")


# ── _split_log_into_blocks ─────────────────────────────────────────────


def test_block_splitter_separates_consecutive_log_levels(harness):
    """REGRESSION: a stream of stdlib log records (``LEVEL:logger:msg``)
    must produce one block per line. Pre-fix every line after the
    first folded into block 0, so the audit could only ever see
    one block per file."""
    text = "INFO:socialhome.app:starting\nWARNING:socialhome.federation.transport:HTTPS-inbox send to x failed: \nERROR:aiolibdatachannel:rtc::impl::DtlsTransport: DTLS handshake failed\nINFO:socialhome.app:ready"
    blocks = harness._split_log_into_blocks(text)
    assert len(blocks) == 4
    assert blocks[0][0].startswith("INFO:socialhome.app:starting")
    assert "DTLS handshake failed" in blocks[2][0]


def test_block_splitter_keeps_traceback_frames_attached(harness):
    """A genuine Python traceback should fold its indented ``File "…"``
    frames + final exception summary into one block, so the benign
    filter can suppress the entire trace when its tail names a
    known cause."""
    text = "INFO:setup:ready\nTraceback (most recent call last):\n  File \"x.py\", line 1, in <module>\n    raise ValueError('boom')\nValueError: boom\nINFO:setup:continued"
    blocks = harness._split_log_into_blocks(text)
    # 3 blocks: INFO setup ready, the Traceback block (5 lines),
    # INFO setup continued.
    assert len(blocks) == 3
    trace_block_body = blocks[1][1]
    assert "Traceback" in trace_block_body
    assert "ValueError: boom" in trace_block_body
    assert 'File "x.py"' in trace_block_body


# ── _LOG_BENIGN coverage ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "line",
    [
        # Outbox terminal-drop on HTTP 410 — by design when
        # dual-transport delivery hits the receiver's replay cache on
        # the second arrival.
        "WARNING:socialhome.app:outbox: peer-abc returned terminal HTTP 410 for msg — dropping",
        "WARNING:socialhome.infrastructure.outbox_processor:OutboxProcessor: peer permanently rejected msg-xyz — dropping",
        # Transient HTTPS-inbox failure during the inner-ring settle
        # window. The trailing empty error (``failed: ``) is what
        # ``str(aiohttp.ClientConnectorError)`` produces in some cases —
        # cosmetic stdlib quirk.
        "WARNING:socialhome.federation.transport:HTTPS-inbox send to peer-abc failed: ",
        # DTLS handshake aborts on loopback when perfect-negotiation
        # glare resolution kills one side mid-handshake. Federation
        # falls through to HTTPS-inbox; per-step content checks
        # validate the actual delivery.
        "ERROR:aiolibdatachannel:rtc::impl::DtlsTransport::doRecv@993: DTLS recv: Handshake timeout",
        "ERROR:aiolibdatachannel:rtc::impl::DtlsTransport::doRecv@1001: DTLS handshake failed",
        # ICE candidate dropped after 30 s buffer window because the
        # remote SDP never arrived (glare aborted one side).
        "WARNING:socialhome.federation.transport:fed RTC: ICE candidate for peer-abc dropped — remote description not applied within 30s",
    ],
)
def test_known_benign_lines_are_in_allowlist(harness, line):
    """Each pattern above is documented in ``_LOG_BENIGN`` with the
    reason it's benign. A regression here means either the line
    text changed (update the allowlist + comment) or the underlying
    behaviour changed (might be a real bug — investigate the
    surface)."""
    assert any(needle in line for needle in harness._LOG_BENIGN), (
        f"line not matched by any _LOG_BENIGN needle: {line!r}"
    )


def test_dtls_block_is_correctly_classified_as_benign(harness):
    """End-to-end: a single-line DTLS error block flows through the
    splitter + audit suppression filter cleanly, even though the
    block_text is just the one line (no indented frames)."""
    text = "INFO:setup:ready\nERROR:aiolibdatachannel:rtc::impl::DtlsTransport::doRecv@1001: DTLS handshake failed\nINFO:setup:continued"
    blocks = harness._split_log_into_blocks(text)
    dtls = next(b for b in blocks if "DTLS handshake failed" in b[0])
    # Header is interesting (ERROR:) and body matches a benign needle.
    assert any(m in dtls[0] for m in harness._LOG_INTERESTING)
    assert any(n in dtls[1] for n in harness._LOG_BENIGN)


# ── v_57 gfs-multi-listing ────────────────────────────────────────────


def test_gfs_b_port_is_free_of_the_other_demo_servers(harness):
    assert harness.GFS_B_PORT not in (harness.GFS_PORT, *harness.GFS_CLUSTER_PORTS)


def test_loopback_shim_treats_only_loopback_as_public(harness, monkeypatch):
    """The harness-only prelude d runs under: 127.0.0.1 counts as public
    for the owner's-list filter and the pinned session, nothing else moves,
    and ``validate_peer_url`` (which keeps B's plain-http URL valid) is left
    alone. Executed without its final ``runpy`` line."""
    monkeypatch.setattr(
        outbound_fetch, "is_public_address", outbound_fetch.is_public_address
    )
    monkeypatch.setattr(gfs_server, "is_private_host", gfs_server.is_private_host)
    prelude, _, tail = harness._LOOPBACK_IS_PUBLIC_PRELUDE.rpartition("import runpy")
    assert "run_module('socialhome'" in tail
    exec(compile(prelude, "<prelude>", "exec"), {})  # noqa: S102
    assert outbound_fetch.is_public_address(ipaddress.ip_address("127.0.0.1"))
    assert not outbound_fetch.is_public_address(ipaddress.ip_address("10.0.0.1"))
    assert not gfs_server.is_private_host("127.0.0.1")
    assert gfs_server.is_private_host("192.168.1.1")
    assert peer_url.is_private_host("127.0.0.1")
    assert "validate_peer_url" not in harness._LOOPBACK_IS_PUBLIC_PRELUDE


# ── v_57 gfs-private-channel-multi ────────────────────────────────────


def test_private_channel_multi_step_is_wired(harness):
    """The step dispatches by name and ``verify`` checks what it left."""
    assert callable(harness.cmd_gfs_private_channel_multi)
    assert callable(harness._check_private_channel_multi)


def test_private_channel_multi_check_flags_what_went_missing(
    harness, monkeypatch, tmp_path
):
    monkeypatch.setattr(harness, "GFS_B_DIR", tmp_path)
    (tmp_path / "gfs.db").write_bytes(b"nothing about it")
    answers = {
        "d": [('[{"inbox_url": "a"}]',)],  # only one server
        "e": [],  # the post is gone
    }
    monkeypatch.setattr(harness, "_rows", lambda label, sql, params=(): answers[label])
    state = {"gfs_private_channel_multi": {"space_id": "sp-x", "post_id": "p-1"}}
    failures: list[str] = []
    harness._check_private_channel_multi(state, failures)
    assert len(failures) == 2
    (tmp_path / "gfs.db").write_bytes(b"... sp-x ...")
    answers["d"] = [('[{"inbox_url": "a"}, {"inbox_url": "b"}]',)]
    answers["e"] = [(1,)]
    failures.clear()
    harness._check_private_channel_multi(state, failures)
    assert failures == ["GFS B: its database names the private space"]


def test_retry_after_honours_the_header_else_the_fallback(harness):
    assert harness._retry_after_s({"Retry-After": "7"}, 10.0) == 7.0
    assert harness._retry_after_s({"retry-after": "0"}, 10.0) == 1.0
    assert harness._retry_after_s({"Retry-After": "soon"}, 10.0) == 10.0
    assert harness._retry_after_s({}, 12.0) == 12.0


def test_as_patient_retries_the_same_call_on_429(harness, monkeypatch):
    answers = [
        (429, {"error": "x"}, {"Retry-After": "3"}),
        (429, {"error": "x"}, {}),
        (200, {"ok": True}, {}),
    ]
    calls: list[tuple[str, object]] = []
    slept: list[float] = []

    def fake(url, *, token=None, method="GET", body=None, timeout=15.0):
        calls.append((method, body))
        return answers.pop(0)

    monkeypatch.setattr(harness, "_request_with_headers", fake)
    monkeypatch.setattr(harness.time, "sleep", slept.append)
    state = {"instances": {"a": {"port": 1, "token": "t"}}}
    st, body = harness._as_patient(state, "a", "POST", "/api/pairing/confirm", {"k": 1})
    assert (st, body) == (200, {"ok": True})
    assert calls == [("POST", {"k": 1})] * 3
    assert slept == [3.0, 20.0]  # Retry-After, then the doubled backoff


def test_as_patient_gives_up_within_its_budget(harness, monkeypatch):
    monkeypatch.setattr(
        harness,
        "_request_with_headers",
        lambda *a, **k: (429, {"error": "x"}, {"Retry-After": "60"}),
    )
    monkeypatch.setattr(harness.time, "sleep", lambda s: None)
    state = {"instances": {"a": {"port": 1, "token": "t"}}}
    st, _ = harness._as_patient(state, "a", "POST", "/x", budget_s=30.0)
    assert st == 429


def test_a_failed_private_channel_multi_run_cleans_up(harness, monkeypatch):
    state = {
        "instances": {
            "a": {"pid": 1, "port": 1, "token": "t"},
            "d": {"pid": 2, "port": 2, "token": "t"},
        },
        "gfs": {"pairings": {}},
    }
    monkeypatch.setattr(harness, "_alive", lambda pid: pid == 2)
    spawned: list[str] = []
    monkeypatch.setattr(
        harness, "_spawn", lambda label, port: spawned.append(label) or 99
    )
    monkeypatch.setattr(harness, "_wait_ready", lambda port: {})
    monkeypatch.setattr(harness, "_save", lambda s: None)
    monkeypatch.setattr(
        harness,
        "_gfs_connection_to",
        lambda st, label, url: {"id": "c", "gfs_instance_id": "g", "status": "active"},
    )
    deleted: list[str] = []
    monkeypatch.setattr(
        harness,
        "_as",
        lambda st, label, method, path, body=None: (
            deleted.append(f"{method} {path}") or (200, {})
        ),
    )
    harness._undo_private_channel_multi(state, {"space_id": "sp-9"})
    assert spawned == ["a"]  # d was still running
    assert deleted == ["DELETE /api/spaces/sp-9"]
    assert state["gfs"]["pairings"]["e"]["id"] == "c"


# ── Expected-noise windows ─────────────────────────────────────────────

_C_IID = "hurw3o3ojni4b5ouy52sfaodssim4aws"
_ABANDON = (
    "WARNING:socialhome.federation.sync.space.provider:sync s1: abandoning "
    f"stream for space sp to {_C_IID} after 3 consecutive chunk failures"
)
_FORGED = (
    f"WARNING:svc:SPACE_CONFIG_CHANGED for sp-1 from {_C_IID}: authority "
    "signature did not verify against the space key — dropping"
)


@pytest.fixture
def demo_root(harness, tmp_path, monkeypatch):
    """A throwaway ``/tmp/sh-demo`` with households a (the witness) and c
    (the one a step takes offline)."""
    monkeypatch.setattr(harness, "ROOT", tmp_path)
    monkeypatch.setattr(harness, "NOISE_WINDOWS_PATH", tmp_path / "noise.json")
    monkeypatch.setattr(harness, "LOG_GENERATIONS_PATH", tmp_path / "gens.json")
    monkeypatch.setattr(harness, "STATE_PATH", tmp_path / "state.json")
    state = {
        "instances": {
            "a": {"instance_id": "a" * 32, "port": 18001, "pid": 1},
            "c": {"instance_id": _C_IID, "port": 18003, "pid": 2},
        }
    }
    harness._save(state)
    for label in ("a", "c"):
        (tmp_path / label).mkdir()
        harness._bump_log_generation(label)
        (tmp_path / label / "log.txt").write_text("INFO:x:boot\n")
    return state


def _append(harness, label, *lines):
    path = harness._instance_dir(label) / "log.txt"
    path.write_text(path.read_text() + "".join(f"{line}\n" for line in lines))


def _audit_a(harness, state):
    return harness._audit_logs({"instances": {"a": state["instances"]["a"]}})


def test_offline_noise_outside_any_window_is_a_finding(harness, demo_root):
    _append(harness, "a", _ABANDON)
    assert _audit_a(harness, demo_root)


def test_offline_noise_naming_the_household_inside_its_window_passes(
    harness, demo_root
):
    harness._open_offline_window("c")
    _append(
        harness, "a", _ABANDON, "WARNING:aiolibdatachannel:x: juice: Lost connectivity"
    )
    harness._bump_log_generation("c")  # c respawned within the step
    harness._close_noise_windows()
    assert _audit_a(harness, demo_root) == []


def test_offline_window_never_excuses_a_line_about_another_household(
    harness, demo_root
):
    """Precise, not blanket: inside c's window an abandon naming d fails."""
    harness._open_offline_window("c")
    _append(harness, "a", _ABANDON.replace(_C_IID, "d" * 32))
    harness._close_noise_windows()
    assert _audit_a(harness, demo_root)


def test_offline_window_never_excuses_other_warnings(harness, demo_root):
    harness._open_offline_window("c")
    _append(harness, "a", f"ERROR:socialhome.app:boom while talking to {_C_IID}")
    harness._close_noise_windows()
    assert _audit_a(harness, demo_root)


def test_offline_window_ends_with_the_step_once_the_household_is_back(
    harness, demo_root
):
    harness._open_offline_window("c")
    harness._bump_log_generation("c")
    harness._close_noise_windows()
    _append(harness, "a", _ABANDON)  # after the step ended
    assert _audit_a(harness, demo_root)


def test_offline_window_stays_open_while_the_household_is_still_down(
    harness, demo_root
):
    """A step that leaves c down: what its peers log about c afterwards is
    still expected."""
    harness._open_offline_window("c")
    harness._close_noise_windows()
    _append(harness, "a", _ABANDON)
    assert _audit_a(harness, demo_root) == []


def test_a_respawned_witness_voids_its_windows(harness, demo_root):
    """A new process truncates ``log.txt``: an old window's line numbers
    describe a file that no longer exists."""
    harness._open_offline_window("c")
    harness._bump_log_generation("a")
    (harness._instance_dir("a") / "log.txt").write_text(_ABANDON + "\n")
    assert _audit_a(harness, demo_root)


def test_negative_control_window_is_scoped_by_name_and_time(harness, demo_root):
    forged = harness._open_noise_window(
        about="c",
        reason="test",
        witnesses=("a",),
        named=("authority signature did not verify",),
        names=[f"for sp-1 from {_C_IID}"],
    )
    _append(harness, "a", _FORGED, _FORGED.replace("sp-1", "sp-2"))
    harness._close_noise_windows(forged)
    # sp-2 is not the control's space.
    assert len(_audit_a(harness, demo_root)) == 1
    _append(harness, "a", _FORGED)
    assert len(_audit_a(harness, demo_root)) == 2, "closed by id, stays closed"


def test_offline_step_closes_its_windows_after_the_tail(
    harness, demo_root, monkeypatch
):
    """The witnesses' last lines about an outage land just after the step
    (ICE consent expiry ~30 s after the kill): the window outlives the step
    by ``OFFLINE_WINDOW_TAIL_S``, then closes — and only the windows open
    when the step ended."""
    monkeypatch.setattr(harness, "OFFLINE_WINDOW_TAIL_S", 0.0)
    gate = harness.threading.Event()
    real_sleep = harness.time.sleep
    monkeypatch.setattr(harness.time, "sleep", lambda s: gate.wait(5) or real_sleep(0))

    @harness._offline_step
    def step():
        harness._open_offline_window("c")
        harness._bump_log_generation("c")  # back up within the step

    step()
    _append(harness, "a", _ABANDON)  # the tail: still expected
    later = harness._open_noise_window(
        about="c", reason="later", witnesses=("a",), named=("x",), names=["y"]
    )
    gate.set()
    for t in harness._NOISE_TAILS:
        t.join()
    harness._NOISE_TAILS.clear()
    assert _audit_a(harness, demo_root) == []
    _append(harness, "a", _ABANDON)  # after the tail closed
    assert len(_audit_a(harness, demo_root)) == 1
    [still_open] = [w for w in harness._load_noise_windows() if w["id"] in later]
    assert still_open["end"] is None, "a window opened after the step stays open"


def test_numbered_splitter_reports_header_lines(harness):
    text = 'INFO:a:x\nTraceback (most recent call last):\n  File "x"\nValueError: v\n\nWARNING:b:y'
    numbered = harness._split_log_into_numbered_blocks(text)
    assert [n for n, _, _ in numbered] == [0, 1, 5]
    assert harness._split_log_into_blocks(text) == [(h, b) for _, h, b in numbered]
