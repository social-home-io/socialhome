"""Tests for :class:`GfsConfig` + TOML loader + env fallback."""

from __future__ import annotations

from pathlib import Path

import pytest

from socialhome.global_server.config import (
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


def test_toml_host_port_apply_without_env(tmp_dir, monkeypatch):
    """Issue #563 (core): with no ``GFS_*`` env set, a ``--config``
    TOML's ``[server] host``/``port`` are honoured. The shipped image
    must not bake env that silently shadows them."""
    toml = """
[server]
host = "127.0.0.1"
port = 7654
base_url = "https://cfg.example"
"""
    p = tmp_dir / "global_server.toml"
    p.write_text(toml)
    for var in (
        "GFS_HOST",
        "GFS_PORT",
        "GFS_BASE_URL",
        "GFS_DATA_DIR",
        "GFS_DB_PATH",
        "GFS_INSTANCE_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    cfg = GfsConfig.load(p)
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 7654


def test_env_overrides_toml(tmp_dir, monkeypatch):
    """Issue #563 (model): an explicit ``GFS_*`` env var overrides the
    TOML's ``[server]`` value (env > file > defaults), consistent with
    :class:`socialhome.config.Config`. Unset vars leave the file value
    intact, so a single override doesn't disturb the rest of the file."""
    toml = """
[server]
host = "10.0.0.1"
port = 9000
base_url = "https://file.example"
data_dir = "/file/dir"
instance_id = "from-file"
"""
    p = tmp_dir / "global_server.toml"
    p.write_text(toml)
    monkeypatch.delenv("SOCIAL_HOME_GFS_CONFIG", raising=False)
    monkeypatch.delenv("SOCIAL_HOME_GFS_DATA", raising=False)
    monkeypatch.setenv("GFS_PORT", "5555")
    monkeypatch.setenv("GFS_BASE_URL", "https://env.example")
    monkeypatch.setenv("GFS_DATA_DIR", "/env/dir")
    monkeypatch.setenv("GFS_INSTANCE_ID", "from-env")
    monkeypatch.delenv("GFS_HOST", raising=False)
    monkeypatch.delenv("GFS_DB_PATH", raising=False)
    cfg = GfsConfig.load(p)
    assert cfg.port == 5555  # env wins
    assert cfg.base_url == "https://env.example"  # env wins
    assert cfg.data_dir == "/env/dir"  # env wins
    assert cfg.instance_id == "from-env"  # env wins
    assert cfg.host == "10.0.0.1"  # file (no env override)


def test_env_db_path_pins_data_dir_to_parent(tmp_dir, monkeypatch):
    """``GFS_DB_PATH`` continues to pin ``data_dir`` to the DB file's
    parent directory, layered over a TOML the same as the dev fallback."""
    toml = """
[server]
base_url = "https://cfg.example"
data_dir = "/file/dir"
"""
    p = tmp_dir / "global_server.toml"
    p.write_text(toml)
    monkeypatch.delenv("SOCIAL_HOME_GFS_CONFIG", raising=False)
    monkeypatch.delenv("SOCIAL_HOME_GFS_DATA", raising=False)
    monkeypatch.setenv("GFS_DB_PATH", "/custom/db/gfs.db")
    monkeypatch.delenv("GFS_DATA_DIR", raising=False)
    cfg = GfsConfig.load(p)
    assert cfg.data_dir == "/custom/db"


def test_gfs_dockerfile_does_not_bake_host_port():
    """Issue #563 guard: the published image must NOT bake
    ``ENV GFS_HOST`` / ``ENV GFS_PORT`` — that would pin them above the
    ``--config`` file (env > file) and silently shadow its ``[server]``
    host/port. They are opt-in run-time overrides only."""
    repo_root = Path(__file__).resolve().parents[2]
    dockerfile = (repo_root / "Dockerfile.gfs").read_text(encoding="utf-8")
    assert "ENV GFS_HOST" not in dockerfile
    assert "ENV GFS_PORT" not in dockerfile


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


# ─── Trusted proxies ─────────────────────────────────────────────────


def test_trusted_proxies_default_is_loopback_and_private():
    """The default keeps existing docker / reverse-proxy deployments (proxy on
    the same host or private network) doing per-client rate limiting with no
    configuration, while an internet-facing peer can never spoof."""
    cfg = GfsConfig()
    assert "127.0.0.0/8" in cfg.trusted_proxies
    assert "::1/128" in cfg.trusted_proxies
    assert "10.0.0.0/8" in cfg.trusted_proxies
    assert "172.16.0.0/12" in cfg.trusted_proxies
    assert "192.168.0.0/16" in cfg.trusted_proxies
    assert "fc00::/7" in cfg.trusted_proxies


def test_from_toml_parses_trusted_proxies(tmp_dir):
    p = Path(tmp_dir) / "gfs.toml"
    p.write_text(
        '[server]\nbase_url = "https://x.example"\n'
        'trusted_proxies = ["203.0.113.7", "198.51.100.0/24"]\n',
        encoding="utf-8",
    )
    cfg = GfsConfig.from_toml(p)
    assert cfg.trusted_proxies == ("203.0.113.7", "198.51.100.0/24")


def test_from_toml_empty_trusted_proxies_disables_forwarded_for(tmp_dir):
    """An explicit empty list must stay empty — not fall back to the default."""
    p = Path(tmp_dir) / "gfs-none.toml"
    p.write_text(
        '[server]\nbase_url = "https://x.example"\ntrusted_proxies = []\n',
        encoding="utf-8",
    )
    assert GfsConfig.from_toml(p).trusted_proxies == ()


def test_env_overrides_trusted_proxies(monkeypatch):
    monkeypatch.setenv("GFS_TRUSTED_PROXIES", "203.0.113.7, 198.51.100.0/24")
    cfg = GfsConfig.from_env_fallback()
    assert cfg.trusted_proxies == ("203.0.113.7", "198.51.100.0/24")


def test_env_can_clear_trusted_proxies(monkeypatch):
    """``GFS_TRUSTED_PROXIES=""`` is the internet-facing posture."""
    monkeypatch.setenv("GFS_TRUSTED_PROXIES", "")
    assert GfsConfig.from_env_fallback().trusted_proxies == ()


def test_example_toml_documents_trusted_proxies():
    from socialhome.global_server.config import EXAMPLE_TOML

    assert "trusted_proxies" in EXAMPLE_TOML


def test_signing_seed_hex_loads_from_toml(tmp_dir):
    """Operators who manage secrets externally pin the GFS identity seed in
    ``[server] signing_seed_hex`` instead of letting the data dir own it."""
    p = tmp_dir / "global_server.toml"
    p.write_text(
        f'[server]\nbase_url = "https://g.example"\nsigning_seed_hex = "{"ab" * 32}"\n'
    )
    assert GfsConfig.from_toml(p).signing_seed_hex == "ab" * 32


def test_signing_seed_hex_defaults_to_empty(tmp_dir):
    """No key in the file → empty, i.e. "use the persisted seed file"."""
    p = tmp_dir / "global_server.toml"
    p.write_text('[server]\nbase_url = "https://g.example"\n')
    assert GfsConfig.from_toml(p).signing_seed_hex == ""


def test_gfs_signing_seed_env_overrides_the_file(tmp_dir, monkeypatch):
    """``GFS_SIGNING_SEED`` wins over the file, like every other [server] key."""
    p = tmp_dir / "global_server.toml"
    p.write_text(
        f'[server]\nbase_url = "https://g.example"\nsigning_seed_hex = "{"ab" * 32}"\n'
    )
    monkeypatch.setenv("GFS_SIGNING_SEED", "cd" * 32)
    assert GfsConfig.load(p).signing_seed_hex == "cd" * 32
