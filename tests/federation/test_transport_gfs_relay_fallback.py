"""FederationTransport — the GFS relay as a fallback tier for paired peers.

A paired household (``source = manual``) that opted into the relay
(``RemoteInstance.gfs_relay``) and has confirmed routes is reached
RTC → HTTPS inbox → connection-server relay, round-robin over the routes.
Link-joined households (``space_session``) keep their relay-only path,
byte for byte — those cases live in ``test_federation_transport.py``.
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib

import pytest

from socialhome.domain.federation import (
    DELIVERY_ERROR_RELAY_THROTTLED,
    DELIVERY_ERROR_RELAY_TOO_LARGE,
    DeliveryResult,
    FederationEventType,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.federation.gfs_relay_transport import (
    RELAY_STATUS_THROTTLED,
    RELAY_STATUS_TOO_LARGE,
    MailboxTarget,
)
from socialhome.federation.transport import (
    TRANSPORT_ERROR_GFS_RELAY_NOT_ENABLED,
    TRANSPORT_ERROR_NO_ROUTE,
    FederationTransport,
    _RtcPeer,
)

ROUTE_A = "https://gfs-a.example.org"
ROUTE_B = "https://gfs-b.example.org"
ROUTE_C = "https://gfs-c.example.org"


def _paired(
    *,
    inbox: str = "https://peer/wh",
    gfs_relay: bool = True,
    iid: str = "peer-1",
) -> RemoteInstance:
    return RemoteInstance(
        id=iid,
        display_name=iid,
        remote_identity_pk="aa" * 32,
        key_self_to_remote="enc",
        key_remote_to_self="enc",
        remote_inbox_url=inbox,
        local_inbox_id=f"wh-{iid}",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
        # On a paired row this is an auto-pair introducer's instance id —
        # it must never be read as a connection-server URL.
        relay_via="introducer-iid",
        remote_keywrap_pk="cc" * 32,
        gfs_relay=gfs_relay,
    )


class _Https:
    def __init__(self, *, ok: bool = True, status: int | None = 200) -> None:
        self.ok = ok
        self.status = status
        self.calls: list[dict] = []

    async def send(self, *, instance, envelope_dict):
        self.calls.append(envelope_dict)
        return self.ok, self.status


class _Relay:
    """Per-URL scripted relay: ``results[url] = (ok, status)``."""

    def __init__(self, results: dict[str, tuple[bool, int | None]] | None = None):
        self.results = results or {}
        self.calls: list[tuple[str | None, dict]] = []

    async def send(self, *, instance, envelope_dict, gfs_url=None):
        self.calls.append((gfs_url, envelope_dict))
        return self.results.get(gfs_url or "", (True, None))


class _Signaler:
    def __init__(self) -> None:
        self.events: list[tuple[str, FederationEventType, dict]] = []

    async def __call__(self, to_instance_id, event_type, payload):
        self.events.append((to_instance_id, event_type, payload))
        return DeliveryResult(instance_id=to_instance_id, ok=True)


def _routes(*urls: str):
    async def _resolve(instance_id: str) -> list[str]:
        return list(urls)

    return _resolve


def _transport(*, https=None, relay=None, routes=(), signal=None):
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https or _Https(),
        gfs_relay=relay if relay is not None else _Relay(),
        gfs_routes=_routes(*routes),
        signaling_send=signal or _Signaler(),
    )
    t.mark_ice_primed()
    return t


def _offers(signal: _Signaler) -> list:
    return [
        e for e in signal.events if e[1] is FederationEventType.FEDERATION_RTC_OFFER
    ]


# ─── Selection order ──────────────────────────────────────────────────────


async def test_a_ready_datachannel_wins_over_https_and_relay():
    https, relay = _Https(), _Relay()
    t = _transport(https=https, relay=relay, routes=(ROUTE_A,))
    inst = _paired()
    peer = _RtcPeer(
        instance_id=inst.id,
        signaling=t._signaling_factory(inst.id),
        inbound=t._inbound_factory(inst.id),
    )

    class _Ch:
        buffered_amount = 0

        async def send(self, data, *, wait_for_drain: bool = True):
            return None

    peer._channel = _Ch()  # type: ignore[attr-defined]
    peer._open.set()  # type: ignore[attr-defined]
    t._peers[inst.id] = peer

    result = await t.send(instance=inst, envelope_dict={"msg_id": "m"})

    assert result.via == "rtc" and result.ok
    assert https.calls == [] and relay.calls == []


async def test_https_wins_over_the_relay_when_it_delivers():
    https, relay = _Https(), _Relay()
    t = _transport(https=https, relay=relay, routes=(ROUTE_A,))

    result = await t.send(instance=_paired(), envelope_dict={"msg_id": "m"})

    assert result.via == "https" and result.ok
    assert relay.calls == []


@pytest.mark.parametrize("status", [None, 500, 502, 503])
async def test_the_relay_carries_it_when_https_fails_at_the_network(status):
    https, relay = _Https(ok=False, status=status), _Relay()
    t = _transport(https=https, relay=relay, routes=(ROUTE_A,))

    result = await t.send(instance=_paired(), envelope_dict={"msg_id": "m"})

    assert result.ok is True
    assert result.via == "gfs_relay"
    assert [c[0] for c in relay.calls] == [ROUTE_A]
    assert len(https.calls) == 1


@pytest.mark.parametrize("status", [400, 403, 404, 410, 429])
async def test_a_deliberate_refusal_from_the_peer_is_not_relayed(status):
    """A 4xx proves the peer is reachable and answered; the relay would
    hand the identical envelope to the identical pipeline."""
    https, relay = _Https(ok=False, status=status), _Relay()
    t = _transport(https=https, relay=relay, routes=(ROUTE_A,))

    result = await t.send(instance=_paired(), envelope_dict={"msg_id": "m"})

    assert result.via == "https" and result.ok is False
    assert result.status_code == status
    assert relay.calls == []


async def test_an_addressless_peer_rides_the_relay_and_still_offers_rtc():
    """No URL at all: the RTC offer is kicked (it rides the relay through
    ``send_event``) and the envelope goes straight to the relay — never an
    HTTPS POST at the empty string."""
    https, relay, signal = _Https(), _Relay(), _Signaler()
    t = _transport(https=https, relay=relay, routes=(ROUTE_A,), signal=signal)

    result = await t.send(instance=_paired(inbox=""), envelope_dict={"msg_id": "m"})

    assert result.ok is True and result.via == "gfs_relay"
    assert https.calls == []
    assert len(_offers(signal)) == 1


async def test_an_addressless_peer_without_the_opt_in_fails_with_a_named_error():
    https, relay = _Https(), _Relay()
    t = _transport(https=https, relay=relay, routes=(ROUTE_A,))

    result = await t.send(
        instance=_paired(inbox="", gfs_relay=False),
        envelope_dict={"msg_id": "m"},
    )

    assert result.ok is False
    assert result.error == TRANSPORT_ERROR_NO_ROUTE
    assert https.calls == [] and relay.calls == []


async def test_an_addressless_opted_in_peer_with_no_routes_fails_with_a_named_error():
    https, relay = _Https(), _Relay()
    t = _transport(https=https, relay=relay, routes=())

    result = await t.send(instance=_paired(inbox=""), envelope_dict={"msg_id": "m"})

    assert result.ok is False
    assert result.error == TRANSPORT_ERROR_NO_ROUTE
    assert https.calls == [] and relay.calls == []


async def test_no_relay_without_the_opt_in_even_with_routes():
    https, relay = _Https(ok=False, status=None), _Relay()
    t = _transport(https=https, relay=relay, routes=(ROUTE_A,))

    result = await t.send(
        instance=_paired(gfs_relay=False),
        envelope_dict={"msg_id": "m"},
    )

    assert result.via == "https" and result.ok is False
    assert relay.calls == []


async def test_no_relay_without_routes_keeps_the_https_failure():
    https, relay = _Https(ok=False, status=503), _Relay()
    t = _transport(https=https, relay=relay, routes=())

    result = await t.send(instance=_paired(), envelope_dict={"msg_id": "m"})

    assert result.via == "https" and result.status_code == 503
    assert relay.calls == []


async def test_no_relay_tier_wired_keeps_the_https_failure():
    https = _Https(ok=False, status=None)
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https,
        gfs_routes=_routes(ROUTE_A),
        signaling_send=_Signaler(),
    )
    t.mark_ice_primed()

    result = await t.send(instance=_paired(), envelope_dict={"msg_id": "m"})

    assert result.via == "https" and result.ok is False


async def test_no_route_resolver_wired_keeps_the_https_failure():
    https, relay = _Https(ok=False, status=None), _Relay()
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=https,
        gfs_relay=relay,
        signaling_send=_Signaler(),
    )
    t.mark_ice_primed()

    result = await t.send(instance=_paired(), envelope_dict={"msg_id": "m"})

    assert result.via == "https" and relay.calls == []


# ─── Round-robin ──────────────────────────────────────────────────────────


async def test_routes_rotate_across_sends():
    relay = _Relay()
    t = _transport(relay=relay, routes=(ROUTE_A, ROUTE_B, ROUTE_C))
    inst = _paired(inbox="")

    for i in range(4):
        await t.send(instance=inst, envelope_dict={"msg_id": f"m{i}"})

    assert [c[0] for c in relay.calls] == [ROUTE_A, ROUTE_B, ROUTE_C, ROUTE_A]


async def test_rotation_is_per_peer():
    relay = _Relay()
    t = _transport(relay=relay, routes=(ROUTE_A, ROUTE_B))

    await t.send(instance=_paired(inbox="", iid="p1"), envelope_dict={"msg_id": "1"})
    await t.send(instance=_paired(inbox="", iid="p2"), envelope_dict={"msg_id": "2"})

    assert [c[0] for c in relay.calls] == [ROUTE_A, ROUTE_A]


async def test_a_failed_route_skips_to_the_next():
    relay = _Relay({ROUTE_A: (False, None)})
    t = _transport(relay=relay, routes=(ROUTE_A, ROUTE_B))

    result = await t.send(instance=_paired(inbox=""), envelope_dict={"msg_id": "m"})

    assert result.ok is True and result.via == "gfs_relay"
    assert [c[0] for c in relay.calls] == [ROUTE_A, ROUTE_B]


async def test_every_route_is_tried_at_most_once():
    relay = _Relay({ROUTE_A: (False, None), ROUTE_B: (False, None)})
    t = _transport(relay=relay, routes=(ROUTE_A, ROUTE_B))

    result = await t.send(instance=_paired(inbox=""), envelope_dict={"msg_id": "m"})

    assert result.ok is False
    assert result.via == "gfs_relay"
    assert result.error == "gfs_relay_failed"
    assert [c[0] for c in relay.calls] == [ROUTE_A, ROUTE_B]


async def test_a_throttled_route_skips_on_and_a_later_route_delivers():
    relay = _Relay({ROUTE_A: (False, RELAY_STATUS_THROTTLED)})
    t = _transport(relay=relay, routes=(ROUTE_A, ROUTE_B))

    result = await t.send(instance=_paired(inbox=""), envelope_dict={"msg_id": "m"})

    assert result.ok is True
    assert [c[0] for c in relay.calls] == [ROUTE_A, ROUTE_B]


async def test_throttling_propagates_as_a_waitable_window():
    relay = _Relay(
        {ROUTE_A: (False, RELAY_STATUS_THROTTLED), ROUTE_B: (False, None)},
    )
    t = _transport(relay=relay, routes=(ROUTE_A, ROUTE_B))

    result = await t.send(instance=_paired(inbox=""), envelope_dict={"msg_id": "m"})

    assert result.ok is False
    assert result.status_code == RELAY_STATUS_THROTTLED
    assert result.error == DELIVERY_ERROR_RELAY_THROTTLED


async def test_too_large_stops_at_once_and_propagates():
    """The frame is over the body cap on every server — no second try."""
    relay = _Relay({ROUTE_A: (False, RELAY_STATUS_TOO_LARGE)})
    t = _transport(relay=relay, routes=(ROUTE_A, ROUTE_B))

    result = await t.send(instance=_paired(inbox=""), envelope_dict={"msg_id": "m"})

    assert result.ok is False
    assert result.status_code == RELAY_STATUS_TOO_LARGE
    assert result.error == DELIVERY_ERROR_RELAY_TOO_LARGE
    assert [c[0] for c in relay.calls] == [ROUTE_A]


# ─── send_via_gfs_relay (the outbox's entry point) ────────────────────────


async def test_send_via_gfs_relay_uses_the_routes_for_a_paired_peer():
    https, relay = _Https(), _Relay()
    t = _transport(https=https, relay=relay, routes=(ROUTE_B,))

    result = await t.send_via_gfs_relay(
        instance=_paired(),
        envelope_dict={"msg_id": "m"},
    )

    assert result.ok is True and result.via == "gfs_relay"
    assert [c[0] for c in relay.calls] == [ROUTE_B]
    assert https.calls == []


async def test_send_via_gfs_relay_refuses_a_peer_that_did_not_opt_in():
    relay = _Relay()
    t = _transport(relay=relay, routes=(ROUTE_A,))

    result = await t.send_via_gfs_relay(
        instance=_paired(gfs_relay=False),
        envelope_dict={"msg_id": "m"},
    )

    assert result.ok is False
    assert result.error == TRANSPORT_ERROR_GFS_RELAY_NOT_ENABLED
    assert relay.calls == []


async def test_send_via_gfs_relay_without_routes_is_a_named_failure():
    relay = _Relay()
    t = _transport(relay=relay, routes=())

    result = await t.send_via_gfs_relay(
        instance=_paired(),
        envelope_dict={"msg_id": "m"},
    )

    assert result.ok is False
    assert result.error == TRANSPORT_ERROR_NO_ROUTE


async def test_send_via_gfs_relay_for_a_link_joined_peer_uses_relay_via():
    relay = _Relay()
    t = _transport(relay=relay, routes=(ROUTE_A,))
    inst = dataclasses.replace(
        _paired(inbox="", gfs_relay=False),
        source=InstanceSource.SPACE_SESSION,
        relay_via="https://introducer-gfs.example.org",
    )

    result = await t.send_via_gfs_relay(instance=inst, envelope_dict={"msg_id": "m"})

    assert result.ok is True
    # No explicit route: the relay tier reads ``relay_via`` itself.
    assert [c[0] for c in relay.calls] == [None]


# ─── Fallback failures surface as the HTTPS failure (review fix) ─────────


@pytest.mark.parametrize(
    "relay_result",
    [
        (False, RELAY_STATUS_TOO_LARGE),
        (False, RELAY_STATUS_THROTTLED),
        (False, None),
    ],
    ids=["too-large", "throttled", "failed"],
)
async def test_a_failed_fallback_relay_reports_the_original_https_failure(
    relay_result,
):
    """After the inbox was not reached, the relay is a bonus attempt: its
    failure must not turn a transient HTTPS outage into a permanent
    (too-large: not queued) or waitable (throttled: no unreachable mark)
    outcome. The caller sees the HTTPS failure and queues as before."""
    relay = _Relay({ROUTE_A: relay_result})
    t = _transport(https=_Https(ok=False, status=503), relay=relay, routes=(ROUTE_A,))

    result = await t.send(instance=_paired(), envelope_dict={"msg_id": "m"})

    assert len(relay.calls) == 1
    assert result.ok is False
    assert result.via == "https"
    assert result.status_code == 503
    assert result.error == "https_inbox_failed"


async def test_a_404_from_the_inbox_is_not_relayed():
    """404 is the peer's Social Home (or its front) answering — the outbox
    treats it as the pair-window race, never a reason to switch carrier."""
    https, relay = _Https(ok=False, status=404), _Relay()
    t = _transport(https=https, relay=relay, routes=(ROUTE_A,))

    result = await t.send(instance=_paired(), envelope_dict={"msg_id": "m"})

    assert result.via == "https" and result.status_code == 404
    assert relay.calls == []


# ─── send_via_gfs_url — one named server, for route discovery (v_53) ─────


async def test_send_via_gfs_url_targets_exactly_that_server_with_no_round_robin():
    relay = _Relay()
    https = _Https()
    t = _transport(https=https, relay=relay, routes=(ROUTE_A, ROUTE_B))

    for _ in range(2):
        result = await t.send_via_gfs_url(
            instance=_paired(),
            envelope_dict={"msg_id": "m"},
            gfs_url=ROUTE_C,
        )
        assert result.ok and result.via == "gfs_relay"

    assert [url for url, _ in relay.calls] == [ROUTE_C, ROUTE_C]
    assert https.calls == []


async def test_send_via_gfs_url_reports_a_relay_refusal():
    relay = _Relay({ROUTE_A: (False, RELAY_STATUS_THROTTLED)})
    t = _transport(relay=relay)

    result = await t.send_via_gfs_url(
        instance=_paired(), envelope_dict={}, gfs_url=ROUTE_A
    )

    assert result.ok is False
    assert result.status_code == RELAY_STATUS_THROTTLED
    assert result.error == DELIVERY_ERROR_RELAY_THROTTLED


@pytest.mark.parametrize("which", ["not-opted-in", "link-joined"])
async def test_send_via_gfs_url_refuses_a_peer_without_our_opt_in(which):
    relay = _Relay()
    t = _transport(relay=relay)
    peer = (
        _paired(gfs_relay=False)
        if which == "not-opted-in"
        else dataclasses.replace(_paired(), source=InstanceSource.SPACE_SESSION)
    )

    result = await t.send_via_gfs_url(instance=peer, envelope_dict={}, gfs_url=ROUTE_A)

    assert result.ok is False
    assert result.error == TRANSPORT_ERROR_GFS_RELAY_NOT_ENABLED
    assert relay.calls == []


async def test_send_via_gfs_url_needs_a_relay_tier_and_a_url():
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=_Https(),
        signaling_send=_Signaler(),
    )
    result = await t.send_via_gfs_url(
        instance=_paired(), envelope_dict={}, gfs_url=ROUTE_A
    )
    assert result.ok is False and result.error == "gfs_relay_unavailable"

    relay = _Relay()
    result = await _transport(relay=relay).send_via_gfs_url(
        instance=_paired(), envelope_dict={}, gfs_url=""
    )
    assert result.ok is False and result.error == "gfs_relay_unavailable"
    assert relay.calls == []


# ── v_87: binary media frames through the relay ──────────────────────────


class _MediaRelay(_Relay):
    def __init__(self, *, ready: bool = True) -> None:
        super().__init__()
        self.ready = ready
        self.media: list[bytes] = []

    async def media_ready(self, instance) -> bool:
        return self.ready

    async def send_media(self, *, instance, media_body):
        self.media.append(media_body)
        return True, None


async def test_relay_media_goes_as_the_inbox_media_body():
    relay = _MediaRelay()
    t = _transport(relay=relay)
    assert await t.relay_media_ready(_paired())
    ok = await t.send_media_relay(
        instance=_paired(), header_dict={"msg_id": "m"}, payload_bytes=b"sealed"
    )
    assert ok == (True, None)
    [body] = relay.media
    assert body.startswith(b"\x00SHM")


async def test_no_relay_tier_means_no_relay_media():
    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=_Https(),
        gfs_routes=_routes(),
        signaling_send=_Signaler(),
    )
    assert not await t.relay_media_ready(_paired())
    assert await t.send_media_relay(
        instance=_paired(), header_dict={}, payload_bytes=b""
    ) == (False, None)


# ── v_88: per-space mailboxes for link-joined households ──────────────────


class _MailboxRelay(_MediaRelay):
    def __init__(self, *, mailbox_ready: bool = True) -> None:
        super().__init__()
        self.mailbox_ready_answer = mailbox_ready
        self.mailbox_calls: list[tuple[MailboxTarget, dict]] = []
        self.mailbox_media: list[tuple[MailboxTarget, bytes]] = []

    async def mailbox_ready(self, target, *, media=False) -> bool:
        return self.mailbox_ready_answer

    async def send_mailbox(self, *, instance, target, envelope_dict):
        self.mailbox_calls.append((target, envelope_dict))
        return True, None

    async def send_media_mailbox(self, *, instance, target, media_body):
        self.mailbox_media.append((target, media_body))
        return True, None


_TARGET = MailboxTarget(mailbox_id="m" * 32, kem_pk=b"k" * 32, gfs_url=ROUTE_A)


def _seat() -> RemoteInstance:
    return dataclasses.replace(
        _paired(inbox="", gfs_relay=False),
        source=InstanceSource.SPACE_SESSION,
        relay_via=ROUTE_A,
    )


def _mailbox_transport(relay, lookups, *, targets=None):
    async def _resolve(instance_id, space_id):
        lookups.append((instance_id, space_id))
        return (targets or {}).get(space_id)

    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=_Https(),
        gfs_relay=relay,
        gfs_routes=_routes(),
        mailbox_targets=_resolve,
        signaling_send=_Signaler(),
    )
    t.mark_ice_primed()
    return t


async def test_a_seat_with_a_mailbox_for_the_space_is_addressed_to_it():
    relay, lookups = _MailboxRelay(), []
    t = _mailbox_transport(relay, lookups, targets={"sp-1": _TARGET})
    env = {"msg_id": "m", "space_id": "sp-1"}
    result = await t.send(instance=_seat(), envelope_dict=env)
    assert result.ok
    assert relay.mailbox_calls == [(_TARGET, env)]
    assert relay.calls == []  # never instance-addressed
    assert lookups == [("peer-1", "sp-1")]


async def test_no_entry_or_a_gfs_without_mailboxes_keeps_todays_relay():
    relay, lookups = _MailboxRelay(), []
    t = _mailbox_transport(relay, lookups, targets={"sp-1": _TARGET})
    await t.send(instance=_seat(), envelope_dict={"msg_id": "m", "space_id": "sp-2"})
    assert relay.mailbox_calls == [] and len(relay.calls) == 1
    relay2 = _MailboxRelay(mailbox_ready=False)
    t2 = _mailbox_transport(relay2, [], targets={"sp-1": _TARGET})
    await t2.send(instance=_seat(), envelope_dict={"msg_id": "m", "space_id": "sp-1"})
    assert relay2.mailbox_calls == [] and len(relay2.calls) == 1


async def test_a_space_less_envelope_asks_for_any_shared_space():
    relay, lookups = _MailboxRelay(), []
    t = _mailbox_transport(relay, lookups, targets={None: _TARGET})
    await t.send(
        instance=_seat(),
        envelope_dict={
            "msg_id": "m",
            "space_id": None,
            "event_type": "instance_capabilities_updated",
        },
    )
    assert lookups == [("peer-1", None)]
    assert len(relay.mailbox_calls) == 1


async def test_a_space_less_space_event_never_rides_a_mailbox():
    """A ``SPACE_*`` envelope without its routing ``space_id`` (a redeem
    reply, a teardown) goes instance-addressed: a mailbox takes its own
    space's events only."""
    relay, lookups = _MailboxRelay(), []
    t = _mailbox_transport(relay, lookups, targets={None: _TARGET})
    await t.send(
        instance=_seat(),
        envelope_dict={
            "msg_id": "m",
            "space_id": None,
            "event_type": "space_invite_token_redeem_ack",
        },
    )
    assert lookups == [] and relay.mailbox_calls == []
    assert len(relay.calls) == 1


async def test_a_paired_peer_never_asks_for_a_mailbox():
    relay, lookups = _MailboxRelay(), []
    t = _mailbox_transport(relay, lookups, targets={"sp-1": _TARGET})
    await t.send(instance=_paired(), envelope_dict={"msg_id": "m", "space_id": "sp-1"})
    assert lookups == [] and relay.mailbox_calls == []


async def test_relay_media_to_a_seat_goes_to_its_mailbox():
    relay, lookups = _MailboxRelay(), []
    t = _mailbox_transport(relay, lookups, targets={"sp-1": _TARGET})
    ok = await t.send_media_relay(
        instance=_seat(),
        header_dict={"msg_id": "m", "space_id": "sp-1"},
        payload_bytes=b"x",
    )
    assert ok == (True, None)
    [(target, body)] = relay.mailbox_media
    assert target is _TARGET and body.startswith(b"\x00SHM")
    assert relay.media == []


async def test_a_failing_mailbox_lookup_falls_back_and_never_raises():
    relay = _MailboxRelay()

    async def _boom(instance_id, space_id):
        raise RuntimeError("db gone")

    t = FederationTransport(
        own_instance_id="self-iid",
        https_inbox=_Https(),
        gfs_relay=relay,
        mailbox_targets=_boom,
        signaling_send=_Signaler(),
    )
    result = await t.send(
        instance=_seat(), envelope_dict={"msg_id": "m", "space_id": "sp-1"}
    )
    assert result.ok and len(relay.calls) == 1


#: Send sites (``send_event`` and the wrappers that end in it) that send a
#: ``SPACE_*`` event WITHOUT its routing ``space_id`` — or with an explicit
#: ``space_id=None`` — on purpose (v_88): such an envelope never rides a
#: mailbox (it goes instance-addressed, and a mailbox refuses it). Mesh
#: routing and discovery are not space content; the redeem legs, a private
#: invite's accept and a join request's answer run before the other side
#: holds the space (a routing ``space_id`` would hit its space gates); the
#: removal notice and the teardown are for a household that no longer
#: does. A new space event belongs on its space's mailbox: pass
#: ``space_id=`` — or add it here with the reason.
_SPACE_LESS_SPACE_SENDS: dict[str, set[str]] = {
    "socialhome/federation/invite_token_redeem.py": {
        "SPACE_INVITE_TOKEN_REDEEM",
        "SPACE_INVITE_TOKEN_REDEEM_ACK",
        "SPACE_INVITE_TOKEN_REDEEM_DENY",
    },
    "socialhome/federation/private_invite_handler.py": {
        "SPACE_REMOTE_MEMBER_REMOVED",
    },
    "socialhome/federation/route_discovery.py": {
        "SPACE_FIND_ROUTE",
        "SPACE_ROUTE_FOUND",
    },
    "socialhome/federation/routed_envelope.py": {
        "SPACE_ROUTED",
        "SPACE_ROUTE_STALE",
    },
    "socialhome/services/peer_directory_service.py": {"SPACE_DIRECTORY_SYNC"},
    "socialhome/services/report_service.py": {"SPACE_REPORT"},
    "socialhome/services/space_service.py": {
        "SPACE_SESSION_CLEANUP",
        "SPACE_PRIVATE_INVITE_ACCEPT",
        "SPACE_JOIN_REQUEST_APPROVED",
        "SPACE_JOIN_REQUEST_DENIED",
    },
}


#: ``send_event`` and the wrappers that end in it.
_SEND_CALLS: frozenset[str] = frozenset(
    {"send_event", "send_event_once", "send_to_instance", "send_with_mesh_fallback"}
)


def test_every_space_event_send_names_its_space_or_is_listed():
    root = pathlib.Path(__file__).resolve().parents[2]
    found: dict[str, set[str]] = {}
    for path in sorted((root / "socialhome").rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else ""
            if name not in _SEND_CALLS:
                continue
            kw = {k.arg: k.value for k in node.keywords}
            et = kw.get("event_type")
            if et is None:
                continue
            space = kw.get("space_id")
            explicit_none = isinstance(space, ast.Constant) and space.value is None
            if space is not None and not explicit_none:
                continue
            src = ast.unparse(et)
            if "FederationEventType.SPACE_" not in src:
                continue
            event = src.rsplit(".", 1)[-1].strip("()")
            rel = str(path.relative_to(root))
            found.setdefault(rel, set()).add(event)
    unexpected = {
        rel: events - _SPACE_LESS_SPACE_SENDS.get(rel, set())
        for rel, events in found.items()
        if events - _SPACE_LESS_SPACE_SENDS.get(rel, set())
    }
    assert unexpected == {}, (
        "a SPACE_* send without space_id= — pass the routing space_id so a "
        f"GFS-only member gets it on that space's mailbox: {unexpected}"
    )


def test_the_guard_catches_a_wrapper_and_an_explicit_none(tmp_path, monkeypatch):
    """Mutation guard for the guard: a ``send_to_instance`` without
    ``space_id`` and a ``send_event(..., space_id=None)`` are both caught."""
    src = (
        "async def f(o):\n"
        "    await o.send_to_instance(to_instance_id='x',"
        " event_type=FederationEventType.SPACE_POST_CREATED, payload={})\n"
        "    await o.send_event(to_instance_id='x',"
        " event_type=FederationEventType.SPACE_TASK_CREATED, payload={},"
        " space_id=None)\n"
    )
    found = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else ""
        if name not in _SEND_CALLS:
            continue
        kw = {k.arg: k.value for k in node.keywords}
        space = kw.get("space_id")
        explicit_none = isinstance(space, ast.Constant) and space.value is None
        if space is None or explicit_none:
            found.append(name)
    assert found == ["send_to_instance", "send_event"]


async def test_a_directory_seat_never_goes_instance_addressed():
    """v_89: a member seated from the host's directory has no ``relay_via``.
    With no usable mailbox it gets nothing over the relay — never an
    instance-addressed frame naming two households that never met at the
    GFS (the caller goes through the host instead)."""
    lazy = dataclasses.replace(_seat(), relay_via=None)
    relay = _MailboxRelay(mailbox_ready=False)
    t = _mailbox_transport(relay, [], targets={"sp-1": _TARGET})
    env = {"msg_id": "m", "space_id": "sp-1"}
    result = await t.send(instance=lazy, envelope_dict=env)
    assert not result.ok and result.error == "no_mailbox"
    assert relay.calls == [] and relay.mailbox_calls == []
    assert not await t.mailbox_reachable(lazy, "sp-1")
    relay2 = _MailboxRelay()
    t2 = _mailbox_transport(relay2, [], targets={"sp-1": _TARGET})
    result = await t2.send(instance=lazy, envelope_dict=env)
    assert result.ok and relay2.mailbox_calls == [(_TARGET, env)]
    assert relay2.calls == []
    assert await t2.mailbox_reachable(lazy, "sp-1")


# ── v_91: one upload for many mailboxes ───────────────────────────────────


class _MultiRelay(_MailboxRelay):
    def __init__(self, *, capacity: int = 500, mailbox_ready: bool = True) -> None:
        super().__init__(mailbox_ready=mailbox_ready)
        self.capacity = capacity
        self.capacity_asks: list[str] = []
        self.multi_calls: list[tuple[list[tuple[MailboxTarget, dict]], dict]] = []
        self.multi_result: tuple[bool, int | None, float | None] = (True, None, None)

    async def multi_capacity(self, gfs_url):
        self.capacity_asks.append(gfs_url)
        return self.capacity

    async def send_multi_mailbox(self, *, to, body):
        self.multi_calls.append((list(to), body))
        return self.multi_result


async def test_a_seat_with_a_mailbox_on_a_multi_gfs_is_a_multi_target():
    relay = _MultiRelay()
    t = _mailbox_transport(relay, [], targets={"sp-1": _TARGET})
    assert await t.multi_mailbox_target(_seat(), "sp-1") is _TARGET
    assert await t.multi_capacity(ROUTE_A) == 500
    assert await t.send_multi_mailbox(to=[(_TARGET, {"w": 1})], body={"x": 1}) == (
        True,
        None,
        None,
    )
    assert relay.multi_calls == [([(_TARGET, {"w": 1})], {"x": 1})]


async def test_a_capacity_cache_probes_each_gfs_once_per_broadcast():
    relay = _MultiRelay(capacity=40)
    t = _mailbox_transport(relay, [], targets={"sp-1": _TARGET})
    caps: dict[str, int] = {}
    for _ in range(3):
        assert await t.multi_mailbox_target(_seat(), "sp-1", caps=caps) is _TARGET
    assert len(relay.capacity_asks) == 1
    assert list(caps.values()) == [40]


async def test_no_multi_target_without_a_mailbox_a_multi_gfs_or_a_seat():
    # No mailbox for that space.
    t = _mailbox_transport(_MultiRelay(), [], targets={"sp-1": _TARGET})
    assert await t.multi_mailbox_target(_seat(), "sp-2") is None
    # The GFS did not prove envelope_mailbox.
    t = _mailbox_transport(
        _MultiRelay(mailbox_ready=False), [], targets={"sp-1": _TARGET}
    )
    assert await t.multi_mailbox_target(_seat(), "sp-1") is None
    # The GFS proves mailboxes but not envelope_multi.
    t = _mailbox_transport(_MultiRelay(capacity=0), [], targets={"sp-1": _TARGET})
    assert await t.multi_mailbox_target(_seat(), "sp-1") is None
    # A paired peer is never a mailbox seat.
    lookups: list = []
    t = _mailbox_transport(_MultiRelay(), lookups, targets={"sp-1": _TARGET})
    assert await t.multi_mailbox_target(_paired(), "sp-1") is None
    assert lookups == []


async def test_multi_without_a_relay_tier_is_off():
    t = _transport(relay=None)
    t._gfs_relay = None
    assert await t.multi_mailbox_target(_seat(), "sp-1") is None
    assert await t.multi_capacity(ROUTE_A) == 0
    assert await t.send_multi_mailbox(to=[(_TARGET, {})], body={}) == (
        False,
        None,
        None,
    )
