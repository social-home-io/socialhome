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

import socialhome.domain.gfs_server as gfs_server
import socialhome.outbound_fetch as outbound_fetch
import socialhome.peer_url as peer_url

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
    text = "\n".join(
        [
            "INFO:socialhome.app:starting",
            "WARNING:socialhome.federation.transport:HTTPS-inbox send to x failed: ",
            "ERROR:aiolibdatachannel:rtc::impl::DtlsTransport: DTLS handshake failed",
            "INFO:socialhome.app:ready",
        ]
    )
    blocks = harness._split_log_into_blocks(text)
    assert len(blocks) == 4
    assert blocks[0][0].startswith("INFO:socialhome.app:starting")
    assert "DTLS handshake failed" in blocks[2][0]


def test_block_splitter_keeps_traceback_frames_attached(harness):
    """A genuine Python traceback should fold its indented ``File "…"``
    frames + final exception summary into one block, so the benign
    filter can suppress the entire trace when its tail names a
    known cause."""
    text = "\n".join(
        [
            "INFO:setup:ready",
            "Traceback (most recent call last):",
            '  File "x.py", line 1, in <module>',
            "    raise ValueError('boom')",
            "ValueError: boom",
            "INFO:setup:continued",
        ]
    )
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
    text = "\n".join(
        [
            "INFO:setup:ready",
            "ERROR:aiolibdatachannel:rtc::impl::DtlsTransport::doRecv@1001: DTLS handshake failed",
            "INFO:setup:continued",
        ]
    )
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
