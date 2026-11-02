"""Cross-household DM coverage for :class:`DmService`.

Specifically exercises the ``other_user_id`` path on
:meth:`DmService.create_dm` (resolving a remote target via
``user_repo.get_remote``) and the corresponding fan-out on
:meth:`DmService.send_message` (resolving the remote member's user_id
via ``user_repo.list_remote_for_instance`` so the federation
envelope's ``recipient_user_ids`` carries the peer-side user the
recipient should be seated against).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.conversation import ConversationType
from socialhome.domain.federation import (
    FederationEventType,
    PairingStatus,
)
from socialhome.domain.user import RemoteUser
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.conversation_repo import SqliteConversationRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.dm_service import DmService
from socialhome.services.user_service import UserService


@pytest.fixture
async def stack(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        """INSERT INTO instance_identity(instance_id, identity_private_key,
           identity_public_key, routing_secret) VALUES(?,?,?,?)""",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    bus = EventBus()
    user_repo = SqliteUserRepo(db)
    conv_repo = SqliteConversationRepo(db)
    user_svc = UserService(user_repo, bus, own_instance_public_key=kp.public_key)
    dm_svc = DmService(conv_repo, user_repo, bus)

    class Stack:
        pass

    s = Stack()
    s.db = db
    s.user_repo = user_repo
    s.conv_repo = conv_repo
    s.user_svc = user_svc
    s.dm_svc = dm_svc
    s.own_instance_id = iid

    async def provision(username: str):
        return await user_svc.provision(username=username, display_name=username)

    s.provision = provision
    yield s
    await db.shutdown()


class _FakeFederationService:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_event(self, *, to_instance_id, event_type, payload):
        self.sent.append(
            {"to": to_instance_id, "type": event_type, "payload": payload},
        )


class _FakeFederationRepo:
    def __init__(self, instances: dict[str, object]) -> None:
        self._instances = instances

    async def get_instance(self, instance_id: str):
        return self._instances.get(instance_id)


def _confirmed_peer(instance_id: str, *, proto_version: int = 3):
    from types import SimpleNamespace

    return SimpleNamespace(
        id=instance_id,
        status=PairingStatus.CONFIRMED,
        proto_version=proto_version,
    )


async def _seed_remote_instance(stack, instance_id: str) -> None:
    """Insert a minimal ``remote_instances`` row so the FK on
    ``remote_users.instance_id`` is satisfied.
    """
    await stack.db.enqueue(
        """INSERT OR IGNORE INTO remote_instances(
               id, display_name, remote_identity_pk, key_self_to_remote,
               key_remote_to_self, remote_inbox_url, local_inbox_id,
               status, source
           ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            instance_id,
            instance_id[:8],
            "00" * 32,
            "k1",
            "k2",
            "https://peer.example/federation/inbox/x",
            "local-inbox",
            "confirmed",
            "manual",
        ),
    )


async def _seed_remote_user(stack, *, instance_id: str, username: str) -> RemoteUser:
    """Mirror a peer's user into ``remote_users`` the way a peer-directory
    snapshot would.
    """
    await _seed_remote_instance(stack, instance_id)
    user_id = f"uid-{username}-remote"
    remote = RemoteUser(
        user_id=user_id,
        instance_id=instance_id,
        remote_username=username,
        display_name=username.title(),
    )
    await stack.user_repo.upsert_remote(remote)
    return remote


async def test_create_dm_with_other_user_id_seats_remote_member(stack):
    await stack.provision("anna")
    bob = await _seed_remote_user(stack, instance_id="peer-b", username="bob")

    dm = await stack.dm_svc.create_dm(
        creator_username="anna",
        other_user_id=bob.user_id,
    )

    assert dm.type is ConversationType.DM
    locals_ = await stack.conv_repo.list_members(dm.id)
    remotes = await stack.conv_repo.list_remote_members(dm.id)
    assert {m.username for m in locals_} == {"anna"}
    assert {(m.instance_id, m.remote_username) for m in remotes} == {
        ("peer-b", "bob"),
    }


async def test_create_dm_with_other_user_id_is_idempotent(stack):
    await stack.provision("anna")
    bob = await _seed_remote_user(stack, instance_id="peer-b", username="bob")
    a = await stack.dm_svc.create_dm(
        creator_username="anna",
        other_user_id=bob.user_id,
    )
    b = await stack.dm_svc.create_dm(
        creator_username="anna",
        other_user_id=bob.user_id,
    )
    assert a.id == b.id


async def test_create_dm_rejects_zero_or_two_targets(stack):
    await stack.provision("anna")
    with pytest.raises(ValueError, match="exactly one"):
        await stack.dm_svc.create_dm(creator_username="anna")
    with pytest.raises(ValueError, match="exactly one"):
        await stack.dm_svc.create_dm(
            creator_username="anna",
            other_username="bob",
            other_user_id="uid-bob",
        )


async def test_create_dm_unknown_remote_user_raises(stack):
    await stack.provision("anna")
    with pytest.raises(KeyError, match="not found"):
        await stack.dm_svc.create_dm(
            creator_username="anna",
            other_user_id="uid-nope",
        )


async def test_send_to_remote_member_includes_user_id_in_envelope(stack):
    """``recipient_user_ids`` must carry the *peer-side* user_id so the
    receiver can seat the right local user.
    """
    await stack.provision("anna")
    bob = await _seed_remote_user(stack, instance_id="peer-b", username="bob")
    fed = _FakeFederationService()
    repo = _FakeFederationRepo({"peer-b": _confirmed_peer("peer-b")})
    stack.dm_svc.attach_federation(fed, repo, own_instance_id=stack.own_instance_id)

    dm = await stack.dm_svc.create_dm(
        creator_username="anna",
        other_user_id=bob.user_id,
    )
    await stack.dm_svc.send_message(
        dm.id,
        sender_username="anna",
        content="hi bob",
    )

    sent = [s for s in fed.sent if s["type"] == FederationEventType.DM_MESSAGE]
    assert len(sent) == 1
    payload = sent[0]["payload"]
    assert payload["conversation_id"] == dm.id
    assert payload["content"] == "hi bob"
    assert bob.user_id in payload["recipient_user_ids"]


# ── v_3 media — enqueue + relay-rejection coverage ───────────────────


class _FakeMediaSync:
    """Records ``build_preview`` + ``enqueue_for_message`` calls
    without exercising the real preview generation or outbox."""

    def __init__(self) -> None:
        self.previews: list[dict] = []
        self.enqueues: list[dict] = []
        self.preview_value: str | None = None

    async def build_preview(self, *, media_url, kind, mime_type):
        self.previews.append(
            {"media_url": media_url, "kind": kind, "mime_type": mime_type},
        )
        return self.preview_value

    async def enqueue_for_message(self, *, message_id, media_url, target_instance_ids):
        self.enqueues.append(
            {
                "message_id": message_id,
                "media_url": media_url,
                "target_instance_ids": target_instance_ids,
            },
        )


async def test_send_image_to_confirmed_peer_enqueues_outbox(stack):
    """A media DM to a directly-paired peer triggers the
    build_preview + enqueue_for_message chain."""
    await stack.provision("anna")
    bob = await _seed_remote_user(stack, instance_id="peer-b", username="bob")
    fed = _FakeFederationService()
    repo = _FakeFederationRepo({"peer-b": _confirmed_peer("peer-b")})
    media_sync = _FakeMediaSync()
    media_sync.preview_value = "fake-preview-b64"
    stack.dm_svc._media_sync = media_sync  # type: ignore[attr-defined]
    stack.dm_svc.attach_federation(fed, repo, own_instance_id=stack.own_instance_id)

    dm = await stack.dm_svc.create_dm(
        creator_username="anna",
        other_user_id=bob.user_id,
    )
    msg = await stack.dm_svc.send_message(
        dm.id,
        sender_username="anna",
        content="",
        type="image",
        media_url="api/media/cat.webp",
        file_name="cat.jpg",
        mime_type="image/webp",
        file_size_bytes=1234,
    )

    # ``build_preview`` was asked for an image preview.
    assert len(media_sync.previews) == 1
    assert media_sync.previews[0]["kind"] == "image"
    # ``enqueue_for_message`` got one row for peer-b.
    assert len(media_sync.enqueues) == 1
    assert media_sync.enqueues[0]["target_instance_ids"] == ["peer-b"]
    assert media_sync.enqueues[0]["message_id"] == msg.id
    # The outbound DM_MESSAGE envelope picked up the preview field +
    # the media metadata triple.
    sent = [s for s in fed.sent if s["type"] == FederationEventType.DM_MESSAGE]
    payload = sent[0]["payload"]
    assert payload["type"] == "image"
    assert payload["media_url"] == "api/media/cat.webp"
    assert payload["file_name"] == "cat.jpg"
    assert payload["mime_type"] == "image/webp"
    assert payload["file_size_bytes"] == 1234
    assert payload["preview_bytes_b64"] == "fake-preview-b64"
    assert payload["media_blob_id"] == msg.id


async def test_send_media_on_unconfirmed_peer_rejects(stack):
    """Sending an image to a peer that's NOT directly paired raises
    ``MediaRequiresDirectPairingError`` — operator decision."""
    from socialhome.services.dm_service import MediaRequiresDirectPairingError

    await stack.provision("anna")
    bob = await _seed_remote_user(stack, instance_id="peer-b", username="bob")
    fed = _FakeFederationService()
    # Peer exists in the repo but with NO status field set (or
    # status != confirmed) — read by ``_peer_is_confirmed`` as
    # not confirmed.
    from types import SimpleNamespace

    repo = _FakeFederationRepo(
        {"peer-b": SimpleNamespace(id="peer-b", status=None)},
    )
    stack.dm_svc.attach_federation(fed, repo, own_instance_id=stack.own_instance_id)

    dm = await stack.dm_svc.create_dm(
        creator_username="anna",
        other_user_id=bob.user_id,
    )
    with pytest.raises(MediaRequiresDirectPairingError, match="directly-paired"):
        await stack.dm_svc.send_message(
            dm.id,
            sender_username="anna",
            content="",
            type="image",
            media_url="api/media/cat.webp",
        )


async def test_send_media_file_type_skips_preview(stack):
    """``type='file'`` triggers the enqueue but ``build_preview``
    returns ``None`` (files don't get inline previews), so the
    outbound payload omits ``preview_bytes_b64``."""
    await stack.provision("anna")
    bob = await _seed_remote_user(stack, instance_id="peer-b", username="bob")
    fed = _FakeFederationService()
    repo = _FakeFederationRepo({"peer-b": _confirmed_peer("peer-b")})
    media_sync = _FakeMediaSync()
    media_sync.preview_value = None  # files → no preview
    stack.dm_svc._media_sync = media_sync  # type: ignore[attr-defined]
    stack.dm_svc.attach_federation(fed, repo, own_instance_id=stack.own_instance_id)
    dm = await stack.dm_svc.create_dm(
        creator_username="anna",
        other_user_id=bob.user_id,
    )
    await stack.dm_svc.send_message(
        dm.id,
        sender_username="anna",
        content="",
        type="file",
        media_url="api/media/doc.pdf",
        file_name="invoice.pdf",
        mime_type="application/pdf",
    )
    sent = [s for s in fed.sent if s["type"] == FederationEventType.DM_MESSAGE]
    payload = sent[0]["payload"]
    assert payload["type"] == "file"
    # File branch: no preview field embedded.
    assert "preview_bytes_b64" not in payload


async def test_send_media_empty_caption_allowed(stack):
    """``type='image'`` with no caption is a valid send."""
    await stack.provision("anna")
    bob = await _seed_remote_user(stack, instance_id="peer-b", username="bob")
    fed = _FakeFederationService()
    repo = _FakeFederationRepo({"peer-b": _confirmed_peer("peer-b")})
    stack.dm_svc.attach_federation(fed, repo, own_instance_id=stack.own_instance_id)
    dm = await stack.dm_svc.create_dm(
        creator_username="anna",
        other_user_id=bob.user_id,
    )
    msg = await stack.dm_svc.send_message(
        dm.id,
        sender_username="anna",
        content="",
        type="image",
        media_url="api/media/cat.webp",
    )
    assert msg.content == ""
    assert msg.type == "image"


async def test_media_allowed_follows_the_direct_pairing_rule(stack):
    """``media_allowed`` answers the send rule before anything is sent:
    a chat with a directly paired household takes photos, a relay-only
    one does not, an unknown chat neither (the gallery share sheet)."""
    await stack.provision("anna")
    bob = await _seed_remote_user(stack, instance_id="peer-b", username="bob")
    fed = _FakeFederationService()
    peers = {"peer-b": SimpleNamespace(id="peer-b", status=None)}
    stack.dm_svc.attach_federation(
        fed, _FakeFederationRepo(peers), own_instance_id=stack.own_instance_id
    )
    dm = await stack.dm_svc.create_dm(
        creator_username="anna", other_user_id=bob.user_id
    )
    assert await stack.dm_svc.media_allowed(dm.id) is False
    peers["peer-b"] = _confirmed_peer("peer-b")
    assert await stack.dm_svc.media_allowed(dm.id) is True
    assert await stack.dm_svc.media_allowed("no-such-chat") is False
