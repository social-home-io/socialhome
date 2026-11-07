"""The shared ``media_refs`` sender behaviour
(:mod:`socialhome.services.space_media_refs`)."""

from __future__ import annotations

import logging

from socialhome.services.space_media_refs import SpaceMediaRefsMixin


class _Plan:
    def wire(self):
        return {"a.webp": {"blob_id": "b" * 64}}


class _Gfs:
    def __init__(self, plan=None, readers=(), boom=False):
        self._plan = plan
        self.readers = set(readers)
        self.boom = boom
        self.calls = []
        self.items = []
        self.uploaded = []

    async def plan(self, space_id, urls, *, item_id, followers):
        if self.boom:
            raise OSError("disk")
        self.calls.append((space_id, list(urls), followers))
        self.items.append(item_id)
        return self._plan

    async def is_relay_reader(self, iid):
        return iid in self.readers

    def upload(self, plan):
        self.uploaded.append(plan)


class _Sender(SpaceMediaRefsMixin):
    __slots__ = ("_media_gfs",)

    def __init__(self):
        self._media_gfs = None


async def test_unwired_is_a_no_op():
    s = _Sender()
    assert (
        await s._plan_media_refs(
            "sp", ["api/media/a.webp"], item_id="item-1", followers=True
        )
        is None
    )
    assert s._media_refs_per_peer(None) is None
    s._upload_media(None)


async def test_no_urls_plans_nothing():
    s = _Sender()
    gfs = _Gfs(plan=_Plan())
    s.attach_media_gfs(gfs)
    assert (
        await s._plan_media_refs("sp", [None, ""], item_id="item-1", followers=False)
        is None
    )
    assert gfs.calls == []


async def test_plan_per_peer_and_upload():
    s = _Sender()
    gfs = _Gfs(plan=_Plan(), readers={"e"})
    s.attach_media_gfs(gfs)
    plan = await s._plan_media_refs(
        "sp", ["api/media/a.webp"], item_id="item-1", followers=True
    )
    assert gfs.calls == [("sp", ["api/media/a.webp"], True)]
    assert gfs.items == ["item-1"]
    per_peer = s._media_refs_per_peer(plan)
    base = {"x": 1}
    assert (await per_peer("e", base))["media_refs"] == plan.wire()
    assert await per_peer("b", base) is base
    assert base == {"x": 1}
    s._upload_media(plan)
    assert gfs.uploaded == [plan]


async def test_a_failure_is_logged_and_swallowed(caplog):
    s = _Sender()
    s.attach_media_gfs(_Gfs(boom=True))
    with caplog.at_level(logging.WARNING):
        assert (
            await s._plan_media_refs(
                "sp", ["api/media/a.webp"], item_id="item-1", followers=False
            )
            is None
        )
    assert "media" in caplog.text
