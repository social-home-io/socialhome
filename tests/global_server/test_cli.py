"""Tests for the GFS console-script CLI (``--init`` / ``--set-password``)."""

from __future__ import annotations

from socialhome.global_server.config import GfsConfig
from socialhome.global_server.server import _cli_init, _cli_set_password


def test_cli_init_writes_example_config(tmp_dir, capsys):
    target = tmp_dir / "global_server.toml"
    rc = _cli_init(target)
    assert rc == 0
    assert target.is_file()
    content = target.read_text()
    assert "[server]" in content
    assert "[admin]" in content
    out = capsys.readouterr().out
    assert "example config" in out.lower()


def test_cli_init_refuses_overwrite(tmp_dir, capsys):
    target = tmp_dir / "global_server.toml"
    _cli_init(target)
    rc = _cli_init(target)
    assert rc == 2
    err = capsys.readouterr().err
    assert "already exists" in err


def test_cli_set_password_writes_hash(tmp_dir, monkeypatch, capsys):
    target = tmp_dir / "global_server.toml"
    _cli_init(target)
    # Patch getpass to avoid stdin.
    import getpass as _gp

    monkeypatch.setattr(_gp, "getpass", lambda prompt="": "verysecretpw")
    rc = _cli_set_password(target)
    assert rc == 0
    cfg = GfsConfig.from_toml(target)
    assert cfg.admin_password_hash.startswith("$2")
    out = capsys.readouterr().out
    assert "hash written" in out.lower()


def test_cli_set_password_without_config_fails(tmp_dir, capsys):
    target = tmp_dir / "no-such.toml"
    rc = _cli_set_password(target)
    assert rc == 2


def test_cli_set_password_short_pw_rejected(tmp_dir, monkeypatch, capsys):
    target = tmp_dir / "global_server.toml"
    _cli_init(target)
    import getpass as _gp

    monkeypatch.setattr(_gp, "getpass", lambda prompt="": "short")
    rc = _cli_set_password(target)
    assert rc == 2


def test_cli_set_password_mismatch_rejected(tmp_dir, monkeypatch, capsys):
    target = tmp_dir / "global_server.toml"
    _cli_init(target)
    answers = iter(["longerpw-one", "longerpw-two"])
    import getpass as _gp

    monkeypatch.setattr(_gp, "getpass", lambda prompt="": next(answers))
    rc = _cli_set_password(target)
    assert rc == 2
