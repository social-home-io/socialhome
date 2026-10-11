"""Tests for socialhome.access_log — query-string redaction."""

from __future__ import annotations

import logging

import aiohttp
import pytest
from aiohttp import web

from socialhome.access_log import (
    RedactingAccessLogger,
    redact_query_string,
    redact_secrets,
)


def test_redact_strips_token_value():
    out = redact_query_string("/api/ws?token=abc123xyz")
    assert out == "/api/ws?token=***"


def test_redact_preserves_path_and_other_params():
    out = redact_query_string("/api/ws?space_id=sp-1&token=secret&limit=10")
    assert out == "/api/ws?space_id=sp-1&token=***&limit=10"


def test_redact_request_line_shape():
    line = "GET /api/ws?token=abcd HTTP/1.1"
    assert redact_query_string(line) == "GET /api/ws?token=*** HTTP/1.1"


def test_redact_handles_api_key_and_access_token():
    assert redact_query_string("/x?api_key=foo").endswith("api_key=***")
    assert redact_query_string("/x?access_token=foo").endswith(
        "access_token=***",
    )


def test_redact_handles_password_value():
    assert redact_query_string("/x?password=hunter2").endswith("password=***")


def test_redact_preserves_lookalike_substrings():
    """``tokenize`` should NOT match the ``token`` param."""
    out = redact_query_string("/api/x?tokenize=maybe&token=real")
    assert "tokenize=maybe" in out
    assert "token=***" in out


def test_redact_noop_on_empty_input():
    assert redact_query_string("") == ""


def test_redact_noop_when_no_sensitive_params():
    assert redact_query_string("/api/x?q=hello") == "/api/x?q=hello"


def test_redact_case_insensitive_param_name():
    assert redact_query_string("/x?TOKEN=abc") == "/x?TOKEN=***"


def test_redact_percent_encoded_param_name():
    """``%74oken`` decodes to ``token`` server-side — redact it too."""
    out = redact_query_string("/api/ws?%74oken=abc123")
    assert out == "/api/ws?%74oken=***"
    out = redact_query_string("/x?a=1&%61pi%5Fkey=foo")
    assert out == "/x?a=1&%61pi%5Fkey=***"


def test_redact_does_not_touch_short_lookalike_params():
    """``tz`` / ``at`` / ``%74z`` must never be mistaken for a secret."""
    line = "/x?tz=Europe%2FZurich&at=2026-01-01&%74z=UTC"
    assert redact_query_string(line) == line


# ─── Bearer tokens carried in the URL *path* (GFS share / invite links) ───


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("GET /join/tok123 HTTP/1.1", "GET /join/*** HTTP/1.1"),
        ("/join/tok123?x=1", "/join/***?x=1"),
        (
            "GET /highlight/inst/hl-1/tok123 HTTP/1.1",
            "GET /highlight/inst/hl-1/*** HTTP/1.1",
        ),
        (
            "POST /gfs/highlight_tokens/tok123/revoke HTTP/1.1",
            "POST /gfs/highlight_tokens/***/revoke HTTP/1.1",
        ),
        (
            "DELETE /gfs/spaces/sp-1/invite/tok123 HTTP/1.1",
            "DELETE /gfs/spaces/sp-1/invite/*** HTTP/1.1",
        ),
        # Referer: a full URL, path anchored after the authority.
        ("https://gfs.example/join/tok123", "https://gfs.example/join/***"),
        (
            "https://gfs.example:8443/highlight/i/h/tok123#x",
            "https://gfs.example:8443/highlight/i/h/***#x",
        ),
    ],
)
def test_redact_path_bearer_tokens(line, expected):
    assert redact_secrets(line) == expected


@pytest.mark.parametrize(
    "line",
    [
        # No token segment at all.
        "GET /join HTTP/1.1",
        "GET /join/ HTTP/1.1",
        "POST /gfs/spaces/sp-1/invite HTTP/1.1",
        "GET /highlight/inst/hl HTTP/1.1",
        # Lookalike prefixes / non-root positions.
        "GET /joined/abc HTTP/1.1",
        "GET /api/spaces/join/abc HTTP/1.1",
        "GET /api/me/join-requests HTTP/1.1",
        "GET /gfs/highlights/hl-1/publish HTTP/1.1",
        "GET /gfs/highlight_rtc/relay/inst/hl HTTP/1.1",
        "GET /static/highlight/a/b/c.js HTTP/1.1",
        "GET /spaces/my-space HTTP/1.1",
    ],
)
def test_redact_path_leaves_non_token_paths_alone(line):
    assert redact_secrets(line) == line


def test_redact_path_and_query_together():
    line = "GET /join/tok123?token=abc HTTP/1.1"
    assert redact_secrets(line) == "GET /join/***?token=*** HTTP/1.1"


# ─── Integration: the logger actually wired into a running server ─────────


async def test_running_server_never_logs_secrets(caplog):
    """Real aiohttp server + ``access_log_class`` — mirrors ``run_app``.

    Asserts neither the query-string token nor the Referer token reaches
    the formatted message or any attribute aiohttp attaches via ``extra``.
    """
    app = web.Application()
    runner = web.AppRunner(app, access_log_class=RedactingAccessLogger)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    caplog.set_level(logging.INFO, logger="aiohttp.access")
    try:
        async with aiohttp.ClientSession() as session:
            for path in (
                "/api/ws?token=SECRET123",
                "/api/ws?space_id=sp-1&%74oken=SECRET456",
            ):
                async with session.get(
                    f"http://127.0.0.1:{port}{path}",
                    headers={"Referer": "http://x/?token=SECRETREF"},
                ) as resp:
                    assert resp.status == 404
    finally:
        await runner.cleanup()

    records = [r for r in caplog.records if r.name == "aiohttp.access"]
    assert len(records) == 2
    for record in records:
        blob = record.getMessage() + repr(vars(record))
        for secret in ("SECRET123", "SECRET456", "SECRETREF"):
            assert secret not in blob
        assert "/api/ws?" in record.getMessage()
        assert record.first_request_line.startswith("GET /api/ws?")
        assert record.request_header["Referer"] == "http://x/?token=***"


def test_redact_dashboard_setup_token_param():
    """The household dashboard's ``/enter?t=<setup token>`` link."""
    assert redact_query_string("GET /enter?t=s3cr3t HTTP/1.1") == (
        "GET /enter?t=*** HTTP/1.1"
    )
    assert redact_query_string("/x?at=1&tz=UTC") == "/x?at=1&tz=UTC"
