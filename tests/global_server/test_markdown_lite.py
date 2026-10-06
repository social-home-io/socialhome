"""Tests for the GFS minimal Markdown renderer — formatting + XSS safety."""

from __future__ import annotations

import pytest

from socialhome.global_server.markdown_lite import render_markdown


# ─── Empty / whitespace ──────────────────────────────────────────────────


@pytest.mark.parametrize("src", ["", None, "   ", "\n\n  \n"])
def test_empty_returns_empty(src):
    assert render_markdown(src) == ""


# ─── Formatting subset ───────────────────────────────────────────────────


def test_paragraphs_split_on_blank_line():
    out = render_markdown("first para\n\nsecond para")
    assert out == "<p>first para</p><p>second para</p>"


def test_single_newline_becomes_br():
    assert render_markdown("line one\nline two") == "<p>line one<br>line two</p>"


def test_headings():
    assert render_markdown("# Title") == "<h1>Title</h1>"
    assert render_markdown("### Small") == "<h3>Small</h3>"


def test_bold_and_italic_and_code():
    assert "<strong>hi</strong>" in render_markdown("**hi**")
    assert "<strong>hi</strong>" in render_markdown("__hi__")
    assert "<em>hi</em>" in render_markdown("*hi*")
    assert "<em>hi</em>" in render_markdown("_hi_")
    assert "<code>x = 1</code>" in render_markdown("`x = 1`")


def test_unordered_list():
    out = render_markdown("- one\n- two")
    assert out == "<ul><li>one</li><li>two</li></ul>"


def test_blockquote():
    out = render_markdown("> a quote")
    assert out == "<blockquote>a quote</blockquote>"


def test_safe_link_renders_anchor_with_rel_guard():
    out = render_markdown("[home](https://example.com)")
    assert '<a href="https://example.com"' in out
    assert 'rel="noopener noreferrer nofollow ugc"' in out
    assert 'target="_blank"' in out
    assert ">home</a>" in out


def test_mailto_link_allowed():
    out = render_markdown("[mail](mailto:a@b.com)")
    assert '<a href="mailto:a@b.com"' in out


# ─── XSS safety (the reason this module exists) ──────────────────────────


def test_script_tag_is_escaped_not_executed():
    out = render_markdown("<script>alert(1)</script>")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_raw_html_is_escaped():
    out = render_markdown('<img src=x onerror="alert(1)">')
    assert "<img" not in out
    assert "&lt;img" in out


def test_javascript_scheme_link_is_neutralised():
    out = render_markdown("[click](javascript:alert(1))")
    # No anchor emitted; left as inert escaped literal text.
    assert "<a " not in out
    assert "javascript:alert(1)" in out  # present only as plain text


def test_data_scheme_link_is_neutralised():
    out = render_markdown("[x](data:text/html,<script>alert(1)</script>)")
    assert "<a " not in out
    assert "&lt;script&gt;" in out


def test_link_cannot_break_out_of_href_attribute():
    # A quote in the URL must be escaped so it can't inject an attribute.
    out = render_markdown('[x](https://e.com"onmouseover=alert(1))')
    assert 'onmouseover=alert(1)"' not in out
    # The double-quote is escaped wherever the URL is emitted.
    assert '"' not in out.split("https://e.com")[1].split(">")[0] or "&quot;" in out


def test_heading_content_is_escaped():
    out = render_markdown("# <b>x</b>")
    assert "<h1>" in out
    assert "<b>" not in out
    assert "&lt;b&gt;" in out


def test_oversize_source_is_truncated():
    """Pathological metacharacter blobs are bounded so the render can't
    block the GFS event loop (super-linear inline passes)."""
    from socialhome.global_server.markdown_lite import MAX_SOURCE_CHARS

    out = render_markdown("*_`[" * (MAX_SOURCE_CHARS))  # 4× over the cap
    # Truncation marker present; output stays proportional to the cap, not
    # to the (much larger) input.
    assert "…" in out
    assert len(out) < MAX_SOURCE_CHARS * 6


def test_link_sends_no_referrer():
    """A click on a link in a public GFS page must not tell the target
    site which page (and so which space) the reader came from."""
    out = render_markdown("[home](https://example.com)")
    rel = out.split('rel="', 1)[1].split('"', 1)[0].split()
    assert "noreferrer" in rel
