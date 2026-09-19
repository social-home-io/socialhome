"""Inbound federation validation pipeline (§24.11) — middleware chain.

The §24.11 pipeline validates every inbound federation inbox before the
payload reaches business logic. Each step is a standalone async callable
(a *middleware*) that receives the validation context and either passes
or raises ``ValueError`` to reject.

Steps (in order):

1. **JSON parse** — ``raw_body`` → envelope dict.
2. **Instance lookup** — ``local_inbox_id`` → ``RemoteInstance``.
2b. **Peer class** — a ``space_session`` sender may only use the space
    vocabulary (``SPACE_SESSION_ALLOWED_EVENT_TYPES``).
3. **Timestamp skew** — ``abs(now - envelope.timestamp) ≤ 300s`` (wider
   for a relay-carried envelope, see ``RELAY_TIMESTAMP_SKEW_SECONDS``).
4. **Signature verify** — Ed25519 with the remote's identity_pk.
5. **Replay check** — ``msg_id`` already seen → reject.
6. **Decrypt payload** — AES-256-GCM using ``key_remote_to_self``.
7. **Parse inner** — decrypted bytes → ``FederationEvent``.
8. **Idempotency** — optional ``idempotency_key`` de-dup.
9. **Ban check** — space-scoped events from banned instances → reject.
10. **Persist replay** — insert ``msg_id`` into the replay table.

The middleware shape matches :class:`InboundStep`: a coroutine that
takes :class:`InboundContext` and returns either ``None`` (pass) or a
``dict`` to short-circuit with an early response. Raising ``ValueError``
means "reject the envelope".

Benefits of the decomposition:

* **Isolated testing** — each step has a dedicated unit-test without
  needing a fully-wired ``FederationService``.
* **Extension** — new steps (e.g. quota enforcement, sealed-sender
  unseal) are added by appending to the chain, not by editing a
  200-line monolith.
* **Reuse** — the same chain validates both HTTPS-inbox and
  DataChannel-delivered envelopes.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import orjson

from ..domain.federation import (
    SPACE_SESSION_ALLOWED_EVENT_TYPES,
    FederationEvent,
    FederationEventType,
    InstanceSource,
    RemoteInstance,
)
from ..domain.space import SpaceRole

log = logging.getLogger(__name__)

#: Maximum allowed clock skew for inbound envelopes (§24.11 §5).
TIMESTAMP_SKEW_SECONDS = 300

#: Transport label for an envelope the connection-server relay carried
#: (:class:`socialhome.federation.gfs_relay_transport.GfsRelayTransport`).
#: Set on :attr:`InboundContext.transport` by the relay dispatch seam.
TRANSPORT_GFS_RELAY = "gfs_relay"

#: How long the relay may hold an undelivered envelope. Mirrors
#: :data:`socialhome.global_server.envelope_relay.ENVELOPE_QUEUE_TTL_SECONDS`
#: — duplicated rather than imported because the household half must not
#: depend on the server package, and pinned equal by
#: ``tests/federation/test_inbound_validator.py``.
RELAY_QUEUE_TTL_SECONDS = 24 * 60 * 60

#: Skew budget for a relay-carried envelope.
#:
#: The relay answers ``202`` the moment it accepts a blob, and that
#: acceptance IS the delivery contract: an offline household gets the
#: bytes on its next hello, up to :data:`RELAY_QUEUE_TTL_SECONDS` later.
#: Judging those bytes against the ±300 s live-wire window means every
#: envelope queued for a sleeping household is rejected on arrival — the
#: sender saw a ``202`` and never used the outbox, so the event is simply
#: gone. The window therefore has to cover the queue's own TTL plus the
#: ordinary clock-skew allowance.
#:
#: Widening a timestamp window only costs what the replay defence cannot
#: cover, so the two are pinned together: the durable
#: ``federation_replay_cache`` retention
#: (:data:`socialhome.crypto.REPLAY_CACHE_WINDOW`, pruned by
#: :class:`~socialhome.infrastructure.replay_cache_scheduler
#: .ReplayCachePruneScheduler`) is ≥ this value, so a captured relay
#: envelope replayed anywhere inside the window still hits a remembered
#: ``msg_id``. ``tests/federation/test_inbound_validator.py`` asserts the
#: inequality so nobody can shrink the retention without noticing.
RELAY_TIMESTAMP_SKEW_SECONDS = RELAY_QUEUE_TTL_SECONDS + TIMESTAMP_SKEW_SECONDS


# ─── Validation context ─────────────────────────────────────────────────


@dataclass
class InboundContext:
    """Mutable bag of state threaded through the pipeline.

    Steps populate fields; later steps read them. By the time the chain
    finishes without error, ``event`` is a validated ``FederationEvent``
    ready for dispatch.
    """

    #: Raw bytes received from the transport.
    raw_body: bytes = b""

    #: Inbox identifier from the URL path (HTTPS inbox transport).
    inbox_id: str = ""

    #: Instance identifier (WebRTC transport — already known from the
    #: DataChannel connection). When set, the lookup step uses this
    #: instead of ``inbox_id``.
    instance_id: str = ""

    #: Parsed envelope dict (populated by ``parse_json``).
    envelope: dict = field(default_factory=dict)

    #: Resolved RemoteInstance wrapper (populated by ``lookup_instance``).
    instance: Any = None

    #: Fully validated FederationEvent (populated by ``decrypt_and_parse``).
    event: FederationEvent | None = None

    #: Short-circuit response (set by idempotency or other steps that
    #: want to return early without dispatching).
    early_response: dict | None = None

    #: Which transport delivered these bytes. Empty for a live wire (RTC
    #: DataChannel, HTTPS inbox); :data:`TRANSPORT_GFS_RELAY` when the
    #: connection-server relay carried them, which is the one case where
    #: the bytes may legitimately be up to a day old.
    transport: str = ""


#: Middleware shape: async callable that takes context + raises or returns.
InboundStep = Callable[[InboundContext], Awaitable[None]]


class _InboxInstance:
    """Thin wrapper that exposes ``RemoteInstance`` fields needed by the
    inbound pipeline while also providing a ``from_instance`` attribute
    for cross-checking the ``from_instance`` field in the envelope."""

    __slots__ = ("_inst",)

    def __init__(self, inst: RemoteInstance) -> None:
        self._inst = inst

    @property
    def from_instance(self) -> str:
        return self._inst.id

    @property
    def remote_identity_pk(self) -> str:
        return self._inst.remote_identity_pk

    @property
    def remote_pq_identity_pk(self) -> str | None:
        return self._inst.remote_pq_identity_pk

    @property
    def sig_suite(self) -> str:
        return self._inst.sig_suite

    @property
    def key_remote_to_self(self) -> str:
        return self._inst.key_remote_to_self

    @property
    def source(self) -> InstanceSource:
        """How the row came to exist — read by the peer-class gate."""
        return self._inst.source


# ─── Individual steps ────────────────────────────────────────────────────


def make_parse_json(*, loads) -> InboundStep:
    """Step 1: parse raw bytes → envelope dict."""

    async def parse_json(ctx: InboundContext) -> None:
        try:
            ctx.envelope = loads(ctx.raw_body)
        except Exception as exc:
            raise ValueError(f"Invalid JSON body: {exc}") from exc

        required = (
            "msg_id",
            "event_type",
            "from_instance",
            "to_instance",
            "timestamp",
            "encrypted_payload",
            "sig_suite",
            "signatures",
        )
        missing = [f for f in required if f not in ctx.envelope]
        if missing:
            raise ValueError(f"Missing required fields: {missing}")
        # §24.11 #17: type-check required string fields. Without this a
        # peer could send e.g. ``"from_instance": ["a", "b"]`` and downstream
        # code would happily plumb the list value into ban-check / replay
        # cache keys.
        for str_field in (
            "msg_id",
            "event_type",
            "from_instance",
            "to_instance",
            "timestamp",
            "encrypted_payload",
            "sig_suite",
        ):
            if not isinstance(ctx.envelope[str_field], str):
                raise ValueError(f"{str_field} must be a string")
        if not isinstance(ctx.envelope["signatures"], dict):
            raise ValueError("signatures must be a dict keyed by algorithm")

        # Validate event_type early — reject unknown types before burning
        # CPU on instance lookup / crypto. Matches original §24.11 order.
        try:
            FederationEventType(ctx.envelope["event_type"])
        except ValueError as exc:
            raise ValueError(
                f"Unknown event_type: {ctx.envelope['event_type']!r}"
            ) from exc

    return parse_json


def make_lookup_instance(*, repo, lookup_fn) -> InboundStep:
    """Step 2 (inbox): resolve RemoteInstance by inbox_id."""

    async def lookup_instance(ctx: InboundContext) -> None:
        instance = await lookup_fn(repo, ctx.inbox_id)
        if instance is None:
            raise ValueError(f"No instance found for inbox_id={ctx.inbox_id!r}")
        # Provisional row written by ``AutoPairCoordinator.request_via``
        # before the ack from C arrives: the inbox_id is real but the
        # ``remote_identity_pk`` / session keys haven't been filled in
        # yet. If C's first envelope races ahead of the relay ack,
        # downstream signature verification reads an empty pk and 403s.
        # Treat the row as not-yet-ready so the response surfaces as
        # 404 ``No instance found`` — the outbox retries 404 (see
        # ``_redeliver_envelope``), the ack lands on us in the meantime,
        # the next retry decodes cleanly.
        if not instance.remote_identity_pk:
            raise ValueError(
                f"No instance found for inbox_id={ctx.inbox_id!r}",
            )
        ctx.instance = instance

    return lookup_instance


def make_lookup_instance_by_id(*, repo) -> InboundStep:
    """Step 2 (WebRTC): resolve RemoteInstance by instance_id.

    When the envelope arrives over a DataChannel the sender's
    ``instance_id`` is already known from the peer connection. This
    step fetches the full ``RemoteInstance`` row so later steps (sig
    verify, decrypt) have the keys they need.
    """

    async def lookup_instance_by_id(ctx: InboundContext) -> None:
        instance = await repo.get_instance(ctx.instance_id)
        if instance is None:
            raise ValueError(f"No instance found for instance_id={ctx.instance_id!r}")
        # See :func:`make_lookup_instance` — a provisional row (created
        # at ``request_via`` time, before the relay ack lands) has an
        # empty ``remote_identity_pk`` and would 403 at sig verify.
        # Mirror the 404 path so the outbox retry layer picks up.
        if not instance.remote_identity_pk:
            raise ValueError(
                f"No instance found for instance_id={ctx.instance_id!r}",
            )
        # Wrap in the same shape the HTTPS inbox lookup returns so the
        # remaining pipeline steps (sig verify, decrypt) work unchanged.
        ctx.instance = _InboxInstance(instance)

    return lookup_instance_by_id


def make_check_timestamp() -> InboundStep:
    """Step 3: reject when clock skew exceeds the transport's threshold.

    ±300 s on a live wire. A relay-carried envelope
    (:data:`TRANSPORT_GFS_RELAY`) gets
    :data:`RELAY_TIMESTAMP_SKEW_SECONDS` instead — see that constant for
    why, and for the replay-retention invariant that pays for it.
    """

    async def check_timestamp(ctx: InboundContext) -> None:
        timestamp_str = ctx.envelope["timestamp"]
        try:
            envelope_ts = datetime.fromisoformat(
                timestamp_str.replace("Z", "+00:00"),
            )
        except ValueError as exc:
            raise ValueError(f"Unparseable timestamp: {timestamp_str!r}") from exc
        max_skew = (
            RELAY_TIMESTAMP_SKEW_SECONDS
            if ctx.transport == TRANSPORT_GFS_RELAY
            else TIMESTAMP_SKEW_SECONDS
        )
        skew = abs((datetime.now(timezone.utc) - envelope_ts).total_seconds())
        if skew > max_skew:
            raise ValueError(f"Timestamp skew too large: {skew:.1f}s (max {max_skew}s)")

    return check_timestamp


def make_check_peer_class() -> InboundStep:
    """Step 2b: hold a space-scoped peer to the space vocabulary (§D2b).

    Runs straight after the instance lookup — the first point at which
    the sender's *class* is known — and before any crypto, so an
    off-vocabulary envelope costs a set lookup.

    A row whose
    :data:`~socialhome.domain.federation.InstanceSource.SPACE_SESSION`
    source says "we met through an invite link" may exchange the space's
    own federation events and nothing else; anything outside
    :data:`~socialhome.domain.federation.SPACE_SESSION_ALLOWED_EVENT_TYPES`
    is rejected with the pipeline's ordinary ``ValueError``.

    Note this is *independent* of the ban step, which only fires when the
    envelope carries a ``space_id`` — most of the traffic this gate
    refuses (DMs, presence, calls, the user roster) carries none and
    would sail past every other check in the chain.
    """

    async def check_peer_class(ctx: InboundContext) -> None:
        source = getattr(ctx.instance, "source", None)
        if source is not InstanceSource.SPACE_SESSION:
            return
        raw_type = ctx.envelope["event_type"]
        # ``parse_json`` already proved this is a known member.
        event_type = FederationEventType(raw_type)
        if event_type in SPACE_SESSION_ALLOWED_EVENT_TYPES:
            return
        # INFO, not WARNING: an older peer that still sends a type we now
        # refuse is a normal, expected rejection, and this surface is
        # reachable by anyone holding an invite link. Type + sender only
        # — never the payload, which is the sender's to keep.
        log.info(
            "inbound: refusing %r from space-scoped instance %s — not "
            "permitted for a household seated from an invite link",
            raw_type,
            getattr(ctx.instance, "from_instance", ""),
        )
        raise ValueError(
            f"Event type {raw_type!r} is not permitted from a space-scoped peer",
        )

    return check_peer_class


def make_verify_signature(*, encoder) -> InboundStep:
    """Step 4: suite-aware signature verification.

    Reads ``sig_suite`` + the ``signatures`` map from the envelope.
    Every algorithm named in the suite must have a matching entry in
    the map and the matching public key on
    :class:`RemoteInstance`. Verification is AND across every
    algorithm — a hybrid envelope whose PQ signature fails is rejected
    even if the classical signature would pass.
    """

    async def verify_signature(ctx: InboundContext) -> None:
        data = ctx.envelope
        # §24.11 #1 / #14: bind ``from_instance`` to the verified signer.
        # Without this, peer A whose Ed25519 key signs the envelope can
        # claim ``from_instance: B`` and downstream the ban check, replay
        # cache, and event dispatch would consume the unauthenticated
        # claim. We compare *before* signature verification so peers that
        # mis-route an envelope get a clean rejection without burning
        # crypto cycles.
        signer_id = ctx.instance.from_instance
        claimed_from = data["from_instance"]
        if claimed_from != signer_id:
            raise ValueError("Invalid envelope signature")
        remote_pk = bytes.fromhex(ctx.instance.remote_identity_pk)
        # Reconstruct the signed bytes (envelope without the signatures map).
        # CANONICAL FIELD ORDER — must stay byte-identical to the order in
        # FederationService.send_event and resign_for_redelivery (the signer
        # side), or signatures fail to verify. Changing a field here means
        # changing it there too.
        envelope_for_verify = {
            "msg_id": data["msg_id"],
            "event_type": data["event_type"],
            "from_instance": data["from_instance"],
            "to_instance": data["to_instance"],
            "timestamp": data["timestamp"],
            "encrypted_payload": data["encrypted_payload"],
            "space_id": data.get("space_id"),
            "proto_version": data.get("proto_version", 1),
            "sig_suite": data["sig_suite"],
        }
        envelope_bytes = orjson.dumps(envelope_for_verify)
        remote_pq_pk_hex = getattr(ctx.instance, "remote_pq_identity_pk", None)
        pq_pk = bytes.fromhex(remote_pq_pk_hex) if remote_pq_pk_hex else None
        if not encoder.verify_signatures_all(
            envelope_bytes,
            suite=data["sig_suite"],
            signatures=data["signatures"],
            ed_public_key=remote_pk,
            pq_public_key=pq_pk,
        ):
            raise ValueError("Invalid envelope signature")

    return verify_signature


def make_check_replay(*, replay_cache) -> InboundStep:
    """Step 5: replay cache check (after sig verify to prevent DoS)."""

    async def check_replay(ctx: InboundContext) -> None:
        msg_id = ctx.envelope["msg_id"]
        from_instance = str(ctx.envelope.get("from_instance") or "")
        if replay_cache.seen(
            msg_id,
            from_instance=from_instance,
            now=datetime.now(timezone.utc),
        ):
            raise ValueError(
                f"Replay detected: msg_id={msg_id!r} from={from_instance!r}",
            )

    return check_replay


def make_decrypt_and_parse(*, key_manager, encoder, loads) -> InboundStep:
    """Steps 6+7: decrypt payload + parse inner JSON → FederationEvent."""

    async def decrypt_and_parse(ctx: InboundContext) -> None:
        data = ctx.envelope
        try:
            session_key = key_manager.decrypt(ctx.instance.key_remote_to_self)
        except Exception as exc:
            raise ValueError(f"Failed to decrypt session key: {exc}") from exc

        try:
            decrypted_json = encoder.decrypt_payload(
                data["encrypted_payload"],
                session_key,
            )
        except Exception as exc:
            raise ValueError(f"Failed to decrypt payload: {exc}") from exc

        try:
            inner = loads(decrypted_json)
        except Exception as exc:
            raise ValueError(f"Decrypted payload is not valid JSON: {exc}") from exc

        ctx.event = FederationEvent(
            msg_id=data["msg_id"],
            event_type=FederationEventType(data["event_type"]),
            from_instance=data["from_instance"],
            to_instance=data["to_instance"],
            timestamp=data["timestamp"],
            payload=inner,
            space_id=data.get("space_id"),
            epoch=data.get("epoch"),
        )

    return decrypt_and_parse


def make_idempotency_check(*, cache_holder) -> InboundStep:
    """Step 8: optional idempotency_key de-dup.

    ``cache_holder`` is a callable returning the ``IdempotencyCache``
    or ``None`` (lazily resolved because it's attached after startup).
    """

    async def idempotency_check(ctx: InboundContext) -> None:
        cache = cache_holder()
        if cache is None or ctx.event is None:
            return
        inner = ctx.event.payload
        ik = inner.get("idempotency_key") if isinstance(inner, dict) else None
        if not isinstance(ik, str) or not ik:
            return
        key = (ctx.event.event_type.value, ctx.event.from_instance, ik)
        if not cache.check_and_mark(key):
            log.debug(
                "inbound: dropped idempotent duplicate event_type=%s key=%s",
                ctx.event.event_type.value,
                ik,
            )
            ctx.early_response = {"status": "ok", "deduped": True}

    return idempotency_check


def make_ban_check(*, federation_repo) -> InboundStep:
    """Step 9: reject space-scoped events from banned instances."""

    async def ban_check(ctx: InboundContext) -> None:
        space_id = ctx.envelope.get("space_id")
        if space_id is None:
            return
        from_instance = ctx.envelope["from_instance"]
        banned = await federation_repo.is_instance_banned_from_space(
            space_id,
            from_instance,
        )
        if banned:
            raise ValueError(
                f"Instance {from_instance!r} is banned from space {space_id!r}"
            )

    return ban_check


def make_persist_replay(*, federation_repo) -> InboundStep:
    """Step 10: insert msg_id into replay table."""

    async def persist_replay(ctx: InboundContext) -> None:
        await federation_repo.insert_replay_id(ctx.envelope["msg_id"])

    return persist_replay


#: Per-event-type author field. The receiver-side deprovisioned-author
#: filter reads this to find the originating user inside the decrypted
#: payload and drop the envelope when that user has been marked
#: ``deprovisioned_at`` in ``remote_users`` (i.e. an admin on the
#: sender's side hid them via the per-pair user-visibility toggle and
#: a ``USER_REMOVED`` already landed here).
#:
#: Sender-side gates suppress most direct-delivery envelopes before
#: they leave the source — this map is the receiver-side backstop for
#: envelopes that arrive via mesh relay (``MomentFederationOutbound``
#: 3-hop relay) where the relaying instance has no visibility into
#: the originator's per-pair hide policy.
_AUTHOR_FIELD_FOR_INBOUND: dict[str, str] = {
    # NB: ``user_updated`` is deliberately NOT in this map. It is the
    # *un-hide signal* — the sender re-publishing a profile clears
    # ``deprovisioned_at`` on our side via ``upsert_remote``. Filtering
    # it would lock the user in the deprovisioned state forever and
    # break the visibility-toggle round-trip.
    "user_status_updated": "user_id",
    "user_online": "user_id",
    "user_idle": "user_id",
    "user_offline": "user_id",
    "dm_message": "sender_user_id",
    "dm_message_deleted": "sender_user_id",
    "dm_message_reaction": "reactor_user_id",
    "dm_media_blob": "sender_user_id",
    "dm_user_typing": "user_id",
    "highlight_created": "author_user_id",
    "highlight_frame_appended": "author_user_id",
    "highlight_deleted": "author_user_id",
    "highlight_frame_deleted": "author_user_id",
    "highlight_frame_viewed": "viewer_user_id",
    "highlight_frame_reacted": "reactor_user_id",
    "highlight_frame_reaction_removed": "reactor_user_id",
    "moment_created": "author_user_id",
    "moment_deleted": "author_user_id",
    "moment_reacted": "reactor_user_id",
    "moment_reaction_removed": "reactor_user_id",
}


def make_check_deprovisioned_author(*, user_repo) -> InboundStep:
    """Step 11: drop user-scoped events whose author is locally deprovisioned.

    Receiver-side enforcement of the per-pair user-visibility toggle.
    When an admin on the sender's side hides a local user from us,
    ``USER_REMOVED`` fires → we mark them as ``deprovisioned_at`` in
    ``remote_users``. Subsequent user-scoped envelopes from that user
    — whether sent directly OR relayed through a third household whose
    relay logic can't see the sender's hide policy — get dropped here
    before the dispatch handler sees them.

    Skips events that are space-scoped, routing-only, or whose author
    field isn't in :data:`_AUTHOR_FIELD_FOR_INBOUND`. Skips when the
    user has no row in ``remote_users`` (a stranger / new contact —
    handled by the upsert path on ``USER_UPDATED``) or has a row
    without ``deprovisioned_at`` set (still active).
    """

    async def check_deprovisioned_author(ctx: InboundContext) -> None:
        event = ctx.event
        if event is None:
            return
        author_field = _AUTHOR_FIELD_FOR_INBOUND.get(event.event_type.value)
        if author_field is None:
            return
        payload = event.payload if isinstance(event.payload, dict) else None
        if payload is None:
            return
        author_user_id = str(payload.get(author_field) or "")
        if not author_user_id:
            return
        try:
            user = await user_repo.get_remote(author_user_id)
        except Exception as exc:  # pragma: no cover — defensive
            # Fail-soft: a transient infra error in the visibility
            # lookup must not block legitimate inbound traffic. Log
            # and pass through.
            log.debug(
                "inbound: deprovisioned-author lookup failed for %s: %s",
                author_user_id,
                exc,
            )
            return
        deprovisioned_at = getattr(user, "deprovisioned_at", None) if user else None
        if deprovisioned_at is not None:
            log.debug(
                "inbound: dropped %s from deprovisioned remote user %s",
                event.event_type.value,
                author_user_id,
            )
            ctx.early_response = {
                "status": "ok",
                "dropped": "deprovisioned-author",
            }

    return check_deprovisioned_author


#: Space-content **write** events and the payload field naming the user who
#: made the write. A household seated as a ``subscriber`` (a Follower that
#: redeemed a role-carrying invite link) is a reader: every one of these is
#: refused on the host regardless of what the sending household believes its
#: own member's role to be. Keyed by event-type *value*, same shape as
#: :data:`_AUTHOR_FIELD_FOR_INBOUND`, with the action name
#: ``SpaceFeatures`` opts in by (``comment`` can be allowed for subscribers
#: via ``allow_subscriber_comment``; ``post`` never can).
#:
#: Only the two CREATE events are listed, because they are the only
#: space-content envelopes that name their author: ``*_updated`` /
#: ``*_deleted`` carry an id and nothing else, and reactions on a space post
#: do not federate at all (they ride the local row and the §25.6 sync
#: snapshot). A follower has no content on the host to edit or delete, so
#: there is nothing for those events to refer to anyway.
_SPACE_WRITE_FOR_INBOUND: dict[str, tuple[str, str]] = {
    "space_post_created": ("author", "post"),
    "space_comment_created": ("author", "comment"),
}


def make_check_space_writer(
    *,
    space_repo,
    remote_member_repo,
    own_instance_id: str,
) -> InboundStep:
    """Step 12: refuse a space write from a household we seated as a reader.

    A ``subscriber`` seat in ``space_remote_members`` (migration 0054) is
    the on-disk fact that a household redeemed a **Follower** invite link.
    Such a household is a full participant on the transport — it sits in
    ``space_instances``, so ``broadcast_to_space_members`` delivers the
    content stream and the epoch content key reaches it — and it must not
    be able to write back.

    Enforced **here**, on the host, rather than trusted to the sender.
    The follower's own household already refuses the write locally
    (``_assert_writable_member`` on its ``space_members`` row), but that
    is its own copy of the rule; a patched or hostile household holds a
    valid content key and could produce a perfectly well-formed,
    correctly-signed ``SPACE_POST_CREATED``. The seat the HOST decided at
    redeem time is the only authority that matters, so it is the one read
    here.

    Scope, deliberately narrow:

    * Only for spaces **we host** (``owner_instance_id == own_instance_id``).
      A member household holds a mirror of the roster, not the authority
      over it; the host is the single place the decision is made, and it is
      the host that fans content out.
    * Only when the sender actually HAS a row with ``role='subscriber'``.
      No row means "not in our mirror", which is the pre-existing state
      for plenty of legitimate senders (a roster that has not converged
      yet) — inventing a rejection there would be a behaviour change well
      beyond a Follower seat, and one that fails in the direction of
      losing real content.
    * ``allow_subscriber_comment`` still governs. It is the same admin
      opt-in that decides what a LOCAL subscriber may do, and a remote
      follower is the same kind of seat; splitting the two would mean a
      space could invite followers it then treats differently depending on
      which household they sit on. (Its sibling
      ``allow_subscriber_react`` has no inbound surface: space reactions
      are not federated events.)

    Fail-soft on an infrastructure error (a lookup that raises) for the
    same reason :func:`make_check_deprovisioned_author` does: a transient
    DB hiccup must not start dropping legitimate space content. The seat
    itself is durable, so the next envelope is gated again.
    """

    async def check_space_writer(ctx: InboundContext) -> None:
        event = ctx.event
        if event is None:
            return
        entry = _SPACE_WRITE_FOR_INBOUND.get(event.event_type.value)
        if entry is None:
            return
        author_field, action = entry
        payload = event.payload if isinstance(event.payload, dict) else None
        if payload is None:
            return
        space_id = event.space_id or str(payload.get("space_id") or "")
        author_user_id = str(payload.get(author_field) or "")
        if not space_id or not author_user_id:
            return
        try:
            space = await space_repo.get(space_id)
            if space is None or space.owner_instance_id != own_instance_id:
                return
            seat = await remote_member_repo.get(
                space_id,
                event.from_instance,
                author_user_id,
            )
        except Exception as exc:  # pragma: no cover — defensive
            log.debug(
                "inbound: space-writer lookup failed for %s in %s: %s",
                author_user_id,
                space_id,
                exc,
            )
            return
        if seat is None or seat.role != SpaceRole.SUBSCRIBER.value:
            return
        features = space.features
        if action == "comment" and features.allow_subscriber_comment:
            return
        log.warning(
            "inbound: refused %s in space %s from %s@%s — that household "
            "holds a read-only Follower seat here",
            event.event_type.value,
            space_id,
            author_user_id,
            event.from_instance,
        )
        ctx.early_response = {"status": "ok", "dropped": "subscriber-write"}

    return check_space_writer


# ─── Pipeline runner ─────────────────────────────────────────────────────


class InboundPipeline:
    """Compose :class:`InboundStep` callables into a linear pipeline.

    The runner calls each step in order. If any step raises
    ``ValueError`` the pipeline aborts immediately (the caller converts
    the error into an HTTP 400/403). If a step sets
    ``ctx.early_response`` the remaining steps are skipped.
    """

    __slots__ = ("_steps",)

    def __init__(self, steps: list[InboundStep]) -> None:
        self._steps = list(steps)

    async def run(self, ctx: InboundContext) -> dict:
        """Execute every step. Returns ``{"status": "ok"}`` or the
        early-response dict set by a step."""
        for step in self._steps:
            await step(ctx)
            if ctx.early_response is not None:
                return ctx.early_response
        return {"status": "ok"}
