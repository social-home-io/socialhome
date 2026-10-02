"""Tests for RecoveryReconnectService — one-shot post-restore peer reconnect.

After a Recovery Kit restore + restart, peers still hold our OLD inbox URL.
On the FIRST boot after a restore the service fans ``URL_UPDATED`` out to every
confirmed peer, guarded by an instance_config marker so it runs exactly once.
"""

from __future__ import annotations

import pytest

from socialhome.db.database import AsyncDatabase
from socialhome.services.recovery_kit_service import RECOVERED_AT_KEY
from socialhome.services.recovery_reconnect_service import (
    RECONNECTED_AT_KEY,
    RecoveryReconnectService,
)


async def _make_db(data_dir):
    db = AsyncDatabase(data_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    return db


async def _set(db, key, value):
    await db.enqueue(
        "INSERT INTO instance_config(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


async def _get(db, key):
    row = await db.fetchone("SELECT value FROM instance_config WHERE key=?", (key,))
    return None if row is None else row["value"]


class _UrlUpdateStub:
    def __init__(self, ret=3, raises=False):
        self.calls: list[dict] = []
        self._ret = ret
        self._raises = raises

    async def publish(self, *, new_inbox_base_url: str) -> int:
        self.calls.append({"new_inbox_base_url": new_inbox_base_url})
        if self._raises:
            raise RuntimeError("boom")
        return self._ret


class _AdapterStub:
    def __init__(self, base="https://new.example/inbox"):
        self._base = base

    async def get_federation_base(self) -> str | None:
        return self._base


@pytest.mark.asyncio
async def test_recovered_and_base_present_fans_out_and_marks(tmp_path):
    db = await _make_db(tmp_path)
    await _set(db, RECOVERED_AT_KEY, "2026-06-14T00:00:00+00:00")
    url_update = _UrlUpdateStub()
    adapter = _AdapterStub("https://new.example/inbox")
    svc = RecoveryReconnectService(db, url_update, adapter)

    ran = await svc.maybe_reconnect()

    assert ran is True
    assert url_update.calls == [{"new_inbox_base_url": "https://new.example/inbox"}]
    assert await _get(db, RECONNECTED_AT_KEY) is not None
    await db.shutdown()


@pytest.mark.asyncio
async def test_not_recovered_does_nothing(tmp_path):
    db = await _make_db(tmp_path)
    url_update = _UrlUpdateStub()
    svc = RecoveryReconnectService(db, url_update, _AdapterStub())

    ran = await svc.maybe_reconnect()

    assert ran is False
    assert url_update.calls == []
    assert await _get(db, RECONNECTED_AT_KEY) is None
    await db.shutdown()


@pytest.mark.asyncio
async def test_already_reconnected_does_nothing(tmp_path):
    db = await _make_db(tmp_path)
    await _set(db, RECOVERED_AT_KEY, "2026-06-14T00:00:00+00:00")
    await _set(db, RECONNECTED_AT_KEY, "2026-06-14T01:00:00+00:00")
    url_update = _UrlUpdateStub()
    svc = RecoveryReconnectService(db, url_update, _AdapterStub())

    ran = await svc.maybe_reconnect()

    assert ran is False
    assert url_update.calls == []
    await db.shutdown()


@pytest.mark.asyncio
async def test_recovered_but_no_base_is_retryable(tmp_path):
    db = await _make_db(tmp_path)
    await _set(db, RECOVERED_AT_KEY, "2026-06-14T00:00:00+00:00")
    url_update = _UrlUpdateStub()
    svc = RecoveryReconnectService(db, url_update, _AdapterStub(base=None))

    ran = await svc.maybe_reconnect()

    assert ran is False
    assert url_update.calls == []
    # Marker stays unset so a later boot (once a base is set) retries.
    assert await _get(db, RECONNECTED_AT_KEY) is None
    await db.shutdown()


@pytest.mark.asyncio
async def test_publish_raises_still_marks_done(tmp_path):
    db = await _make_db(tmp_path)
    await _set(db, RECOVERED_AT_KEY, "2026-06-14T00:00:00+00:00")
    url_update = _UrlUpdateStub(raises=True)
    svc = RecoveryReconnectService(db, url_update, _AdapterStub())

    ran = await svc.maybe_reconnect()

    assert ran is True
    assert url_update.calls == [{"new_inbox_base_url": "https://new.example/inbox"}]
    # Durable outbox retries delivery; we mark done to avoid re-fanning.
    assert await _get(db, RECONNECTED_AT_KEY) is not None
    await db.shutdown()


# ── v_44: one authority rotation per restore ─────────────────────────────


class _RotationStub:
    def __init__(self, raises=False):
        self.calls = 0
        self._raises = raises

    async def rotate_hosted_after_restore(self, marker=None) -> int:
        self.calls += 1
        if self._raises:
            raise RuntimeError("boom")
        return 2


@pytest.mark.asyncio
async def test_rotation_runs_once_per_restore(tmp_dir):
    from socialhome.services.backup_service import BACKUP_RESTORED_AT_KEY

    db = await _make_db(tmp_dir)
    try:
        svc = RecoveryReconnectService(db, _UrlUpdateStub(), _AdapterStub())
        rot = _RotationStub()
        assert await svc.maybe_rotate_space_authority(rot) == 0  # no restore
        assert rot.calls == 0
        await _set(db, RECOVERED_AT_KEY, "2026-10-01T00:00:00+00:00")
        assert await svc.maybe_rotate_space_authority(rot) == 2
        assert await svc.maybe_rotate_space_authority(rot) == 0  # same restore
        assert rot.calls == 1
        await _set(db, BACKUP_RESTORED_AT_KEY, "2026-10-02T00:00:00+00:00")
        assert await svc.maybe_rotate_space_authority(rot) == 2  # a new restore
        assert rot.calls == 2
    finally:
        await db.shutdown()


@pytest.mark.asyncio
async def test_rotation_failure_is_retried_next_boot(tmp_dir):
    db = await _make_db(tmp_dir)
    try:
        svc = RecoveryReconnectService(db, _UrlUpdateStub(), _AdapterStub())
        await _set(db, RECOVERED_AT_KEY, "2026-10-01T00:00:00+00:00")
        assert await svc.maybe_rotate_space_authority(_RotationStub(raises=True)) == 0
        assert await svc.maybe_rotate_space_authority(_RotationStub()) == 2
    finally:
        await db.shutdown()


@pytest.mark.asyncio
async def test_authority_rotation_pending_until_the_restore_rotated(tmp_dir):
    """v_46 restore gate: pending from a restore until its post-restore
    rotation ran; a failed rotation keeps it pending (fail closed)."""
    db = await _make_db(tmp_dir)
    try:
        svc = RecoveryReconnectService(db, _UrlUpdateStub(), _AdapterStub())
        assert await svc.authority_rotation_pending() is False  # no restore
        await _set(db, RECOVERED_AT_KEY, "2026-10-01T00:00:00+00:00")
        assert await svc.authority_rotation_pending() is True
        await svc.maybe_rotate_space_authority(_RotationStub(raises=True))
        assert await svc.authority_rotation_pending() is True
        await svc.maybe_rotate_space_authority(_RotationStub())
        assert await svc.authority_rotation_pending() is False
    finally:
        await db.shutdown()


@pytest.mark.asyncio
async def test_rotation_gets_the_restore_marker_and_failure_warns(tmp_dir, caplog):
    db = await _make_db(tmp_dir)
    try:
        seen: list = []

        class _Rec(_RotationStub):
            async def rotate_hosted_after_restore(self, marker=None) -> int:
                seen.append(marker)
                return await super().rotate_hosted_after_restore(marker)

        svc = RecoveryReconnectService(db, _UrlUpdateStub(), _AdapterStub())
        await _set(db, RECOVERED_AT_KEY, "2026-10-01T00:00:00+00:00")
        with caplog.at_level("WARNING"):
            await svc.maybe_rotate_space_authority(_RotationStub(raises=True))
        assert any("echo healing" in r.getMessage() for r in caplog.records)
        await svc.maybe_rotate_space_authority(_Rec())
        assert seen == ["2026-10-01T00:00:00+00:00|"]
    finally:
        await db.shutdown()
