"""Ask to join a global space through the GFS (v_84).

A global space is listed in a GFS directory with its owner's instance id
(``owning_instance``) and nothing that reaches it: the GFS shields
households from each other. A household connected to the same GFS but not
paired with the host used to see the space and have no way to ask in — the
§D2 ``SPACE_JOIN_REQUEST`` needs a confirmed pairing, and the §D2b sealed
relay leg only ever carried an invite-link redeem.

This module carries the "Ask to join" over that same sealed leg:

1. **Applicant** — gets the host's identity + key-wrap keys from a GFS that
   lists the space (``POST /gfs/spaces/{id}/contact``, signed, only for
   households registered there), binds them to the listed instance id end
   to end, keeps its own pending request (the id it picks, as the paired
   path does) and seals ``SPACE_JOIN_REQUEST_BOOTSTRAP`` (person, name,
   message) to the host over ``POST /gfs/envelope``. The relay routes on
   the recipient id only and can read none of it; it queues the blob while
   the host is offline.
2. **Host** — the body opens and verifies like every bootstrap body
   (signature, ``derive_instance_id`` anti-tamper, timestamp, replay on the
   nonce, per-sender rate limit). A live global space it hosts that takes
   requests files an ordinary pending request under the applicant's id —
   the admins' queue, their bell — keeping the applicant's relay contact for
   the answer (migration 0143). A banned person, a repeat or a space that
   takes no requests is dropped without a word, exactly like the paired
   path.
3. **Answer** — the admins approve or decline as for any remote request.
   :meth:`GfsJoinRequestCoordinator.send_answer` seals
   ``SPACE_JOIN_REQUEST_BOOTSTRAP_ANSWER`` back: an approval carries a
   single-use invite token, which the applicant redeems over the §D2b
   bootstrap leg with the host keys the answer itself carries (bound again
   before the seal). The host's ``SpaceInviteTokenRedeemCoordinator`` seats
   it as a ``space_session`` member — the same seat, content and rights as
   any member reached through the relay.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import orjson

from ..domain.events import RemoteJoinRequestApproved, RemoteJoinRequestDenied
from ..domain.federation_capabilities import OURS
from ..domain.space import HostNotReachableError, JoinMode, SpaceType
from ..services.local_community import (
    file_remote_join_request,
    pending_join_request_id,
)
from .invite_bootstrap import (
    KIND_JOIN_REQUEST,
    KIND_JOIN_REQUEST_ANSWER,
    InviteBootstrapHint,
    RelayEnvelopeSender,
    seal_bootstrap_envelope,
    validate_bootstrap_body,
    verify_peer_keywrap,
)

if TYPE_CHECKING:
    from ..infrastructure.event_bus import EventBus
    from ..rate_limiter import RateLimiter
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.public_space_repo import AbstractPublicSpaceRepo
    from ..repositories.space_repo import AbstractSpaceRepo
    from ..repositories.user_repo import AbstractUserRepo
    from .federation_service import FederationService

log = logging.getLogger(__name__)

#: Caps on what a request carries (the admins see the message and name).
MAX_MESSAGE_CHARS: int = 1000
MAX_NAME_CHARS: int = 120

#: Per-sender budget on the host for join-request bodies, per window.
PER_SENDER_LIMIT: int = 10
PER_SENDER_WINDOW_S: int = 3600

#: How long a request waits for the admins (both sides keep it this long).
REQUEST_TTL_DAYS: int = 7

#: The answers the host seals back.
ANSWER_APPROVED: str = "approved"
ANSWER_DENIED: str = "denied"

#: Looks up the host's keys on our GFSs: ``(space_id, owner_instance_id)`` →
#: ``(contact, gfs_url)`` or ``None``
#: (:meth:`GfsConnectionService.fetch_space_contact`).
ContactLookup = Callable[[str, str], Awaitable[tuple[dict[str, str], str] | None]]


class GfsJoinRequestCoordinator:
    """Both ends of an "Ask to join" sealed over the GFS relay."""

    __slots__ = (
        "_bus",
        "_federation",
        "_federation_repo",
        "_hints",
        "_keywrap_public_key",
        "_keywrap_sig",
        "_lookup_contact",
        "_public_spaces",
        "_rate_limiter",
        "_relay_sender",
        "_spaces",
        "_users",
    )

    def __init__(
        self,
        *,
        federation: FederationService,
        federation_repo: AbstractFederationRepo,
        space_repo: AbstractSpaceRepo,
        public_space_repo: AbstractPublicSpaceRepo,
        user_repo: AbstractUserRepo,
        bus: EventBus,
        relay_sender: RelayEnvelopeSender,
        lookup_contact: ContactLookup,
        keywrap_public_key: bytes,
        keywrap_sig: str,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self._federation = federation
        self._federation_repo = federation_repo
        self._spaces = space_repo
        self._public_spaces = public_space_repo
        self._users = user_repo
        self._bus = bus
        self._relay_sender = relay_sender
        self._lookup_contact = lookup_contact
        self._keywrap_public_key = keywrap_public_key
        self._keywrap_sig = keywrap_sig
        self._rate_limiter = rate_limiter
        #: request id → the redeem hint an approval left for the seat step
        #: (:meth:`take_hint`, read by ``SpaceService``).
        self._hints: dict[str, InviteBootstrapHint] = {}

    # ─── Applicant side ───────────────────────────────────────────────────

    async def host_of_listing(self, space_id: str) -> str | None:
        """The host instance id a GFS directory listed *space_id* under, or
        ``None`` when no GFS of ours lists it."""
        listing = await self._public_spaces.get(space_id)
        if listing is None or not listing.instance_id:
            return None
        return listing.instance_id

    async def request_join(
        self,
        space_id: str,
        *,
        applicant_user_id: str,
        host_instance_id: str,
        message: str | None = None,
    ) -> str:
        """Seal an "Ask to join" to *host_instance_id* through the GFS.
        Returns the request id, kept locally as a pending request (so
        ``/api/me/join-requests`` shows "Requested").

        Raises :class:`HostNotReachableError` when no GFS of ours has the
        host's keys, the keys don't bind to the listed host, or the relay
        refuses the blob. Once the relay took it the request stands: the GFS
        queues it for a host that is offline.
        """
        found = await self._lookup_contact(space_id, host_instance_id)
        if found is None:
            log.warning(
                "gfs join request: no GFS has a contact for space %s (host %s)",
                space_id,
                host_instance_id,
            )
            raise HostNotReachableError()
        contact, gfs_url = found
        user = await self._users.get_by_user_id(applicant_user_id)
        name = ""
        if user is not None:
            name = str(user.display_name or user.username or "")
        local = await self._federation_repo.get_local_identity()
        request_id = uuid.uuid4().hex
        text = str(message)[:MAX_MESSAGE_CHARS] if message else None
        body: dict[str, Any] = {
            "kind": KIND_JOIN_REQUEST,
            "request_nonce": uuid.uuid4().hex,
            "request_id": request_id,
            "space_id": space_id,
            "ts": datetime.now(UTC).isoformat(),
            "instance_id": self._federation.own_instance_id,
            "identity_pk": self._federation.own_identity_pk.hex(),
            "keywrap_pk": self._keywrap_public_key.hex(),
            "keywrap_sig": self._keywrap_sig,
            "display_name": str((local or {}).get("display_name") or ""),
            "proto_version": OURS,
            "user_id": applicant_user_id,
            "user_display_name": name[:MAX_NAME_CHARS],
            "user_public_key": getattr(user, "public_key", None),
        }
        if text:
            body["message"] = text
        try:
            envelope = seal_bootstrap_envelope(
                body=body,
                identity_seed=self._federation.own_identity_seed,
                recipient_instance_id=host_instance_id,
                recipient_identity_pk=contact["identity_public_key"],
                recipient_keywrap_pk=contact["keywrap_public_key"],
                recipient_keywrap_sig=contact["keywrap_sig"],
            )
        except ValueError as exc:
            # The GFS served keys that do not belong to the listed host.
            log.warning(
                "gfs join request: refusing to seal to %s: %s",
                host_instance_id,
                exc,
            )
            raise HostNotReachableError() from exc
        delivered = await self._relay_sender.send_sealed_envelope(
            to_instance_id=host_instance_id,
            envelope=envelope,
            gfs_url=gfs_url,
        )
        if not delivered:
            raise HostNotReachableError()
        # Ours under the id the host files it by (as on the paired path), so
        # its answer finds the person.
        await self._spaces.save_join_request(
            space_id,
            applicant_user_id,
            message=text,
            ttl_days=REQUEST_TTL_DAYS,
            remote_applicant_instance_id=host_instance_id,
            request_id=request_id,
        )
        log.info(
            "gfs join request %s for space %s sent to %s through %s",
            request_id,
            space_id,
            host_instance_id,
            gfs_url,
        )
        return request_id

    def take_hint(self, request_id: str) -> InviteBootstrapHint | None:
        """The bootstrap hint an approval of *request_id* left (once)."""
        return self._hints.pop(request_id, None)

    # ─── Inbound (both sides) ─────────────────────────────────────────────

    async def handle_body(self, body: dict, *, gfs_url: str = "") -> dict:
        """One unsealed join-request body from the relay (request or
        answer). Raises :class:`ValueError` on a body to drop."""
        validate_bootstrap_body(
            body,
            expected_kinds=frozenset({KIND_JOIN_REQUEST, KIND_JOIN_REQUEST_ANSWER}),
        )
        sender = str(body["instance_id"])
        if self._rate_limiter is not None and not self._rate_limiter.is_allowed(
            f"gfs-join:sender:{sender}",
            limit=PER_SENDER_LIMIT,
            window_s=PER_SENDER_WINDOW_S,
        ):
            raise ValueError("gfs join request per-sender rate limit exceeded")
        nonce = str(body.get("request_nonce") or "")
        if not nonce:
            raise ValueError("gfs join body missing request_nonce")
        if await self._federation.note_replay_id(f"gfs-join:{nonce}"):
            raise ValueError(f"Replay detected: gfs join nonce={nonce!r}")
        if body["kind"] == KIND_JOIN_REQUEST:
            return await self._on_request(body, gfs_url=gfs_url)
        return await self._on_answer(body, gfs_url=gfs_url)

    async def _on_request(self, body: dict, *, gfs_url: str) -> dict:
        """Host side: file the request when it may be filed; drop it
        without a word otherwise."""
        applicant = str(body["instance_id"])
        if (
            verify_peer_keywrap(
                instance_id=applicant,
                identity_pk=str(body.get("identity_pk") or ""),
                keywrap_pk=str(body.get("keywrap_pk") or ""),
                keywrap_sig=str(body.get("keywrap_sig") or ""),
            )
            is None
        ):
            raise ValueError(
                "gfs join request key-wrap key is not bound to its identity"
            )
        space_id = str(body.get("space_id") or "")
        user_id = str(body.get("user_id") or "")
        request_id = str(body.get("request_id") or "")
        if not space_id or not user_id or not request_id:
            raise ValueError("gfs join request missing space_id / user_id / id")
        space = await self._spaces.get(space_id)
        if (
            space is None
            or space.dissolved
            or space.owner_instance_id != self._federation.own_instance_id
            or space.space_type is not SpaceType.GLOBAL
            or space.join_mode is JoinMode.INVITE_ONLY
        ):
            log.info(
                "gfs join request from %s for space %s refused: not a live "
                "global space this household hosts that takes requests",
                applicant,
                space_id,
            )
            return {"ok": True, "filed": False}
        if await self._spaces.is_banned(space_id, user_id):
            log.info(
                "gfs join request from %s refused: applicant banned from %s",
                applicant,
                space_id,
            )
            return {"ok": True, "filed": False}
        if (
            await pending_join_request_id(
                self._spaces, space_id, user_id, household_instance_id=applicant
            )
            is not None
        ):
            log.debug(
                "gfs join request from %s for %s: already pending", applicant, space_id
            )
            return {"ok": True, "filed": False}
        if await self._spaces.get_join_request(request_id) is not None:
            # An id that names another request already: never two rows
            # under one id (the answer would reach the wrong household).
            raise ValueError("gfs join request id already in use")
        message = body.get("message")
        user_pk = body.get("user_public_key")
        rid = await file_remote_join_request(
            self._spaces,
            self._bus,
            space_id=space_id,
            household_instance_id=applicant,
            user_id=user_id,
            area_verdict=None,
            applicant_pk=str(user_pk) if isinstance(user_pk, str) and user_pk else None,
            message=str(message)[:MAX_MESSAGE_CHARS] if message else None,
            request_id=request_id,
            applicant_relay=_relay_json(
                identity_pk=str(body["identity_pk"]),
                keywrap_pk=str(body["keywrap_pk"]),
                keywrap_sig=str(body["keywrap_sig"]),
                gfs_url=gfs_url,
            ),
        )
        log.info(
            "gfs join request %s from %s filed for space %s", rid, applicant, space_id
        )
        return {"ok": True, "filed": True}

    async def _on_answer(self, body: dict, *, gfs_url: str) -> dict:
        """Applicant side: the admins' answer. Taken only for a pending
        request of ours that names the sending household as its host."""
        request_id = str(body.get("request_id") or "")
        row = await self._spaces.get_join_request(request_id) if request_id else None
        host = str(body["instance_id"])
        if (
            row is None
            or row.get("status") != "pending"
            or row.get("remote_applicant_instance_id") != host
            or row.get("space_id") != str(body.get("space_id") or "")
        ):
            raise ValueError("gfs join answer for no pending request of ours")
        space_id = str(row["space_id"])
        answer = str(body.get("answer") or "")
        if answer == ANSWER_APPROVED:
            token = str(body.get("invite_token") or "")
            if not token:
                raise ValueError("gfs join approval without a token")
            if not await self._spaces.answer_join_request(request_id, ANSWER_APPROVED):
                return {"ok": True, "stale": True}
            # The redeem goes to the host over the same sealed leg, to the
            # keys this signed answer carries (bound again before the seal).
            self._hints[request_id] = InviteBootstrapHint(
                invite_token=token,
                space_id=space_id,
                instance_id=host,
                identity_pk=str(body["identity_pk"]),
                keywrap_pk=str(body.get("keywrap_pk") or ""),
                keywrap_sig=str(body.get("keywrap_sig") or ""),
                proto_version=_int(body.get("proto_version")),
                gfs_url=gfs_url,
            )
            # The same bus event a paired host's SPACE_JOIN_REQUEST_APPROVED
            # raises: the bell, and the SpaceService redeem (which takes the
            # hint above).
            await self._bus.publish(
                RemoteJoinRequestApproved(
                    request_id=request_id, space_id=space_id, invite_token=token
                )
            )
            log.info("gfs join request %s approved by %s", request_id, host)
            return {"ok": True}
        if answer == ANSWER_DENIED:
            if await self._spaces.answer_join_request(request_id, ANSWER_DENIED):
                await self._bus.publish(
                    RemoteJoinRequestDenied(request_id=request_id, space_id=space_id)
                )
            return {"ok": True}
        raise ValueError(f"unknown gfs join answer {answer!r}")

    # ─── Host side: the answer ────────────────────────────────────────────

    async def send_answer(
        self,
        row: dict,
        *,
        answer: str,
        invite_token: str | None = None,
    ) -> bool:
        """Seal the admins' answer to the household that asked through the
        GFS (``row`` is the host's join request, ``applicant_relay`` set).
        ``True`` when the relay took it. Never raises."""
        try:
            relay = orjson.loads(row.get("applicant_relay") or "null")
        except ValueError:
            relay = None
        if not isinstance(relay, dict):
            return False
        recipient = str(row.get("remote_applicant_instance_id") or "")
        reply: dict[str, Any] = {
            "kind": KIND_JOIN_REQUEST_ANSWER,
            "request_nonce": uuid.uuid4().hex,
            "request_id": str(row["id"]),
            "space_id": str(row["space_id"]),
            "answer": answer,
        }
        if invite_token:
            reply["invite_token"] = invite_token
        try:
            local = await self._federation_repo.get_local_identity()
            envelope = seal_bootstrap_envelope(
                body={
                    **reply,
                    "ts": datetime.now(UTC).isoformat(),
                    "instance_id": self._federation.own_instance_id,
                    "identity_pk": self._federation.own_identity_pk.hex(),
                    "keywrap_pk": self._keywrap_public_key.hex(),
                    "keywrap_sig": self._keywrap_sig,
                    "display_name": str((local or {}).get("display_name") or ""),
                    "proto_version": OURS,
                },
                identity_seed=self._federation.own_identity_seed,
                recipient_instance_id=recipient,
                recipient_identity_pk=str(relay.get("identity_pk") or ""),
                recipient_keywrap_pk=str(relay.get("keywrap_pk") or ""),
                recipient_keywrap_sig=str(relay.get("keywrap_sig") or ""),
            )
            delivered = await self._relay_sender.send_sealed_envelope(
                to_instance_id=recipient,
                envelope=envelope,
                gfs_url=str(relay.get("gfs_url") or ""),
            )
        except Exception:
            log.exception("gfs join request: answer to %s failed", recipient)
            return False
        if not delivered:
            log.warning("gfs join request: the GFS refused our answer to %s", recipient)
        return bool(delivered)


def _relay_json(
    *, identity_pk: str, keywrap_pk: str, keywrap_sig: str, gfs_url: str
) -> str:
    return orjson.dumps(
        {
            "identity_pk": identity_pk,
            "keywrap_pk": keywrap_pk,
            "keywrap_sig": keywrap_sig,
            "gfs_url": gfs_url,
        }
    ).decode()


def _int(raw: object) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int | str):
        return 1
    try:
        return int(raw)
    except TypeError, ValueError:
        return 1


__all__ = [
    "ANSWER_APPROVED",
    "ANSWER_DENIED",
    "GfsJoinRequestCoordinator",
]
