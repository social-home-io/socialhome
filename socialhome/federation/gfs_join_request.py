"""Ask to join a global space through the GFS (v_84).

A global space is listed in a GFS directory with its owner's instance id
and nothing that reaches it: the GFS shields households from each other. A
household connected to the same GFS but not paired with the host used to
see the space and have no way to ask in — the §D2 ``SPACE_JOIN_REQUEST``
needs a confirmed pairing, and the §D2b sealed relay leg only ever carried
an invite-link redeem.

This module carries the "Ask to join" over that same sealed leg:

1. **Applicant** — reads the host's join contact off the GFS listing (its
   keys, signed by the space authority key and pinned on first sight —
   :mod:`socialhome.federation.join_contact_pin`; no lookup, so the GFS
   learns nothing about who asks), keeps its own pending request (the id it
   picks) and seals ``SPACE_JOIN_REQUEST_BOOTSTRAP`` (person, names,
   message) to the host over ``POST /gfs/envelope``. The relay routes on
   the recipient id only and queues the blob while the host is offline.
2. **Host** — the body opens and verifies like every bootstrap body
   (signature, ``derive_instance_id`` anti-tamper, replay on the nonce);
   its ``ts`` may be as old as the relay's queue TTL (the nonce's replay
   entry lives at least that long). It is rate-limited per sender and per
   space, and a space keeps at most :data:`MAX_PENDING_PER_SPACE` pending
   requests from the GFS (past it new ones are refused — the admins' queue
   and earlier applicants stay as they are). A live global space it hosts
   that takes requests files an ordinary pending request under the
   applicant's id, keeping the applicant's relay contact and the names it
   sent (shown as "not verified"). A banned person, a repeat or a space
   that takes no requests is dropped without a word. A request refused for
   CAPACITY (the pending cap or the space's budget) is answered "busy" —
   at most :data:`PER_SPACE_LIMIT` such answers per space per window — so
   the applicant hears at once and can ask again instead of waiting out
   :data:`REQUEST_TTL_DAYS`.
3. **Answer** — the admins approve or decline. :meth:`send_answer` seals
   ``SPACE_JOIN_REQUEST_BOOTSTRAP_ANSWER`` back: an approval carries a
   single-use invite token **bound to the applicant household and person**
   with its expiry. The applicant stores it with the request and redeems it
   over the §D2b bootstrap leg; a redeem that fails (host offline) is
   retried on the next start and GFS reconnect until it seats the person
   or the token expires. The request reads "approved, joining…" meanwhile.
   A "busy" answer ends the applicant's request (``expired``, marked busy)
   and tells the person; Discover offers "Ask to join" again.

A **follow** (v_96) is a join for the read-only ``subscriber`` role and
rides the same leg: the sealed body carries ``requested_role:
"subscriber"`` (the field the paired path carries), the host applies every
budget and gate above in the same order, but needs a space that takes
followers (``allow_subscribers``; the join mode does not matter) and,
instead of filing a request for the admins, hands it to its automatic
approval (:meth:`GfsJoinRequestCoordinator.attach_follow_approver`), which
answers "approved" with a bound subscriber token. The applicant redeems it
like any approval and is seated as a subscriber.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import orjson

from ..domain.events import (
    RemoteJoinRequestApproved,
    RemoteJoinRequestBusy,
    RemoteJoinRequestDenied,
)
from ..domain.federation_capabilities import OURS
from ..domain.space import (
    FollowApprover,
    HostNotReachableError,
    JoinMode,
    SpacePermissionError,
    SpaceRole,
    SpaceType,
    takes_follows,
)
from ..services.local_community import (
    file_remote_join_request,
    pending_join_request_id,
)
from ..services.space_member_guard import user_id_proven
from .inbound_validator import RELAY_QUEUE_TTL_SECONDS, RELAY_TIMESTAMP_SKEW_SECONDS
from .invite_bootstrap import (
    KIND_JOIN_REQUEST,
    KIND_JOIN_REQUEST_ANSWER,
    InviteBootstrapHint,
    RelayEnvelopeSender,
    body_identity_pk,
    seal_bootstrap_envelope,
    validate_bootstrap_body,
    verify_peer_keywrap,
)
from .join_contact_pin import usable_contact

if TYPE_CHECKING:
    from ..infrastructure.event_bus import EventBus
    from ..rate_limiter import RateLimiter
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.public_space_repo import AbstractPublicSpaceRepo
    from ..repositories.space_repo import AbstractSpaceRepo
    from ..repositories.user_repo import AbstractUserRepo
    from .federation_service import FederationService

log = logging.getLogger(__name__)

#: Caps on what a request carries (the admins see the message and names).
MAX_MESSAGE_CHARS: int = 1000
MAX_NAME_CHARS: int = 120

#: A user id / request id on this leg: what our ids look like, bounded.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: Per-sender budget on either side, per window. A sender is a keypair, so
#: this alone does not stop a flood (fresh keypairs are free) — the
#: per-space budget and the pending cap below do.
PER_SENDER_LIMIT: int = 10
PER_SENDER_WINDOW_S: int = 3600

#: Per-space budget on the host: requests one space takes from the GFS per
#: window, whoever sends them, so one flooded space never starves another.
#: Sized for a room: a local-community meetup where 40 households ask within
#: minutes (every edge-of-area join lands here too), with headroom for a
#: second wave the same hour. The same number bounds the "busy" answers one
#: space sends per window (its own budget).
PER_SPACE_LIMIT: int = 120
PER_SPACE_WINDOW_S: int = 3600

#: Pending requests from the GFS one space keeps. Past it a new one is
#: REFUSED — answered "busy", never filed — earlier applicants and the
#: admins' queue stay as they are; one is admitted again once the admins
#: answer some.
MAX_PENDING_PER_SPACE: int = 200

#: How long the applicant's Discover keeps saying "busy" after the answer.
BUSY_SHOWN_HOURS: int = 24

#: How long a request waits for the admins (both sides keep it this long).
REQUEST_TTL_DAYS: int = 7

#: Life of the single-use token an approval mints: the answer may sit in the
#: GFS queue up to its TTL while the applicant is offline, and the applicant
#: then retries the redeem (host offline too) for as long again.
APPROVAL_TOKEN_TTL: timedelta = timedelta(seconds=2 * RELAY_QUEUE_TTL_SECONDS)

#: The answers the host seals back. ``busy`` (no admin saw it — the space
#: had no room) reaches an applicant from before it as an unknown answer,
#: which it drops: the silence every applicant got before.
ANSWER_APPROVED: str = "approved"
ANSWER_DENIED: str = "denied"
ANSWER_BUSY: str = "busy"

#: Seats the approved person: ``(token, user_id, issuer, bootstrap)`` →
#: ``{space_id, role}`` (:meth:`SpaceService.redeem_invite_token`).
Redeem = Callable[..., Awaitable[dict]]

#: The one role a request may ask for (v_96): a follow. Rides as
#: ``requested_role`` inside the sealed body, the same field the paired
#: ``SPACE_JOIN_REQUEST`` carries.
_FOLLOW_ROLE: str = SpaceRole.SUBSCRIBER.value


class GfsJoinRequestCoordinator:
    """Both ends of an "Ask to join" sealed over the GFS relay."""

    __slots__ = (
        "_bus",
        "_federation",
        "_federation_repo",
        "_follow_approver",
        "_in_flight",
        "_keywrap_public_key",
        "_keywrap_sig",
        "_public_spaces",
        "_rate_limiter",
        "_redeem",
        "_relay_sender",
        "_spaces",
        "_tasks",
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
        self._keywrap_public_key = keywrap_public_key
        self._keywrap_sig = keywrap_sig
        self._rate_limiter = rate_limiter
        self._redeem: Redeem | None = None
        #: Approves a follow (v_96) — :meth:`attach_follow_approver`; until
        #: then a follow is dropped (fail closed).
        self._follow_approver: FollowApprover | None = None
        #: Request ids whose redeem is running (never two at once).
        self._in_flight: set[str] = set()
        self._tasks: set[asyncio.Task] = set()

    def attach_redeem(self, redeem: Redeem) -> None:
        """Wire the seat step (:meth:`SpaceService.redeem_invite_token`)."""
        self._redeem = redeem

    def attach_follow_approver(self, approver: FollowApprover) -> None:
        """Wire the host's automatic approval of a follow (v_96,
        :meth:`SpaceService.approve_remote_follow`)."""
        self._follow_approver = approver

    # ─── Applicant side ───────────────────────────────────────────────────

    async def host_of_listing(self, space_id: str) -> str | None:
        """The host a GFS listing names in its verified join contact, or
        ``None`` (not listed, no contact, or a conflicting authority)."""
        found = usable_contact(await self._public_spaces.get_pin(space_id))
        return found[0]["instance_id"] if found is not None else None

    async def request_join(
        self,
        space_id: str,
        *,
        applicant_user_id: str,
        host_instance_id: str,
        message: str | None = None,
        requested_role: SpaceRole | None = None,
    ) -> str:
        """Seal an "Ask to join" to *host_instance_id* through the GFS.
        Returns the request id, kept locally as a pending request.

        ``requested_role`` — :attr:`SpaceRole.SUBSCRIBER` for a follow
        (v_96): ``requested_role: "subscriber"`` rides inside the sealed body
        (the GFS never sees it), the field the paired path carries too. The
        host approves it on its own while the space takes followers. Any
        other role raises :class:`ValueError`.

        Raises :class:`HostNotReachableError` when the listing carries no
        usable contact for that host or the relay refuses the blob. Once the
        relay took it the request stands: the GFS queues it while the host
        is offline.
        """
        if requested_role is not None and requested_role is not SpaceRole.SUBSCRIBER:
            raise ValueError(f"cannot ask for the {requested_role!r} role")
        found = usable_contact(await self._public_spaces.get_pin(space_id))
        if found is None or found[0]["instance_id"] != host_instance_id:
            log.warning(
                "gfs join request: no usable join contact for space %s (host %s)",
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
            "display_name": str((local or {}).get("display_name") or "")[
                :MAX_NAME_CHARS
            ],
            "proto_version": OURS,
            "user_id": applicant_user_id,
            "user_display_name": name[:MAX_NAME_CHARS],
            "user_public_key": getattr(user, "public_key", None),
        }
        if user is not None:
            # P5 — proves ``user_id`` against ``identity_pk``: a follow is
            # approved on it (the redeem of the token proves again).
            body["user_anchor"] = user.identity_anchor or user.username
        if text:
            body["message"] = text
        if requested_role is not None:
            body["requested_role"] = _FOLLOW_ROLE
        try:
            envelope = seal_bootstrap_envelope(
                body=body,
                identity_seed=self._federation.own_identity_seed,
                recipient_instance_id=host_instance_id,
                recipient_identity_pk=contact["identity_pk"],
                recipient_keywrap_pk=contact["keywrap_pk"],
                recipient_keywrap_sig=contact["keywrap_sig"],
            )
        except ValueError as exc:
            log.warning(
                "gfs join request: refusing to seal to %s: %s", host_instance_id, exc
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
        # its answer finds the person. ``gfs_relay`` marks it as one that
        # crossed the GFS; the approval is kept in it later.
        await self._spaces.save_join_request(
            space_id,
            applicant_user_id,
            message=text,
            ttl_days=REQUEST_TTL_DAYS,
            remote_applicant_instance_id=host_instance_id,
            request_id=request_id,
            gfs_relay=_dump({"side": "applicant", "gfs_url": gfs_url}),
        )
        log.info(
            "gfs join request %s for space %s sent to %s through %s",
            request_id,
            space_id,
            host_instance_id,
            gfs_url,
        )
        return request_id

    async def joining_space_ids(self, user_id: str) -> list[str]:
        """Spaces whose request the host approved and whose seat is still
        being made ("approved, joining…")."""
        out: list[str] = []
        for row in await self._spaces.list_pending_gfs_join_requests():
            relay = _load(row.get("gfs_relay"))
            if row.get("user_id") == user_id and relay.get("approval"):
                out.append(str(row["space_id"]))
        return out

    async def busy_space_ids(self, user_id: str) -> list[str]:
        """Spaces whose host answered *user_id*'s NEWEST request through
        the GFS (asked in the last :data:`BUSY_SHOWN_HOURS`) "busy" —
        Discover says so next to "Ask to join". A later request, whatever
        its answer, replaces the note."""
        seen: set[str] = set()
        out: list[str] = []
        for row in await self._spaces.list_recent_gfs_join_requests(
            user_id, within_hours=BUSY_SHOWN_HOURS
        ):
            relay = _load(row.get("gfs_relay"))
            space_id = str(row["space_id"])
            if relay.get("side") != "applicant" or space_id in seen:
                continue
            seen.add(space_id)
            if row.get("status") == "expired" and relay.get("busy"):
                out.append(space_id)
        return out

    async def resume_approvals(self) -> int:
        """Redeem every approval that has not seated its person yet (start,
        GFS reconnect, a fresh answer). Returns how many seated."""
        seated = 0
        for row in await self._spaces.list_pending_gfs_join_requests():
            if not _load(row.get("gfs_relay")).get("approval"):
                continue
            try:
                if await self._seat(row):
                    seated += 1
            except Exception:
                # One bad row (corrupt approval, a store fault) never stops
                # the others; it is retried next time.
                log.exception("gfs join request %s: resume failed", row.get("id"))
        return seated

    def resume_soon(self) -> None:
        """:meth:`resume_approvals` as its own task (inbound handlers must
        not wait on a redeem that waits on the same socket)."""
        task = asyncio.create_task(self.resume_approvals(), name="gfs-join-resume")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _seat(self, row: dict) -> bool:
        request_id = str(row["id"])
        if request_id in self._in_flight or self._redeem is None:
            return False
        approval = _load(row.get("gfs_relay")).get("approval") or {}
        expires = _parse_ts(approval.get("expires_at"))
        # No (readable) expiry reads as expired — never a retry forever.
        if expires is None or expires <= datetime.now(UTC):
            await self._spaces.answer_join_request(request_id, "expired")
            log.info(
                "gfs join request %s: approval expired before it seated", request_id
            )
            return False
        hint = InviteBootstrapHint(
            invite_token=str(approval.get("invite_token") or ""),
            space_id=str(row["space_id"]),
            instance_id=str(row["remote_applicant_instance_id"]),
            identity_pk=str(approval.get("identity_pk") or ""),
            keywrap_pk=str(approval.get("keywrap_pk") or ""),
            keywrap_sig=str(approval.get("keywrap_sig") or ""),
            proto_version=_int(approval.get("proto_version")),
            gfs_url=str(approval.get("gfs_url") or ""),
        )
        self._in_flight.add(request_id)
        try:
            await self._redeem(
                hint.invite_token,
                user_id=str(row["user_id"]),
                issuer_instance_id=hint.instance_id,
                bootstrap=hint,
            )
        except (SpacePermissionError, TimeoutError, KeyError) as exc:
            # Kept as approved-and-joining: the next start / reconnect
            # retries until the token expires.
            log.warning(
                "gfs join request %s: redeem failed, retried later: %s",
                request_id,
                exc,
            )
            return False
        finally:
            self._in_flight.discard(request_id)
        if await self._spaces.answer_join_request(request_id, ANSWER_APPROVED):
            await self._bus.publish(
                RemoteJoinRequestApproved(
                    request_id=request_id,
                    space_id=hint.space_id,
                    invite_token=hint.invite_token,
                )
            )
        log.info("gfs join request %s: seated in space %s", request_id, hint.space_id)
        return True

    # ─── Inbound (both sides) ─────────────────────────────────────────────

    async def handle_body(self, body: dict, *, gfs_url: str = "") -> dict:
        """One unsealed join-request body from the relay (request or
        answer). Raises :class:`ValueError` on a body to drop."""
        # Either may have waited in the relay's queue for an offline
        # household: its ``ts`` is judged against the queue TTL, and the
        # nonce's replay entry (``REPLAY_CACHE_WINDOW``) outlives that.
        validate_bootstrap_body(
            body,
            expected_kinds=frozenset({KIND_JOIN_REQUEST, KIND_JOIN_REQUEST_ANSWER}),
            max_age_seconds=RELAY_TIMESTAMP_SKEW_SECONDS,
        )
        sender = str(body["instance_id"])
        if not self._allowed(
            f"gfs-join:sender:{sender}", PER_SENDER_LIMIT, PER_SENDER_WINDOW_S
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

    def _has_room(self, key: str, limit: int, window_s: int) -> bool:
        if self._rate_limiter is None:
            return True
        return self._rate_limiter.has_room(key, limit=limit, window_s=window_s)

    def _allowed(self, key: str, limit: int, window_s: int) -> bool:
        if self._rate_limiter is None:
            return True
        return self._rate_limiter.is_allowed(key, limit=limit, window_s=window_s)

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
        if not _ID_RE.match(user_id) or not _ID_RE.match(request_id):
            raise ValueError("gfs join request user_id / request_id malformed")
        role = body.get("requested_role")
        if role is not None and role != _FOLLOW_ROLE:
            raise ValueError("gfs join request asks for an unknown requested_role")
        follow = role == _FOLLOW_ROLE
        space = await self._spaces.get(space_id)
        own = self._federation.own_instance_id
        if (
            space is None
            or space.dissolved
            or space.owner_instance_id != own
            # Only a global space is listed on a GFS (a local community,
            # being public, never is).
            or space.space_type is not SpaceType.GLOBAL
            # A follow (v_96) is a join for the read-only role: it needs a
            # space that takes followers, whatever its join mode; a request
            # to join as a member needs one that takes requests.
            or (
                not takes_follows(space, own)
                if follow
                else space.join_mode is JoinMode.INVITE_ONLY
            )
        ):
            log.info(
                "gfs join request from %s for space %s refused: not a live "
                "global space this household hosts that takes %s",
                applicant,
                space_id,
                "followers" if follow else "requests",
            )
            return {"ok": True, "filed": False}
        if follow and self._follow_approver is None:
            log.warning(
                "gfs follow from %s for space %s dropped: no follow approver wired",
                applicant,
                space_id,
            )
            return {"ok": True, "filed": False}
        # Capacity FIRST, before anything about the person: a full space
        # answers "busy" to everyone alike, so the answer can never tell a
        # prober whether a user id is banned or already asked.
        space_budget = f"gfs-join:space:{space_id}"
        pending = await self._spaces.list_pending_gfs_join_requests(space_id=space_id)
        if len(pending) >= MAX_PENDING_PER_SPACE:
            log.warning(
                "gfs join request for space %s refused: %d requests from the GFS"
                " already wait for its admins",
                space_id,
                len(pending),
            )
            return await self._refuse_busy(body, gfs_url=gfs_url)
        if not self._has_room(space_budget, PER_SPACE_LIMIT, PER_SPACE_WINDOW_S):
            log.warning(
                "gfs join request for space %s refused: the space's budget is spent",
                space_id,
            )
            return await self._refuse_busy(body, gfs_url=gfs_url)
        applicant_identity_pk = body_identity_pk(body)
        applicant_anchor = body.get("user_anchor")
        if follow and not user_id_proven(
            applicant_identity_pk, applicant_anchor, user_id
        ):
            # P5 — a follow is approved on its own, so it must prove its
            # person first: before the ban, the pending request and the
            # space budget, dropped the same silent way whatever that id
            # holds. A member request is reviewed by an admin; the token an
            # approval mints is bound to the person and its redeem proves.
            log.info(
                "gfs follow from %s for space %s refused: user id %s is not "
                "proven by the household's identity key",
                applicant,
                space_id,
                user_id,
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
        # The per-space budget (peeked above) is spent only by a request that
        # passed every cheap check — junk (a space that takes none, a banned
        # person, a repeat) cannot use it up for real applicants.
        if not self._allowed(space_budget, PER_SPACE_LIMIT, PER_SPACE_WINDOW_S):
            log.warning(
                "gfs join request for space %s refused: the space's budget is spent",
                space_id,
            )
            return await self._refuse_busy(body, gfs_url=gfs_url)
        message = body.get("message")
        user_pk = body.get("user_public_key")
        applicant_pk = str(user_pk) if isinstance(user_pk, str) and user_pk else None
        # The applicant's relay contact: the answer goes back the same way.
        gfs_relay = _dump(
            {
                "side": "host",
                "identity_pk": str(body["identity_pk"]),
                "keywrap_pk": str(body["keywrap_pk"]),
                "keywrap_sig": str(body["keywrap_sig"]),
                "gfs_url": gfs_url,
                # What the applicant says about itself — shown to the
                # admins as "via the GFS, not verified".
                "person_name": _name(body.get("user_display_name")),
                "household_name": _name(body.get("display_name")),
            }
        )
        if follow:
            # v_96 — approved on its own (no admin queue); the approver
            # re-checks every gate and answers through the GFS with a bound
            # subscriber token.
            assert self._follow_approver is not None
            approved = await self._follow_approver(
                space_id=space_id,
                instance_id=applicant,
                user_id=user_id,
                request_id=request_id,
                applicant_identity_pk=applicant_identity_pk,
                applicant_anchor=applicant_anchor,
                applicant_pk=applicant_pk,
                area_verdict=None,
                gfs_relay=gfs_relay,
            )
            log.info(
                "gfs follow %s from %s for space %s: %s",
                request_id,
                applicant,
                space_id,
                "approved" if approved else "refused",
            )
            return {"ok": True, "filed": bool(approved)}
        rid = await file_remote_join_request(
            self._spaces,
            self._bus,
            space_id=space_id,
            household_instance_id=applicant,
            user_id=user_id,
            area_verdict=None,
            applicant_pk=applicant_pk,
            message=str(message)[:MAX_MESSAGE_CHARS] if message else None,
            request_id=request_id,
            gfs_relay=gfs_relay,
        )
        log.info(
            "gfs join request %s from %s filed for space %s", rid, applicant, space_id
        )
        return {"ok": True, "filed": True}

    async def _refuse_busy(self, body: dict, *, gfs_url: str) -> dict:
        """Host side: a request refused for capacity is answered "busy" —
        sealed to the keys the (verified) request carried, through the GFS
        it came on — so the applicant hears now instead of waiting out the
        TTL. Nothing is stored. The answers have their own per-space budget:
        a flood of fresh keypairs at a full space gets at most that many an
        hour, the rest stay silent."""
        space_id = str(body["space_id"])
        if not self._allowed(
            f"gfs-join:busy:{space_id}", PER_SPACE_LIMIT, PER_SPACE_WINDOW_S
        ):
            log.info(
                "gfs join request for space %s: busy answers spent, not answered",
                space_id,
            )
            return {"ok": True, "filed": False}
        sent = await self._seal_answer(
            recipient=str(body["instance_id"]),
            identity_pk=str(body["identity_pk"]),
            keywrap_pk=str(body["keywrap_pk"]),
            keywrap_sig=str(body["keywrap_sig"]),
            gfs_url=gfs_url,
            reply={
                "request_id": str(body["request_id"]),
                "space_id": space_id,
                "answer": ANSWER_BUSY,
            },
        )
        return {"ok": True, "filed": False, "busy": sent}

    async def _on_answer(self, body: dict, *, gfs_url: str) -> dict:
        """Applicant side: the admins' answer. Taken only for a pending
        request of ours that names the sending household as its host."""
        request_id = str(body.get("request_id") or "")
        row = await self._spaces.get_join_request(request_id) if request_id else None
        host = str(body["instance_id"])
        relay = _load(row.get("gfs_relay")) if row is not None else {}
        if (
            row is None
            or row.get("status") != "pending"
            or row.get("remote_applicant_instance_id") != host
            or row.get("space_id") != str(body.get("space_id") or "")
            or relay.get("side") != "applicant"
        ):
            raise ValueError("gfs join answer for no pending request of ours")
        space_id = str(row["space_id"])
        answer = str(body.get("answer") or "")
        if answer == ANSWER_APPROVED:
            token = str(body.get("invite_token") or "")
            if not token:
                raise ValueError("gfs join approval without a token")
            # Kept with the request until the redeem seated the person, so a
            # failed redeem is retried (start, reconnect) instead of lost.
            relay["approval"] = {
                "invite_token": token,
                "expires_at": str(body.get("token_expires_at") or ""),
                "identity_pk": str(body["identity_pk"]),
                "keywrap_pk": str(body.get("keywrap_pk") or ""),
                "keywrap_sig": str(body.get("keywrap_sig") or ""),
                "proto_version": _int(body.get("proto_version")),
                "gfs_url": gfs_url,
            }
            await self._spaces.set_join_request_gfs_relay(request_id, _dump(relay))
            log.info("gfs join request %s approved by %s — joining", request_id, host)
            self.resume_soon()
            return {"ok": True}
        if answer == ANSWER_DENIED:
            if await self._spaces.answer_join_request(request_id, ANSWER_DENIED):
                await self._bus.publish(
                    RemoteJoinRequestDenied(request_id=request_id, space_id=space_id)
                )
            return {"ok": True}
        if answer == ANSWER_BUSY:
            # Never filed on the host: the request ends here (``expired`` —
            # no admin decided), marked busy so Discover says why and offers
            # "Ask to join" again. Never over an approval still being seated.
            if relay.get("approval"):
                raise ValueError("gfs join busy answer for an approved request")
            if await self._spaces.answer_join_request(request_id, "expired"):
                relay["busy"] = True
                await self._spaces.set_join_request_gfs_relay(request_id, _dump(relay))
                await self._bus.publish(
                    RemoteJoinRequestBusy(request_id=request_id, space_id=space_id)
                )
            log.info("gfs join request %s: %s is busy", request_id, host)
            return {"ok": True}
        raise ValueError(f"unknown gfs join answer {answer!r}")

    # ─── Host side: the answer ────────────────────────────────────────────

    async def send_answer(
        self,
        row: dict,
        *,
        answer: str,
        invite_token: str | None = None,
        token_expires_at: str | None = None,
    ) -> bool:
        """Seal the admins' answer to the household that asked through the
        GFS (``row`` is the host's join request, ``gfs_relay`` set).
        ``True`` when the relay took it. Never raises."""
        relay = _load(row.get("gfs_relay"))
        if relay.get("side") != "host":
            return False
        reply: dict[str, Any] = {
            "request_id": str(row["id"]),
            "space_id": str(row["space_id"]),
            "answer": answer,
        }
        if invite_token:
            reply["invite_token"] = invite_token
            reply["token_expires_at"] = token_expires_at or ""
        return await self._seal_answer(
            recipient=str(row.get("remote_applicant_instance_id") or ""),
            identity_pk=str(relay.get("identity_pk") or ""),
            keywrap_pk=str(relay.get("keywrap_pk") or ""),
            keywrap_sig=str(relay.get("keywrap_sig") or ""),
            gfs_url=str(relay.get("gfs_url") or ""),
            reply=reply,
        )

    async def _seal_answer(
        self,
        *,
        recipient: str,
        identity_pk: str,
        keywrap_pk: str,
        keywrap_sig: str,
        gfs_url: str,
        reply: dict[str, Any],
    ) -> bool:
        """Seal one ``SPACE_JOIN_REQUEST_BOOTSTRAP_ANSWER`` (*reply*: request
        id, space id, answer, maybe a token) to the applicant household's
        keys and hand it to the relay. ``True`` when the relay took it.
        Never raises."""
        try:
            local = await self._federation_repo.get_local_identity()
            envelope = seal_bootstrap_envelope(
                body={
                    "kind": KIND_JOIN_REQUEST_ANSWER,
                    "request_nonce": uuid.uuid4().hex,
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
                recipient_identity_pk=identity_pk,
                recipient_keywrap_pk=keywrap_pk,
                recipient_keywrap_sig=keywrap_sig,
            )
            delivered = await self._relay_sender.send_sealed_envelope(
                to_instance_id=recipient,
                envelope=envelope,
                gfs_url=gfs_url,
            )
        except Exception:
            log.exception("gfs join request: answer to %s failed", recipient)
            return False
        if not delivered:
            log.warning("gfs join request: the GFS refused our answer to %s", recipient)
        return bool(delivered)


def unverified_names(row: dict) -> tuple[str | None, str | None] | None:
    """``(person, household)`` a host's GFS request carried — what the
    applicant says, never verified — or ``None`` for any other request."""
    relay = _load(row.get("gfs_relay"))
    if relay.get("side") != "host":
        return None
    return relay.get("person_name") or None, relay.get("household_name") or None


def _name(raw: object) -> str:
    return str(raw)[:MAX_NAME_CHARS] if isinstance(raw, str) else ""


def _load(raw: object) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        value = orjson.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _dump(value: dict) -> str:
    return orjson.dumps(value).decode()


def _parse_ts(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def _int(raw: object) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int | str):
        return 1
    try:
        return int(raw)
    except TypeError, ValueError:
        return 1


__all__ = [
    "ANSWER_APPROVED",
    "ANSWER_BUSY",
    "ANSWER_DENIED",
    "APPROVAL_TOKEN_TTL",
    "MAX_PENDING_PER_SPACE",
    "GfsJoinRequestCoordinator",
    "unverified_names",
]
