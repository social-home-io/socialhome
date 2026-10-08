"""Tests for :mod:`socialhome.services.gfs_relay_inbound`.

Real seal / unseal (the key-wrap crypto is the whole point of the entry
point); the federation service, the invite coordinator and the connection
repo are recording stand-ins at the boundary.
"""

from __future__ import annotations

import json
import logging

import pytest

from socialhome.crypto import generate_x25519_keypair
from socialhome.domain.federation import GfsConnection
from socialhome.federation.gfs_relay_transport import seal_relay_envelope
from socialhome.federation.inbound_validator import TRANSPORT_GFS_RELAY
from socialhome.federation.invite_bootstrap import KIND_REDEEM
from socialhome.federation.invite_token_redeem import (
    BOOTSTRAP_BODY_INBOUND_LIMIT,
    BOOTSTRAP_INBOUND_LIMIT,
    BOOTSTRAP_INBOUND_WINDOW_S,
    RELAY_ENVELOPE_INBOUND_LIMIT,
)
from socialhome.federation.keywrap_seal import seal_to_keywrap
from socialhome.rate_limiter import RateLimiter
from socialhome.services.gfs_relay_inbound import RELAY_DELIVERED_VIA, GfsRelayInbound

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

    with caplog.at_level(logging.INFO):
        with pytest.raises(ValueError, match="signature"):
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
    with caplog.at_level(logging.INFO, logger="socialhome.services.gfs_relay_inbound"):
        with pytest.raises(ValueError, match="No pending pairing"):
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
