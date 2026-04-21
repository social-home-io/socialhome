"""Tests for :class:`GfsConfig` + TOML loader + env fallback."""

from __future__ import annotations

import pytest

from social_home.global_server.config import (
    GfsConfig,
    set_password_in_toml,
    write_example_config,
)


def test_from_toml_parses_all_sections(tmp_dir):
    """Every section in a fully-populated TOML round-trips correctly."""
    toml = """
[server]
host     = "1.2.3.4"
port     = 9000
base_url = "https://test.example.com"
data_dir = "/tmp/sh-gfs"
instance_id = "gfs-test"

[branding]
server_name       = "Test GFS"
landing_markdown  = "# hi"
header_image_file = "hero.webp"

[policy]
auto_accept_clients = false
auto_accept_spaces  = true
fraud_threshold     = 10

[admin]
password_hash = "$2b$12$fake"

[webrtc]
stun_urls   = ["stun:custom:19302"]
turn_url    = "turn:turn.example.com"
turn_secret = "s3cret"

[cluster]
enabled = true
node_id = "node-a"
peers   = ["https://peer1", "https://peer2"]
"""
    p = tmp_dir / "global_server.toml"
    p.write_text(toml)
    cfg = GfsConfig.from_toml(p)
    assert cfg.host == "1.2.3.4"
    assert cfg.port == 9000
    assert cfg.base_url == "https://test.example.com"
    assert cfg.server_name == "Test GFS"
    assert cfg.auto_accept_clients is False
    assert cfg.auto_accept_spaces is True
    assert cfg.fraud_threshold == 10
    assert cfg.admin_password_hash == "$2b$12$fake"
    assert cfg.stun_urls == ("stun:custom:19302",)
    assert cfg.turn_url == "turn:turn.example.com"
    assert cfg.cluster_enabled is True
    assert cfg.cluster_peers == ("https://peer1", "https://peer2")
    assert cfg.db_path.endswith("/gfs.db")


def test_from_toml_missing_base_url_raises(tmp_dir):
    """A TOML without base_url is rejected — public URLs would break."""
    toml = """
[server]
host = "0.0.0.0"
port = 8765
"""
    p = tmp_dir / "global_server.toml"
    p.write_text(toml)
    with pytest.raises(ValueError, match="base_url"):
        GfsConfig.from_toml(p)


def test_from_env_fallback_maps_legacy_vars(monkeypatch):
    monkeypatch.setenv("GFS_HOST", "127.0.0.1")
    monkeypatch.setenv("GFS_PORT", "4321")
    monkeypatch.setenv("GFS_INSTANCE_ID", "gfs-legacy")
    cfg = GfsConfig.from_env_fallback()
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 4321
    assert cfg.instance_id == "gfs-legacy"
    assert cfg.base_url == "http://127.0.0.1:4321"


def test_load_discovers_config(tmp_dir, monkeypatch):
    """``GfsConfig.load`` walks the documented search order."""
    toml = """
[server]
host = "0.0.0.0"
port = 9999
base_url = "https://discovered.example"
data_dir = "/tmp/disc"
"""
    p = tmp_dir / "global_server.toml"
    p.write_text(toml)
    monkeypatch.setenv("SOCIAL_HOME_GFS_CONFIG", str(p))
    cfg = GfsConfig.load()
    assert cfg.port == 9999
    assert cfg.base_url == "https://discovered.example"


def test_load_falls_back_to_env_without_toml(monkeypatch, tmp_dir):
    monkeypatch.delenv("SOCIAL_HOME_GFS_CONFIG", raising=False)
    monkeypatch.delenv("SOCIAL_HOME_GFS_DATA", raising=False)
    monkeypatch.setenv("GFS_HOST", "0.0.0.0")
    monkeypatch.setenv("GFS_PORT", "8765")
    # Chdir somewhere without a global_server.toml.
    monkeypatch.chdir(tmp_dir)
    cfg = GfsConfig.load()
    assert cfg.base_url == "http://0.0.0.0:8765"


def test_write_example_config_refuses_overwrite(tmp_dir):
    target = tmp_dir / "global_server.toml"
    write_example_config(target)
    assert target.is_file()
    import pytest

    with pytest.raises(FileExistsError):
        write_example_config(target)


def test_set_password_in_toml_updates_admin_section(tmp_dir):
    target = tmp_dir / "global_server.toml"
    write_example_config(target)
    set_password_in_toml(target, "$2b$12$somehash")
    cfg = GfsConfig.from_toml(target)
    assert cfg.admin_password_hash == "$2b$12$somehash"
