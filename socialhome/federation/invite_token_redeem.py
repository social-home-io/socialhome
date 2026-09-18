"""Cross-instance redeem of a space invite token (§D2).

When a household pastes an invite code minted on a different household,
the SPA POSTs ``/api/spaces/join`` with ``{token, issuer_instance_id}``.
The receiving instance can't validate the token locally — the token row
lives in the *issuer's* ``space_invite_tokens`` table. This coordinator
runs the cross-instance handshake:

1. **Receiver → issuer:** :data:`SPACE_INVITE_TOKEN_REDEEM`
   carrying the token + the redeemer's identity (``user_id``,
   ``public_key``, ``display_name``) + a ``redeem_nonce`` that keys the
   in-flight Future on the receiver side.
2. **Issuer:** atomically consumes the token, seats the redeemer as a
   :class:`SpaceRemoteMember`, registers the receiver's
   ``space_instance``, and ships :data:`SPACE_INVITE_TOKEN_REDEEM_ACK`
   back with ``{redeem_nonce, space_id, role}``.
3. **Receiver:** resolves the Future on the ACK — the route handler
   wakes from its ``await``, persists ``add_space_instance(space_id,
   issuer_instance_id)`` locally, and returns ``{space_id, role}`` to
   the SPA.

On any issuer-side failure (token unknown / expired / exhausted, ban,
crypto error), the issuer sends
:data:`SPACE_INVITE_TOKEN_REDEEM_DENY` with a human-readable
``reason``; the receiver's Future raises
:class:`SpacePermissionError` carrying that reason. On no response
within ``REDEEM_TIMEOUT_SECONDS``, the receiver's Future raises
``TimeoutError`` and the route maps that to HTTP 504.

PR 2 layers federation-mesh routing on top: when the issuer is *not*
a direct CONFIRMED peer, the coordinator runs route discovery (via
:class:`RouteDiscoveryService`) and ships the REDEEM wrapped in
:data:`FederationEventType.SPACE_ROUTED` along the discovered chain.
The issuer's ACK / DENY follows the reverse path automatically —
``_on_redeem`` reads ``event.routed_path`` and ships the response
through the same :class:`SpaceRoutedHandler` when it was set.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..domain.federation import FederationEventType, PairingStatus
from ..domain.federation_capabilities import FederationCapability
from ..domain.space import SpaceMember, SpacePermissionError, SpaceRole
from ..services.space_service import (
    _coerce_min_age,
    apply_space_content_key_from_metadata,
    apply_space_cover_from_metadata,
    apply_space_icon_from_metadata,
    build_space_snapshot_for_federation,
    can_seat_remote_stub,
    stub_space_from_metadata,
)

if TYPE_CHECKING:
    from ..domain.federation import FederationEvent
    from ..infrastructure.event_bus import EventBus
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.space_cover_repo import AbstractSpaceCoverRepo
    from ..repositories.space_remote_member_repo import (
        AbstractSpaceRemoteMemberRepo,
    )
    from ..repositories.space_repo import AbstractSpaceRepo
    from ..repositories.user_repo import AbstractUserRepo
    from .federation_service import FederationService
    from .route_discovery import RouteDiscoveryService
    from .routed_envelope import SpaceRoutedHandler

log = logging.getLogger(__name__)


#: How long the receiver waits for an ACK / DENY before giving up.
#: Calibrated for a single hop over a healthy WebRTC DataChannel.
REDEEM_TIMEOUT_SECONDS: float = 10.0


class SpaceInviteTokenRedeemCoordinator:
    """Coordinator for the cross-instance ``SPACE_INVITE_TOKEN_REDEEM``
    round-trip.

    The same instance plays both roles in a deployment — receiver when a
    local user pastes a peer's token, issuer when a peer redeems one of
    our tokens. The coordinator hosts both inbound handlers and the
    outbound ``request_redeem`` driver.
    """

    __slots__ = (
        "_bus",
        "_federation",
        "_spaces",
        "_remote_members",
        "_users",
        "_federation_repo",
        "_cover_repo",
        "_icon_repo",
        "_space_crypto",
        "_child_protection",
        "_pending",
        "_timeout",
        "_route_service",
        "_routed_handler",
    )

    def __init__(
        self,
        *,
        bus: "EventBus",
        federation_service: "FederationService",
        space_repo: "AbstractSpaceRepo",
        space_remote_member_repo: "AbstractSpaceRemoteMemberRepo",
        user_repo: "AbstractUserRepo",
        federation_repo: "AbstractFederationRepo",
        timeout: float = REDEEM_TIMEOUT_SECONDS,
        route_service: "RouteDiscoveryService | None" = None,
        routed_handler: "SpaceRoutedHandler | None" = None,
        cover_repo: "AbstractSpaceCoverRepo | None" = None,
        icon_repo=None,
        space_crypto_service=None,
        child_protection_service=None,
    ) -> None:
        self._bus = bus
        self._federation = federation_service
        self._spaces = space_repo
        self._remote_members = space_remote_member_repo
        self._users = user_repo
        self._federation_repo = federation_repo
        #: Optional — when wired, the issuer ships the host's WebP
        #: cover bytes alongside ``cover_hash`` in the ACK so the
        #: receiver's local stub doesn't fall back to the gradient
        #: placeholder (§D1b #116).
        self._cover_repo = cover_repo
        self._icon_repo = icon_repo
        #: Optional — when wired, the issuer ships the current
        #: epoch's space content key in the ACK and the receiver
        #: imports it into its local space_keys (#117).
        self._space_crypto = space_crypto_service
        #: Optional — when wired, §CP.F1 age-gates the redeemer before
        #: seating them locally, so a protected minor can't redeem an
        #: invite link into a remote-hosted age-restricted space. The
        #: host's ``min_age`` rides the ACK's ``space_meta``.
        self._child_protection = child_protection_service
        #: ``redeem_nonce`` → in-flight Future awaiting the ACK / DENY.
        self._pending: dict[str, asyncio.Future[dict]] = {}
        self._timeout = timeout
        #: Mesh-routing pair. Optional so legacy tests that exercise
        #: only the direct-pair path can construct the coordinator
        #: without the routing layer. When both are present, REDEEM
        #: requests against unpaired issuers run discovery + wrap
        #: rather than fail-fast with "pair first".
        self._route_service = route_service
        self._routed_handler = routed_handler

    def attach_to(self, federation_service: "FederationService") -> None:
        """Wire the three inbound event-type handlers into the registry."""
        registry = federation_service._event_registry  # noqa: SLF001
        registry.register(
            FederationEventType.SPACE_INVITE_TOKEN_REDEEM,
            self._on_redeem,
        )
        registry.register(
            FederationEventType.SPACE_INVITE_TOKEN_REDEEM_ACK,
            self._on_redeem_ack,
        )
        registry.register(
            FederationEventType.SPACE_INVITE_TOKEN_REDEEM_DENY,
            self._on_redeem_deny,
        )

    # ── Sender side ────────────────────────────────────────────────────

    async def request_redeem(
        self,
        token: str,
        *,
        viewer_user_id: str,
        issuer_instance_id: str,
    ) -> dict:
        """Drive the receiver-side handshake.

        If the issuer is a CONFIRMED direct peer, ships REDEEM via
        the regular ``send_event`` path. Otherwise tries
        :class:`RouteDiscoveryService` to find a chain of confirmed
        peers leading to the issuer, then wraps REDEEM in
        :data:`FederationEventType.SPACE_ROUTED` and ships along
        that path. The ACK / DENY arrives via the reverse path and
        resolves the same nonce-keyed Future.

        Returns ``{space_id, role}`` on ACK. Raises
        :class:`SpacePermissionError` on DENY (or "no route to issuer"
        when discovery fails), ``TimeoutError`` on no response within
        ``self._timeout``.
        """
        instance = await self._federation_repo.get_instance(issuer_instance_id)
        direct_peer = (
            instance is not None and instance.status is PairingStatus.CONFIRMED
        )

        # Look up the local user so we can ship their identity to the
        # issuer; the issuer needs a public_key + display_name to seat
        # us as a SpaceRemoteMember on its side.
        user = await self._users.get_by_user_id(viewer_user_id)
        if user is None:
            # Surfaces as 403 — a missing local user record on a
            # supposedly-authenticated request is a permission-shape
            # error, not a client validation problem.
            raise SpacePermissionError("local user not found")

        # The mesh-routing path is opt-in (the bootstrap injects it);
        # without it, fall back to direct-only semantics.
        mesh_available = (
            self._route_service is not None and self._routed_handler is not None
        )

        if not direct_peer and not mesh_available:
            raise SpacePermissionError(
                "issuer instance is not a confirmed peer — pair first",
            )

        # Direct-peer fast-path: we already have an envelope route to
        # the issuer and can gate on their announced proto_version.
        # Pre-v_6 issuers don't know SPACE_INVITE_TOKEN_REDEEM. Don't
        # ship the envelope into a 10 s timeout — fail fast with a
        # message naming the right next step.
        if direct_peer:
            if not await self._federation.peer_supports(
                issuer_instance_id,
                min_version=FederationCapability.MIN_FOR_SPACE_INVITE_REDEEM,
            ):
                raise SpacePermissionError(
                    "issuer instance is on an older protocol version — "
                    "ask them to upgrade before redeeming this code",
                )

        route_path: list[str] | None = None
        target_eph_pk: str | None = None
        if not direct_peer:
            # Run mesh discovery. The discovery layer already gates
            # candidate hops on v_6 so we don't need to re-check
            # ``peer_supports`` here. The discovery returns the
            # target's ephemeral X25519 pub alongside the path — that
            # pub is what ``send_routed`` seals the inner payload
            # against, so relays never see the plaintext.
            assert self._route_service is not None
            discovery_result = await self._route_service.discover_route(
                issuer_instance_id,
            )
            if discovery_result is None:
                raise SpacePermissionError(
                    "no route to issuer — pair with them, or with one of"
                    " their household's peers",
                )
            route_path, target_eph_pk = discovery_result
            if len(route_path) < 2:
                raise SpacePermissionError(
                    "no route to issuer — pair with them, or with one of"
                    " their household's peers",
                )

        nonce = uuid.uuid4().hex
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[dict] = loop.create_future()
        self._pending[nonce] = fut
        payload = {
            "redeem_nonce": nonce,
            "invite_token": token,
            "redeemer_user_id": viewer_user_id,
            "redeemer_display_name": (user.display_name or user.username),
            "redeemer_public_key": getattr(user, "public_key", None),
        }
        try:
            if direct_peer:
                await self._federation.send_event(
                    to_instance_id=issuer_instance_id,
                    event_type=FederationEventType.SPACE_INVITE_TOKEN_REDEEM,
                    payload=payload,
                )
            else:
                assert self._routed_handler is not None
                assert self._route_service is not None
                assert route_path is not None
                assert target_eph_pk is not None
                # Pin the issuer identity pk discovery verified, so the
                # origin holds a SPACE_ROUTE_STALE nack for this send
                # against the key WE checked (same shape as
                # ``FederationService.send_with_mesh_fallback``).
                pinned_pk = (
                    self._route_service.cached_target_identity_pk(issuer_instance_id)
                    or ""
                )
                await self._routed_handler.send_routed(
                    path=route_path,
                    target_eph_pk_b64=target_eph_pk,
                    inner_event_type=(FederationEventType.SPACE_INVITE_TOKEN_REDEEM),
                    inner_payload=payload,
                    target_identity_pk=pinned_pk,
                )
            try:
                result = await asyncio.wait_for(fut, timeout=self._timeout)
            except asyncio.TimeoutError as exc:
                raise TimeoutError(
                    "issuer did not respond to invite-token redeem",
                ) from exc
        finally:
            self._pending.pop(nonce, None)

        # Receiver-side post-condition: register the (space_id, issuer)
        # mapping locally so subsequent space-scoped fan-outs include
        # the issuer's household.
        space_id = str(result.get("space_id") or "")
        role_str = str(result.get("role") or SpaceRole.MEMBER.value)
        if space_id:
            meta = result.get("space_meta")
            meta_dict = meta if isinstance(meta, dict) else None
            # §CP.F1 — refuse to seat an under-age protected minor in a
            # remote-hosted age-restricted space. Checked BEFORE any local
            # persistence (no space_instance mapping, no stub, no content
            # key, no membership) so a blocked minor gains nothing — not
            # even the ability to decrypt fan-out. The host's min_age rides
            # the ACK's space_meta; an older issuer that ships none → 0 →
            # allowed (the historical behaviour). The host may have seated
            # us as a remote member on the valid token; without our
            # space_instance + content key that residue is inert (it can
            # only ever receive undecryptable ciphertext).
            # Coerce defensively: a malicious issuer could ship a
            # non-conforming min_age; _coerce_min_age clamps to {0,13,16,18}
            # (out-of-set / garbage → 0) so int() can't raise here.
            min_age = _coerce_min_age(meta_dict.get("min_age")) if meta_dict else 0
            if self._child_protection is not None and not (
                await self._child_protection.is_age_allowed(viewer_user_id, min_age)
            ):
                log.warning(
                    "CP.F1: refusing invite-link redeem seat for under-age "
                    "user=%s in space=%s (min_age=%d)",
                    viewer_user_id,
                    space_id,
                    min_age,
                )
                raise SpacePermissionError(
                    f"This space is restricted to users aged {min_age}+."
                )
            # §D1b anti-hijack — refuse if a local space with this id is
            # already owned by a DIFFERENT host. Otherwise a malicious
            # issuer could ship an ACK whose space_id collides with a space
            # we hold under another host and clobber its config + content
            # key. Compared against issuer_instance_id (the authenticated
            # sender), never meta["owner_instance_id"] (issuer-controlled).
            # Checked before any persistence so a conflict seats nothing.
            if not await can_seat_remote_stub(
                self._spaces, space_id, issuer_instance_id
            ):
                log.warning(
                    "§D1b: refusing redeem seat — space=%s already owned "
                    "locally by another host, issuer=%s",
                    space_id,
                    issuer_instance_id,
                )
                raise SpacePermissionError(
                    "This invite points at a space that conflicts with one "
                    "you already belong to under a different host."
                )
            await self._spaces.add_space_instance(
                space_id,
                issuer_instance_id,
            )
            # §D1b — seat a local stub + membership so the redeemer's
            # /api/spaces actually shows the joined space. The ACK
            # carries the host's metadata snapshot; older issuers that
            # don't ship it fall through to today's mapping-only
            # behaviour. SpaceMember is keyed on (space_id, user_id)
            # so a repeat redeem is a no-op via INSERT OR REPLACE.
            if meta_dict is not None:
                meta = meta_dict
                stub = stub_space_from_metadata(
                    space_id,
                    host_instance_id=issuer_instance_id,
                    meta=meta,
                )
                await self._spaces.save(stub)
                # §D1b cover bytes (#116) — persist host's WebP
                # when shipped inline so the stub renders properly.
                await apply_space_cover_from_metadata(
                    space_id,
                    meta=meta,
                    cover_repo=self._cover_repo,
                )
                await apply_space_icon_from_metadata(
                    space_id,
                    meta=meta,
                    icon_repo=self._icon_repo,
                )
                # §D1b space content key (#117) — persist the
                # receiver's local epoch key from the ACK so
                # subsequent SPACE_POST_CREATED decrypts succeed.
                await apply_space_content_key_from_metadata(
                    space_id,
                    meta=meta,
                    space_crypto_service=self._space_crypto,
                )
                await self._spaces.save_member(
                    SpaceMember(
                        space_id=space_id,
                        user_id=viewer_user_id,
                        role=role_str,
                        joined_at=datetime.now(timezone.utc).isoformat(),
                    )
                )
                # §D1b member-list mirror (#115) — seat every other
                # member of this space as a ``SpaceRemoteMember`` so
                # the Members tab on the receiver's stub shows the
                # full roster (the host, federated peers, …) rather
                # than just the redeemer. Skipping ``viewer_user_id``
                # because that lives in the local ``space_members``
                # row we just inserted.
                roster = meta.get("roster")
                if isinstance(roster, list):
                    for entry in roster:
                        if not isinstance(entry, dict):
                            continue
                        user_id = str(entry.get("user_id") or "")
                        inst_id = str(entry.get("instance_id") or "")
                        if not user_id or not inst_id or user_id == viewer_user_id:
                            continue
                        await self._remote_members.add(
                            space_id=space_id,
                            instance_id=inst_id,
                            user_id=user_id,
                            user_pk=(
                                str(entry["user_pk"]) if entry.get("user_pk") else None
                            ),
                            display_name=(
                                str(entry["display_name"])
                                if entry.get("display_name")
                                else None
                            ),
                        )
        return {
            "space_id": space_id,
            "role": role_str,
        }

    # ── Receiver side (the issuer in this exchange) ────────────────────

    async def _on_redeem(self, event: "FederationEvent") -> None:
        """Issuer-side: validate the token, seat the remote redeemer,
        send ACK. On any failure ship a DENY with a ``reason``.
        """
        p = event.payload
        nonce = str(p.get("redeem_nonce") or "")
        token = str(p.get("invite_token") or "")
        if not nonce or not token:
            log.debug(
                "SPACE_INVITE_TOKEN_REDEEM from %s missing nonce/token",
                event.from_instance,
            )
            return  # cannot DENY without a nonce to key the reply

        redeemer_user_id = str(p.get("redeemer_user_id") or "")
        redeemer_pk_raw = p.get("redeemer_public_key")
        redeemer_pk = str(redeemer_pk_raw) if redeemer_pk_raw else None
        redeemer_display_raw = p.get("redeemer_display_name")
        redeemer_display = str(redeemer_display_raw) if redeemer_display_raw else None

        # Routed redeem: when the inbound came via SPACE_ROUTED, the
        # synthesised event carries the forward-leg ``route_id``. The
        # ACK / DENY ships back via :meth:`send_routed_reply`, which
        # reuses the cached ephemeral keypair so the reply is sealed
        # under the target→origin key and travels the reverse of the
        # original path — a direct ship-back would fail (receiver
        # isn't a confirmed peer) and just leak a 10 s timeout to the
        # SPA.
        routed_route_id: str | None = None
        if (
            event.routed_path is not None
            and event.routed_route_id is not None
            and self._routed_handler is not None
        ):
            routed_route_id = event.routed_route_id

        if not redeemer_user_id:
            await self._send_deny(
                event.from_instance,
                nonce,
                "redeemer_user_id missing from redeem payload",
                routed_route_id=routed_route_id,
            )
            return

        try:
            row = await self._spaces.consume_invite_token(token)
        except Exception:
            log.exception(
                "SPACE_INVITE_TOKEN_REDEEM: consume_invite_token raised"
                " for token from %s",
                event.from_instance,
            )
            await self._send_deny(
                event.from_instance,
                nonce,
                "issuer storage error during token consume",
                routed_route_id=routed_route_id,
            )
            return

        if row is None:
            await self._send_deny(
                event.from_instance,
                nonce,
                "invite token invalid, expired, or exhausted",
                routed_route_id=routed_route_id,
            )
            return

        space_id = str(row.get("space_id") or "")
        if not space_id:
            await self._send_deny(
                event.from_instance,
                nonce,
                "invite token row missing space_id",
                routed_route_id=routed_route_id,
            )
            return

        # §13.7 — a ban on the issuer side overrides a valid token.
        try:
            banned = await self._spaces.is_banned(space_id, redeemer_user_id)
        except Exception:
            log.exception(
                "SPACE_INVITE_TOKEN_REDEEM: is_banned raised for"
                " space_id=%s user_id=%s",
                space_id,
                redeemer_user_id,
            )
            await self._send_deny(
                event.from_instance,
                nonce,
                "issuer storage error during ban check",
                routed_route_id=routed_route_id,
            )
            return
        if banned:
            await self._send_deny(
                event.from_instance,
                nonce,
                "banned from this space",
                routed_route_id=routed_route_id,
            )
            return

        # Seat the remote redeemer + register their instance so the
        # issuer's outbound fan-outs reach them.
        try:
            await self._remote_members.add(
                space_id=space_id,
                instance_id=event.from_instance,
                user_id=redeemer_user_id,
                user_pk=redeemer_pk,
                display_name=redeemer_display,
            )
            await self._spaces.add_space_instance(
                space_id,
                event.from_instance,
            )
        except Exception:
            log.exception(
                "SPACE_INVITE_TOKEN_REDEEM: seating remote member failed"
                " for space_id=%s instance=%s user_id=%s",
                space_id,
                event.from_instance,
                redeemer_user_id,
            )
            await self._send_deny(
                event.from_instance,
                nonce,
                "issuer storage error during member seat",
                routed_route_id=routed_route_id,
            )
            return

        # Pull the full space row so we can ship metadata + the
        # member roster back to the receiver. Without the meta the
        # receiver's stub card is blank; without the roster the
        # receiver's Members tab shows only herself (see PR for
        # #115).
        space = await self._spaces.get(space_id)
        ack_payload: dict = {
            "redeem_nonce": nonce,
            "space_id": space_id,
            "role": SpaceRole.MEMBER.value,
        }
        if space is not None:
            ack_payload["space_meta"] = await build_space_snapshot_for_federation(
                space,
                space_repo=self._spaces,
                remote_member_repo=self._remote_members,
                user_repo=self._users,
                own_instance_id=self._federation.own_instance_id,
                cover_repo=self._cover_repo,
                icon_repo=self._icon_repo,
                space_crypto_service=self._space_crypto,
            )
        if routed_route_id is not None and self._routed_handler is not None:
            await self._routed_handler.send_routed_reply(
                route_id=routed_route_id,
                inner_event_type=(FederationEventType.SPACE_INVITE_TOKEN_REDEEM_ACK),
                inner_payload=ack_payload,
            )
        else:
            await self._federation.send_event(
                to_instance_id=event.from_instance,
                event_type=FederationEventType.SPACE_INVITE_TOKEN_REDEEM_ACK,
                payload=ack_payload,
            )

    async def _on_redeem_ack(self, event: "FederationEvent") -> None:
        """Receiver-side: resolve the in-flight Future with the issuer's
        payload. No-op if the nonce isn't ours (late ACK after timeout).
        """
        p = event.payload
        nonce = str(p.get("redeem_nonce") or "")
        if not nonce:
            return
        fut = self._pending.get(nonce)
        if fut is None or fut.done():
            return
        fut.set_result(
            {
                "space_id": str(p.get("space_id") or ""),
                "role": str(p.get("role") or SpaceRole.MEMBER.value),
                # Forward the host's snapshot so request_redeem can seat the
                # local stub + membership + roster. Dropping it here was why
                # a cross-household invite-link redeem never surfaced the
                # space on the redeemer's side (the seating block keyed on
                # result["space_meta"] which was always absent).
                "space_meta": p.get("space_meta"),
            }
        )

    async def _on_redeem_deny(self, event: "FederationEvent") -> None:
        """Receiver-side: resolve the in-flight Future with a
        :class:`SpacePermissionError` carrying the issuer's reason.
        """
        p = event.payload
        nonce = str(p.get("redeem_nonce") or "")
        if not nonce:
            return
        fut = self._pending.get(nonce)
        if fut is None or fut.done():
            return
        reason = str(p.get("reason") or "invite redeem denied by issuer")
        fut.set_exception(SpacePermissionError(reason))

    # ── Internal helpers ───────────────────────────────────────────────

    async def _send_deny(
        self,
        to_instance_id: str,
        nonce: str,
        reason: str,
        *,
        routed_route_id: str | None = None,
    ) -> None:
        """Best-effort DENY ship-back. Logged but never raised — a
        failed DENY just leaves the receiver hanging until its timeout,
        which is the same outcome as the network dropping the frame.

        ``routed_route_id``, when set, ships the DENY back via
        :meth:`SpaceRoutedHandler.send_routed_reply` — required when
        the inbound came via SPACE_ROUTED because the receiver isn't
        a direct peer. The reply leg reuses the forward leg's
        ephemeral keypair so relays still see only ciphertext.
        """
        deny_payload = {
            "redeem_nonce": nonce,
            "reason": reason,
        }
        try:
            if routed_route_id is not None and self._routed_handler is not None:
                await self._routed_handler.send_routed_reply(
                    route_id=routed_route_id,
                    inner_event_type=(
                        FederationEventType.SPACE_INVITE_TOKEN_REDEEM_DENY
                    ),
                    inner_payload=deny_payload,
                )
            else:
                await self._federation.send_event(
                    to_instance_id=to_instance_id,
                    event_type=(FederationEventType.SPACE_INVITE_TOKEN_REDEEM_DENY),
                    payload=deny_payload,
                )
        except Exception:
            log.exception(
                "SPACE_INVITE_TOKEN_REDEEM_DENY ship-back to %s failed",
                to_instance_id,
            )
