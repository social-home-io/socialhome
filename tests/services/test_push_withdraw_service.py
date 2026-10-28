"""A handled push comes back off the device — once, together, and only if
it was pushed (:class:`PushWithdrawService`)."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

from socialhome.services import push_withdraw_service as mod
from socialhome.services.push_withdraw_service import PushWithdrawService


class _Users:
    async def get_by_user_id(self, user_id: str):
        if user_id == "ghost":
            return None
        return SimpleNamespace(user_id=user_id, username=user_id)


class _WebPush:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self.fail = fail

    async def withdraw_from_user(self, user_id: str, tags: list[str]) -> int:
        self.calls.append((user_id, list(tags)))
        if self.fail:
            raise RuntimeError("push service down")
        return 1


class _Adapter:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self.fail = fail

    async def withdraw_push(self, user, tags: list[str]) -> None:
        self.calls.append((user.user_id, list(tags)))
        if self.fail:
            raise RuntimeError("HA down")


def _svc(window: float = 0.05):
    svc = PushWithdrawService(_Users(), window_seconds=window)
    web, adapter = _WebPush(), _Adapter()
    svc.attach_push_service(web)
    svc.attach_platform_adapter(adapter)
    return svc, web, adapter


async def test_only_pushed_tags_are_withdrawn_and_only_once():
    svc, web, adapter = _svc()
    svc.note_pushed("u1", "dm_message:/dms/c1")
    assert svc.withdraw("u1", ["dm_message:/dms/c1", "task_assigned:/tasks/t"]) == [
        "dm_message:/dms/c1"
    ]
    # Already withdrawn: a second read sends nothing.
    assert svc.withdraw("u1", ["dm_message:/dms/c1"]) == []
    # Never pushed to this user: nothing.
    assert svc.withdraw("u2", ["dm_message:/dms/c1"]) == []
    await svc.flush("u1")
    assert web.calls == [("u1", ["dm_message:/dms/c1"])]
    assert adapter.calls == [("u1", ["dm_message:/dms/c1"])]


async def test_withdraws_within_the_window_go_out_as_one_push():
    svc, web, adapter = _svc(window=0.05)
    for tag in ("a:/1", "b:/2", "c:/3"):
        svc.note_pushed("u1", tag)
    await svc.start()
    try:
        svc.withdraw("u1", ["a:/1"])
        svc.withdraw("u1", ["b:/2", "c:/3"])
        assert web.calls == []  # not before the window ran out
        await asyncio.sleep(0.2)
    finally:
        await svc.stop()
    assert web.calls == [("u1", ["a:/1", "b:/2", "c:/3"])]
    assert adapter.calls == [("u1", ["a:/1", "b:/2", "c:/3"])]


async def test_a_fresh_push_with_the_same_tag_cancels_its_pending_withdraw():
    svc, web, _ = _svc()
    svc.note_pushed("u1", "dm_message:/dms/c1")
    svc.withdraw("u1", ["dm_message:/dms/c1"])
    # A new message lands before the withdraw went out: keep it on screen.
    svc.note_pushed("u1", "dm_message:/dms/c1")
    await svc.flush("u1")
    assert web.calls == []
    # …and it can be withdrawn again later.
    assert svc.withdraw("u1", ["dm_message:/dms/c1"]) == ["dm_message:/dms/c1"]


async def test_stop_sends_what_is_still_waiting():
    svc, web, _ = _svc(window=60.0)
    svc.note_pushed("u1", "a:/1")
    await svc.start()
    svc.withdraw("u1", ["a:/1"])
    await svc.stop()
    assert web.calls == [("u1", ["a:/1"])]


async def test_start_is_idempotent_and_a_second_user_gets_their_own_push():
    svc, web, _ = _svc(window=0.02)
    svc.note_pushed("u1", "a:/1")
    svc.note_pushed("u2", "a:/1")
    await svc.start()
    await svc.start()
    try:
        svc.withdraw("u1", ["a:/1"])
        svc.withdraw("u2", ["a:/1"])
        await asyncio.sleep(0.15)
    finally:
        await svc.stop()
    assert sorted(web.calls) == [("u1", ["a:/1"]), ("u2", ["a:/1"])]


async def test_old_pushes_and_overflow_are_forgotten(monkeypatch):
    svc, _, _ = _svc()
    monkeypatch.setattr(mod, "MAX_PUSHED_PER_USER", 3)
    for i in range(5):
        svc.note_pushed("u1", f"t:{i}")
    # The two oldest fell out of the record.
    assert svc.withdraw("u1", ["t:0", "t:1", "t:4"]) == ["t:4"]
    monkeypatch.setattr(mod, "PUSHED_TTL_SECONDS", -1.0)
    assert svc.withdraw("u1", ["t:2"]) == []


async def test_a_failing_surface_never_stops_the_other(caplog):
    svc = PushWithdrawService(_Users(), window_seconds=0.01)
    web, adapter = _WebPush(fail=True), _Adapter(fail=True)
    svc.attach_push_service(web)
    svc.attach_platform_adapter(adapter)
    svc.note_pushed("u1", "a:/1")
    svc.withdraw("u1", ["a:/1"])
    with caplog.at_level(logging.WARNING):
        await svc.flush("u1")
    assert web.calls and adapter.calls
    assert "push withdraw (web) failed" in caplog.text
    assert "push withdraw (platform) failed" in caplog.text


async def test_ha_clears_are_bounded_and_unknown_users_skipped(monkeypatch):
    svc, web, adapter = _svc()
    monkeypatch.setattr(mod, "MAX_HA_CLEARS_PER_FLUSH", 2)
    for tag in ("a:/1", "b:/2", "c:/3"):
        svc.note_pushed("u1", tag)
        svc.note_pushed("ghost", tag)
    svc.withdraw("u1", ["a:/1", "b:/2", "c:/3"])
    svc.withdraw("ghost", ["a:/1"])
    await svc.flush("u1")
    await svc.flush("ghost")
    await svc.flush("nobody")
    assert web.calls == [("u1", ["a:/1", "b:/2", "c:/3"]), ("ghost", ["a:/1"])]
    assert adapter.calls == [("u1", ["a:/1", "b:/2"])]


async def test_without_surfaces_nothing_breaks():
    svc = PushWithdrawService(_Users())
    svc.note_pushed("u1", "a:/1")
    svc.withdraw("u1", ["a:/1"])
    await svc.flush("u1")
