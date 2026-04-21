"""Tests for social_home.config."""

from __future__ import annotations

from social_home.config import Config, _split_toml


def test_defaults():
    """Config loaded with no TOML falls back to sensible defaults."""
    cfg = Config.from_env()
    assert cfg.listen_port == 8099
    assert cfg.mode == "standalone"


def test_media_fields_removed():
    """Config no longer has media processing fields (protocol constants now)."""
    cfg = Config()
    assert not hasattr(cfg, "image_max_dimension")
    assert not hasattr(cfg, "image_webp_quality")
    assert not hasattr(cfg, "video_max_dimension")
    assert not hasattr(cfg, "video_crf")
    assert not hasattr(cfg, "video_max_duration_seconds")
    assert not hasattr(cfg, "video_audio_bitrate_kbps")
    assert not hasattr(cfg, "video_max_input_bytes")
    assert hasattr(cfg, "max_storage_bytes")


def test_toml_loader(tmp_path, monkeypatch):
    """TOML file at $SH_CONFIG is loaded."""
    toml_file = tmp_path / "social_home.toml"
    toml_file.write_text(
        '[server]\nlisten_port = 7777\n\n[federation]\ninstance_name = "TOML Home"\n'
    )
    monkeypatch.setenv("SH_CONFIG", str(toml_file))
    cfg = Config.from_env()
    assert cfg.listen_port == 7777
    assert cfg.instance_name == "TOML Home"


def test_toml_webrtc_prefix(tmp_path, monkeypatch):
    """[webrtc] keys are prefixed with webrtc_ when flattened."""
    toml_file = tmp_path / "social_home.toml"
    toml_file.write_text('[webrtc]\nstun_url = "stun:example.com:3478"\n')
    monkeypatch.setenv("SH_CONFIG", str(toml_file))
    cfg = Config.from_env()
    assert cfg.webrtc_stun_url == "stun:example.com:3478"


def test_env_overrides_toml(tmp_path, monkeypatch):
    """Environment variables take precedence over TOML values."""
    toml_file = tmp_path / "social_home.toml"
    toml_file.write_text("[server]\nlisten_port = 7777\n")
    monkeypatch.setenv("SH_CONFIG", str(toml_file))
    monkeypatch.setenv("SH_LISTEN_PORT", "5555")
    cfg = Config.from_env()
    assert cfg.listen_port == 5555


def test_split_toml_core_and_platform():
    """_split_toml flattens core sections and isolates platform sections."""
    raw = {
        "server": {"listen_port": 8080, "log_level": "DEBUG"},
        "webrtc": {"stun_url": "stun:example.com"},
        "homeassistant": {"ai_task_entity_id": "ai_task.openai"},
        "standalone": {},
        "top_level_key": "value",
    }
    flat, platform = _split_toml(raw)
    assert flat["listen_port"] == 8080
    assert flat["log_level"] == "DEBUG"
    assert flat["webrtc_stun_url"] == "stun:example.com"
    assert flat["top_level_key"] == "value"
    assert platform["homeassistant"]["ai_task_entity_id"] == "ai_task.openai"
    assert "standalone" in platform


def test_platform_options_loaded_from_toml(tmp_path, monkeypatch):
    """[homeassistant] survives unchanged under config.platform_options."""
    toml_file = tmp_path / "social_home.toml"
    toml_file.write_text('[homeassistant]\nai_task_entity_id = "ai_task.openai"\n')
    monkeypatch.setenv("SH_CONFIG", str(toml_file))
    cfg = Config.from_env()
    assert (
        cfg.platform_options["homeassistant"]["ai_task_entity_id"] == "ai_task.openai"
    )


def test_malformed_toml_ignored(tmp_path, monkeypatch):
    """A malformed TOML file is silently ignored."""
    toml_file = tmp_path / "social_home.toml"
    toml_file.write_text("this is not valid TOML {{{")
    monkeypatch.setenv("SH_CONFIG", str(toml_file))
    cfg = Config.from_env()
    assert cfg.listen_port == 8099


def test_mode_defaults_to_standalone():
    """Mode defaults to 'standalone', not 'ha'."""
    cfg = Config.from_env()
    assert cfg.mode == "standalone"


def test_xdg_default_paths():
    """Default data_dir follows XDG_DATA_HOME convention."""
    cfg = Config()
    assert "social-home" in cfg.data_dir


# ── HA credentials ────────────────────────────────────────────────────────


def test_ha_url_default():
    """ha_url defaults to homeassistant.local:8123."""
    cfg = Config()
    assert cfg.ha_url == "http://homeassistant.local:8123"
    assert cfg.ha_token == ""


def test_ha_url_from_env(monkeypatch):
    """SH_HA_URL / SH_HA_TOKEN env vars are picked up."""
    monkeypatch.setenv("SH_HA_URL", "http://ha.local:8123")
    monkeypatch.setenv("SH_HA_TOKEN", "llat-123")
    cfg = Config.from_env()
    assert cfg.ha_url == "http://ha.local:8123"
    assert cfg.ha_token == "llat-123"


def test_ha_creds_from_toml(tmp_path, monkeypatch):
    """[homeassistant] url/token in TOML populate ha_url / ha_token."""
    toml_file = tmp_path / "social_home.toml"
    toml_file.write_text(
        "[homeassistant]\n"
        'url = "http://ha.toml:8123"\n'
        'token = "toml-token"\n'
        'stt_entity_id = "stt.whisper"\n'
    )
    monkeypatch.setenv("SH_CONFIG", str(toml_file))
    cfg = Config.from_env()
    assert cfg.ha_url == "http://ha.toml:8123"
    assert cfg.ha_token == "toml-token"
    # url / token do NOT leak into platform_options — that stays a pure
    # adapter-options pass-through.
    assert cfg.platform_options["homeassistant"] == {"stt_entity_id": "stt.whisper"}


def test_ha_env_overrides_toml(tmp_path, monkeypatch):
    """SH_HA_URL wins over [homeassistant] url in TOML."""
    toml_file = tmp_path / "social_home.toml"
    toml_file.write_text(
        '[homeassistant]\nurl = "http://toml:8123"\ntoken = "toml-tok"\n'
    )
    monkeypatch.setenv("SH_CONFIG", str(toml_file))
    monkeypatch.setenv("SH_HA_URL", "http://env:8123")
    cfg = Config.from_env()
    assert cfg.ha_url == "http://env:8123"
    # Env didn't set token → TOML value wins.
    assert cfg.ha_token == "toml-tok"
