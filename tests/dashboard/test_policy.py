"""Tests for the dashboard Content-Security-Policy builder."""

from __future__ import annotations

from socialhome.csp import style_hash
from socialhome.dashboard import app as dashboard_app
from socialhome.dashboard.policy import DASHBOARD_CSP_DIRECTIVES, build_dashboard_csp


def test_no_unsafe_inline_and_same_origin_only():
    csp = build_dashboard_csp()
    assert "'unsafe-inline'" not in csp and "'unsafe-eval'" not in csp
    assert "script-src 'self';" in csp
    assert "style-src 'self';" in csp
    assert "frame-ancestors 'none'" in csp
    assert "http" not in csp


def test_inline_styles_are_admitted_by_exact_hash_once():
    css = ".k0{--kc:#357bbb;--kt:#ffffff}"
    csp = build_dashboard_csp([css, css])
    assert f"style-src 'self' {style_hash(css)};" in csp
    assert csp.count("'sha256-") == 1


def test_app_re_exports_the_policy():
    assert dashboard_app.build_dashboard_csp is build_dashboard_csp
    assert dashboard_app.DASHBOARD_CSP_DIRECTIVES is DASHBOARD_CSP_DIRECTIVES
