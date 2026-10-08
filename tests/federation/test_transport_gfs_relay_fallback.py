"""FederationTransport — the GFS relay as a fallback tier for paired peers.

A paired household (``source = manual``) that opted into the relay
(``RemoteInstance.gfs_relay``) and has confirmed routes is reached
RTC → HTTPS inbox → connection-server relay, round-robin over the routes.
Link-joined households (``space_session``) keep their relay-only path,
byte for byte — those cases live in ``test_federation_transport.py``.
"""

from __future__ import annotations

import dataclasses

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

        async def send(self, data):
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
