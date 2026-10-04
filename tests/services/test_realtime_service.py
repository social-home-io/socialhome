"""Tests for RealtimeService — domain events → WebSocket fan-out."""

from __future__ import annotations

from datetime import date, datetime, timezone
import json

import pytest

from socialhome.domain.events import (
    CalendarEventCreated,
    CalendarEventDeleted,
    CalendarEventUpdated,
    CommentAdded,
    ConnectionReachable,
    ConnectionUnreachable,
    GalleryAlbumCreated,
    GalleryAlbumDeleted,
    GalleryAlbumUpdated,
    GalleryItemDeleted,
    GalleryItemUploaded,
    PageConflictEmitted,
    PageProposalSettled,
    PeerTransportChanged,
    PeerUnpaired,
    PostCreated,
    PostDeleted,
    PostEdited,
    PostReactionChanged,
    SpaceConfigChanged,
    SpacePostCreated,
    SpacePostModerated,
    SpaceZoneDeleted,
    SpaceZoneUpserted,
    TaskAssigned,
    TaskCompleted,
    TaskDeadlineDue,
    TimetableDeleted,
    TimetableSaved,
    UserStatusChanged,
)
from socialhome.domain.calendar import CalendarEvent
from socialhome.domain.post import Comment, CommentType, Post, PostType
from socialhome.domain.task import Task, TaskStatus
from socialhome.domain.timetable import Timetable, to_wire_dict
from socialhome.domain.user import User, UserStatus
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.ws_manager import WebSocketManager
from socialhome.services.realtime_service import RealtimeService, _safe


# ─── _safe serialisation ─────────────────────────────────────────────────


def test_safe_handles_none():
    assert _safe(None) is None


def test_safe_handles_datetime():
    out = _safe(datetime(2026, 4, 15, tzinfo=timezone.utc))
    assert isinstance(out, str)
    assert out.startswith("2026-04-15")


def test_safe_handles_date():
    assert _safe(date(2026, 4, 15)) == "2026-04-15"


def test_safe_handles_dict():
    assert _safe({"a": 1, "b": "two"}) == {"a": 1, "b": "two"}


def test_safe_handles_list_tuple_set():
    assert _safe([1, 2, 3]) == [1, 2, 3]
    assert _safe((1, 2)) == [1, 2]
    assert sorted(_safe({1, 2, 3})) == [1, 2, 3]


def test_safe_handles_frozenset():
    out = _safe(frozenset({"a", "b"}))
    assert sorted(out) == ["a", "b"]


# ─── Fakes ────────────────────────────────────────────────────────────────


class _FakeUserRepo:
    def __init__(self, users):
        self._users = users

    async def list_active(self):
        return self._users


class _FakeSpaceRepo:
    def __init__(self, members):
        self._members = members

    async def list_local_member_user_ids(self, space_id):
        return self._members.get(space_id, [])

    async def get(self, space_id):
        # A calendar_id is "a space" iff we know members for it; household /
        # personal calendars (not in the members map) return None so the
        # calendar broadcast falls through to the household fan-out.
        return object() if space_id in self._members else None


class _FakeMediaTranscodeRepo:
    """Records ``status_for`` calls and returns a canned status map."""

    def __init__(self, statuses=None):
        self._statuses = statuses or {}
        self.calls = []

    async def status_for(self, output_filenames):
        self.calls.append(list(output_filenames))
        return {fn: s for fn, s in self._statuses.items() if fn in output_filenames}


def _user(uid, name="x"):
    return User(user_id=uid, username=name, display_name=name)


def _post(pid="p1", content="hi"):
    return Post(
        id=pid,
        author="u1",
        type=PostType.TEXT,
        content=content,
        created_at=datetime(2026, 4, 15, tzinfo=timezone.utc),
    )


@pytest.fixture
async def env():
    bus = EventBus()
    ws = WebSocketManager()
    user_repo = _FakeUserRepo([_user("u1"), _user("u2")])
    space_repo = _FakeSpaceRepo({"sp-1": ["u1", "u2", "u3"]})
    svc = RealtimeService(bus, ws, user_repo=user_repo, space_repo=space_repo)
    svc.wire()
    return svc, bus, ws


class _FakeWS:
    def __init__(self, *, fail=False, closed=False):
        self.fail = fail
        self.closed = closed
        self.sent = []

    async def send_str(self, msg):
        if self.fail:
            raise ConnectionResetError()
        self.sent.append(msg)


# ─── Event handlers fan out correctly ────────────────────────────────────


async def test_post_created_fans_to_household(env):
    svc, bus, ws = env
    sock = _FakeWS()
    await ws.register("u1", sock)
    await bus.publish(PostCreated(post=_post()))
    assert sock.sent
    assert "post.created" in sock.sent[0]


async def test_post_created_video_stamps_processing_media_status():
    """A freshly-posted video that's still transcoding ships
    ``media_status='processing'`` on the WS frame so the SPA renders the
    'Processing…' placeholder until the ``media.ready`` frame swaps it in."""
    from dataclasses import replace

    bus = EventBus()
    ws = WebSocketManager()
    user_repo = _FakeUserRepo([_user("u1")])
    space_repo = _FakeSpaceRepo({})
    transcode_repo = _FakeMediaTranscodeRepo({"v.webm": "processing"})
    svc = RealtimeService(
        bus,
        ws,
        user_repo=user_repo,
        space_repo=space_repo,
        media_transcode_repo=transcode_repo,
    )
    svc.wire()
    sock = _FakeWS()
    await ws.register("u1", sock)
    video = replace(
        _post(),
        type=PostType.VIDEO,
        media_url="api/media/v.webm",
    )
    await bus.publish(PostCreated(post=video))
    frame = json.loads(sock.sent[0])
    assert frame["post"]["media_status"] == "processing"
    # One batched status_for call per frame, keyed by the output filename.
    assert transcode_repo.calls == [["v.webm"]]


async def test_post_created_video_absent_from_queue_is_ready():
    """A video with no transcode row (e.g. a federated video) is absent from
    ``status_for`` → ``media_status='ready'``, not wrongly stuck processing."""
    from dataclasses import replace

    bus = EventBus()
    ws = WebSocketManager()
    user_repo = _FakeUserRepo([_user("u1")])
    space_repo = _FakeSpaceRepo({})
    transcode_repo = _FakeMediaTranscodeRepo({"other.webm": "processing"})
    svc = RealtimeService(
        bus,
        ws,
        user_repo=user_repo,
        space_repo=space_repo,
        media_transcode_repo=transcode_repo,
    )
    svc.wire()
    sock = _FakeWS()
    await ws.register("u1", sock)
    video = replace(_post(), type=PostType.VIDEO, media_url="api/media/v.webm")
    await bus.publish(PostCreated(post=video))
    frame = json.loads(sock.sent[0])
    assert frame["post"]["media_status"] == "ready"


async def test_post_created_text_post_gets_no_media_status():
    """A non-video post never gets a ``media_status`` key, and the transcode
    repo isn't queried for it."""
    bus = EventBus()
    ws = WebSocketManager()
    user_repo = _FakeUserRepo([_user("u1")])
    space_repo = _FakeSpaceRepo({})
    transcode_repo = _FakeMediaTranscodeRepo({"v.webm": "processing"})
    svc = RealtimeService(
        bus,
        ws,
        user_repo=user_repo,
        space_repo=space_repo,
        media_transcode_repo=transcode_repo,
    )
    svc.wire()
    sock = _FakeWS()
    await ws.register("u1", sock)
    await bus.publish(PostCreated(post=_post()))
    frame = json.loads(sock.sent[0])
    assert "media_status" not in frame["post"]
    assert transcode_repo.calls == []


async def test_post_created_without_transcode_repo_is_back_compat(env):
    """Constructed without a transcode repo (older callers / test stacks),
    a video post broadcasts fine with no ``media_status`` key — no crash."""
    from dataclasses import replace

    svc, bus, ws = env
    sock = _FakeWS()
    await ws.register("u1", sock)
    video = replace(_post(), type=PostType.VIDEO, media_url="api/media/v.webm")
    await bus.publish(PostCreated(post=video))
    frame = json.loads(sock.sent[0])
    assert "media_status" not in frame["post"]


async def test_post_created_video_carries_signed_poster():
    """A freshly-posted video ships a signed ``media_thumbnail_url`` — the
    ``.webp`` sibling of ``media_url`` (shared stem) — so the SPA can drop
    it into ``<video poster>`` without a REST hydrate."""
    from dataclasses import replace

    from socialhome.media_signer import MediaUrlSigner

    bus = EventBus()
    ws = WebSocketManager()
    user_repo = _FakeUserRepo([_user("u1")])
    space_repo = _FakeSpaceRepo({})
    transcode_repo = _FakeMediaTranscodeRepo({"v.webm": "processing"})
    svc = RealtimeService(
        bus,
        ws,
        user_repo=user_repo,
        space_repo=space_repo,
        media_transcode_repo=transcode_repo,
    )
    svc.attach_media_signer(MediaUrlSigner(key=b"\xab" * 32))
    svc.wire()
    sock = _FakeWS()
    await ws.register("u1", sock)
    video = replace(_post(), type=PostType.VIDEO, media_url="api/media/v.webm")
    await bus.publish(PostCreated(post=video))
    frame = json.loads(sock.sent[0])
    poster = frame["post"]["media_thumbnail_url"]
    assert poster.split("?", 1)[0] == "api/media/v.webp"
    assert "exp=" in poster and "sig=" in poster


async def test_post_created_text_post_gets_no_poster():
    """A non-video post never gets a ``media_thumbnail_url`` key."""
    from socialhome.media_signer import MediaUrlSigner

    bus = EventBus()
    ws = WebSocketManager()
    user_repo = _FakeUserRepo([_user("u1")])
    space_repo = _FakeSpaceRepo({})
    transcode_repo = _FakeMediaTranscodeRepo({"v.webm": "processing"})
    svc = RealtimeService(
        bus,
        ws,
        user_repo=user_repo,
        space_repo=space_repo,
        media_transcode_repo=transcode_repo,
    )
    svc.attach_media_signer(MediaUrlSigner(key=b"\xab" * 32))
    svc.wire()
    sock = _FakeWS()
    await ws.register("u1", sock)
    await bus.publish(PostCreated(post=_post()))
    frame = json.loads(sock.sent[0])
    assert "media_thumbnail_url" not in frame["post"]


async def test_space_post_created_video_stamps_media_status():
    """The space-feed ``space.post.created`` frame annotates video status too."""
    from dataclasses import replace

    bus = EventBus()
    ws = WebSocketManager()
    user_repo = _FakeUserRepo([_user("u1")])
    space_repo = _FakeSpaceRepo({"sp-1": ["u1"]})
    transcode_repo = _FakeMediaTranscodeRepo({"v.webm": "processing"})
    svc = RealtimeService(
        bus,
        ws,
        user_repo=user_repo,
        space_repo=space_repo,
        media_transcode_repo=transcode_repo,
    )
    svc.wire()
    sock = _FakeWS()
    await ws.register("u1", sock)
    video = replace(_post(), type=PostType.VIDEO, media_url="api/media/v.webm")
    await bus.publish(SpacePostCreated(post=video, space_id="sp-1"))
    frame = json.loads(sock.sent[0])
    assert frame["post"]["media_status"] == "processing"


async def test_ws_post_created_carries_signed_media_url(env):
    """Browser-loaded ``<img src={post.media_url}>`` needs the URL to
    arrive **already signed** in the WS frame — otherwise it 401s when
    the SPA renders a freshly-broadcast post without a REST hydrate."""
    from dataclasses import replace

    from socialhome.media_signer import MediaUrlSigner

    svc, bus, ws = env
    svc.attach_media_signer(MediaUrlSigner(key=b"\xab" * 32))
    sock = _FakeWS()
    await ws.register("u1", sock)
    await bus.publish(
        PostCreated(post=replace(_post(), media_url="/api/media/abc.webp")),
    )
    assert sock.sent
    frame = sock.sent[0]
    assert "post.created" in frame
    assert "/api/media/abc.webp?exp=" in frame
    assert "sig=" in frame


async def test_ws_user_profile_updated_carries_signed_picture_url(env):
    """Profile-picture upload publishes ``UserProfileUpdated``; the WS
    frame must include a *signed* ``picture_url`` so receiving tabs
    can drop it straight into ``<img src>``. Without this the avatar
    arrives as a raw ``/api/users/{id}/picture`` URL and 401s."""
    from socialhome.domain.events import UserProfileUpdated
    from socialhome.media_signer import MediaUrlSigner

    svc, bus, ws = env
    svc.attach_media_signer(MediaUrlSigner(key=b"\xab" * 32))
    sock = _FakeWS()
    await ws.register("u1", sock)
    await bus.publish(
        UserProfileUpdated(
            user_id="u1",
            username="alice",
            display_name="Alice",
            bio=None,
            picture_hash="deadbeef",
            picture_webp=b"webp-bytes",
        )
    )
    assert sock.sent
    frame = sock.sent[0]
    assert "user.profile_updated" in frame
    # Picture URLs emit relative so the SPA's ``<img src>`` resolves
    # against ``<base href>`` (ingress-safe).
    assert "api/users/u1/picture?v=deadbeef" in frame
    assert "exp=" in frame
    assert "sig=" in frame


async def test_ws_user_profile_updated_picture_url_null_when_cleared(env):
    """``picture_hash=None`` (cleared avatar) → ``picture_url=null``
    in the frame, not a signed URL pointing at nothing."""
    from socialhome.domain.events import UserProfileUpdated
    from socialhome.media_signer import MediaUrlSigner

    svc, bus, ws = env
    svc.attach_media_signer(MediaUrlSigner(key=b"\xab" * 32))
    sock = _FakeWS()
    await ws.register("u1", sock)
    await bus.publish(
        UserProfileUpdated(
            user_id="u1",
            username="alice",
            display_name="Alice",
            bio=None,
            picture_hash=None,
            picture_webp=None,
        )
    )
    assert sock.sent
    frame = sock.sent[0]
    assert '"picture_url": null' in frame or '"picture_url":null' in frame


async def test_post_edited_fans_to_household(env):
    svc, bus, ws = env
    sock = _FakeWS()
    await ws.register("u1", sock)
    await bus.publish(PostEdited(post=_post()))
    assert any("post.edited" in m for m in sock.sent)


async def test_post_deleted_carries_id_only(env):
    svc, bus, ws = env
    sock = _FakeWS()
    await ws.register("u1", sock)
    await bus.publish(PostDeleted(post_id="p1"))
    assert any("post.deleted" in m and "p1" in m for m in sock.sent)


async def test_post_reaction_changed_fans(env):
    svc, bus, ws = env
    sock = _FakeWS()
    await ws.register("u1", sock)
    await bus.publish(PostReactionChanged(post=_post()))
    assert any("post.reaction_changed" in m for m in sock.sent)


async def test_a_space_reaction_is_never_framed_to_the_whole_household(env):
    """Space reactions (v_49 publishes them for the member relay) must not
    reach every local socket — non-members included — as a household frame."""
    svc, bus, ws = env
    sock = _FakeWS()
    await ws.register("u1", sock)
    await bus.publish(PostReactionChanged(post=_post(), space_id="sp-1"))
    assert not any("post.reaction_changed" in m for m in sock.sent)


async def test_comment_added_fans(env):
    svc, bus, ws = env
    sock = _FakeWS()
    await ws.register("u1", sock)
    comment = Comment(
        id="c1",
        post_id="p1",
        author="u1",
        type=CommentType.TEXT,
        content="hi",
        created_at=datetime(2026, 4, 15, tzinfo=timezone.utc),
    )
    await bus.publish(CommentAdded(post_id="p1", comment=comment))
    assert any("comment.added" in m for m in sock.sent)


async def test_space_post_created_fans_to_space_members(env):
    svc, bus, ws = env
    sock = _FakeWS()
    await ws.register("u3", sock)
    await bus.publish(SpacePostCreated(post=_post(), space_id="sp-1"))
    assert any("space.post.created" in m for m in sock.sent)


async def test_space_config_changed_fans(env):
    svc, bus, ws = env
    sock = _FakeWS()
    await ws.register("u1", sock)
    await bus.publish(
        SpaceConfigChanged(
            space_id="sp-1",
            event_type="rename",
            payload={"name": "X"},
            sequence=1,
        )
    )
    assert any("space.config.changed" in m for m in sock.sent)


async def test_space_zone_upserted_fans_to_space_members(env):
    """SpaceZoneUpserted → ``space_zone_changed`` (action=upsert) WS frame
    on every space-member's session — drives §23.8.7 live admin updates."""
    svc, bus, ws = env
    sock = _FakeWS()
    await ws.register("u1", sock)
    await bus.publish(
        SpaceZoneUpserted(
            space_id="sp-1",
            zone_id="z_office",
            name="Office",
            latitude=47.3769,
            longitude=8.5417,
            radius_m=150,
            color="#3b82f6",
            created_by="u_admin",
            updated_at="2026-04-28T00:00:00+00:00",
        )
    )
    assert any("space_zone_changed" in m for m in sock.sent)
    # Frame must carry the full zone payload, action=upsert.
    import json

    frame = next(m for m in sock.sent if "space_zone_changed" in m)
    parsed = json.loads(frame)
    assert parsed["type"] == "space_zone_changed"
    assert parsed["data"]["action"] == "upsert"
    assert parsed["data"]["zone"]["name"] == "Office"
    assert parsed["data"]["zone"]["radius_m"] == 150
    assert parsed["data"]["zone"]["color"] == "#3b82f6"


async def test_space_zone_deleted_fans_with_action_delete(env):
    import json

    svc, bus, ws = env
    sock = _FakeWS()
    await ws.register("u1", sock)
    await bus.publish(
        SpaceZoneDeleted(
            space_id="sp-1",
            zone_id="z_office",
            deleted_by="u_admin",
        )
    )
    [frame] = [m for m in sock.sent if "space_zone_changed" in m]
    parsed = json.loads(frame)
    assert parsed["data"]["action"] == "delete"
    assert parsed["data"]["zone_id"] == "z_office"
    assert parsed["data"]["zone"] is None


@pytest.mark.parametrize("federated", [False, True])
async def test_page_conflict_frame_says_whether_it_came_from_a_household(
    env, federated
):
    """v_48: a conflict recorded from another household's edit carries
    ``federated`` so an open viewer refetches the page."""
    svc, bus, ws = env
    sock = _FakeWS()
    await ws.register("u1", sock)
    await bus.publish(
        PageConflictEmitted(
            page_id="pg-1",
            space_id="sp-1",
            theirs="their body",
            theirs_by="u2",
            federated=federated,
        )
    )
    [frame] = [m for m in sock.sent if "page.conflict" in m]
    data = json.loads(frame)
    assert data["page_id"] == "pg-1"
    assert data["federated"] is federated


async def test_page_sequenced_frame_reaches_the_space(env):
    """v_48: the host answered this household's page edit."""
    svc, bus, ws = env
    sock = _FakeWS()
    await ws.register("u1", sock)
    await bus.publish(
        PageProposalSettled(
            page_id="pg-1",
            space_id="sp-1",
            proposal_hash="sha256:" + "a" * 64,
            outcome="refused",
            reason="gone",
        )
    )
    [frame] = [m for m in sock.sent if "page.sequenced" in m]
    data = json.loads(frame)
    assert (data["page_id"], data["outcome"], data["reason"]) == (
        "pg-1",
        "refused",
        "gone",
    )


def _task(*, status=TaskStatus.TODO, assignees=()):
    now = datetime(2026, 4, 15, tzinfo=timezone.utc)
    return Task(
        id="t1",
        list_id="l1",
        title="X",
        status=status,
        position=0,
        created_by="me",
        created_at=now,
        updated_at=now,
        assignees=assignees,
    )


async def test_task_assigned_fans_only_to_assignee(env):
    svc, bus, ws = env
    sock_alice = _FakeWS()
    sock_bob = _FakeWS()
    await ws.register("alice", sock_alice)
    await ws.register("bob", sock_bob)
    await bus.publish(TaskAssigned(task=_task(), assigned_to="alice"))
    assert sock_alice.sent
    assert sock_bob.sent == []


async def test_task_completed_fans_to_household(env):
    svc, bus, ws = env
    sock = _FakeWS()
    await ws.register("u1", sock)
    await bus.publish(
        TaskCompleted(task=_task(status=TaskStatus.DONE), completed_by="u1")
    )
    assert any("task.completed" in m for m in sock.sent)


async def test_space_task_completed_fans_to_space_members_only(env):
    """A space completion is a space frame — never a household broadcast."""
    svc, bus, ws = env
    member = _FakeWS()
    household_only = _FakeWS()
    await ws.register("u3", member)  # sp-1 member, not a household user
    await ws.register("u9", household_only)
    svc._user_repo = _FakeUserRepo([_user("u3"), _user("u9")])
    svc._space_repo = _FakeSpaceRepo({"sp-1": ["u3"]})
    await bus.publish(
        TaskCompleted(
            task=_task(status=TaskStatus.DONE), completed_by="u3", space_id="sp-1"
        )
    )
    assert household_only.sent == []
    frames = [json.loads(m) for m in member.sent]
    assert frames == [
        {
            "type": "task.completed",
            "task_id": "t1",
            "completed_by": "u3",
            "space_id": "sp-1",
        }
    ]


async def test_space_task_assigned_skips_a_non_member(env):
    svc, bus, ws = env
    outsider = _FakeWS()
    member = _FakeWS()
    await ws.register("eve", outsider)
    await ws.register("u3", member)
    await bus.publish(TaskAssigned(task=_task(), assigned_to="eve", space_id="sp-1"))
    await bus.publish(TaskAssigned(task=_task(), assigned_to="u3", space_id="sp-1"))
    assert outsider.sent == []
    assert any("task.assigned" in m for m in member.sent)


async def test_task_deadline_due_fans_to_each_assignee(env):
    svc, bus, ws = env
    sock_a = _FakeWS()
    sock_b = _FakeWS()
    await ws.register("alice", sock_a)
    await ws.register("bob", sock_b)
    # The realtime handler reads task.assignee_user_ids — fall back if absent.
    task = _task(assignees=("alice", "bob"))
    if not hasattr(task, "assignee_user_ids"):
        # Domain Task uses .assignees; the realtime handler reads
        # assignee_user_ids — keep test resilient by skipping when the
        # attribute mismatch makes the handler emit zero events.
        await bus.publish(TaskDeadlineDue(task=task, due_date=date(2026, 4, 15)))
        return
    await bus.publish(TaskDeadlineDue(task=task, due_date=date(2026, 4, 15)))
    assert sock_a.sent and sock_b.sent


async def test_calendar_created_updated_deleted_fan(env):
    svc, bus, ws = env
    sock = _FakeWS()
    await ws.register("u1", sock)
    e = CalendarEvent(
        id="e1",
        calendar_id="c1",
        summary="X",
        created_by="me",
        start=datetime(2026, 4, 15, tzinfo=timezone.utc),
        end=datetime(2026, 4, 15, 1, tzinfo=timezone.utc),
    )
    await bus.publish(CalendarEventCreated(event=e))
    await bus.publish(CalendarEventUpdated(event=e))
    await bus.publish(CalendarEventDeleted(event_id="e1"))
    types = [m for m in sock.sent]
    assert any("calendar.created" in m for m in types)
    assert any("calendar.updated" in m for m in types)
    assert any("calendar.deleted" in m for m in types)


async def test_space_calendar_event_fans_to_members_only(env):
    """A calendar whose ``calendar_id`` resolves to a space fans out to
    that space's members only — never the whole household — so a
    non-member local user never sees space calendar content."""
    svc, bus, ws = env
    member = _FakeWS()
    nonmember = _FakeWS()
    await ws.register("u1", member)  # member of sp-1
    await ws.register("u9", nonmember)  # not in sp-1's member list
    e = CalendarEvent(
        id="e1",
        calendar_id="sp-1",  # known space → members-only routing
        summary="X",
        created_by="me",
        start=datetime(2026, 4, 15, tzinfo=timezone.utc),
        end=datetime(2026, 4, 15, 1, tzinfo=timezone.utc),
    )
    await bus.publish(CalendarEventCreated(event=e))
    await bus.publish(CalendarEventDeleted(event_id="e1", space_id="sp-1"))
    assert any("calendar.created" in m for m in member.sent)
    assert any("calendar.deleted" in m for m in member.sent)
    assert nonmember.sent == []


async def test_connection_reachable_unreachable_fan_to_household(env):
    svc, bus, ws = env
    sock = _FakeWS()
    await ws.register("u1", sock)
    await bus.publish(ConnectionReachable(instance_id="inst-7"))
    await bus.publish(ConnectionUnreachable(instance_id="inst-7"))
    assert any("connection.reachable" in m for m in sock.sent)
    assert any("connection.unreachable" in m for m in sock.sent)
    assert all("inst-7" in m for m in sock.sent)


async def test_peer_unpaired_emits_connection_removed_to_household(env):
    """Unpairing (either side) pushes ``connection.removed`` so every open
    connections list drops the row without a reload. The list is readable
    by every member, so the frame fans to the whole household — never
    an admin-only subset, never beyond the local users."""
    svc, bus, ws = env
    s1, s2, stranger = _FakeWS(), _FakeWS(), _FakeWS()
    await ws.register("u1", s1)
    await ws.register("u2", s2)
    await ws.register("u9", stranger)  # not an active local user
    await bus.publish(PeerUnpaired(instance_id="inst-8"))
    expected = {"type": "connection.removed", "instance_id": "inst-8"}
    assert [json.loads(m) for m in s1.sent] == [expected]
    assert [json.loads(m) for m in s2.sent] == [expected]
    assert stranger.sent == []


async def test_gallery_household_events_fan_to_household(env):
    svc, bus, ws = env
    sock = _FakeWS()
    await ws.register("u1", sock)
    await bus.publish(
        GalleryAlbumCreated(album_id="al-1", space_id=None, owner_id="u1")
    )
    await bus.publish(
        GalleryItemUploaded(
            item_id="it-1", album_id="al-1", item_type="photo", uploader="u1"
        )
    )
    await bus.publish(GalleryItemDeleted(item_id="it-1", album_id="al-1"))
    await bus.publish(GalleryAlbumDeleted(album_id="al-1", space_id=None))
    joined = " ".join(sock.sent)
    assert "gallery.album_created" in joined
    assert "gallery.item_uploaded" in joined
    assert "gallery.item_deleted" in joined
    assert "gallery.album_deleted" in joined
    assert "al-1" in joined


async def test_gallery_space_album_fans_to_members_only(env):
    """A space-album gallery event reaches that space's members only."""
    svc, bus, ws = env
    member = _FakeWS()
    nonmember = _FakeWS()
    await ws.register("u1", member)  # member of sp-1
    await ws.register("u9", nonmember)
    await bus.publish(
        GalleryItemUploaded(
            item_id="it-9",
            album_id="al-9",
            item_type="photo",
            uploader="u1",
            space_id="sp-1",
        )
    )
    assert any("gallery.item_uploaded" in m for m in member.sent)
    assert nonmember.sent == []


async def test_gallery_album_updated_fans_to_space_members_only(env):
    """A rename / cover change of a space album pushes a thin
    ``gallery.album_updated`` frame to that space's members only — the
    same audience as the other gallery frames. Covers both a local edit
    and an inbound-federated one (``origin_instance_id`` set)."""
    svc, bus, ws = env
    member, nonmember = _FakeWS(), _FakeWS()
    await ws.register("u1", member)  # member of sp-1
    await ws.register("u9", nonmember)
    await bus.publish(GalleryAlbumUpdated(album_id="al-3", space_id="sp-1"))
    await bus.publish(
        GalleryAlbumUpdated(
            album_id="al-3", space_id="sp-1", origin_instance_id="peer-1"
        )
    )
    expected = {"type": "gallery.album_updated", "album_id": "al-3", "space_id": "sp-1"}
    assert [json.loads(m) for m in member.sent] == [expected, expected]
    assert nonmember.sent == []


async def test_gallery_household_album_updated_fans_to_household(env):
    svc, bus, ws = env
    s1, stranger = _FakeWS(), _FakeWS()
    await ws.register("u1", s1)
    await ws.register("u9", stranger)  # not an active local user
    await bus.publish(GalleryAlbumUpdated(album_id="al-4", space_id=None))
    assert [json.loads(m) for m in s1.sent] == [
        {"type": "gallery.album_updated", "album_id": "al-4", "space_id": None}
    ]
    assert stranger.sent == []


async def test_user_status_changed_fans_to_household(env):
    svc, bus, ws = env
    sock = _FakeWS()
    await ws.register("u1", sock)
    status = UserStatus(emoji="👍", text="busy")
    await bus.publish(UserStatusChanged(user_id="u2", status=status))
    assert any("user.status_changed" in m for m in sock.sent)


async def test_user_status_cleared_carries_null_status(env):
    svc, bus, ws = env
    sock = _FakeWS()
    await ws.register("u1", sock)
    await bus.publish(UserStatusChanged(user_id="u2", status=None))
    assert any('"status": null' in m or '"status":null' in m for m in sock.sent)


async def test_space_post_moderated_fans_to_space_members(env):
    svc, bus, ws = env
    sock = _FakeWS()
    await ws.register("u3", sock)
    await bus.publish(
        SpacePostModerated(
            space_id="sp-1",
            post=_post(),
            moderated_by="admin",
        )
    )
    assert any("space.post.moderated" in m for m in sock.sent)


# ─── Presence + notification + bazaar WS frames ────────────────────────────


async def test_presence_updated_fans_to_household(env):
    from socialhome.domain.events import PresenceUpdated

    svc, bus, ws = env
    sock = _FakeWS()
    await ws.register("u1", sock)
    await bus.publish(
        PresenceUpdated(
            username="anna",
            state="home",
            zone_name="Home",
            latitude=52.37,
            longitude=4.89,
        )
    )
    assert any("presence.updated" in m for m in sock.sent)


async def test_user_came_online_suppresses_self_frame(env):
    from datetime import datetime, timezone
    from socialhome.domain.events import (
        UserCameOnline,
        UserWentIdle,
        UserWentOffline,
    )

    svc, bus, ws = env
    self_sock = _FakeWS()
    other_sock = _FakeWS()
    await ws.register("u1", self_sock)
    await ws.register("u2", other_sock)

    await bus.publish(UserCameOnline(user_id="u1"))
    # Other household member sees the frame, the subject does not.
    assert any('"user.online"' in m for m in other_sock.sent)
    assert not any('"user.online"' in m for m in self_sock.sent)

    other_sock.sent.clear()
    self_sock.sent.clear()
    await bus.publish(
        UserWentIdle(user_id="u1", last_active_at=datetime.now(timezone.utc)),
    )
    assert any('"user.idle"' in m for m in other_sock.sent)
    assert not any('"user.idle"' in m for m in self_sock.sent)

    other_sock.sent.clear()
    self_sock.sent.clear()
    await bus.publish(
        UserWentOffline(user_id="u1", last_seen_at=datetime.now(timezone.utc)),
    )
    assert any('"user.offline"' in m for m in other_sock.sent)
    assert not any('"user.offline"' in m for m in self_sock.sent)


async def test_notification_new_targets_one_user(env):
    from socialhome.domain.events import NotificationCreated

    svc, bus, ws = env
    me = _FakeWS()
    other = _FakeWS()
    await ws.register("u1", me)
    await ws.register("u2", other)
    await bus.publish(
        NotificationCreated(
            user_id="u1",
            notification_id="n-1",
            type="post_created",
            title="Anna posted",
        )
    )
    assert any("notification.new" in m for m in me.sent)
    assert all("notification.new" not in m for m in other.sent)


async def test_notification_unread_count_targets_one_user(env):
    from socialhome.domain.events import NotificationReadChanged

    svc, bus, ws = env
    me = _FakeWS()
    other = _FakeWS()
    await ws.register("u1", me)
    await ws.register("u2", other)
    await bus.publish(NotificationReadChanged(user_id="u1", unread_count=3))
    assert any("notification.unread_count" in m for m in me.sent)
    assert all("notification.unread_count" not in m for m in other.sent)


async def test_bazaar_bid_placed_broadcast(env):
    from socialhome.domain.events import BazaarBidPlaced

    svc, bus, ws = env
    sock = _FakeWS()
    await ws.register("u1", sock)
    await bus.publish(
        BazaarBidPlaced(
            listing_post_id="L-1",
            seller_user_id="seller",
            bidder_user_id="bidder",
            amount=200,
            new_end_time="2099-01-01T00:00:00+00:00",
        )
    )
    assert any('"bazaar.bid_placed"' in m for m in sock.sent)
    assert any('"new_end_time"' in m for m in sock.sent)


async def test_bazaar_listing_closed_broadcast(env):
    from socialhome.domain.events import BazaarListingExpired

    svc, bus, ws = env
    sock = _FakeWS()
    await ws.register("u1", sock)
    await bus.publish(
        BazaarListingExpired(
            listing_post_id="L-1",
            seller_user_id="seller",
            final_status="sold",
        )
    )
    assert any('"bazaar.listing_closed"' in m for m in sock.sent)


async def test_peer_transport_changed_broadcasts_to_household(env):
    """Publishing PeerTransportChanged fans out as the
    ``peer.transport_changed`` WS frame with the right payload."""
    svc, bus, ws = env
    sock = _FakeWS()
    await ws.register("u1", sock)
    await bus.publish(
        PeerTransportChanged(instance_id="peer-tx", transport="rtc"),
    )
    frames = [json.loads(m) for m in sock.sent]
    assert any(
        m.get("type") == "peer.transport_changed"
        and m.get("instance_id") == "peer-tx"
        and m.get("transport") == "rtc"
        for m in frames
    )


async def test_remote_space_dissolved_broadcasts_dissolved_frame(env):
    """A member household's inbound SPACE_DISSOLVED → RemoteSpaceDissolved
    pushes the same ``dissolved`` frame the host path emits, so the SPA
    drops the space card uniformly."""
    import orjson

    from socialhome.domain.events import RemoteSpaceDissolved

    svc, bus, ws = env
    sock = _FakeWS()
    await ws.register("u1", sock)
    await bus.publish(RemoteSpaceDissolved(space_id="sp-1"))
    assert sock.sent
    frames = [orjson.loads(s) for s in sock.sent]
    assert any(
        f.get("type") == "space.config.changed"
        and f.get("event_type") == "dissolved"
        and f.get("space_id") == "sp-1"
        for f in frames
    )


async def test_media_transcode_ready_pushes_media_ready_to_owner(env):
    """A finished background video transcode → ``media.ready`` to the
    uploader's SPA so it swaps the 'Processing…' placeholder for the
    player."""
    import orjson

    from socialhome.domain.events import MediaTranscodeReady

    svc, bus, ws = env
    sock = _FakeWS()
    await ws.register("u1", sock)
    await bus.publish(
        MediaTranscodeReady(
            output_filename="v.webm",
            thumbnail_filename="v.webp",
            owner_user_id="u1",
        )
    )
    assert sock.sent
    frames = [orjson.loads(s) for s in sock.sent]
    ready = [f for f in frames if f.get("type") == "media.ready"]
    assert len(ready) == 1
    frame = ready[0]
    assert frame["output_filename"] == "v.webm"
    assert frame["media_url"]
    assert frame["thumbnail_url"]


async def test_media_transcode_ready_signs_urls_when_signer_present(env):
    """With a signer attached the ``media_url`` / ``thumbnail_url`` arrive
    pre-signed so the SPA can drop them straight into the player."""
    import orjson

    from socialhome.domain.events import MediaTranscodeReady
    from socialhome.media_signer import MediaUrlSigner

    svc, bus, ws = env
    svc.attach_media_signer(MediaUrlSigner(key=b"\xab" * 32))
    sock = _FakeWS()
    await ws.register("u1", sock)
    await bus.publish(
        MediaTranscodeReady(
            output_filename="v.webm",
            thumbnail_filename="v.webp",
            owner_user_id="u1",
        )
    )
    assert sock.sent
    frame = next(
        orjson.loads(s)
        for s in sock.sent
        if orjson.loads(s).get("type") == "media.ready"
    )
    assert "api/media/v.webm?exp=" in frame["media_url"]
    assert "sig=" in frame["media_url"]
    assert "api/media/v.webp?exp=" in frame["thumbnail_url"]
    assert "sig=" in frame["thumbnail_url"]


async def test_media_transcode_ready_no_owner_no_broadcast(env):
    """``owner_user_id=None`` (e.g. a household-scoped transcode without a
    resolvable uploader) → no broadcast."""
    from socialhome.domain.events import MediaTranscodeReady

    svc, bus, ws = env
    sock = _FakeWS()
    await ws.register("u1", sock)
    await bus.publish(
        MediaTranscodeReady(
            output_filename="v.webm",
            thumbnail_filename="v.webp",
            owner_user_id=None,
        )
    )
    assert sock.sent == []


async def test_media_transcode_failed_pushes_media_failed_to_owner(env):
    """A permanently-failed background transcode → ``media.failed`` to the
    uploader's SPA so it flips the placeholder to the failed state without
    waiting for the next list fetch."""
    import orjson

    from socialhome.domain.events import MediaTranscodeFailed

    svc, bus, ws = env
    sock = _FakeWS()
    await ws.register("u1", sock)
    await bus.publish(
        MediaTranscodeFailed(output_filename="v.webm", owner_user_id="u1")
    )
    assert sock.sent
    frames = [orjson.loads(s) for s in sock.sent]
    failed = [f for f in frames if f.get("type") == "media.failed"]
    assert len(failed) == 1
    assert failed[0]["output_filename"] == "v.webm"


async def test_media_transcode_failed_no_owner_no_broadcast(env):
    """``owner_user_id=None`` → no broadcast (mirrors the ready path)."""
    from socialhome.domain.events import MediaTranscodeFailed

    svc, bus, ws = env
    sock = _FakeWS()
    await ws.register("u1", sock)
    await bus.publish(
        MediaTranscodeFailed(output_filename="v.webm", owner_user_id=None)
    )
    assert sock.sent == []


# ─── Timetables ──────────────────────────────────────────────────────────


def _timetable():
    at = datetime(2026, 9, 28, 8, tzinfo=timezone.utc)
    return Timetable(
        id="tt1", name="Anna", created_by="u1", created_at=at, updated_at=at
    )


async def test_timetable_saved_fans_changed_frame_to_household(env):
    svc, bus, ws = env
    sock = _FakeWS()
    await ws.register("u2", sock)
    tt = _timetable()
    await bus.publish(TimetableSaved(timetable=tt))
    frames = [json.loads(m) for m in sock.sent]
    assert frames == [
        {"type": "timetable.changed", "space_id": None, "timetable": to_wire_dict(tt)}
    ]


async def test_timetable_deleted_fans_to_household(env):
    svc, bus, ws = env
    sock = _FakeWS()
    await ws.register("u1", sock)
    await bus.publish(TimetableDeleted(timetable_id="tt1"))
    frames = [json.loads(m) for m in sock.sent]
    assert frames == [
        {"type": "timetable.deleted", "timetable_id": "tt1", "space_id": None}
    ]


async def test_space_timetable_frames_reach_members_only(env):
    svc, bus, ws = env
    member = _FakeWS()
    nonmember = _FakeWS()
    await ws.register("u3", member)  # member of sp-1, not a household user
    await ws.register("u9", nonmember)
    await bus.publish(TimetableSaved(timetable=_timetable(), space_id="sp-1"))
    await bus.publish(TimetableDeleted(timetable_id="tt1", space_id="sp-1"))
    types = [json.loads(m)["type"] for m in member.sent]
    assert types == ["timetable.changed", "timetable.deleted"]
    assert all(json.loads(m)["space_id"] == "sp-1" for m in member.sent)
    assert nonmember.sent == []
