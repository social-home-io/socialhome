"""Unit tests for :mod:`socialhome.global_server.html_page`."""

from __future__ import annotations

import pytest

from socialhome.csp import build_public_page_csp, style_hash
from socialhome.global_server.html_page import css_color, html_response


@pytest.mark.parametrize("value", ["#abc", "#ABCD", "#a1b2c3", "#a1b2c3d4"])
def test_css_color_accepts_hex(value):
    assert css_color(value, "#000000") == value


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "red",
        "#12",
        "#12345",
        "#1234567",
        "#123456;}body{x:y}",
        "#123456\n",
        "url(https://evil.example)",
        "expression(alert(1))",
    ],
)
def test_css_color_falls_back_for_anything_else(value):
    assert css_color(value, "#D2542A") == "#D2542A"


def test_html_response_carries_the_public_page_csp():
    css = "body{color:red}"
    resp = html_response(f"<style>{css}</style>", inline_styles=[css], status=404)
    assert resp.status == 404
    assert resp.content_type == "text/html"
    header = resp.headers["Content-Security-Policy"]
    assert header == build_public_page_csp([css])
    assert style_hash(css) in header


def test_html_response_without_styles_admits_no_inline_style():
    header = html_response("<p>x</p>").headers["Content-Security-Policy"]
    assert header == build_public_page_csp()
    assert "sha256-" not in header
