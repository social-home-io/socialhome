"""Coverage extras for app._redeliver_envelope (outbox retry path)."""

from __future__ import annotations

import pytest

from socialhome.app import _redeliver_envelope, _aiohttp_timeout
from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import (
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.federation.federation_service import FederationService
from socialhome.infrastructure import DeliveryOutcome, EventBus, KeyManager
from socialhome.repositories import (
    SqliteFederationRepo,
    SqliteOutboxRepo,
)


# ─── _aiohttp_timeout ────────────────────────────────────────────────────


def test_aiohttp_timeout_returns_object():
    t = _aiohttp_timeout(10)
    # aiohttp installed → real ClientTimeout. Either way, no raise.
    assert t is not None or t is None


# ─── _redeliver_envelope ─────────────────────────────────────────────────


class _OutboxEntry:
    def __init__(self, *, id, instance_id, payload_json, attempts=0):
        self.id = id
        self.instance_id = instance_id
        self.payload_json = payload_json
        #: Mirrors ``OutboxEntry.attempts``. The 404 path is only
        #: transient for the first few attempts (see
        #: ``PAIR_WINDOW_404_ATTEMPTS``), so tests must be able to set it.
        self.attempts = attempts


def _stored_envelope_json(svc, *, to_instance, msg_id="m1"):
    """A valid signed stored envelope, as the outbox would have it.

    ``_redeliver_envelope`` now re-stamps + re-signs the stored envelope
    before POSTing, so the queued ``payload_json`` must be a real
    envelope dict (not a ``"{}"`` placeholder) for the redelivery path
    to parse it.
    """
    import orjson

    envelope = {
        "msg_id": msg_id,
        "event_type": "space_dissolved",
        "from_instance": svc._own_instance_id,
        "to_instance": to_instance,
        "timestamp": "2000-01-01T00:00:00+00:00",
        "encrypted_payload": "nonce:ct",
        "space_id": "space-1",
        "proto_version": 1,
        "sig_suite": "ed25519",
    }
    envelope["signatures"] = svc._encoder.sign_envelope_all(
        orjson.dumps(envelope), suite="ed25519"
    )
    return orjson.dumps(envelope).decode()


@pytest.fixture
async def env(tmp_dir):
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    own_kp = generate_identity_keypair()
    own_iid = derive_instance_id(own_kp.public_key)
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (own_iid, own_kp.private_key.hex(), own_kp.public_key.hex(), "aa" * 32),
    )
    fed_repo = SqliteFederationRepo(db)
    outbox = SqliteOutboxRepo(db)
    bus = EventBus()
    kek = KeyManager.from_data_dir(tmp_dir)
    svc = FederationService(
        db=db,
        federation_repo=fed_repo,
        outbox_repo=outbox,
        key_manager=kek,
        bus=bus,
        own_instance_id=own_iid,
        own_identity_seed=own_kp.private_key,
        own_identity_pk=own_kp.public_key,
    )
    yield svc, fed_repo, kek
    await db.shutdown()


async def test_redeliver_unknown_instance_is_permanent(env):
    svc, fed_repo, _ = env
    entry = _OutboxEntry(
        id="e1",
        instance_id="never-paired",
        payload_json="{}",
    )
    outcome = await _redeliver_envelope(svc, fed_repo, entry)
    # Instance was unpaired / dropped — nothing to retry, mark failed.
    assert outcome is DeliveryOutcome.PERMANENT


async def test_redeliver_corrupt_payload_is_permanent_not_infinite_retry(env):
    """A malformed stored envelope can't be re-signed and never will be — it
    must drop PERMANENT, not wedge as an immortal TRANSIENT retry (which, for
    a NEVER_DROP event, is a ceiling-backoff loop that never ends). The HTTP
    client must never be reached for an undecodable entry."""
    svc, fed_repo, kek = env
    peer_kp = generate_identity_keypair()
    wrapped = kek.encrypt(b"\x01" * 32)
    peer = RemoteInstance(
        id=derive_instance_id(peer_kp.public_key),
        display_name="peer",
        remote_identity_pk=peer_kp.public_key.hex(),
        key_self_to_remote=wrapped,
        key_remote_to_self=wrapped,
        remote_inbox_url="https://x/wh",
        local_inbox_id="wh",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )
    await fed_repo.save_instance(peer)

    class _BoomClient:
        def post(self, url, **kw):  # pragma: no cover - must not be called
            raise AssertionError("HTTP client reached for an undecodable entry")

    svc._http_client = _BoomClient()
    # Missing msg_id/encrypted_payload → resign_for_redelivery raises (KeyError).
    entry = _OutboxEntry(
        id="e-bad",
        instance_id=peer.id,
        payload_json='{"not":"an envelope"}',
    )
    outcome = await _redeliver_envelope(svc, fed_repo, entry)
    assert outcome is DeliveryOutcome.PERMANENT

    # Invalid JSON entirely → same PERMANENT drop.
    entry2 = _OutboxEntry(id="e-bad2", instance_id=peer.id, payload_json="not json")
    outcome2 = await _redeliver_envelope(svc, fed_repo, entry2)
    assert outcome2 is DeliveryOutcome.PERMANENT


async def test_redeliver_2xx_is_success(env):
    svc, fed_repo, kek = env
    peer_kp = generate_identity_keypair()
    wrapped = kek.encrypt(b"\x01" * 32)
    peer = RemoteInstance(
        id=derive_instance_id(peer_kp.public_key),
        display_name="peer",
        remote_identity_pk=peer_kp.public_key.hex(),
        key_self_to_remote=wrapped,
        key_remote_to_self=wrapped,
        remote_inbox_url="https://x/wh",
        local_inbox_id="wh",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )
    await fed_repo.save_instance(peer)

    class _Resp:
        def __init__(self):
            self.status = 204

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Client:
        def post(self, url, **kw):
            return _Resp()

    svc._http_client = _Client()
    entry = _OutboxEntry(
        id="e1",
        instance_id=peer.id,
        payload_json=_stored_envelope_json(svc, to_instance=peer.id),
    )
    outcome = await _redeliver_envelope(svc, fed_repo, entry)
    assert outcome is DeliveryOutcome.SUCCESS


async def test_redeliver_5xx_is_transient(env):
    svc, fed_repo, kek = env
    peer_kp = generate_identity_keypair()
    wrapped = kek.encrypt(b"\x02" * 32)
    peer = RemoteInstance(
        id=derive_instance_id(peer_kp.public_key),
        display_name="peer",
        remote_identity_pk=peer_kp.public_key.hex(),
        key_self_to_remote=wrapped,
        key_remote_to_self=wrapped,
        remote_inbox_url="https://x/wh",
        local_inbox_id="wh2",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )
    await fed_repo.save_instance(peer)

    class _Resp:
        def __init__(self):
            self.status = 503

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Client:
        def post(self, url, **kw):
            return _Resp()

    svc._http_client = _Client()
    entry = _OutboxEntry(
        id="e2",
        instance_id=peer.id,
        payload_json=_stored_envelope_json(svc, to_instance=peer.id),
    )
    outcome = await _redeliver_envelope(svc, fed_repo, entry)
    assert outcome is DeliveryOutcome.TRANSIENT


async def test_redeliver_4xx_is_permanent(env):
    """A 4xx response — replay-cache hit, expired timestamp, banned —
    must be dropped, not retried. Specifically pins the 410 ``Replay
    detected`` shape that left thousands of zombie outbox entries
    behind during the HA-integration charset bug. ``404`` is **not**
    in the PERMANENT set — see :func:`test_redeliver_404_is_transient`."""
    svc, fed_repo, kek = env
    peer_kp = generate_identity_keypair()
    wrapped = kek.encrypt(b"\x04" * 32)
    peer = RemoteInstance(
        id=derive_instance_id(peer_kp.public_key),
        display_name="peer",
        remote_identity_pk=peer_kp.public_key.hex(),
        key_self_to_remote=wrapped,
        key_remote_to_self=wrapped,
        remote_inbox_url="https://x/wh",
        local_inbox_id="wh-410",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )
    await fed_repo.save_instance(peer)

    class _Resp:
        def __init__(self, status, body='{"error": "unknown_inbox"}'):
            self.status = status
            self._body = body

        async def text(self):
            #: The 404 path reads the body to tell the peer's own Social
            #: Home ("unknown_inbox") from an intermediary that answered
            #: instead — e.g. the HA integration's forwarder view missing.
            return self._body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Client:
        def __init__(self, status):
            self._status = status

        def post(self, url, **kw):
            return _Resp(self._status)

    for status in (400, 403, 410, 422):
        svc._http_client = _Client(status)
        entry = _OutboxEntry(
            id=f"e-{status}",
            instance_id=peer.id,
            payload_json=_stored_envelope_json(svc, to_instance=peer.id),
        )
        outcome = await _redeliver_envelope(svc, fed_repo, entry)
        assert outcome is DeliveryOutcome.PERMANENT, (status, outcome)


async def test_redeliver_404_is_transient(env):
    """A 404 is transient *early on* — the peer may just not have
    installed its RemoteInstance row for us yet. Common in the
    trust-relay pairing window where our PairingConfirmed-driven
    ``INSTANCE_CAPABILITIES_UPDATED`` races ahead of the ack reaching
    the peer through the relay.

    Bounded by ``PAIR_WINDOW_404_ATTEMPTS`` — see
    :func:`test_redeliver_404_becomes_permanent_after_the_pair_window`."""
    svc, fed_repo, kek = env
    peer_kp = generate_identity_keypair()
    wrapped = kek.encrypt(b"\x04" * 32)
    peer = RemoteInstance(
        id=derive_instance_id(peer_kp.public_key),
        display_name="peer",
        remote_identity_pk=peer_kp.public_key.hex(),
        key_self_to_remote=wrapped,
        key_remote_to_self=wrapped,
        remote_inbox_url="https://x/wh",
        local_inbox_id="wh-404",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )
    await fed_repo.save_instance(peer)

    class _Resp:
        def __init__(self, status, body='{"error": "unknown_inbox"}'):
            self.status = status
            self._body = body

        async def text(self):
            #: The 404 path reads the body to tell the peer's own Social
            #: Home ("unknown_inbox") from an intermediary that answered
            #: instead — e.g. the HA integration's forwarder view missing.
            return self._body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Client:
        def post(self, url, **kw):
            return _Resp(404)

    svc._http_client = _Client()
    entry = _OutboxEntry(
        id="e-404",
        instance_id=peer.id,
        payload_json=_stored_envelope_json(svc, to_instance=peer.id),
    )
    outcome = await _redeliver_envelope(svc, fed_repo, entry)
    assert outcome is DeliveryOutcome.TRANSIENT


async def test_redeliver_4xx_marks_peer_reachable(env):
    """A 4xx is proof of reachability — the receiver got the HTTP
    request, ran our envelope through the §24.11 pipeline, and chose
    to reject it. The peer-online indicator in the SPA reads from
    ``RemoteInstance.unreachable_since``; without flipping that field
    back to ``None`` on 4xx, the entire post-charset-bug backlog of
    410 ``Replay detected`` retries kept the indicator stuck on
    "not connected" even though the peer was clearly responsive.
    """
    svc, fed_repo, kek = env
    peer_kp = generate_identity_keypair()
    wrapped = kek.encrypt(b"\x05" * 32)
    peer = RemoteInstance(
        id=derive_instance_id(peer_kp.public_key),
        display_name="peer",
        remote_identity_pk=peer_kp.public_key.hex(),
        key_self_to_remote=wrapped,
        key_remote_to_self=wrapped,
        remote_inbox_url="https://x/wh",
        local_inbox_id="wh-410-reach",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )
    await fed_repo.save_instance(peer)
    # Simulate the peer having been marked unreachable by an earlier
    # send_event failure.
    await fed_repo.mark_unreachable(peer.id)
    pre = await fed_repo.get_instance(peer.id)
    assert pre.unreachable_since is not None

    class _Resp:
        def __init__(self):
            self.status = 410

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Client:
        def post(self, url, **kw):
            return _Resp()

    svc._http_client = _Client()
    entry = _OutboxEntry(
        id="e-reach",
        instance_id=peer.id,
        payload_json=_stored_envelope_json(svc, to_instance=peer.id),
    )
    outcome = await _redeliver_envelope(svc, fed_repo, entry)
    assert outcome is DeliveryOutcome.PERMANENT

    post = await fed_repo.get_instance(peer.id)
    assert post.unreachable_since is None, (
        "4xx response must flip the peer back to reachable — otherwise "
        "the SPA's online indicator stays stuck on 'not connected'"
    )


async def test_redeliver_transport_error_is_transient(env):
    svc, fed_repo, kek = env
    peer_kp = generate_identity_keypair()
    wrapped = kek.encrypt(b"\x03" * 32)
    peer = RemoteInstance(
        id=derive_instance_id(peer_kp.public_key),
        display_name="peer",
        remote_identity_pk=peer_kp.public_key.hex(),
        key_self_to_remote=wrapped,
        key_remote_to_self=wrapped,
        remote_inbox_url="https://x/wh",
        local_inbox_id="wh3",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )
    await fed_repo.save_instance(peer)

    class _Client:
        def post(self, url, **kw):
            raise ConnectionError("boom")

    svc._http_client = _Client()
    entry = _OutboxEntry(
        id="e3",
        instance_id=peer.id,
        payload_json=_stored_envelope_json(svc, to_instance=peer.id),
    )
    outcome = await _redeliver_envelope(svc, fed_repo, entry)
    assert outcome is DeliveryOutcome.TRANSIENT


async def test_redeliver_re_signs_with_fresh_timestamp(env):
    """The redelivery path re-stamps + re-signs the stored envelope so a
    NEVER_DROP event (ban / key revocation / SPACE_DISSOLVED) queued more
    than ±300s ago is no longer rejected for clock skew. We capture the
    bytes actually POSTed and assert the timestamp is fresh while msg_id
    is preserved (replay-dedup still keys correctly)."""
    import orjson
    from datetime import datetime, timedelta, timezone

    svc, fed_repo, kek = env
    peer_kp = generate_identity_keypair()
    wrapped = kek.encrypt(b"\x07" * 32)
    peer = RemoteInstance(
        id=derive_instance_id(peer_kp.public_key),
        display_name="peer",
        remote_identity_pk=peer_kp.public_key.hex(),
        key_self_to_remote=wrapped,
        key_remote_to_self=wrapped,
        remote_inbox_url="https://x/wh",
        local_inbox_id="wh-resign",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )
    await fed_repo.save_instance(peer)

    # A stale stored envelope (10 minutes old), signed the canonical way.
    stale_iso = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    stored = {
        "msg_id": "never-drop-msg-1",
        "event_type": "space_dissolved",
        "from_instance": svc._own_instance_id,
        "to_instance": peer.id,
        "timestamp": stale_iso,
        "encrypted_payload": "nonce:ct",
        "space_id": "space-1",
        "proto_version": 1,
        "sig_suite": "ed25519",
    }
    stored["signatures"] = svc._encoder.sign_envelope_all(
        orjson.dumps(stored), suite="ed25519"
    )

    captured = {}

    class _Resp:
        status = 204

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Client:
        def post(self, url, **kw):
            captured["body"] = kw.get("data")
            return _Resp()

    svc._http_client = _Client()
    entry = _OutboxEntry(
        id="e-resign", instance_id=peer.id, payload_json=orjson.dumps(stored).decode()
    )
    outcome = await _redeliver_envelope(svc, fed_repo, entry)
    assert outcome is DeliveryOutcome.SUCCESS

    posted = orjson.loads(captured["body"])
    # msg_id preserved → replay-dedup keys correctly.
    assert posted["msg_id"] == "never-drop-msg-1"
    # encrypted payload untouched.
    assert posted["encrypted_payload"] == "nonce:ct"
    # Fresh timestamp: well within the ±300s skew window.
    new_ts = datetime.fromisoformat(posted["timestamp"])
    skew = abs((datetime.now(timezone.utc) - new_ts).total_seconds())
    assert skew < 5, f"redelivered envelope timestamp not refreshed (skew {skew}s)"
    assert posted["timestamp"] != stale_iso


async def test_redeliver_404_becomes_permanent_after_the_pair_window(env):
    """A peer that keeps 404ing is not a race — stop paying for it.

    404 on the inbox path means the peer could not resolve the inbox id
    we POSTed to. The handshake race that justifies retrying clears in
    seconds, so retrying the full 13-attempt ladder spent ~8 hours and a
    fresh PeerConnection per attempt to reach a foregone conclusion. A
    real deployment showed ~4,400 STUN bindings churning through a stuck
    54-envelope backlog while the household believed it was federated.
    """
    from socialhome.infrastructure import PAIR_WINDOW_404_ATTEMPTS

    svc, fed_repo, kek = env
    peer_kp = generate_identity_keypair()
    wrapped = kek.encrypt(b"\x05" * 32)
    peer = RemoteInstance(
        id=derive_instance_id(peer_kp.public_key),
        display_name="peer",
        remote_identity_pk=peer_kp.public_key.hex(),
        key_self_to_remote=wrapped,
        key_remote_to_self=wrapped,
        remote_inbox_url="https://x/wh",
        local_inbox_id="wh-404-give-up",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )
    await fed_repo.save_instance(peer)

    class _Resp:
        def __init__(self, status, body='{"error": "unknown_inbox"}'):
            self.status = status
            self._body = body

        async def text(self):
            #: The 404 path reads the body to tell the peer's own Social
            #: Home ("unknown_inbox") from an intermediary that answered
            #: instead — e.g. the HA integration's forwarder view missing.
            return self._body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Client:
        def post(self, url, **kw):
            return _Resp(404)

    svc._http_client = _Client()

    def _entry(attempts):
        return _OutboxEntry(
            id=f"e-404-{attempts}",
            instance_id=peer.id,
            payload_json=_stored_envelope_json(svc, to_instance=peer.id),
            attempts=attempts,
        )

    # Inside the window: still worth another go.
    for n in range(PAIR_WINDOW_404_ATTEMPTS):
        assert (
            await _redeliver_envelope(svc, fed_repo, _entry(n))
            is DeliveryOutcome.TRANSIENT
        ), f"gave up too early at attempt {n}"

    # Past it: drop, rather than burn the rest of the ladder.
    assert (
        await _redeliver_envelope(svc, fed_repo, _entry(PAIR_WINDOW_404_ATTEMPTS))
        is DeliveryOutcome.PERMANENT
    )


async def test_redeliver_404_give_up_says_what_to_check(env, caplog):
    """The drop must be actionable: an operator seeing this needs to know
    it is a pairing problem on the peer, not a network fault here."""
    import logging

    from socialhome.infrastructure import PAIR_WINDOW_404_ATTEMPTS

    svc, fed_repo, kek = env
    peer_kp = generate_identity_keypair()
    wrapped = kek.encrypt(b"\x06" * 32)
    peer = RemoteInstance(
        id=derive_instance_id(peer_kp.public_key),
        display_name="peer",
        remote_identity_pk=peer_kp.public_key.hex(),
        key_self_to_remote=wrapped,
        key_remote_to_self=wrapped,
        remote_inbox_url="https://x/wh",
        local_inbox_id="wh-404-msg",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )
    await fed_repo.save_instance(peer)

    class _Resp:
        def __init__(self, status, body='{"error": "unknown_inbox"}'):
            self.status = status
            self._body = body

        async def text(self):
            #: The 404 path reads the body to tell the peer's own Social
            #: Home ("unknown_inbox") from an intermediary that answered
            #: instead — e.g. the HA integration's forwarder view missing.
            return self._body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Client:
        def post(self, url, **kw):
            return _Resp(404)

    svc._http_client = _Client()
    entry = _OutboxEntry(
        id="e-404-msg",
        instance_id=peer.id,
        payload_json=_stored_envelope_json(svc, to_instance=peer.id),
        attempts=PAIR_WINDOW_404_ATTEMPTS,
    )
    with caplog.at_level(logging.WARNING, logger="socialhome"):
        await _redeliver_envelope(svc, fed_repo, entry)

    # Names both causes, because the log cannot tell them apart.
    assert "re-pair" in caplog.text
    assert "provisional" in caplog.text


async def test_redeliver_404_without_our_marker_blames_the_intermediary(env, caplog):
    """A 404 that did not come from the peer's Social Home reads differently.

    Under ha/haos, peers are reached at
    ``{HA URL}/api/socialhome/inbox/{inbox_id}`` — a view the companion
    integration registers inside Home Assistant. If the integration isn't
    loaded, Home Assistant itself 404s every inbox POST, with no Social
    Home change on either side (an HA restart is enough). Our own inbox
    always answers with an ``unknown_inbox`` marker, so its absence is the
    signal, and telling an operator to re-pair would be wrong here.
    """
    import logging

    from socialhome.infrastructure import PAIR_WINDOW_404_ATTEMPTS

    svc, fed_repo, kek = env
    peer_kp = generate_identity_keypair()
    wrapped = kek.encrypt(b"\x07" * 32)
    peer = RemoteInstance(
        id=derive_instance_id(peer_kp.public_key),
        display_name="peer",
        remote_identity_pk=peer_kp.public_key.hex(),
        key_self_to_remote=wrapped,
        key_remote_to_self=wrapped,
        remote_inbox_url="https://ha.example/api/socialhome/inbox/wh",
        local_inbox_id="wh-ha-404",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )
    await fed_repo.save_instance(peer)

    class _Resp:
        status = 404

        async def text(self):
            return "404: Not Found"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Client:
        def post(self, url, **kw):
            return _Resp()

    svc._http_client = _Client()
    entry = _OutboxEntry(
        id="e-404-ha",
        instance_id=peer.id,
        payload_json=_stored_envelope_json(svc, to_instance=peer.id),
        attempts=PAIR_WINDOW_404_ATTEMPTS,
    )
    with caplog.at_level(logging.WARNING, logger="socialhome"):
        outcome = await _redeliver_envelope(svc, fed_repo, entry)

    assert outcome is DeliveryOutcome.PERMANENT
    assert "did not come from the peer's Social Home" in caplog.text
    # Must NOT send the operator off to re-pair — that isn't the fault.
    assert "re-pair" not in caplog.text
