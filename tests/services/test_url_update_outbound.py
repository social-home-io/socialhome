"""Tests for :class:`UrlUpdateOutbound` — URL_UPDATED fan-out (§11)."""

from __future__ import annotations

from socialhome.domain.federation import (
    FederationEventType,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.services.url_update_outbound import UrlUpdateOutbound


class _FakeFederationRepo:
    def __init__(self, peers: list[RemoteInstance]) -> None:
        self._peers = peers

    async def list_social_instances(self):
        return await self.list_instances(status="confirmed")

    async def list_instances(
        self,
        *,
        source: str | None = None,
        status: str | None = None,
    ) -> list[RemoteInstance]:
        out = list(self._peers)
        if status is not None:
            out = [p for p in out if p.status.value == status]
        return out


class _FakeFederationService:
    def __init__(self, *, own: str, fail_for: set[str] | None = None) -> None:
        self._own_instance_id = own
        self._fail_for = fail_for or set()
        self.sent: list[tuple[str, FederationEventType, dict]] = []

    @property
    def own_instance_id(self) -> str:
        return self._own_instance_id

    async def send_event(self, *, to_instance_id, event_type, payload) -> None:
        if to_instance_id in self._fail_for:
            raise RuntimeError(f"simulated failure for {to_instance_id}")
        self.sent.append((to_instance_id, event_type, payload))


def _peer(
    iid: str, local_inbox_id: str, status=PairingStatus.CONFIRMED
) -> RemoteInstance:
    return RemoteInstance(
        id=iid,
        display_name=iid,
        remote_identity_pk="aa" * 32,
        key_self_to_remote="enc",
        key_remote_to_self="enc",
        remote_inbox_url=f"https://old.example/{iid}",
        local_inbox_id=local_inbox_id,
        status=status,
        source=InstanceSource.MANUAL,
    )


async def test_publish_fans_out_to_confirmed_peers_with_per_peer_urls():
    peers = [
        _peer("peer-a", "wh-a"),
        _peer("peer-b", "wh-b"),
    ]
    fed = _FakeFederationService(own="self-iid")
    repo = _FakeFederationRepo(peers)
    svc = UrlUpdateOutbound(federation_service=fed, federation_repo=repo)

    sent = await svc.publish(new_inbox_base_url="https://new.example/federation/inbox")

    assert sent == 2
    urls = {iid: payload["inbox_url"] for iid, _, payload in fed.sent}
    assert urls["peer-a"] == "https://new.example/federation/inbox/wh-a"
    assert urls["peer-b"] == "https://new.example/federation/inbox/wh-b"
    for _, event_type, _ in fed.sent:
        assert event_type is FederationEventType.URL_UPDATED


async def test_publish_skips_self_and_failed_sends():
    peers = [
        _peer("self-iid", "wh-self"),
        _peer("peer-a", "wh-a"),
        _peer("peer-b", "wh-b"),
    ]
    fed = _FakeFederationService(own="self-iid", fail_for={"peer-b"})
    repo = _FakeFederationRepo(peers)
    svc = UrlUpdateOutbound(federation_service=fed, federation_repo=repo)

    sent = await svc.publish(new_inbox_base_url="https://new.example/federation/inbox/")

    # Own instance skipped; peer-b raise swallowed.
    assert sent == 1
    sent_iids = [iid for iid, _, _ in fed.sent]
    assert sent_iids == ["peer-a"]


async def test_publish_empty_when_no_confirmed_peers():
    peers = [_peer("peer-a", "wh-a", status=PairingStatus.PENDING_SENT)]
    fed = _FakeFederationService(own="self-iid")
    repo = _FakeFederationRepo(peers)
    svc = UrlUpdateOutbound(federation_service=fed, federation_repo=repo)

    sent = await svc.publish(new_inbox_base_url="https://new.example")

    assert sent == 0
    assert fed.sent == []
