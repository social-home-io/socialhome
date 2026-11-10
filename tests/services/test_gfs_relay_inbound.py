"""Tests for :mod:`socialhome.services.gfs_relay_inbound`.

Real seal / unseal (the key-wrap crypto is the whole point of the entry
point); the federation service, the invite coordinator and the connection
repo are recording stand-ins at the boundary.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Collection

import orjson
import pytest

from socialhome.crypto import (
    b64url_decode,
    b64url_encode,
    derive_instance_id,
    generate_identity_keypair,
    generate_x25519_keypair,
)
from socialhome.domain.federation import (
    FederationEventType,
    GfsConnection,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.federation.encoder import FederationEncoder
from socialhome.federation.federation_service import FederationService
from socialhome.federation.gfs_mailbox import MailboxFlooded, derive_mailbox_keys
from socialhome.federation.gfs_relay_media import seal_relay_media
from socialhome.federation.gfs_relay_transport import (
    GfsRelayTransport,
    MailboxTarget,
    seal_relay_envelope,
)
from socialhome.federation.inbound_validator import (
    TRANSPORT_GFS_MAILBOX,
    TRANSPORT_GFS_RELAY,
)
from socialhome.federation.invite_bootstrap import KIND_REDEEM
from socialhome.federation.invite_token_redeem import (
    BOOTSTRAP_BODY_INBOUND_LIMIT,
    BOOTSTRAP_INBOUND_LIMIT,
    BOOTSTRAP_INBOUND_WINDOW_S,
    RELAY_ENVELOPE_INBOUND_LIMIT,
)
from socialhome.federation.keywrap_seal import UnsupportedKemSuite, seal_to_keywrap
from socialhome.federation.multi_recipient import (
    MULTI_TO_INSTANCE,
    UnsupportedMrSuite,
    build_multi_envelope,
    new_mr_key,
    seal_multi,
    wrap_mr_key,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.rate_limiter import RateLimiter
from socialhome.services import gfs_relay_inbound as gri
from socialhome.services.gfs_relay_inbound import RELAY_DELIVERED_VIA, GfsRelayInbound
from tests.federation.test_federation_service import (
    InMemoryFederationRepo,
    InMemoryOutboxRepo,
)

ENVELOPE = {
    "msg_id": "m-1",
    "event_type": "space.post_created",
    "from_instance": "a" * 32,
    "to_instance": "b" * 32,
    "timestamp": "2026-10-07T10:00:00+00:00",
    "encrypted_payload": "nonce:ct",
    "proto_version": 1,
    "sig_suite": "ed25519",
    "signatures": {"ed25519": "sig"},
}


class _Federation:
    def __init__(self, *, raises: Exception | None = None) -> None:
        self.calls: list[tuple[str, bytes, str]] = []
        self.seen_via: list[str | None] = []
        self.raises = raises

    async def handle_inbound_rtc(self, instance_id, raw_body, *, transport=""):
        self.calls.append((instance_id, raw_body, transport))
        self.seen_via.append(RELAY_DELIVERED_VIA.get())
        if self.raises is not None:
            raise self.raises
        return {"status": "ok"}


class _Invite:
    """The invite coordinator's bootstrap entry point, recorded."""

    def __init__(self) -> None:
        self.bodies: list[tuple[dict, str]] = []

    async def handle_bootstrap_body(self, body, *, gfs_url=""):
        self.bodies.append((body, gfs_url))
        return {"ok": True}


class _GfsRepo:
    def __init__(self, conns: list[GfsConnection] | None = None) -> None:
        self.conns = conns or []

    async def list_active(self):
        return list(self.conns)


def _conn(gfs_id: str, url: str, status: str = "active") -> GfsConnection:
    return GfsConnection(
        id=gfs_id,
        gfs_instance_id=f"gi-{gfs_id}",
        display_name=gfs_id,
        public_key="pk",
        inbox_url=url,
        status=status,
        paired_at="2026-01-01T00:00:00+00:00",
    )


@pytest.fixture
def keys():
    return generate_x25519_keypair()


def _inbound(keys, *, federation=None, invite=None, repo=None, limiter=None):
    return GfsRelayInbound(
        federation=federation or _Federation(),
        keywrap_private_key=keys.private_key,
        invite_coordinator=invite or _Invite(),
        gfs_connection_repo=repo or _GfsRepo(),
        rate_limiter=limiter,
    )


def _envelope_frame(keys, envelope=None) -> dict:
    return {
        "type": "envelope",
        "sealed": seal_relay_envelope(
            envelope_dict=envelope or ENVELOPE,
            peer_keywrap_pub=keys.public_key,
        ),
    }


def _bootstrap_frame(keys) -> dict:
    body = {"kind": KIND_REDEEM, "redeem_nonce": "n" * 32}
    return {
        "type": "envelope",
        "sealed": seal_to_keywrap(
            recipient_keywrap_pub=keys.public_key,
            plaintext=json.dumps(body).encode(),
        ),
    }


# ─── Relayed §24.11 envelopes ────────────────────────────────────────────


async def test_a_relayed_envelope_goes_to_the_pipeline_as_a_relay_transport(keys):
    fed, invite = _Federation(), _Invite()
    inbound = _inbound(keys, federation=fed, invite=invite)

    result = await inbound.handle_frame(_envelope_frame(keys))

    assert result == {"status": "ok"}
    assert len(fed.calls) == 1
    instance_id, raw, transport = fed.calls[0]
    assert instance_id == "a" * 32
    assert json.loads(raw) == ENVELOPE
    assert transport == TRANSPORT_GFS_RELAY
    # Never the bootstrap path — and no dependency on its readiness.
    assert invite.bodies == []


async def test_the_delivering_connection_is_visible_during_dispatch_only(keys):
    fed = _Federation()
    repo = _GfsRepo(
        [
            _conn("gfs-other", "https://other.example.org"),
            _conn("gfs-mine", "https://gfs.example.org"),
        ],
    )
    inbound = _inbound(keys, federation=fed, repo=repo)

    await inbound.handle_frame(
        _envelope_frame(keys),
        # Case / trailing slash must not decide the match.
        gfs_url="https://GFS.example.org/",
    )

    assert fed.seen_via == ["gfs-mine"]
    assert RELAY_DELIVERED_VIA.get() is None


async def test_the_contextvar_is_reset_when_the_pipeline_rejects(keys, caplog):
    fed = _Federation(raises=ValueError("Invalid signature"))
    repo = _GfsRepo([_conn("gfs-1", "https://gfs.example.org")])
    inbound = _inbound(keys, federation=fed, repo=repo)

    with (
        caplog.at_level(logging.INFO),
        pytest.raises(ValueError, match="signature"),
    ):
        await inbound.handle_frame(
            _envelope_frame(keys),
            gfs_url="https://gfs.example.org",
        )

    assert fed.seen_via == ["gfs-1"]
    assert RELAY_DELIVERED_VIA.get() is None
    # Dropped, never silently — type and sender, never the payload.
    assert "space.post_created" in caplog.text
    assert "a" * 32 in caplog.text
    assert "nonce:ct" not in caplog.text


@pytest.mark.parametrize(
    ("conns", "url"),
    [
        ([], "https://gfs.example.org"),
        (
            [_conn("gfs-1", "https://gfs.example.org", "suspended")],
            "https://gfs.example.org",
        ),
        ([_conn("gfs-1", "https://gfs.example.org")], "https://elsewhere.example.org"),
        ([_conn("gfs-1", "https://gfs.example.org")], ""),
    ],
    ids=["no-connections", "inactive", "unknown-server", "no-url"],
)
async def test_an_unknown_delivering_server_leaves_the_contextvar_empty(
    keys, conns, url
):
    fed = _Federation()
    inbound = _inbound(keys, federation=fed, repo=_GfsRepo(conns))

    await inbound.handle_frame(_envelope_frame(keys), gfs_url=url)

    assert fed.seen_via == [None]


@pytest.mark.parametrize(
    "envelope",
    [
        {"no": "from_instance"},
        {**ENVELOPE, "from_instance": ""},
    ],
)
async def test_an_envelope_without_a_sender_is_refused(keys, envelope):
    fed = _Federation()
    inbound = _inbound(keys, federation=fed)

    with pytest.raises(ValueError, match="from_instance"):
        await inbound.handle_frame(_envelope_frame(keys, envelope))
    assert fed.calls == []


async def test_a_relay_body_without_an_envelope_is_refused(keys):
    fed = _Federation()
    inbound = _inbound(keys, federation=fed)
    frame = {
        "sealed": seal_to_keywrap(
            recipient_keywrap_pub=keys.public_key,
            plaintext=json.dumps({"kind": "space_relay_envelope"}).encode(),
        ),
    }

    with pytest.raises(ValueError, match="envelope body"):
        await inbound.handle_frame(frame)
    assert fed.calls == []


# ─── Bootstrap bodies ────────────────────────────────────────────────────


async def test_bootstrap_bodies_are_delegated_with_the_server_url(keys):
    fed, invite = _Federation(), _Invite()
    inbound = _inbound(keys, federation=fed, invite=invite)

    result = await inbound.handle_frame(
        _bootstrap_frame(keys),
        gfs_url="https://gfs.example.org",
    )

    assert result == {"ok": True}
    assert invite.bodies[0][0]["kind"] == KIND_REDEEM
    assert invite.bodies[0][1] == "https://gfs.example.org"
    assert fed.calls == []


# ─── Fail-closed + throttles ─────────────────────────────────────────────


async def test_a_blob_for_somebody_else_does_not_open(keys):
    other = generate_x25519_keypair()
    inbound = _inbound(keys)

    with pytest.raises(ValueError):
        await inbound.handle_frame(_envelope_frame(other))


@pytest.mark.parametrize("frame", [{}, {"sealed": None}, {"sealed": "x"}, "junk"])
async def test_a_malformed_frame_is_refused(keys, frame):
    with pytest.raises(ValueError):
        await _inbound(keys).handle_frame(frame)


async def test_without_a_keywrap_key_nothing_opens(keys):
    inbound = GfsRelayInbound(
        federation=_Federation(),
        keywrap_private_key=b"",
        invite_coordinator=_Invite(),
        gfs_connection_repo=_GfsRepo(),
    )

    with pytest.raises(ValueError, match="key-wrap"):
        await inbound.handle_frame(_envelope_frame(keys))


async def test_the_process_wide_throttle_runs_before_the_unseal(keys):
    limiter = RateLimiter()
    for _ in range(BOOTSTRAP_INBOUND_LIMIT):
        assert limiter.is_allowed(
            "invite-bootstrap:inbound",
            limit=BOOTSTRAP_INBOUND_LIMIT,
            window_s=BOOTSTRAP_INBOUND_WINDOW_S,
        )
    fed = _Federation()
    inbound = _inbound(keys, federation=fed, limiter=limiter)

    # Not even a sealed payload — the throttle answers first.
    with pytest.raises(ValueError, match="inbound rate limit"):
        await inbound.handle_frame({"sealed": {}})
    assert fed.calls == []


async def test_relayed_envelopes_have_their_own_bucket(keys):
    limiter = RateLimiter()
    for _ in range(RELAY_ENVELOPE_INBOUND_LIMIT):
        assert limiter.is_allowed(
            "invite-bootstrap:envelopes",
            limit=RELAY_ENVELOPE_INBOUND_LIMIT,
            window_s=BOOTSTRAP_INBOUND_WINDOW_S,
        )
    fed, invite = _Federation(), _Invite()
    inbound = _inbound(keys, federation=fed, invite=invite, limiter=limiter)

    with pytest.raises(ValueError, match="envelopes rate limit"):
        await inbound.handle_frame(_envelope_frame(keys))
    assert fed.calls == []

    # …and a full envelope bucket never starves an invite redeem.
    await inbound.handle_frame(_bootstrap_frame(keys))
    assert len(invite.bodies) == 1


# ── Relayed §11 pairing bodies ──────────────────────────────────────────


class _PairingFederation:
    """The two pairing handlers, recorded with the ContextVar they saw."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict, str | None, str | None]] = []
        self.raises = raises

    async def _record(self, name, body, relayed_via):
        self.calls.append((name, body, relayed_via, RELAY_DELIVERED_VIA.get()))
        if self.raises is not None:
            raise self.raises
        return {"ok": True}

    async def handle_peer_accept(self, body, *, relayed_via=None):
        return await self._record("accept", body, relayed_via)

    async def handle_peer_confirm(self, body, *, relayed_via=None):
        return await self._record("confirm", body, relayed_via)


def _pairing_frame(keys, kind: str, *, event_type: str | None = None) -> dict:
    inner = {"event_type": event_type or kind, "token": "t", "signature": "s"}
    return {
        "type": "envelope",
        "sealed": seal_to_keywrap(
            recipient_keywrap_pub=keys.public_key,
            plaintext=json.dumps({"kind": kind, "pairing": inner}).encode(),
        ),
    }


@pytest.mark.parametrize(
    ("kind", "handler"),
    [("pairing_peer_accept", "accept"), ("pairing_peer_confirm", "confirm")],
)
async def test_a_relayed_pairing_body_reaches_the_inbox_handler(keys, kind, handler):
    federation = _PairingFederation()
    invite = _Invite()
    inbound = _inbound(
        keys,
        federation=federation,
        invite=invite,
        repo=_GfsRepo([_conn("own-g", "https://g.example")]),
    )

    assert await inbound.handle_frame(
        _pairing_frame(keys, kind), gfs_url="https://G.example/"
    ) == {"ok": True}

    [(name, body, relayed_via, ctx)] = federation.calls
    assert name == handler
    assert body == {"event_type": kind, "token": "t", "signature": "s"}
    # Our own connection id — passed explicitly AND visible for the dispatch.
    assert relayed_via == "own-g" and ctx == "own-g"
    assert RELAY_DELIVERED_VIA.get() is None
    assert invite.bodies == []


async def test_a_relayed_pairing_body_from_an_unknown_server_is_dropped(keys):
    federation = _PairingFederation()
    inbound = _inbound(keys, federation=federation, repo=_GfsRepo([]))

    with pytest.raises(ValueError, match="one of our connection servers"):
        await inbound.handle_frame(
            _pairing_frame(keys, "pairing_peer_accept"),
            gfs_url="https://g.example",
        )
    assert federation.calls == []


async def test_a_relabelled_pairing_body_is_refused(keys):
    """The wrapper's kind must match the signed body's own event_type —
    a confirm can't be replayed into the accept handler."""
    federation = _PairingFederation()
    inbound = _inbound(
        keys,
        federation=federation,
        repo=_GfsRepo([_conn("own-g", "https://g.example")]),
    )
    with pytest.raises(ValueError, match="does not match"):
        await inbound.handle_frame(
            _pairing_frame(
                keys, "pairing_peer_accept", event_type="pairing_peer_confirm"
            ),
            gfs_url="https://g.example",
        )
    assert federation.calls == []


async def test_a_rejected_pairing_body_resets_the_contextvar(keys, caplog):
    federation = _PairingFederation(raises=ValueError("No pending pairing"))
    inbound = _inbound(
        keys,
        federation=federation,
        repo=_GfsRepo([_conn("own-g", "https://g.example")]),
    )
    with (
        caplog.at_level(logging.INFO, logger="socialhome.services.gfs_relay_inbound"),
        pytest.raises(ValueError, match="No pending pairing"),
    ):
        await inbound.handle_frame(
            _pairing_frame(keys, "pairing_peer_confirm"),
            gfs_url="https://g.example",
        )
    assert RELAY_DELIVERED_VIA.get() is None
    assert "pairing_peer_confirm" in caplog.text
    assert '"t"' not in caplog.text  # never the body


async def test_relayed_pairing_bodies_have_their_own_bucket(keys):
    limiter = RateLimiter()
    for _ in range(BOOTSTRAP_BODY_INBOUND_LIMIT):
        assert limiter.is_allowed(
            "invite-bootstrap:pairing",
            limit=BOOTSTRAP_BODY_INBOUND_LIMIT,
            window_s=BOOTSTRAP_INBOUND_WINDOW_S,
        )
    federation, invite = _PairingFederation(), _Invite()
    inbound = _inbound(
        keys,
        federation=federation,
        invite=invite,
        repo=_GfsRepo([_conn("own-g", "https://g.example")]),
        limiter=limiter,
    )
    with pytest.raises(ValueError, match="relayed pairing rate limit"):
        await inbound.handle_frame(
            _pairing_frame(keys, "pairing_peer_accept"), gfs_url="https://g.example"
        )
    assert federation.calls == []
    # A full pairing bucket never starves an invite redeem.
    await inbound.handle_frame(_bootstrap_frame(keys))
    assert len(invite.bodies) == 1


# ── v_88: frames on our per-space mailboxes ──────────────────────────────


class _MailboxFederation:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes, str]] = []
        self.media: list[tuple[bytes, str]] = []

    async def handle_inbound_rtc(
        self, instance_id, raw_body, *, transport="", multi_key=None
    ):
        self.calls.append((instance_id, raw_body, transport))
        return {"status": "ok"}

    async def handle_inbound_media_relay(self, body, *, transport=""):
        self.media.append((body, transport))
        return {"status": "ok"}

    own_instance_id = "c" * 32


class _MailboxSender:
    """Captures what the GFS would see for a mailbox frame."""

    def __init__(self) -> None:
        self.posts: list[dict] = []

    async def mailbox_connection(self, gfs_url):
        return object()

    async def media_connection(self, gfs_url):
        return object()

    async def send_to_mailbox(
        self, *, mailbox_id, gfs_url, sealed=None, media_body=None
    ):
        self.posts.append(
            {"mailbox_id": mailbox_id, "sealed": sealed, "media_body": media_body}
        )
        return True


def _mailbox_keys(gen=0):
    return derive_mailbox_keys(b"\x05" * 32, "g" * 32, gen)


async def test_mailbox_round_trip_goes_through_the_pipeline(keys):
    mkeys = _mailbox_keys()
    sender = _MailboxSender()
    transport = GfsRelayTransport(relay_sender=sender)
    target = MailboxTarget(
        mailbox_id=mkeys.mailbox_id, kem_pk=mkeys.kem_public, gfs_url="https://g"
    )
    peer = RemoteInstance(
        id="b" * 32,
        display_name="b",
        remote_identity_pk="00",
        key_self_to_remote="k",
        key_remote_to_self="k",
        remote_inbox_url="",
        local_inbox_id="l",
    )
    envelope = {**ENVELOPE, "space_id": "sp-1"}
    ok, _ = await transport.send_mailbox(
        instance=peer, target=target, envelope_dict=envelope
    )
    assert ok
    [post] = sender.posts
    # What the GFS sees names the mailbox only — never an instance id.
    wire = json.dumps(post)
    assert post["mailbox_id"] == mkeys.mailbox_id
    assert "a" * 32 not in wire and "b" * 32 not in wire and "sp-1" not in wire
    federation = _MailboxFederation()
    inbound = _inbound(keys, federation=federation)
    await inbound.handle_mailbox_frame(
        {"type": "envelope", "sealed": post["sealed"]}, keys=mkeys
    )
    [(sender_id, raw, label)] = federation.calls
    assert sender_id == "a" * 32
    assert json.loads(raw) == envelope  # byte-for-byte the same envelope
    # The sender gate (a card for the envelope's space) runs in the pipeline.
    assert label == TRANSPORT_GFS_MAILBOX


async def test_mailbox_frame_sealed_to_the_keywrap_key_does_not_open(keys):
    """A mailbox frame opens only under the mailbox key — the household's
    key-wrap key (instance-addressed traffic) never doubles for it."""
    federation = _MailboxFederation()
    inbound = _inbound(keys, federation=federation)
    with pytest.raises(ValueError):
        await inbound.handle_mailbox_frame(_envelope_frame(keys), keys=_mailbox_keys())
    assert federation.calls == []


async def test_mailbox_carries_no_bootstrap_bodies(keys):
    mkeys = _mailbox_keys()
    frame = {
        "type": "envelope",
        "sealed": seal_to_keywrap(
            recipient_keywrap_pub=mkeys.kem_public,
            plaintext=json.dumps({"kind": KIND_REDEEM}).encode(),
        ),
    }
    invite = _Invite()
    inbound = _inbound(keys, invite=invite, federation=_MailboxFederation())
    with pytest.raises(ValueError, match="relayed envelopes only"):
        await inbound.handle_mailbox_frame(frame, keys=mkeys)
    assert invite.bodies == []


async def test_mailbox_media_frame_opens_under_the_mailbox_id(keys):
    mkeys = _mailbox_keys()
    body = b"\x00SHM" + b"\x01" + b"x" * 100
    frame = seal_relay_media(
        to_instance=mkeys.mailbox_id,
        media_body=body,
        recipient_keywrap_pub=mkeys.kem_public,
    )
    federation = _MailboxFederation()
    inbound = _inbound(keys, federation=federation)
    await inbound.handle_mailbox_frame(frame, keys=mkeys)
    assert federation.media == [(body, TRANSPORT_GFS_MAILBOX)]
    # Addressed to another mailbox: refused.
    with pytest.raises(ValueError):
        await inbound.handle_mailbox_frame(frame, keys=_mailbox_keys(1))


def _sealed_from(sender: str, mkeys, space: str = "sp-1") -> dict:
    return {
        "type": "envelope",
        "sealed": seal_relay_envelope(
            envelope_dict={**ENVELOPE, "from_instance": sender, "space_id": space},
            peer_keywrap_pub=mkeys.kem_public,
        ),
    }


async def test_a_flood_on_one_mailbox_shuts_only_that_mailbox(keys, monkeypatch):
    """Every frame on a mailbox — junk, a frame that opens but fails the
    pipeline, a good one — is charged to that mailbox's own bucket before
    any open, so a flood shuts only that mailbox (refused as
    ``MailboxFlooded``, which the mailbox service answers with a rotation)
    and never another (a household's mailbox at another GFS)."""
    monkeypatch.setattr(gri, "MAILBOX_INBOUND_LIMIT", 4)
    mbox_a, mbox_b = _mailbox_keys(0), _mailbox_keys(1)

    class _RefusingFirst(_MailboxFederation):
        def __init__(self) -> None:
            super().__init__()
            self.refuse = 1

        async def handle_inbound_rtc(self, *a, **kw):
            if self.refuse:
                self.refuse -= 1
                raise ValueError("bad signature")
            return await super().handle_inbound_rtc(*a, **kw)

    federation = _RefusingFirst()
    inbound = _inbound(keys, federation=federation, limiter=RateLimiter())
    junk = {
        "type": "envelope",
        "sealed": {"kem_suite": "x25519", "eph_pk": "AA", "ciphertext": "AA"},
    }
    with pytest.raises(ValueError):
        await inbound.handle_mailbox_frame(junk, keys=mbox_a)  # fails to open
    with pytest.raises(ValueError):  # opens, fails the pipeline
        await inbound.handle_mailbox_frame(_sealed_from("x" * 32, mbox_a), keys=mbox_a)
    await inbound.handle_mailbox_frame(_sealed_from("x" * 32, mbox_a), keys=mbox_a)
    await inbound.handle_mailbox_frame(_sealed_from("x" * 32, mbox_a), keys=mbox_a)
    with pytest.raises(MailboxFlooded, match="rate limit"):
        await inbound.handle_mailbox_frame(_sealed_from("x" * 32, mbox_a), keys=mbox_a)
    # The other mailbox is untouched.
    await inbound.handle_mailbox_frame(
        _sealed_from("y" * 32, mbox_b, "sp-2"), keys=mbox_b
    )
    assert [c[0] for c in federation.calls] == ["x" * 32] * 2 + ["y" * 32]


# ── v_91: multi-recipient frames, through the real §24.11 pipeline ───────

_SPACE = "sp-multi"
_OTHER_SPACE = "sp-other"


class _MultiHousehold:
    """A receiver with a real FederationService pipeline, our mailbox and
    carded senders."""

    def __init__(self, *, limiter=None) -> None:
        own = generate_identity_keypair()
        self.own_id = derive_instance_id(own.public_key)
        self.repo = InMemoryFederationRepo()
        self.federation = FederationService(
            db=None,  # type: ignore[arg-type]
            federation_repo=self.repo,
            outbox_repo=InMemoryOutboxRepo(),
            key_manager=KeyManager(b"\x01" * 32),
            bus=EventBus(),
            own_instance_id=derive_instance_id(own.public_key),
            own_identity_seed=own.private_key,
            own_identity_pk=own.public_key,
        )
        self.dispatched: list = []

        async def _record(event) -> None:
            self.dispatched.append(event)

        self.federation._event_registry.register(
            FederationEventType.SPACE_POST_CREATED, _record
        )
        #: (sender, space) pairs whose member card we hold.
        self.cards: set[tuple[str, str]] = set()

        async def _card(sender: str, space_id: str) -> bool:
            return (sender, space_id) in self.cards

        self.federation._mailbox_sender_check = _card
        self.mailbox = _mailbox_keys()
        self.inbound = GfsRelayInbound(
            federation=self.federation,
            keywrap_private_key=b"\x02" * 32,
            invite_coordinator=_Invite(),
            gfs_connection_repo=_GfsRepo(),
            rate_limiter=limiter,
        )

    async def add_sender(self, *, card_for: str | None = _SPACE):
        kp = generate_identity_keypair()
        sender_id = derive_instance_id(kp.public_key)
        await self.repo.save_instance(
            RemoteInstance(
                id=sender_id,
                display_name="sender",
                remote_identity_pk=kp.public_key.hex(),
                key_self_to_remote="unused",
                key_remote_to_self="unused",
                remote_inbox_url="",
                local_inbox_id="l-" + sender_id[:8],
                status=PairingStatus.CONFIRMED,
                source=InstanceSource.SPACE_SESSION,
            )
        )
        if card_for is not None:
            self.cards.add((sender_id, card_for))
        return sender_id, FederationEncoder(kp.private_key)

    @property
    def audience(self) -> tuple[str, str]:
        """Us and one other member household."""
        return (self.own_id, "m" * 32)

    async def frame(self, frame: dict) -> dict:
        return await self.inbound.handle_mailbox_frame(frame, keys=self.mailbox)


def _multi_frame(
    h: _MultiHousehold,
    sender_id: str,
    encoder: FederationEncoder,
    *,
    to: Collection[str],
    envelope_space: str = _SPACE,
    payload_space: str | None = None,
    event_type: FederationEventType = FederationEventType.SPACE_POST_CREATED,
) -> tuple[dict, dict, bytes]:
    """What the GFS pushes to *h*'s mailbox: the body plus its wrap of K."""
    key = new_mr_key()
    payload: dict = {"post_id": "p-1", "content": "hello"}
    if payload_space is not None:
        payload["space_id"] = payload_space
    env = build_multi_envelope(
        encoder=encoder,
        own_instance_id=sender_id,
        event_type=event_type.value,
        payload_json=orjson.dumps(payload).decode(),
        space_id=envelope_space,
        key=key,
        recipients=to,
        msg_id=str(uuid.uuid4()),
    )
    sealed = {
        **seal_multi(env, key=key),
        "wrap": wrap_mr_key(key, h.mailbox.kem_public),
    }
    return {"type": "envelope", "sealed": sealed}, env, key


async def test_a_multi_frame_from_a_carded_member_is_dispatched():
    h = _MultiHousehold()
    sender, enc = await h.add_sender()
    frame, env, _ = _multi_frame(h, sender, enc, to=h.audience)
    assert await h.frame(frame) == {"status": "ok"}
    [event] = h.dispatched
    assert event.from_instance == sender
    assert event.space_id == _SPACE
    assert event.to_instance.startswith(MULTI_TO_INSTANCE + ":")
    assert event.payload == {"post_id": "p-1", "content": "hello"}
    assert event.msg_id == env["msg_id"]


async def test_a_wrap_for_another_mailbox_is_refused(caplog):
    h = _MultiHousehold()
    sender, enc = await h.add_sender()
    frame, _, key = _multi_frame(h, sender, enc, to=h.audience)
    frame["sealed"]["wrap"] = wrap_mr_key(key, _mailbox_keys(gen=1).kem_public)
    with caplog.at_level(logging.DEBUG), pytest.raises(ValueError, match="wrap"):
        await h.frame(frame)
    assert h.dispatched == []
    assert h.mailbox.mailbox_id not in caplog.text


async def test_an_unknown_suite_is_refused():
    h = _MultiHousehold()
    sender, enc = await h.add_sender()
    frame, _, _ = _multi_frame(h, sender, enc, to=h.audience)
    frame["sealed"]["mr_suite"] = "mr-aesgcm-256"  # the withdrawn shape
    with pytest.raises(UnsupportedMrSuite):
        await h.frame(frame)
    assert h.dispatched == []


async def test_an_unknown_wrap_suite_is_refused():
    h = _MultiHousehold()
    sender, enc = await h.add_sender()
    frame, _, _ = _multi_frame(h, sender, enc, to=h.audience)
    frame["sealed"]["wrap"]["kem_suite"] = "x25519+mlkem768"
    with pytest.raises(UnsupportedKemSuite):
        await h.frame(frame)
    assert h.dispatched == []


async def test_a_tampered_multi_ciphertext_is_refused():
    h = _MultiHousehold()
    sender, enc = await h.add_sender()
    frame, _, _ = _multi_frame(h, sender, enc, to=h.audience)
    raw = bytearray(b64url_decode(frame["sealed"]["ciphertext"]))
    raw[-1] ^= 0x01
    frame["sealed"]["ciphertext"] = b64url_encode(bytes(raw))
    with pytest.raises(ValueError, match="does not open"):
        await h.frame(frame)
    assert h.dispatched == []


async def test_a_star_envelope_in_an_ordinary_mailbox_frame_is_refused():
    """A per-recipient seal (our mailbox's KEM key) carrying ``to = "*"``:
    no multi context, so the pipeline refuses the star."""
    h = _MultiHousehold()
    sender, enc = await h.add_sender()
    _, env, _ = _multi_frame(h, sender, enc, to=h.audience)
    frame = {
        "type": "envelope",
        "sealed": seal_relay_envelope(
            envelope_dict=env, peer_keywrap_pub=h.mailbox.kem_public
        ),
    }
    with pytest.raises(ValueError, match="multi-recipient"):
        await h.frame(frame)
    assert h.dispatched == []


async def test_a_payload_naming_another_space_than_its_envelope_is_refused():
    """The signed envelope names space A, the payload space B (where the
    sender even holds a card): refused before any handler reads it."""
    h = _MultiHousehold()
    sender, enc = await h.add_sender()
    h.cards.add((sender, _OTHER_SPACE))
    frame, _, _ = _multi_frame(
        h, sender, enc, to=h.audience, payload_space=_OTHER_SPACE
    )
    with pytest.raises(ValueError, match="another space"):
        await h.frame(frame)
    assert h.dispatched == []
    frame, _, _ = _multi_frame(h, sender, enc, to=h.audience, payload_space=_SPACE)
    assert await h.frame(frame) == {"status": "ok"}


async def test_an_envelope_naming_no_space_is_refused():
    h = _MultiHousehold()
    sender, enc = await h.add_sender()
    frame, _, _ = _multi_frame(h, sender, enc, to=h.audience, envelope_space="")
    with pytest.raises(ValueError, match="names no space"):
        await h.frame(frame)
    assert h.dispatched == []


async def test_a_sender_without_a_card_for_the_space_is_refused():
    h = _MultiHousehold()
    sender, enc = await h.add_sender(card_for=_OTHER_SPACE)
    frame, _, _ = _multi_frame(h, sender, enc, to=h.audience)
    with pytest.raises(ValueError, match="member card"):
        await h.frame(frame)
    assert h.dispatched == []


@pytest.mark.parametrize("removal", ["card_dropped", "row_gone"])
async def test_a_removed_member_is_refused(removal):
    """A frame from a member is taken; once the roster drops it (its card
    goes, or its seat row with it), the mailbox-sender check refuses its
    next frame before dispatch."""
    h = _MultiHousehold()
    sender, enc = await h.add_sender()
    frame, _, _ = _multi_frame(h, sender, enc, to=h.audience)
    assert await h.frame(frame) == {"status": "ok"}
    h.cards.discard((sender, _SPACE))
    match = "member card"
    if removal == "row_gone":
        await h.repo.delete_instance(sender)
        match = "No instance found"
    frame, _, _ = _multi_frame(h, sender, enc, to=h.audience)
    with pytest.raises(ValueError, match=match):
        await h.frame(frame)
    assert len(h.dispatched) == 1


async def test_a_bad_signature_is_refused():
    h = _MultiHousehold()
    sender, _ = await h.add_sender()
    _, impostor = await h.add_sender()  # signs with ANOTHER identity key
    frame, _, _ = _multi_frame(h, sender, impostor, to=h.audience)
    with pytest.raises(ValueError, match="signature"):
        await h.frame(frame)
    assert h.dispatched == []


async def test_a_replayed_multi_frame_is_refused():
    h = _MultiHousehold()
    sender, enc = await h.add_sender()
    frame, _, _ = _multi_frame(h, sender, enc, to=h.audience)
    await h.frame(frame)
    with pytest.raises(ValueError, match="Replay"):
        await h.frame(frame)
    assert len(h.dispatched) == 1


async def test_a_multi_frame_is_charged_to_the_mailbox_bucket(monkeypatch):
    """Multi frames share the mailbox's own bucket, charged before the wrap
    is opened — a junk flood ends in a dropped frame, not an X25519."""
    monkeypatch.setattr(gri, "MAILBOX_INBOUND_LIMIT", 2)
    h = _MultiHousehold(limiter=RateLimiter())
    sender, enc = await h.add_sender()
    for _ in range(2):
        frame, _, _ = _multi_frame(h, sender, enc, to=h.audience)
        frame["sealed"]["wrap"] = {}
        with pytest.raises(ValueError):
            await h.frame(frame)
    frame, _, _ = _multi_frame(h, sender, enc, to=h.audience)
    with pytest.raises(MailboxFlooded):
        await h.frame(frame)
    assert h.dispatched == []


async def test_a_frame_re_pushed_to_a_household_the_sender_left_out_is_refused():
    """A recipient learned K and re-wraps a frame the sender addressed to
    two members to our mailbox (or the GFS replays it). Our tag is not in
    the signed audience: refused, nothing dispatched."""
    h = _MultiHousehold()
    sender, enc = await h.add_sender()
    frame, _, _ = _multi_frame(h, sender, enc, to=("m" * 32, "n" * 32))
    with pytest.raises(ValueError, match="not addressed to us"):
        await h.frame(frame)
    assert h.dispatched == []


async def test_a_frame_whose_audience_lists_only_us_is_taken():
    h = _MultiHousehold()
    sender, enc = await h.add_sender()
    frame, _, _ = _multi_frame(h, sender, enc, to=(h.own_id,))
    assert await h.frame(frame) == {"status": "ok"}
    assert len(h.dispatched) == 1


async def test_an_excluded_event_type_in_a_multi_frame_is_refused():
    h = _MultiHousehold()
    sender, enc = await h.add_sender()
    frame, _, _ = _multi_frame(
        h,
        sender,
        enc,
        to=h.audience,
        event_type=FederationEventType.SPACE_KEY_EXCHANGE_REKEY,
    )
    with pytest.raises(ValueError, match="never rides a multi-recipient"):
        await h.frame(frame)
    assert h.dispatched == []


async def test_a_multi_context_on_a_non_mailbox_transport_is_refused():
    """Through the real ``FederationService.handle_inbound_rtc``: a multi
    context paired with any transport but the mailbox (a misuse of the
    seam) is refused before anything is dispatched."""
    h = _MultiHousehold()
    sender, enc = await h.add_sender()
    _, env, key = _multi_frame(h, sender, enc, to=h.audience)
    for transport in ("", TRANSPORT_GFS_RELAY):
        with pytest.raises(ValueError, match="mailbox only"):
            await h.federation.handle_inbound_rtc(
                sender, orjson.dumps(env), transport=transport, multi_key=key
            )
    assert h.dispatched == []
    # The same envelope over the mailbox leg is taken.
    assert await h.federation.handle_inbound_rtc(
        sender, orjson.dumps(env), transport=TRANSPORT_GFS_MAILBOX, multi_key=key
    ) == {"status": "ok"}
