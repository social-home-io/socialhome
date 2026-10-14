"""Tests for the dashboard's Jinja2 renderer + its i18n strings."""

from __future__ import annotations

import json
import re
from pathlib import Path
from string import Formatter
from unittest.mock import patch

import jinja2
import pytest

import socialhome.i18n as i18n_pkg
from socialhome.dashboard.clock import WallClock
from socialhome.dashboard.render import (
    STATIC_DIR,
    DashboardRenderer,
    load_static_assets,
    text_direction,
)
from socialhome.domain.household_dashboard import SUPPORTED_DASHBOARD_LOCALES
from socialhome.i18n import Catalog

MESSAGES = Path(i18n_pkg.__file__).parent / "messages"


@pytest.fixture(scope="module")
def renderer() -> DashboardRenderer:
    return DashboardRenderer(Catalog.from_directory(MESSAGES))


def _login_ctx(**over):
    ctx = {
        "household": "Home",
        "next_path": "/",
        "error": None,
        "keep": True,
        "show_form": True,
        "notice": None,
    }
    ctx.update(over)
    return ctx


def test_text_direction():
    assert text_direction("en") == "ltr"
    assert text_direction("de") == "ltr"
    assert text_direction("ar") == "rtl"
    assert text_direction("he") == "rtl"


def test_login_renders_form_in_locale(renderer):
    html = renderer.render("login.html.j2", locale="de", **_login_ctx())
    assert '<html lang="de" dir="ltr">' in html
    assert "Dashboard-Passwort" in html
    assert 'autocomplete="current-password"' in html
    assert 'name="keep"' in html and "checked" in html
    assert (
        '<link rel="stylesheet" href="/static/dashboard.css" '
        'referrerpolicy="no-referrer">'
    ) in html
    assert "<style" not in html  # no inline styles → no CSP hashes needed


def test_rtl_hook(renderer):
    html = renderer.render("login.html.j2", locale="ar", **_login_ctx())
    assert 'dir="rtl"' in html


def test_autoescape(renderer):
    html = renderer.render(
        "login.html.j2",
        locale="en",
        **_login_ctx(household="<script>x</script>", next_path='/a"b'),
    )
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;x&lt;/script&gt;" in html
    assert "/a&#34;b" in html


def test_keep_unchecked(renderer):
    html = renderer.render("login.html.j2", locale="en", **_login_ctx(keep=False))
    assert "checked" not in html


def test_not_set_up_hides_form(renderer):
    html = renderer.render(
        "login.html.j2",
        locale="en",
        **_login_ctx(error="The dashboard isn't set up yet.", show_form=False),
    )
    assert "<form" not in html
    assert "set up yet" in html


def test_not_found_page(renderer):
    html = renderer.render("not_found.html.j2", locale="en", household="Casa")
    assert "Casa" in html
    assert "This page isn&#39;t on the dashboard." in html
    assert 'href="/"' in html


def test_lock_button(renderer):
    html = renderer.render("_lock_button.html.j2", locale="de")
    assert '<form class="dash-lock-form" method="post" action="/lock">' in html
    assert 'aria-label="Bildschirm sperren"' in html
    assert 'class="dash-lock-button"' in html


def test_login_notice(renderer):
    html = renderer.render(
        "login.html.j2", locale="en", **_login_ctx(notice="Signed out")
    )
    assert '<p class="dash-notice" role="status">Signed out</p>' in html


def _clock(**over):
    data = {
        "time": "09:00",
        "date": "Friday, January 15",
        "day": "2027-01-15",
        "iso": "2027-01-15T09:00:00+01:00",
        "tz": "Europe/Berlin",
        "unix": 1_800_000_000,
    }
    data.update(over)
    return WallClock(**data)


def test_lock_screen(renderer):
    html = renderer.render(
        "lock.html.j2",
        locale="en",
        household="<b>Casa</b>",
        clock=_clock(),
        error=None,
    )
    assert "&lt;b&gt;Casa&lt;/b&gt;" in html
    assert (
        '<time class="dash-lock-time" datetime="2027-01-15T09:00:00+01:00" '
        'data-clock data-tz="Europe/Berlin" data-day="2027-01-15" '
        'data-now="1800000000">09:00</time>'
    ) in html
    assert "Friday, January 15" in html
    assert 'data-label-delete="Delete last digit"' in html
    assert 'data-label-submit="Unlock"' in html
    assert "Screen locked" in html
    assert 'role="alert"' not in html
    assert '<noscript><meta http-equiv="refresh" content="60;url=/"></noscript>' in html


def test_lock_screen_error_opens_the_keypad(renderer):
    html = renderer.render(
        "lock.html.j2",
        locale="fr",
        household="Casa",
        clock=_clock(),
        error="Code PIN incorrect",
        tries_left=None,
    )
    assert '<details class="dash-lock-pin" open>' in html
    assert '<p class="dash-error" role="alert">Code PIN incorrect</p>' in html
    assert "dash-lock-error" in html
    assert "autofocus" in html
    assert "dash-pin-tries" not in html


def test_lock_screen_says_how_many_tries_are_left(renderer):
    """Like the wall panel: "Tries left: n" in the same alert as "Wrong
    PIN", so a screen reader announces both."""
    html = renderer.render(
        "lock.html.j2",
        locale="en",
        household="Casa",
        clock=_clock(),
        error="Wrong PIN",
        tries_left="Tries left: 3",
    )
    assert (
        '<p class="dash-error" role="alert">Wrong PIN '
        '<span class="dash-pin-tries">Tries left: 3</span></p>'
    ) in html


def test_strict_undefined(renderer):
    with pytest.raises(jinja2.UndefinedError):
        renderer.render("not_found.html.j2", locale="en")


def test_gettext_with_format(renderer):
    assert renderer.gettext("dashboard.login.too_many_tries", "en", minutes=3) == (
        "Too many tries — try again in 3 min"
    )


def _dashboard_keys(locale: str) -> dict[str, str]:
    data = json.loads((MESSAGES / f"{locale}.json").read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if k.startswith("dashboard.")}


def _fields(msg: str) -> set[str]:
    return {f for _, f, _, _ in Formatter().parse(msg) if f}


def test_every_locale_has_the_dashboard_strings():
    en = _dashboard_keys("en")
    assert len(en) >= 10
    for locale in SUPPORTED_DASHBOARD_LOCALES:
        msgs = _dashboard_keys(locale)
        assert set(msgs) == set(en), locale
        for key, text in msgs.items():
            assert text.strip(), (locale, key)
            assert _fields(text) == _fields(en[key]), (locale, key)


def test_templates_never_reload_from_disk(renderer):
    """Rendering happens inside request handlers — no FS access there."""
    with (
        patch.object(
            jinja2.FileSystemLoader, "get_source", side_effect=AssertionError("disk")
        ),
        patch("os.path.getmtime", side_effect=AssertionError("stat")),
    ):
        for _ in range(2):
            renderer.render("not_found.html.j2", locale="en", household="H")


def test_lock_script_preloaded(renderer):
    js = renderer.static_asset("lock.js")
    assert js is not None
    assert js.content_type == "text/javascript"
    assert js.body == (STATIC_DIR / "lock.js").read_bytes()


def test_dashboard_script_preloaded_and_on_every_page(renderer):
    js = renderer.static_asset("dashboard.js")
    assert js is not None and js.content_type == "text/javascript"
    tag = '<script src="/static/dashboard.js" defer></script>'
    assert tag in renderer.render("login.html.j2", locale="en", **_login_ctx())
    assert tag in renderer.render("not_found.html.j2", locale="en", household="H")


def test_translator_binds_the_locale(renderer):
    t = renderer.translator("de")
    assert t("dashboard.lock.unlock") == "Entsperren"
    assert t("dashboard.login.too_many_tries", minutes=2).endswith("2 Min. noch einmal")


def test_static_assets_preloaded(renderer):
    css = renderer.static_asset("dashboard.css")
    assert css is not None
    assert css.content_type == "text/css"
    assert css.body == (STATIC_DIR / "dashboard.css").read_bytes()
    assert css.etag.startswith('"') and css.etag.endswith('"')
    assert renderer.static_asset("missing.css") is None
    assert renderer.static_asset("../render.py") is None


def test_load_static_assets_skips_dotfiles_and_dirs(tmp_path):
    (tmp_path / "a.css").write_text("a{}")
    (tmp_path / ".hidden").write_text("x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "blob.unknownext").write_bytes(b"\x00")
    assets = load_static_assets(tmp_path)
    assert set(assets) == {"a.css", "blob.unknownext"}
    assert assets["blob.unknownext"].content_type == "application/octet-stream"


def test_picture_clock_hands_turn_by_attribute_never_by_style(renderer):
    """The CSP forbids style attributes: the analog clock's hands turn
    through their SVG ``transform`` attribute, and no CSS rule may set a
    ``transform`` property on them (it would override the attribute)."""
    js = renderer.static_asset("dashboard.js").body.decode()
    assert 'el.setAttribute("transform", value)' in js
    assert ".style" not in js
    css = renderer.static_asset("dashboard.css").body.decode()
    for selector, body in re.findall(r"([^{}]+)\{([^}]*)\}", css):
        if "dash-pv-hand" in selector:
            assert "transform" not in body, selector
