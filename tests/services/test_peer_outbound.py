"""Tests for the outbound-federation peer mixins (peer_outbound)."""

from socialhome.services.peer_outbound import (
    ConfirmedPeerBroadcaster,
    SingleTargetSender,
)


class _Peer:
    def __init__(self, id_, local_inbox_id=None):
        self.id = id_
        self.local_inbox_id = local_inbox_id


class _FakeRepo:
    def __init__(self, peers, *, raises=False):
        self._peers = peers
        self._raises = raises
        self.calls = []

    async def list_social_instances(self):
        return await self.list_instances(status="confirmed")

    async def list_instances(self, *, status):
        self.calls.append(status)
        if self._raises:
            raise RuntimeError("boom")
        return list(self._peers)


class _FakeFederation:
    def __init__(self, own="me", reject_space_id=False):
        self.own_instance_id = own
        self.sent = []
        self._reject_space_id = reject_space_id

    async def send_event(self, *, to_instance_id, event_type, payload, **kw):
        if self._reject_space_id and "space_id" in kw:
            raise TypeError("unexpected space_id")
        self.sent.append((to_instance_id, event_type, payload, kw))


class _Broadcaster(ConfirmedPeerBroadcaster):
    __slots__ = ("_federation", "_federation_repo")

    def __init__(self, federation, repo):
        self._federation = federation
        self._federation_repo = repo


class _Sender(SingleTargetSender):
    __slots__ = ("_federation",)

    def __init__(self, federation):
        self._federation = federation


async def test_confirmed_peers_skips_self_and_nulls():
    fed = _FakeFederation(own="me")
    repo = _FakeRepo([_Peer("me"), _Peer(None), _Peer(""), _Peer("a"), _Peer("b")])
    svc = _Broadcaster(fed, repo)

    peers = await svc.confirmed_peers()
    assert [p.id for p in peers] == ["a", "b"]
    assert repo.calls == ["confirmed"]

    ids = await svc.list_confirmed_peer_ids()
    assert ids == ["a", "b"]


async def test_confirmed_peers_fail_soft_on_repo_error():
    svc = _Broadcaster(_FakeFederation(), _FakeRepo([], raises=True))
    assert await svc.confirmed_peers() == []
    assert await svc.list_confirmed_peer_ids() == []


async def test_confirmed_peers_fail_soft_without_repo():
    svc = _Broadcaster(_FakeFederation(), None)
    assert await svc.confirmed_peers() == []


async def test_confirmed_peers_exposes_full_row_fields():
    repo = _FakeRepo([_Peer("a", local_inbox_id="inbox-a")])
    svc = _Broadcaster(_FakeFederation(own="me"), repo)
    [peer] = await svc.confirmed_peers()
    assert peer.local_inbox_id == "inbox-a"


async def test_send_to_instance_omits_space_id_when_none():
    fed = _FakeFederation(reject_space_id=True)
    svc = _Sender(fed)
    await svc.send_to_instance("a", "EVT", {"k": "v"})
    assert fed.sent == [("a", "EVT", {"k": "v"}, {})]


async def test_send_to_instance_forwards_space_id_when_set():
    fed = _FakeFederation()
    svc = _Sender(fed)
    await svc.send_to_instance("a", "EVT", {}, space_id="space-1")
    assert fed.sent[0][3] == {"space_id": "space-1"}


async def test_send_to_instance_is_fail_soft():
    class _Boom(_FakeFederation):
        async def send_event(self, **kw):
            raise RuntimeError("network down")

    svc = _Sender(_Boom())
    # Must swallow — one bad peer can't break a fan-out.
    await svc.send_to_instance("a", "EVT", {})


async def test_send_to_instance_noop_without_federation():
    svc = _Sender(None)
    await svc.send_to_instance("a", "EVT", {})


class _SourceAwareRepo:
    """Repo double that distinguishes the two peer lists.

    ``list_instances`` returns everything CONFIRMED (including the §D2b
    ``space_session`` household); ``list_social_instances`` returns only
    the socially-paired one. A surface that regresses to the former is
    caught by the assertion, not by a mock call count.
    """

    def __init__(self):
        self.social_calls = 0

    async def list_instances(self, **kwargs):
        return [_Peer("paired"), _Peer("space-session")]

    async def list_social_instances(self):
        self.social_calls += 1
        return [_Peer("paired")]


async def test_confirmed_peers_excludes_space_session_households():
    """§D2b — joining somebody's space via an invite link must not put
    them on the social fan-out list (profile updates, capabilities, URL
    changes, moments, highlights all ride this mixin)."""
    repo = _SourceAwareRepo()
    svc = _Broadcaster(_FakeFederation(own="me"), repo)

    peers = await svc.confirmed_peers()

    assert [p.id for p in peers] == ["paired"]
    assert repo.social_calls == 1
    assert await svc.list_confirmed_peer_ids() == ["paired"]
