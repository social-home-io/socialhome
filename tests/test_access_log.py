"""Tests for socialhome.access_log — query-string redaction."""

from __future__ import annotations

from socialhome.access_log import redact_query_string


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
