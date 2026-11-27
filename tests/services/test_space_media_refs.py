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

    async def plan(self, space_id, urls, *, item_id, followers, author_user_id=""):
        if self.boom:
            raise OSError("disk")
        self.calls.append((space_id, list(urls), followers))
        self.items.append(item_id)
        self.authors = getattr(self, "authors", []) + [author_user_id]
        return self._plan

    async def is_relay_reader(self, iid):
        return iid in self.readers

    async def upload(self, plan, *, author_user_id=""):
        self.uploaded.append((plan, author_user_id))


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
    assert s._with_media_refs({"x": 1}, None) == {"x": 1}
    await s._upload_media(None)


async def test_no_urls_plans_nothing():
    s = _Sender()
    gfs = _Gfs(plan=_Plan())
    s.attach_media_gfs(gfs)
    assert (
        await s._plan_media_refs("sp", [None, ""], item_id="item-1", followers=False)
        is None
    )
    assert gfs.calls == []


async def test_plan_shared_descriptor_and_upload():
    """v_97 — in a GFS space the descriptor rides the ONE shared payload
    every household reads (no per-household copy), and the upload is
    queued for its author."""
    s = _Sender()
    gfs = _Gfs(plan=_Plan(), readers={"e"})
    s.attach_media_gfs(gfs)
    plan = await s._plan_media_refs(
        "sp",
        ["api/media/a.webp"],
        item_id="item-1",
        followers=True,
        author_user_id="anna",
    )
    assert gfs.calls == [("sp", ["api/media/a.webp"], True)]
    assert gfs.items == ["item-1"]
    assert gfs.authors == ["anna"]
    base = {"x": 1}
    assert s._with_media_refs(base, plan) == {"x": 1, "media_refs": plan.wire()}
    assert base == {"x": 1}
    await s._upload_media(plan, author_user_id="anna")
    assert gfs.uploaded == [(plan, "anna")]


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
