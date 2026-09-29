"""Tests for :mod:`socialhome.services.link_preview_html`."""

from __future__ import annotations

from socialhome.services.link_preview_html import (
    PageMeta,
    decode_html,
    extract_page_meta,
)

BASE = "https://example.com/articles/1"


def test_open_graph_wins() -> None:
    html = b"""<html><head>
    <title>HTML title</title>
    <meta property="og:title" content="OG &amp; title">
    <meta name="twitter:title" content="Twitter title">
    <meta property="og:description" content="OG desc">
    <meta name="description" content="Plain desc">
    <meta property="og:site_name" content="Example">
    <meta property="og:image" content="/img/card.png">
    <meta property="og:url" content="https://example.com/articles/1?utm=x">
    </head><body><meta property="og:title" content="late"></body></html>"""
    meta = extract_page_meta(html, "utf-8", BASE)
    assert meta == PageMeta(
        title="OG & title",
        description="OG desc",
        site_name="Example",
        image_url="https://example.com/img/card.png",
        canonical_url="https://example.com/articles/1?utm=x",
    )


def test_twitter_then_html_fallbacks() -> None:
    html = b"""<head><title> Plain  </title>
    <meta name="twitter:description" content="tw desc">
    <meta name="twitter:image" content="https://cdn.example/x.jpg">
    <link rel="canonical" href="/canon">
    </head>"""
    meta = extract_page_meta(html, None, BASE)
    assert meta.title == "Plain"
    assert meta.description == "tw desc"
    assert meta.image_url == "https://cdn.example/x.jpg"
    assert meta.canonical_url == "https://example.com/canon"
    assert meta.site_name is None


def test_plain_description_and_empty_values_skipped() -> None:
    html = b"""<head><meta property="og:title" content="  ">
    <meta name="description" content="Plain desc"><title>T</title></head>"""
    meta = extract_page_meta(html, None, BASE)
    assert meta.title == "T"
    assert meta.description == "Plain desc"


def test_stops_at_body() -> None:
    html = b"<html><body><title>in body</title><meta property='og:title' content='x'>"
    meta = extract_page_meta(html, None, BASE)
    assert meta.title is None
    assert meta.description is None


def test_no_head_at_all() -> None:
    assert extract_page_meta(b"not html at all", None, BASE) == PageMeta()


def test_charset_from_header_meta_and_fallback() -> None:
    latin = "<title>Caf\xe9</title>".encode("latin-1")
    assert decode_html(latin, "iso-8859-1") == "<title>Café</title>"
    sniffed = b'<meta charset="iso-8859-1"><title>Caf\xe9</title>'
    assert "Café" in decode_html(sniffed, None)
    # unknown header charset → falls through to utf-8 replace
    assert decode_html(b"ok \xff", "no-such-charset") == "ok �"
    meta = extract_page_meta(sniffed, None, BASE)
    assert meta.title == "Café"


def test_script_is_not_parsed_as_meta() -> None:
    html = b"""<head><script>var s = '<meta property="og:title" content="evil">';</script>
    <title>Real</title></head>"""
    meta = extract_page_meta(html, None, BASE)
    assert meta.title == "Real"
