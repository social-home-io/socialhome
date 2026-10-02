"""Unit tests for socialhome.services.space_moderation_federation (v_43).

The protocol-level proof (four real households) lives in
``tests/protocol/test_space_moderation_federated.py``; these pin the
outbound targeting and the remote-payload sanitiser over stubs.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from socialhome.domain.federation import DeliveryResult, FederationEventType
from socialhome.domain.space import (
    HostTooOldError,
    ModerationStatus,
    SpaceModerationItem,
)
from socialhome.services.space_moderation_federation import (
    SpaceModerationFederation,
    sanitize_remote_payload,
)

FET = FederationEventType
NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


class _Fed:
    """Only the targeted door — no ``broadcast_to_space_members`` at all, so
    a pending item that tried to broadcast would raise."""

    def __init__(self, own: str, versions: dict[str, int]) -> None:
        self.own_instance_id = own
        self.versions = versions
        self.sent: list[tuple[str, FederationEventType, dict]] = []

    async def peer_supports(self, instance_id, *, min_version):
        return self.versions.get(instance_id, 0) >= min_version

    async def send_with_mesh_fallback(
        self, *, to_instance_id, event_type, payload, space_id=None
    ):
        self.sent.append((to_instance_id, event_type, payload))
        return DeliveryResult(instance_id=to_instance_id, ok=True)


class _Seats:
    def __init__(self, rows) -> None:
        self.rows = rows

    async def list_instances_with_roles(self, space_id, roles):
        return sorted(
            {r.instance_id for r in self.rows if r.role in roles and not r.tombstoned}
        )

    async def get_including_tombstones(self, space_id, instance_id, user_id):
        for r in self.rows:
            if r.user_id == user_id:
                return r
        return None


class _Media:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def enqueue_for_post(self, **kwargs) -> None:
        self.calls.append(kwargs)


def _seat(inst, uid, role="member", tombstoned=False):
    return SimpleNamespace(
        instance_id=inst,
        user_id=uid,
        role=role,
        tombstoned=tombstoned,
        display_name=uid.title(),
    )


SPACE = SimpleNamespace(id="sp", owner_instance_id="host")


def _fed(own="house-a", versions=None, rows=None, media=None):
    fed = _Fed(own, versions or {"host": 43, "house-mod": 43, "house-adm": 43})
    out = SpaceModerationFederation(
        federation_service=fed,  # type: ignore[arg-type]
        space_repo=SimpleNamespace(),  # type: ignore[arg-type]
        remote_member_repo=_Seats(  # type: ignore[arg-type]
            rows
            if rows is not None
            else [
                _seat("host", "u-h"),
                _seat("house-a", "u-a"),
                _seat("house-d", "u-d"),
                _seat("house-mod", "u-mod", "moderator"),
                _seat("house-adm", "u-adm", "admin"),
                _seat("house-gone", "u-g", "moderator", tombstoned=True),
            ]
        ),
        authorship=SimpleNamespace(),  # type: ignore[arg-type]
        media_sync=media,
    )
    return out, fed


def _item(feature="stickies", payload=None):
    return SpaceModerationItem(
        id="item-1",
        space_id="sp",
        feature=feature,
        action="create",
        submitted_by="u-a",
        payload=payload or {"entity": "sticky", "target_id": "s-1", "content": "x"},
        current_snapshot=None,
        submitted_at=NOW,
        expires_at=NOW + timedelta(days=7),
        status=ModerationStatus.PENDING,
    )


async def test_targets_are_the_host_and_live_reviewer_households_only():
    out, _ = _fed()
    assert await out.submission_targets(SPACE) == ["host", "house-adm", "house-mod"]


async def test_the_host_sends_to_the_reviewers_but_never_to_itself():
    out, _ = _fed(own="host")
    assert await out.submission_targets(SPACE) == ["house-adm", "house-mod"]


async def test_an_old_reviewer_is_skipped_and_an_old_host_refuses():
    out, _ = _fed(versions={"host": 43, "house-mod": 42, "house-adm": 43})
    assert await out.submission_targets(SPACE) == ["host", "house-adm"]
    old_host, _ = _fed(versions={"host": 42, "house-mod": 43, "house-adm": 43})
    with pytest.raises(HostTooOldError):
        await old_host.submission_targets(SPACE)


async def test_a_submission_is_one_sealed_send_per_target_never_a_broadcast():
    out, fed = _fed()
    await out.send_submitted(SPACE, _item(), ["host", "house-mod"])
    assert [(to, et) for to, et, _p in fed.sent] == [
        ("host", FET.SPACE_MODERATION_SUBMITTED),
        ("house-mod", FET.SPACE_MODERATION_SUBMITTED),
    ]
    payload = fed.sent[0][2]
    assert payload["item_id"] == "item-1"
    assert payload["submitted_by"] == "u-a"
    assert payload["payload"]["content"] == "x"
    assert payload["space_id"] == "sp"


async def test_a_posts_media_goes_to_the_targets_only():
    media = _Media()
    out, _ = _fed(media=media)
    post = _item(
        "posts",
        {
            "entity": "post",
            "target_id": "p-1",
            "image_urls": ["api/media/a.webp"],
            "media_url": None,
            "attachments": {"bazaar": {"image_urls": ["api/media/b.webp"]}},
        },
    )
    await out.send_submitted(SPACE, post, ["host"])
    assert media.calls == [
        {
            "post_id": "p-1",
            "target_instance_ids": ["host"],
            "media_urls": ["api/media/a.webp", "api/media/b.webp"],
            "space_id": "sp",
        }
    ]
    await out.send_submitted(SPACE, _item(), ["host"])
    assert len(media.calls) == 1  # a sticky has no media


async def test_a_decision_reaches_the_reviewers_and_the_submitters_household():
    out, fed = _fed(
        own="house-mod", versions={"host": 43, "house-a": 43, "house-adm": 43}
    )
    await out.send_decided(
        SPACE,
        _item(),
        decision=ModerationStatus.REJECTED,
        decided_by="u-mod",
        reason="spam",
    )
    assert sorted(to for to, _et, _p in fed.sent) == ["host", "house-a", "house-adm"]
    assert all(et is FET.SPACE_MODERATION_DECIDED for _to, et, _p in fed.sent)
    assert fed.sent[0][2]["reason"] == "spam"
    assert "house-d" not in {to for to, _et, _p in fed.sent}


async def test_remote_writer_and_display_name_read_the_roster_mirror():
    out, _ = _fed()
    assert await out.is_remote_writer("sp", "u-d")
    assert not await out.is_remote_writer("sp", "u-g")  # tombstoned
    assert not await out.is_remote_writer("sp", "u-a")  # our own household
    assert not await out.is_remote_writer("sp", "u-nobody")
    assert await out.display_name("sp", "u-d") == "U-D"
    assert await out.display_name("sp", "u-nobody") is None


def test_sanitize_keeps_local_media_and_drops_remote_urls():
    out = sanitize_remote_payload(
        "posts",
        {
            "media_url": "https://tracker.example/x.gif",
            "image_urls": [
                "api/media/a.webp",
                "https://evil/b.png",
                "/api/media/c.webp",
            ],
            "file_meta": {"url": "https://evil/f.pdf"},
            "location": {"lat": 47.123456789, "lon": 8.987654321, "label": None},
            "link_preview": {"url": "https://x", "thumbnail_url": "https://evil/t.png"},
            "attachments": {"bazaar": {"image_urls": ["https://evil/z.png"]}},
        },
    )
    assert out["media_url"] is None
    assert out["image_urls"] == ["api/media/a.webp", "/api/media/c.webp"]
    assert out["file_meta"] is None
    assert out["location"] == {"lat": 47.1235, "lon": 8.9877, "label": None}
    assert out["link_preview"]["thumbnail_url"] is None
    assert out["attachments"]["bazaar"]["image_urls"] == []
    page = sanitize_remote_payload(
        "pages",
        {"cover_image_url": "https://evil/c.png", "patch": {"cover_image_url": "x"}},
    )
    assert page["cover_image_url"] is None
    assert page["patch"]["cover_image_url"] is None
    event = sanitize_remote_payload("calendar", {"cover_url": "/api/media/ok.webp"})
    assert event["cover_url"] == "/api/media/ok.webp"
    assert sanitize_remote_payload("stickies", {"content": "x"}) == {"content": "x"}


async def test_a_reviewer_households_approval_goes_to_the_host_only():
    out, fed = _fed(own="house-mod")
    await out.send_release_request(SPACE, _item(), decided_by="u-mod")
    assert [(to, et, p["decision"]) for to, et, p in fed.sent] == [
        ("host", FET.SPACE_MODERATION_DECIDED, "approved")
    ]
    host_side, host_fed = _fed(own="host")
    await host_side.send_release_request(SPACE, _item(), decided_by="u-h")
    assert host_fed.sent == []
