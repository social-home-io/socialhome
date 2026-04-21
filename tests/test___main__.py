"""Tests for social_home.__main__ — verifies the module is importable."""

from __future__ import annotations

import importlib
import sys


def test_main_module_is_importable():
    """social_home.__main__ can be imported without side effects."""
    # Remove from cache if already imported to get a fresh import
    mod_name = "social_home.__main__"
    sys.modules.pop(mod_name, None)
    mod = importlib.import_module(mod_name)
    assert mod is not None


def test_main_module_has_expected_contents():
    """social_home.__main__ imports create_app and Config."""
    import social_home.__main__ as main_mod

    # The module references create_app and Config at module level
    assert hasattr(main_mod, "create_app")
    assert hasattr(main_mod, "Config")
