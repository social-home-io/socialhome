"""Inbound federation validation pipeline (§24.11) — middleware chain.

The §24.11 pipeline validates every inbound federation webhook before the
payload reaches business logic. Each step is a standalone async callable
(a *middleware*) that receives the validation context and either passes
or raises ``ValueError`` to reject.

Steps (in order):

1. **JSON parse** — ``raw_body`` → envelope dict.
2. **Instance lookup** — ``local_webhook_id`` → ``RemoteInstance``.
3. **Timestamp skew** — ``abs(now - envelope.timestamp) ≤ 300s``.
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
* **Reuse** — the same chain validates both HTTPS-webhook and
  DataChannel-delivered envelopes.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import orjson

from ..domain.federation import FederationEvent, FederationEventType, RemoteInstance

log = logging.getLogger(__name__)

#: Maximum allowed clock skew for inbound envelopes (§24.11 §5).
TIMESTAMP_SKEW_SECONDS = 300


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

    #: Webhook identifier from the URL path (HTTPS webhook transport).
    webhook_id: str = ""

    #: Instance identifier (WebRTC transport — already known from the
    #: DataChannel connection). When set, the lookup step uses this
    #: instead of ``webhook_id``.
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


#: Middleware shape: async callable that takes context + raises or returns.
InboundStep = Callable[[InboundContext], Awaitable[None]]


class _WebhookInstance:
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
    """Step 2 (webhook): resolve RemoteInstance by webhook_id."""

    async def lookup_instance(ctx: InboundContext) -> None:
        instance = await lookup_fn(repo, ctx.webhook_id)
        if instance is None:
            raise ValueError(f"No instance found for webhook_id={ctx.webhook_id!r}")
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
        # Wrap in the same shape the webhook lookup returns so the
        # remaining pipeline steps (sig verify, decrypt) work unchanged.
        ctx.instance = _WebhookInstance(instance)

    return lookup_instance_by_id


def make_check_timestamp() -> InboundStep:
    """Step 3: reject when clock skew exceeds threshold."""

    async def check_timestamp(ctx: InboundContext) -> None:
        timestamp_str = ctx.envelope["timestamp"]
        try:
            envelope_ts = datetime.fromisoformat(
                timestamp_str.replace("Z", "+00:00"),
            )
        except ValueError as exc:
            raise ValueError(f"Unparseable timestamp: {timestamp_str!r}") from exc
        skew = abs((datetime.now(timezone.utc) - envelope_ts).total_seconds())
        if skew > TIMESTAMP_SKEW_SECONDS:
            raise ValueError(
                f"Timestamp skew too large: {skew:.1f}s (max {TIMESTAMP_SKEW_SECONDS}s)"
            )

    return check_timestamp


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
        remote_pk = bytes.fromhex(ctx.instance.remote_identity_pk)
        # Reconstruct the signed bytes (envelope without the signatures map).
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
