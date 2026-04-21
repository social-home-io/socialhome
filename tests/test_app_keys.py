"""Tests for social_home.app_keys — AppKey instances are importable and unique."""

from __future__ import annotations

from aiohttp.web import AppKey


async def test_all_app_keys_are_importable():
    """Every name in app_keys that ends with _key is an AppKey instance."""
    import social_home.app_keys as K

    keys = [v for name, v in vars(K).items() if name.endswith("_key")]
    assert len(keys) > 0, "no AppKey instances found in app_keys"
    for key in keys:
        assert isinstance(key, AppKey), f"{key!r} is not an AppKey"


async def test_all_app_key_names_are_unique():
    """Each AppKey carries a unique name string."""
    import social_home.app_keys as K

    keys = {name: v for name, v in vars(K).items() if name.endswith("_key")}
    names = [k._name for k in keys.values()]  # type: ignore[attr-defined]
    assert len(names) == len(set(names)), "duplicate AppKey names found"


async def test_db_key_is_importable_from_app_keys():
    """db_key specifically is importable and is an AppKey."""
    from social_home.app_keys import db_key

    assert isinstance(db_key, AppKey)


async def test_config_key_is_importable():
    """config_key is importable from app_keys."""
    from social_home.app_keys import config_key

    assert isinstance(config_key, AppKey)


async def test_service_keys_exist():
    """All expected service keys are present in app_keys."""
    import social_home.app_keys as K

    expected = [
        "user_service_key",
        "feed_service_key",
        "space_service_key",
        "notification_service_key",
        "dm_service_key",
    ]
    for name in expected:
        assert hasattr(K, name), f"{name!r} missing from app_keys"
        assert isinstance(getattr(K, name), AppKey)
