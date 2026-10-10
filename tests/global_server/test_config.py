"""Tests for :class:`GfsConfig` + TOML loader + env fallback."""

from __future__ import annotations

from pathlib import Path

import pytest

from socialhome.db.database import DEFAULT_WRITE_BATCH_WINDOW_MS
from socialhome.global_server.config import (
    EXAMPLE_TOML,
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


def test_write_batch_window_defaults_to_the_interactive_window():
    """The GFS no longer runs the old 500 ms coalescing window.

    The writer waits the whole window for companion statements before it
    commits, so every sequential write a request makes (publish, register,
    relay bookkeeping, invite mint) cost up to one window — half a second
    each, with no knob to turn it down.
    """
    assert GfsConfig().write_batch_window_ms == DEFAULT_WRITE_BATCH_WINDOW_MS
    assert DEFAULT_WRITE_BATCH_WINDOW_MS <= 20


def test_write_batch_window_loads_from_toml(tmp_dir):
    p = tmp_dir / "global_server.toml"
    p.write_text(
        '[server]\nbase_url = "https://g.example"\nwrite_batch_window_ms = 37\n'
    )
    assert GfsConfig.from_toml(p).write_batch_window_ms == 37


def test_write_batch_window_missing_from_toml_uses_default(tmp_dir):
    p = tmp_dir / "global_server.toml"
    p.write_text('[server]\nbase_url = "https://g.example"\n')
    assert GfsConfig.from_toml(p).write_batch_window_ms == DEFAULT_WRITE_BATCH_WINDOW_MS


def test_write_batch_window_zero_in_toml_is_kept(tmp_dir):
    """``0`` is a real value (commit each statement alone), not "unset"."""
    p = tmp_dir / "global_server.toml"
    p.write_text(
        '[server]\nbase_url = "https://g.example"\nwrite_batch_window_ms = 0\n'
    )
    assert GfsConfig.from_toml(p).write_batch_window_ms == 0


def test_write_batch_window_env_overrides_the_file(tmp_dir, monkeypatch):
    p = tmp_dir / "global_server.toml"
    p.write_text(
        '[server]\nbase_url = "https://g.example"\nwrite_batch_window_ms = 37\n'
    )
    monkeypatch.setenv("GFS_WRITE_BATCH_WINDOW_MS", "3")
    assert GfsConfig.load(p).write_batch_window_ms == 3


def test_negative_write_batch_window_is_rejected(tmp_dir):
    p = tmp_dir / "global_server.toml"
    p.write_text(
        '[server]\nbase_url = "https://g.example"\nwrite_batch_window_ms = -1\n'
    )
    with pytest.raises(ValueError, match="write_batch_window_ms"):
        GfsConfig.from_toml(p)


def test_example_toml_documents_write_batch_window():
    from socialhome.global_server.config import EXAMPLE_TOML

    assert f"write_batch_window_ms = {DEFAULT_WRITE_BATCH_WINDOW_MS}\n" in EXAMPLE_TOML


def test_cluster_peers_are_normalised_at_load(tmp_dir, caplog):
    """``[cluster] peers`` go through the same normaliser as an admin
    add-peer, so a configured URL matches the stored row's URL (and the
    HELLO names its recipient); an unusable entry is dropped with a
    WARNING, never sent to."""
    p = tmp_dir / "global_server.toml"
    p.write_text(
        """
[server]
base_url = "https://gfs.example.com"

[cluster]
enabled = true
peers = [
  "HTTP://Peer-B.Example:8080/",
  "https://peer-c.example",
  "http://169.254.169.254",
  "http://h:80\\r\\nX-Inj: 1",
  42,
]
"""
    )
    with caplog.at_level("WARNING"):
        cfg = GfsConfig.from_toml(p)
    assert cfg.cluster_peers == ("http://peer-b.example:8080", "https://peer-c.example")
    warned = " ".join(r.getMessage() for r in caplog.records)
    assert "169.254.169.254" in warned
    # The unsafe value is logged escaped: no raw line break reaches the log.
    assert "\r" not in warned and "\n" not in warned


def _cluster_toml(tmp_dir, cluster_body: str):
    p = tmp_dir / "global_server.toml"
    p.write_text(
        f"""
[server]
base_url = "https://gfs.example.com"

[cluster]
enabled = true
{cluster_body}
"""
    )
    return p


def test_cluster_advertise_url_is_loaded_and_normalised(tmp_dir):
    p = _cluster_toml(tmp_dir, 'advertise_url = "HTTP://10.0.0.5:28467/"')
    cfg = GfsConfig.from_toml(p)
    assert cfg.cluster_advertise_url == "http://10.0.0.5:28467"
    assert cfg.cluster_self_url == "http://10.0.0.5:28467"


def test_cluster_self_url_defaults_to_base_url(tmp_dir):
    cfg = GfsConfig.from_toml(_cluster_toml(tmp_dir, ""))
    assert cfg.cluster_advertise_url == ""
    assert cfg.cluster_self_url == "https://gfs.example.com"


@pytest.mark.parametrize("bad", ["https://u:p@x.test", "not a url"])
def test_cluster_advertise_url_unusable_is_a_config_error(tmp_dir, bad):
    p = _cluster_toml(tmp_dir, f'advertise_url = "{bad}"')
    with pytest.raises(ValueError, match="advertise_url"):
        GfsConfig.from_toml(p)


def test_env_cluster_advertise_url_overrides_the_file(tmp_dir, monkeypatch):
    p = _cluster_toml(tmp_dir, 'advertise_url = "http://10.0.0.5:1"')
    monkeypatch.setenv("GFS_CLUSTER_ADVERTISE_URL", "http://10.0.0.9:2/")
    cfg = GfsConfig.from_toml(p)._with_env_overrides()
    assert cfg.cluster_self_url == "http://10.0.0.9:2"


def test_env_cluster_advertise_url_unusable_is_a_config_error(monkeypatch):
    monkeypatch.setenv("GFS_CLUSTER_ADVERTISE_URL", "ftp://x.test")
    with pytest.raises(ValueError, match="advertise_url"):
        GfsConfig(base_url="https://gfs.example.com")._with_env_overrides()


@pytest.mark.parametrize("blank", ["", "   "])
def test_env_cluster_advertise_url_empty_is_unset(tmp_dir, monkeypatch, blank):
    """A templated-but-empty env var (e.g. a Nomad template that rendered
    nothing) must not clear the file's value: falling back to ``base_url``
    would advertise the load balancer — the failure advertise_url fixes."""
    p = _cluster_toml(tmp_dir, 'advertise_url = "http://10.0.0.5:1"')
    monkeypatch.setenv("GFS_CLUSTER_ADVERTISE_URL", blank)
    cfg = GfsConfig.from_toml(p)._with_env_overrides()
    assert cfg.cluster_self_url == "http://10.0.0.5:1"


# ── Public identity: instance_id aliases + cluster node_id ──────────────


def _server_toml(tmp_dir, server_body: str, cluster_body: str = ""):
    p = tmp_dir / "global_server.toml"
    p.write_text(
        f"""
[server]
base_url = "https://gfs.example.com"
{server_body}

[cluster]
{cluster_body}
"""
    )
    return p


def test_instance_id_aliases_default_to_none():
    assert GfsConfig().instance_id_aliases == ()


def test_instance_id_aliases_load_from_toml(tmp_dir):
    p = _server_toml(
        tmp_dir,
        'instance_id = "gfs-shared"\n'
        'instance_id_aliases = ["gfs-0", " gfs-1 ", "", "gfs-shared", "gfs-0"]',
    )
    cfg = GfsConfig.from_toml(p)
    assert cfg.instance_id == "gfs-shared"
    # Trimmed, de-duplicated, empty entries and the id itself dropped.
    assert cfg.instance_id_aliases == ("gfs-0", "gfs-1")


def test_instance_id_aliases_env_overrides_the_file(tmp_dir, monkeypatch):
    p = _server_toml(tmp_dir, 'instance_id_aliases = ["gfs-0"]')
    monkeypatch.setenv("GFS_INSTANCE_ID_ALIASES", "gfs-2, gfs-3,")
    cfg = GfsConfig.load(p)
    assert cfg.instance_id_aliases == ("gfs-2", "gfs-3")


def test_instance_id_aliases_env_empty_clears(tmp_dir, monkeypatch):
    p = _server_toml(tmp_dir, 'instance_id_aliases = ["gfs-0"]')
    monkeypatch.setenv("GFS_INSTANCE_ID_ALIASES", "")
    assert GfsConfig.load(p).instance_id_aliases == ()


def test_an_alias_equal_to_an_env_instance_id_is_dropped(tmp_dir, monkeypatch):
    p = _server_toml(tmp_dir, 'instance_id_aliases = ["gfs-0", "gfs-new"]')
    monkeypatch.setenv("GFS_INSTANCE_ID", "gfs-new")
    assert GfsConfig.load(p).instance_id_aliases == ("gfs-0",)


def test_cluster_mode_requires_an_explicit_node_id():
    """No fallback to instance_id: that id is shared by every node."""
    with pytest.raises(ValueError, match="node_id"):
        GfsConfig(cluster_enabled=True, cluster_node_id="").check_cluster_identity()
    with pytest.raises(ValueError, match="node_id"):
        GfsConfig(cluster_enabled=True, cluster_node_id="  ").check_cluster_identity()
    # Set, or cluster mode off: fine.
    GfsConfig(cluster_enabled=True, cluster_node_id="gfs-0").check_cluster_identity()
    GfsConfig(cluster_enabled=False).check_cluster_identity()


def test_example_toml_documents_the_shared_public_identity():
    assert "instance_id_aliases" in EXAMPLE_TOML
    assert "MUST be identical on every node" in EXAMPLE_TOML
    assert "GFS_INSTANCE_ID_ALIASES" in EXAMPLE_TOML
