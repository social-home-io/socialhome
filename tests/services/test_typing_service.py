"""Tests for TypingService — local + federated typing indicator relay."""

from __future__ import annotations

from types import SimpleNamespace


from socialhome.domain.federation import FederationEventType
from socialhome.services.typing_service import (
    TYPING_TTL_SECONDS,
    TypingService,
)


# ─── Fakes ────────────────────────────────────────────────────────────────


class _FakeMember:
    def __init__(self, user_id: str):
        self.user_id = user_id


class _FakeRemoteMember:
    def __init__(self, instance_id: str, remote_username: str = ""):
        self.instance_id = instance_id
        self.remote_username = remote_username


class _FakeConvoRepo:
    def __init__(self, members=None, remote=None):
        self._m = members or []
        self._r = remote or []

    async def list_members(self, cid):
        return self._m

    async def list_remote_members(self, cid):
        return self._r


class _FakeUserRepo:
    """Knows one remote user: ``remote-eve`` (``eve``) of ``remote-1``."""

    async def get_instance_for_user(self, user_id):
        return "remote-1" if user_id == "remote-eve" else None

    async def get_remote(self, user_id):
        if user_id != "remote-eve":
            return None
        return SimpleNamespace(instance_id="remote-1", remote_username="eve")


class _FakeWS:
    def __init__(self):
        self.calls: list[tuple[list, dict]] = []

    async def broadcast_to_users(self, user_ids, payload):
        self.calls.append((list(user_ids), payload))
        return len(user_ids)


class _FakeFed:
    def __init__(self):
        self.own_instance_id = "self"
        self.sent: list[tuple] = []

    async def send_event(self, *, to_instance_id, event_type, payload, **kw):
        self.sent.append((to_instance_id, event_type, payload))


class _Event:
    def __init__(self, et, from_inst, payload):
        self.event_type = et
        self.from_instance = from_inst
        self.payload = payload


# ─── Local fan-out ───────────────────────────────────────────────────────


async def test_user_started_typing_fans_to_other_local_members():
    repo = _FakeConvoRepo(
        members=[
            _FakeMember("alice"),
            _FakeMember("bob"),
            _FakeMember("carol"),
        ]
    )
    ws = _FakeWS()
    svc = TypingService(
        conversation_repo=repo,
        user_repo=_FakeUserRepo(),
        ws_manager=ws,
    )
    n = await svc.user_started_typing(
        conversation_id="c1",
        sender_user_id="alice",
        sender_username="alice",
    )
    assert n == 2
    targets, payload = ws.calls[0]
    assert set(targets) == {"bob", "carol"}
    assert payload["type"] == "conversation.user_typing"
    assert payload["sender_user_id"] == "alice"


async def test_typing_does_not_fan_to_self():
    repo = _FakeConvoRepo(members=[_FakeMember("alice"), _FakeMember("bob")])
    ws = _FakeWS()
    svc = TypingService(
        conversation_repo=repo,
        user_repo=_FakeUserRepo(),
        ws_manager=ws,
    )
    await svc.user_started_typing(
        conversation_id="c1",
        sender_user_id="alice",
        sender_username="alice",
    )
    targets, _ = ws.calls[0]
    assert "alice" not in targets


async def test_typing_throttle_within_one_second():
    """Two events within 1s for the same (conv,user) → second is dropped."""
    repo = _FakeConvoRepo(members=[_FakeMember("alice"), _FakeMember("bob")])
    ws = _FakeWS()
    svc = TypingService(
        conversation_repo=repo,
        user_repo=_FakeUserRepo(),
        ws_manager=ws,
    )
    await svc.user_started_typing(
        conversation_id="c1",
        sender_user_id="alice",
        sender_username="a",
        now=100.0,
    )
    n2 = await svc.user_started_typing(
        conversation_id="c1",
        sender_user_id="alice",
        sender_username="a",
        now=100.5,
    )
    assert n2 == 0
    assert len(ws.calls) == 1


async def test_typing_throttle_lifts_after_a_second():
    repo = _FakeConvoRepo(members=[_FakeMember("alice"), _FakeMember("bob")])
    ws = _FakeWS()
    svc = TypingService(
        conversation_repo=repo,
        user_repo=_FakeUserRepo(),
        ws_manager=ws,
    )
    await svc.user_started_typing(
        conversation_id="c1",
        sender_user_id="alice",
        sender_username="a",
        now=100.0,
    )
    await svc.user_started_typing(
        conversation_id="c1",
        sender_user_id="alice",
        sender_username="a",
        now=101.5,
    )
    assert len(ws.calls) == 2


# ─── is_typing / active_typers ───────────────────────────────────────────


async def test_is_typing_true_within_ttl():
    repo = _FakeConvoRepo(members=[_FakeMember("alice"), _FakeMember("bob")])
    svc = TypingService(
        conversation_repo=repo,
        user_repo=_FakeUserRepo(),
        ws_manager=_FakeWS(),
    )
    await svc.user_started_typing(
        conversation_id="c1",
        sender_user_id="alice",
        sender_username="a",
        now=100.0,
    )
    assert svc.is_typing("c1", "alice", now=103.0) is True
    # After TTL it expires.
    assert svc.is_typing("c1", "alice", now=100.0 + TYPING_TTL_SECONDS + 0.1) is False


async def test_active_typers_filters_by_conversation():
    repo = _FakeConvoRepo(members=[_FakeMember("alice"), _FakeMember("bob")])
    svc = TypingService(
        conversation_repo=repo,
        user_repo=_FakeUserRepo(),
        ws_manager=_FakeWS(),
    )
    await svc.user_started_typing(
        conversation_id="c1",
        sender_user_id="alice",
        sender_username="a",
        now=100.0,
    )
    await svc.user_started_typing(
        conversation_id="c2",
        sender_user_id="bob",
        sender_username="b",
        now=100.0,
    )
    assert svc.active_typers("c1", now=101.0) == ["alice"]
    assert svc.active_typers("c2", now=101.0) == ["bob"]


# ─── Federation fan-out ──────────────────────────────────────────────────


async def test_typing_relays_to_remote_instances():
    repo = _FakeConvoRepo(
        members=[_FakeMember("alice"), _FakeMember("bob")],
        remote=[
            _FakeRemoteMember("remote-1"),
            _FakeRemoteMember("remote-2"),
            _FakeRemoteMember("remote-1"),  # duplicate — dedup
            _FakeRemoteMember("self"),  # own instance — skip
        ],
    )
    fed = _FakeFed()
    svc = TypingService(
        conversation_repo=repo,
        user_repo=_FakeUserRepo(),
        ws_manager=_FakeWS(),
        federation_service=fed,
        own_instance_id="self",
    )
    await svc.user_started_typing(
        conversation_id="c1",
        sender_user_id="alice",
        sender_username="alice",
    )
    targets = {t for t, _, _ in fed.sent}
    assert targets == {"remote-1", "remote-2"}
    for _, et, payload in fed.sent:
        assert et == FederationEventType.DM_USER_TYPING
        assert payload["conversation_id"] == "c1"


async def test_typing_no_federation_when_unattached():
    repo = _FakeConvoRepo(
        members=[_FakeMember("alice"), _FakeMember("bob")],
        remote=[_FakeRemoteMember("remote-x")],
    )
    svc = TypingService(
        conversation_repo=repo,
        user_repo=_FakeUserRepo(),
        ws_manager=_FakeWS(),
    )
    # No federation attached → silent skip.
    await svc.user_started_typing(
        conversation_id="c1",
        sender_user_id="alice",
        sender_username="a",
    )


# ─── Inbound (federation → local WS) ─────────────────────────────────────


async def test_handle_remote_typing_fans_to_local_members():
    repo = _FakeConvoRepo(
        members=[
            _FakeMember("alice"),
            _FakeMember("bob"),
        ],
        remote=[_FakeRemoteMember("remote-1", "eve")],
    )
    ws = _FakeWS()
    svc = TypingService(
        conversation_repo=repo,
        user_repo=_FakeUserRepo(),
        ws_manager=ws,
    )
    n = await svc.handle_remote_typing(
        _Event(
            FederationEventType.DM_USER_TYPING,
            "remote-1",
            {
                "conversation_id": "c1",
                "sender_user_id": "remote-eve",
                "sender_username": "eve",
            },
        )
    )
    assert n == 2
    targets, payload = ws.calls[0]
    assert set(targets) == {"alice", "bob"}
    assert payload["from_instance"] == "remote-1"
    assert payload["sender_user_id"] == "remote-eve"


async def test_handle_remote_typing_drops_self_target():
    repo = _FakeConvoRepo(
        members=[
            _FakeMember("alice"),
            _FakeMember("remote-eve"),
        ],
        remote=[_FakeRemoteMember("remote-1", "eve")],
    )
    ws = _FakeWS()
    svc = TypingService(
        conversation_repo=repo,
        user_repo=_FakeUserRepo(),
        ws_manager=ws,
    )
    await svc.handle_remote_typing(
        _Event(
            FederationEventType.DM_USER_TYPING,
            "remote-1",
            {
                "conversation_id": "c1",
                "sender_user_id": "remote-eve",
                "sender_username": "eve",
            },
        )
    )
    targets, _ = ws.calls[0]
    assert "remote-eve" not in targets


async def test_handle_remote_typing_refuses_an_unseated_typist():
    """Nobody is shown typing unless the sender's own user holds a seat."""
    ws = _FakeWS()
    svc = TypingService(
        conversation_repo=_FakeConvoRepo(members=[_FakeMember("alice")]),
        user_repo=_FakeUserRepo(),
        ws_manager=ws,
    )
    n = await svc.handle_remote_typing(
        _Event(
            FederationEventType.DM_USER_TYPING,
            "remote-1",
            {"conversation_id": "c1", "sender_user_id": "remote-eve"},
        )
    )
    assert n == 0
    assert ws.calls == []


async def test_handle_remote_typing_missing_fields_returns_zero():
    svc = TypingService(
        conversation_repo=_FakeConvoRepo(),
        user_repo=_FakeUserRepo(),
        ws_manager=_FakeWS(),
    )
    n = await svc.handle_remote_typing(
        _Event(
            FederationEventType.DM_USER_TYPING,
            "remote-1",
            {},
        )
    )
    assert n == 0


# ─── _resolve_user_id branches ──────────────────────────────────────────


class _UsernameMember:
    def __init__(self, username):
        self.username = username


class _RealUser:
    def __init__(self, username, user_id):
        self.username = username
        self.user_id = user_id


class _FakeUserRepoWithLookup:
    def __init__(self, mapping):
        self._mapping = mapping

    async def get(self, username):
        return self._mapping.get(username)


async def test_resolve_user_id_from_username_lookup():
    """Member has only ``username`` → resolve via user_repo."""
    repo = _FakeConvoRepo(members=[_UsernameMember("alice")])
    ws = _FakeWS()
    user_repo = _FakeUserRepoWithLookup(
        {
            "alice": _RealUser("alice", "alice-uid"),
        }
    )
    svc = TypingService(
        conversation_repo=repo,
        user_repo=user_repo,
        ws_manager=ws,
    )
    await svc.user_started_typing(
        conversation_id="c1",
        sender_user_id="bob-uid",
        sender_username="bob",
    )
    targets, _ = ws.calls[0]
    assert "alice-uid" in targets


async def test_resolve_user_id_lookup_failure_drops_member():
    """user_repo failure → member silently dropped."""
    repo = _FakeConvoRepo(members=[_UsernameMember("ghost")])
    ws = _FakeWS()

    class _Raises:
        async def get(self, _):
            raise RuntimeError("DB down")

    svc = TypingService(
        conversation_repo=repo,
        user_repo=_Raises(),
        ws_manager=ws,
    )
    await svc.user_started_typing(
        conversation_id="c1",
        sender_user_id="b",
        sender_username="b",
    )
    targets, _ = ws.calls[0]
    assert targets == []


async def test_resolve_user_id_unknown_username_returns_none():
    repo = _FakeConvoRepo(members=[_UsernameMember("nobody")])
    ws = _FakeWS()
    svc = TypingService(
        conversation_repo=repo,
        user_repo=_FakeUserRepoWithLookup({}),
        ws_manager=ws,
    )
    await svc.user_started_typing(
        conversation_id="c1",
        sender_user_id="b",
        sender_username="b",
    )
    targets, _ = ws.calls[0]
    assert targets == []


# ─── Comment-thread typing ────────────────────────────────────────────────


class _FakeUser:
    def __init__(self, user_id: str):
        self.user_id = user_id


class _FakeUserRepoWithList:
    """User repo fake that returns a list of local active users — used by
    the household-scope branch of comment-typing fan-out."""

    def __init__(self, users):
        self._users = users

    async def list_active(self):
        return self._users


class _FakeSpaceRepo:
    def __init__(self, members):
        self._m = members

    async def list_members(self, space_id):
        return self._m


async def test_comment_typing_household_broadcasts_to_all_local_users():
    """``space_id is None`` → fan to every local user except the sender."""
    users = _FakeUserRepoWithList(
        [_FakeUser("alice"), _FakeUser("bob"), _FakeUser("carol")],
    )
    ws = _FakeWS()
    svc = TypingService(
        conversation_repo=_FakeConvoRepo(),
        user_repo=users,
        ws_manager=ws,
    )
    delivered = await svc.user_typing_on_comment(
        post_id="p1",
        space_id=None,
        sender_user_id="alice",
        sender_username="alice",
    )
    assert delivered == 2
    targets, payload = ws.calls[0]
    assert sorted(targets) == ["bob", "carol"]
    assert payload["type"] == "comment.user_typing"
    assert payload["post_id"] == "p1"
    assert payload["space_id"] is None
    assert payload["sender_user_id"] == "alice"


async def test_comment_typing_space_scopes_to_space_members():
    """``space_id`` set → fan to space members only."""
    users = _FakeUserRepoWithList([_FakeUser("alice"), _FakeUser("dave")])
    space = _FakeSpaceRepo(
        [_FakeMember("alice"), _FakeMember("bob"), _FakeMember("carol")],
    )
    ws = _FakeWS()
    svc = TypingService(
        conversation_repo=_FakeConvoRepo(),
        user_repo=users,
        ws_manager=ws,
        space_repo=space,
    )
    await svc.user_typing_on_comment(
        post_id="p1",
        space_id="space-x",
        sender_user_id="alice",
        sender_username="alice",
    )
    targets, payload = ws.calls[0]
    assert sorted(targets) == ["bob", "carol"]
    # ``dave`` is a local user but not a space member — must not receive.
    assert "dave" not in targets
    assert payload["space_id"] == "space-x"


async def test_comment_typing_throttle_drops_rapid_duplicates():
    """Two emits within 1 s from the same user collapse to one fan-out."""
    users = _FakeUserRepoWithList([_FakeUser("alice"), _FakeUser("bob")])
    ws = _FakeWS()
    svc = TypingService(
        conversation_repo=_FakeConvoRepo(),
        user_repo=users,
        ws_manager=ws,
    )
    await svc.user_typing_on_comment(
        post_id="p1",
        space_id=None,
        sender_user_id="alice",
        sender_username="alice",
        now=100.0,
    )
    delivered = await svc.user_typing_on_comment(
        post_id="p1",
        space_id=None,
        sender_user_id="alice",
        sender_username="alice",
        now=100.5,
    )
    assert delivered == 0
    assert len(ws.calls) == 1
    # After 1 s the throttle clears.
    await svc.user_typing_on_comment(
        post_id="p1",
        space_id=None,
        sender_user_id="alice",
        sender_username="alice",
        now=101.6,
    )
    assert len(ws.calls) == 2


async def test_is_typing_on_comment_expires_after_ttl():
    users = _FakeUserRepoWithList([])
    svc = TypingService(
        conversation_repo=_FakeConvoRepo(),
        user_repo=users,
        ws_manager=_FakeWS(),
    )
    await svc.user_typing_on_comment(
        post_id="p1",
        space_id=None,
        sender_user_id="alice",
        sender_username="alice",
        now=100.0,
    )
    assert svc.is_typing_on_comment("p1", "alice", now=100.5)
    assert not svc.is_typing_on_comment(
        "p1",
        "alice",
        now=100.0 + TYPING_TTL_SECONDS + 1,
    )


# ─── Visibility-gated federation fan-out ─────────────────────────────────


class _FakeVisibilityRepo:
    """Per-peer hide list — Protocol-shape fake."""

    def __init__(self) -> None:
        self._hidden: dict[str, set[str]] = {}

    def hide(self, peer: str, user_id: str) -> None:
        self._hidden.setdefault(peer, set()).add(user_id)

    async def hidden_user_ids_for_peer(self, peer: str) -> frozenset[str]:
        return frozenset(self._hidden.get(peer, set()))


async def test_user_typing_skips_peers_where_sender_is_hidden():
    """Sender hidden from ``peer-hider`` → no DM_USER_TYPING sent there."""
    repo = _FakeConvoRepo(
        members=[_FakeMember("alice")],
        remote=[
            _FakeRemoteMember("peer-open"),
            _FakeRemoteMember("peer-hider"),
        ],
    )
    fed = _FakeFed()
    vis = _FakeVisibilityRepo()
    vis.hide("peer-hider", "u-alice")
    svc = TypingService(
        conversation_repo=repo,
        user_repo=_FakeUserRepo(),
        ws_manager=_FakeWS(),
        federation_service=fed,
        own_instance_id="self",
        visibility_repo=vis,
    )
    await svc.user_started_typing(
        conversation_id="c1",
        sender_user_id="u-alice",
        sender_username="alice",
    )
    targets = {t for t, _, _ in fed.sent}
    assert "peer-open" in targets
    assert "peer-hider" not in targets


async def test_user_typing_no_repo_fans_to_every_peer():
    """``visibility_repo=None`` → default-visible, every peer receives."""
    repo = _FakeConvoRepo(
        members=[_FakeMember("alice")],
        remote=[
            _FakeRemoteMember("peer-open"),
            _FakeRemoteMember("peer-hider"),
        ],
    )
    fed = _FakeFed()
    svc = TypingService(
        conversation_repo=repo,
        user_repo=_FakeUserRepo(),
        ws_manager=_FakeWS(),
        federation_service=fed,
        own_instance_id="self",
        visibility_repo=None,
    )
    await svc.user_started_typing(
        conversation_id="c1",
        sender_user_id="u-alice",
        sender_username="alice",
    )
    targets = {t for t, _, _ in fed.sent}
    assert targets == {"peer-open", "peer-hider"}
