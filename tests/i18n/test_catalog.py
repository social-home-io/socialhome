"""Tests for the i18n Catalog (gettext-style lookup)."""

from __future__ import annotations

import json


from social_home.i18n import Catalog


# ─── Construction ────────────────────────────────────────────────────────


def test_empty_catalog_returns_key_unchanged():
    c = Catalog()
    assert c.gettext("missing.key") == "missing.key"


def test_default_locale_is_en():
    assert Catalog().default_locale == "en"


def test_custom_default_locale():
    c = Catalog(default_locale="de")
    c.load_locale("de", {"hi": "Hallo"})
    assert c.gettext("hi") == "Hallo"


# ─── Lookup ──────────────────────────────────────────────────────────────


def test_load_locale_then_gettext():
    c = Catalog()
    c.load_locale("en", {"greet": "Hello, {name}"})
    assert c.gettext("greet", name="Pascal") == "Hello, Pascal"


def test_locale_specific_lookup():
    c = Catalog()
    c.load_locale("en", {"greet": "Hello"})
    c.load_locale("de", {"greet": "Hallo"})
    assert c.gettext("greet", locale="de") == "Hallo"
    assert c.gettext("greet", locale="en") == "Hello"


def test_locale_falls_back_to_default():
    c = Catalog()
    c.load_locale("en", {"greet": "Hello"})
    # No 'fr' loaded → falls back to en.
    assert c.gettext("greet", locale="fr") == "Hello"


def test_missing_key_returns_key():
    c = Catalog()
    c.load_locale("en", {})
    assert c.gettext("not.there") == "not.there"


def test_format_substitution_with_missing_arg_returns_raw():
    c = Catalog()
    c.load_locale("en", {"greet": "Hello, {name}"})
    # Missing 'name' should not crash — return raw msg.
    assert "Hello" in c.gettext("greet")


def test_loaded_locales_listed_in_sorted_order():
    c = Catalog()
    c.load_locale("zh", {})
    c.load_locale("de", {})
    c.load_locale("ar", {})
    locales = c.loaded_locales
    assert locales == sorted(locales)


# ─── from_directory ──────────────────────────────────────────────────────


def test_from_directory_loads_every_json(tmp_path):
    (tmp_path / "en.json").write_text(json.dumps({"greet": "Hello"}))
    (tmp_path / "de.json").write_text(json.dumps({"greet": "Hallo"}))
    c = Catalog.from_directory(tmp_path)
    assert c.gettext("greet", locale="en") == "Hello"
    assert c.gettext("greet", locale="de") == "Hallo"


def test_from_directory_handles_missing_dir(tmp_path):
    """No exception if the directory doesn't exist — empty catalog."""
    c = Catalog.from_directory(tmp_path / "no-such")
    assert c.gettext("anything") == "anything"


def test_from_directory_skips_invalid_json(tmp_path):
    (tmp_path / "broken.json").write_text("not json {")
    (tmp_path / "ok.json").write_text(json.dumps({"k": "v"}))
    c = Catalog.from_directory(tmp_path)
    assert c.gettext("k", locale="ok") == "v"
    # Broken file silently dropped, no entry for 'broken' locale.
    assert "broken" not in c.loaded_locales


def test_bundled_messages_loadable():
    """Sanity: the bundled en/de/fr files all parse."""
    from pathlib import Path
    import social_home

    base = Path(social_home.__file__).parent / "i18n" / "messages"
    c = Catalog.from_directory(base)
    assert "en" in c.loaded_locales
    assert "de" in c.loaded_locales
    assert "fr" in c.loaded_locales
    # Pick a key present in all three.
    assert (
        c.gettext("notification.task.assigned", locale="en", title="X")
        != "notification.task.assigned"
    )
    assert (
        c.gettext("notification.task.assigned", locale="de", title="X")
        != "notification.task.assigned"
    )
    assert (
        c.gettext("notification.task.assigned", locale="fr", title="X")
        != "notification.task.assigned"
    )
