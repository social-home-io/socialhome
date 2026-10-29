"""AppFederationService — cross-household Social Home Apps federation bridge.

Translates between the federation layer (inbound events + binary frames from
``fed-app-v1``) and the SPA layer (WebSocket pushes to local users).

Security invariants
-------------------
* ``_require_enabled`` gates every outbound — an uninstalled or disabled app
  cannot initiate or send federation messages.
* Inbound delivery is fail-soft: if the app is not installed or not enabled,
  the event is silently dropped after a debug log (no error to the peer).
* No application payload ever appears in plaintext in a federation envelope.
  Both the binary and JSON paths rely on :class:`FederationService` for
  encryption; this service only passes dicts down to the send methods and
  never touches the wire format.

The ``session_id`` returned by :meth:`open_session` is the de-facto game /
whiteboard / mini-app session namespace.  All messages carry it so the SPA can
dispatch to the correct in-page component without any server-side state.

Person-addressed sessions: :meth:`open_session` and :meth:`send_message` both
target a specific person (local or remote).  A local-loopback delivers a frame
straight to the target and initiator over WebSocket (no federation send); a
remote open sends an ``APP_SESSION`` event and a remote message goes via
``send_app_message``, both carrying ``to_user`` / ``from_user`` to v_18+ peers
(gated on :data:`FederationCapability.MIN_FOR_APP_USER_ROUTING`) so the peer
can route to the addressed person.

Inbound routing: when an inbound JSON event or binary ``fed-app-v1`` frame
carries a non-empty ``to_user``, :meth:`_deliver` delivers only to that local
user (and names the sender as ``from_user`` on the frame); without one (an
older sender) it falls back to the household fan-out and the app's
``session_id`` scopes the semantics for the SPA.

Game invites: an open addressed to one person is kept for them
(``app_pending_sessions``, local and remote opens alike) until the invitee
plays it (:meth:`accept_session` — "Play" in "Needs you",
one invite at a time; or a message they send in that session — "accepted";
opening the app accepts nothing), the invitee declines it
(:meth:`decline_session`), the challenger withdraws it
(:meth:`cancel_session`, or an inbound ``APP_SESSION {verb:"close"}`` from
the household and person that opened it) or it expires
(:data:`~socialhome.domain.apps.PENDING_SESSION_TTL_SECONDS`). Each end but
the expiry publishes :class:`AppChallengeEnded`, which takes the "Needs you"
item down. A decline / withdrawal tells the other side with an
``APP_SESSION {verb:"close", reason}`` — the existing session-control verb.

Authorization: both outbound entry points call :meth:`_assert_target_allowed`,
which rejects (``AppContactNotFoundError``) any target that is not in the
actor's challengeable roster (the same block-aware set as
:meth:`list_contacts`).  Legacy household-addressed sends (``user_ref == ""``)
are exempt for back-compat — unless a guardian block of the actor names
someone homed on that household.

Guardian blocks (§CP.F2, via :class:`ProtectionGateMixin`): like calls and
DMs, apps never connect two people a guardian block separates — they are
missing from each other's contacts, an open / move / withdrawal between them
is refused on the way out, an inbound one is dropped on receipt (per-person
and household fan-out alike), and an invite that waited from before the
block is dropped when the app drains it.

The mailbox (D11): the household keeps, per person and per game it knows
(``app_pending_sessions``: an invite they got, a game they played or
sent), the **latest** message from the other side of that game that their
app hasn't taken yet — a move, a decline, a withdrawal. It is kept whether
the app is open, closed or turned off (the server can't see whether a tab
has the app mounted), and handed over when the app next opens
(:meth:`take_held`), then cleared. A decline also gives the challenger a
bell row. Nothing more goes over the wire: the mailbox only keeps what was
sent anyway. A message for a session the household doesn't know for that
person — or from anyone but its other side — is never kept.

Per move, the sender's session row stands in for the roster scan: a
message in a game this household knows with that very person needs one
primary-key read plus the block checks, not the whole contact list.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import orjson

from ..domain.apps import (
    HELD_MESSAGE_MAX_BYTES,
    PENDING_SESSION_TTL_SECONDS,
    PERMISSION_FEDERATION,
    PERMISSION_PEOPLE,
    SESSION_ACTIVE,
    SESSION_INVITED,
    AppAgeRestrictedError,
    AppChallengeHandledError,
    AppContactNotFoundError,
    AppNotEnabledError,
    AppNotFoundError,
    AppPendingSession,
)
from ..domain.events import (
    AppChallengeDeclined,
    AppChallengeEnded,
    AppChallengeReceived,
)
from ..domain.federation import FederationEvent, FederationEventType
from ..domain.federation_capabilities import FederationCapability
from .protection_gate import ProtectionGateMixin

if TYPE_CHECKING:
    from ..domain.apps import InstalledApp
    from ..domain.events import DomainEvent
    from ..domain.user import RemoteUser
    from ..infrastructure.event_bus import EventBus
    from ..infrastructure.ws_manager import WebSocketManager
    from ..repositories.app_repo import AbstractAppRepo
    from ..repositories.cp_repo import AbstractCpRepo
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.user_repo import AbstractUserRepo

#: Cap on the bounded inbound de-dupe of ``APP_SESSION`` opens — keeps memory
#: flat (LRU eviction of the oldest session id) while still suppressing the
#: realistic double-delivery window (WebRTC + HTTPS fallback, retransmits).
_SEEN_OPEN_SESSIONS_MAX = 4096

#: A game's row has its TTL moved on by the person's own moves at most once
#: in this long — one write an hour per game, not one per move.
_TOUCH_EVERY = timedelta(hours=1)

log = logging.getLogger(__name__)


def with_household(person: str, household: str | None) -> str:
    """``"Ben · Familie Okafor"`` — a person from another household, named
    with it so two people called "Admin" in two households read apart.
    Just *person* when the household is unknown or has the same name."""
    if not household or household == person:
        return person
    return f"{person} · {household}"


def _is_decline(kind: str, payload: dict) -> bool:
    """Whether an app frame is the invitee's "no": ``{verb: "close",
    reason: "declined"}`` on the session channel."""
    return (
        kind == "session"
        and payload.get("verb") == "close"
        and payload.get("reason") == "declined"
    )


def _target_permission(target: dict) -> str:
    """The permission a game with *target* takes: ``people`` for someone
    in this household, ``federation`` for anyone in another one."""
    return PERMISSION_PEOPLE if target.get("is_local") else PERMISSION_FEDERATION


class AppFederationService(ProtectionGateMixin):
    """Bridge between the federation layer and Social Home Apps.

    Constructor parameters are injected; no I/O of its own beyond what the
    injected services provide.

    Parameters
    ----------
    app_repo:
        Installed-apps registry — used to validate that an app exists
        and is enabled before any outbound or inbound delivery.
    user_repo:
        Local user directory — used to enumerate user ids for WebSocket
        fan-out on inbound messages.
    ws:
        WebSocket manager — delivers ``app.message`` frames to all open
        connections.
    federation:
        The core :class:`FederationService` — used for outbound sends
        (``send_event`` for session control, ``send_app_message`` for
        data messages).
    federation_repo:
        Federation peer directory — used by :meth:`list_peers`.
    """

    __slots__ = (
        "_app_repo",
        "_bus",
        "_child_protection",
        "_cp_repo",
        "_federation",
        "_federation_repo",
        "_seen_open_sessions",
        "_user_repo",
        "_ws",
    )

    def __init__(
        self,
        *,
        app_repo: AbstractAppRepo,
        user_repo: AbstractUserRepo,
        ws: WebSocketManager,
        federation: Any,
        federation_repo: AbstractFederationRepo,
        cp_repo: AbstractCpRepo | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self._app_repo = app_repo
        self._user_repo = user_repo
        self._ws = ws
        self._federation = federation
        self._federation_repo = federation_repo
        self._cp_repo = cp_repo
        self._bus = bus
        self._child_protection = None
        #: Bounded LRU of inbound ``APP_SESSION`` open ``session_id``s already
        #: handled — guards against a double federation delivery (WebRTC +
        #: HTTPS fallback / retransmit) seating two invites + two notifications
        #: for the same challenge. Scoped to inbound only; outbound
        #: ``open_session`` always mints a fresh uuid so it never dedupes.
        self._seen_open_sessions: OrderedDict[tuple[str, str], None] = OrderedDict()

    async def _emit(self, event: DomainEvent) -> None:
        """Publish ``event`` fail-soft; no-op without a bus.

        Mirrors :meth:`socialhome.services.bus_publisher.BusPublisherMixin._emit`
        but additionally swallows + logs any publish error: a notification /
        bus failure must NEVER break the WebSocket delivery of an app frame.
        """
        if self._bus is None:
            return
        try:
            await self._bus.publish(event)
        except Exception as exc:  # noqa: BLE001 — fail-soft publish
            log.warning("app_federation: bus publish failed: %s", exc)

    # ─── Public API ───────────────────────────────────────────────────────────

    async def list_peers(self) -> list[dict]:
        """Return confirmed instances as ``[{instance_id, display_name}]``.

        The SPA uses this to populate the peer picker when an app wants
        to start a cross-household session.
        """
        # §D2b: social surface — a ``space_session`` row (a household we
        # only share a space with, via an invite link) is not a social
        # peer, so read the social list, not every CONFIRMED row.
        instances = await self._federation_repo.list_social_instances()
        return [
            {
                "instance_id": inst.id,
                "display_name": inst.effective_display_name,
            }
            for inst in instances
        ]

    async def list_contacts(self, *, self_user_id: str) -> list[dict]:
        """Return the set of people a user can challenge to an app session.

        This is the same roster that DMs and the ``/friends`` page expose:
        members of paired households, minus personal blocks.  The per-peer
        hide-list is applied upstream at pairing time, so hidden members
        never reach ``remote_users`` in the first place.

        Includes:
        * All active local household members (excluding the caller).
        * All known remote users across every paired household.

        Blocked contacts are excluded using the same mechanism as
        ``/friends``: ``list_blocked(self_user_id)`` returns
        ``[(blocked_user_id, blocked_at), ...]`` and users whose
        ``user_id`` is in that set are dropped from both the local and
        remote populations.

        Each contact is a dict with keys:
        ``instance_id``, ``user_ref``, ``display_name``, ``is_local``,
        ``online``, ``person_name`` and ``household_name``.

        Two households can each have an "Admin" or a "Ben": a person from
        another household reads "Ben · Familie Okafor" in ``display_name``
        (:func:`with_household`), so an app's picker tells them apart
        without knowing about households. ``person_name`` is the bare name
        and ``household_name`` the household's (``None`` for your own).

        Remote online presence is not yet wired in — ``online`` is always
        ``False`` for remote contacts (deferred to a future wiring task).
        """
        own = self._federation.own_instance_id
        connected = self._ws.connected_users()

        # Personal blocks (§Privacy) — same filter as /friends and DM roster.
        blocked_ids = {
            uid for uid, _ in await self._user_repo.list_blocked(self_user_id)
        }
        # §CP.F2: nobody a guardian block separates from the caller (either
        # way, local or remote) — same as calls and DMs.
        blocked_ids |= await self._guardian_block_counterparts(self_user_id)

        out: list[dict] = []

        for u in await self._user_repo.list_active():
            if u.user_id == self_user_id:
                continue
            if u.user_id in blocked_ids:
                continue
            out.append(
                {
                    "instance_id": own,
                    "user_ref": u.user_id,
                    "display_name": u.display_name,
                    "is_local": True,
                    "online": u.user_id in connected,
                    "person_name": u.display_name,
                    "household_name": None,
                }
            )

        households: dict[str, str | None] = {}
        for r in await self._user_repo.list_all_known_remote():
            if r.user_id in blocked_ids:
                continue
            if r.instance_id not in households:
                households[r.instance_id] = await self._household_name(r.instance_id)
            household = households[r.instance_id]
            person = r.display_name or r.remote_username
            out.append(
                {
                    "instance_id": r.instance_id,
                    "user_ref": r.remote_username,
                    "display_name": with_household(person, household),
                    "person_name": person,
                    "household_name": household,
                    "is_local": False,
                    "online": await self._remote_online(
                        r.instance_id, r.remote_username
                    ),
                }
            )

        return out

    async def open_session(
        self,
        *,
        app_id: str,
        target: dict,
        actor_user_id: str,
    ) -> str:
        """Open an app session with a specific *person* (local or remote).

        ``target`` is ``{"instance_id": str, "user_ref": str, "is_local":
        bool}`` as returned by :meth:`list_contacts`:

        * ``is_local`` — the target lives on this household; ``user_ref`` is
          a local ``user_id``.  We deliver the ``kind="session"`` open frame
          straight to the target over WebSocket — no federation send, no
          fan-out to every local user, no echo to the initiator.
        * remote — ``instance_id`` is the target household and ``user_ref``
          is the target's remote username on that household.  We send an
          ``APP_SESSION`` event there.

        Validates that the app is installed and enabled and that the actor
        passes the age gate, allocates a ``session_id``, and returns it so
        the caller can hand it back to the SPA.

        §FIX-I2 (relaxed, proto v_18): per-user identity on the wire
        (``to_user`` / ``from_user``) is now permitted because the
        challengeable roster is exactly the pairing-scoped DM / ``/friends``
        set — a consensual, not covert, audience.  ``to_user`` lets the peer
        route the open to the addressed person instead of fanning to every
        local user; ``from_user`` is the initiator's username so the
        recipient can show who challenged them.  Both fields are gated on
        :data:`FederationCapability.MIN_FOR_APP_USER_ROUTING` and are omitted
        for sub-v_18 peers, which fall back to the legacy household-addressed
        fan-out (no ``to_user``).  Session control always rides the JSON
        event path regardless of peer version — the binary channel is only
        used for :meth:`send_message`.
        """
        await self._require_enabled(app_id, target)
        await self._assert_age_allowed(app_id, actor_user_id)
        await self._assert_target_allowed(actor_user_id, target)
        session_id = uuid4().hex
        own = self._federation.own_instance_id

        if target.get("is_local"):
            # Local loopback — deliver the open frame to the addressed person
            # only: never a fan-out to all local users, and never an echo to
            # the initiator — an invite is for the invitee, and the
            # initiator's app has the session id from this call (an echo
            # raced that reply and read as an invite from themselves).
            recipients = await self._age_filter_recipients(app_id, [target["user_ref"]])
            await self._emit_frame(
                recipients,
                app_id=app_id,
                session_id=session_id,
                from_instance=own,
                from_user=actor_user_id,
                kind="session",
                payload={
                    "app_id": app_id,
                    "session_id": session_id,
                    "verb": "open",
                },
            )
            # Raise a bell row + push for the *target only* (not the
            # initiator) — but only if the target survived the age filter.
            # Keep the invite for them like a remote one, so it waits until
            # their app takes it in even if it isn't open now.
            if target["user_ref"] in recipients:
                await self._app_repo.add_pending_session(
                    AppPendingSession(
                        app_id=app_id,
                        user_id=target["user_ref"],
                        session_id=session_id,
                        from_instance=own,
                        from_user=actor_user_id,
                        payload={
                            "app_id": app_id,
                            "session_id": session_id,
                            "verb": "open",
                        },
                        created_at=datetime.now(UTC).isoformat(),
                    )
                )
                initiator = await self._user_repo.get_by_user_id(actor_user_id)
                from_display = (
                    initiator.display_name if initiator is not None else actor_user_id
                )
                await self._emit(
                    AppChallengeReceived(
                        app_id=app_id,
                        session_id=session_id,
                        to_user_id=target["user_ref"],
                        from_display=from_display,
                        from_user_id=actor_user_id,
                    )
                )
            await self._know_game(app_id, actor_user_id, session_id, own, target)
            return session_id

        # Remote — address the peer household; include per-user routing fields
        # only when the peer is v_18+ (else legacy household fan-out).
        payload: dict[str, Any] = {
            "app_id": app_id,
            "session_id": session_id,
            "verb": "open",
        }
        supported = await self._federation.peer_supports(
            target["instance_id"],
            min_version=FederationCapability.MIN_FOR_APP_USER_ROUTING,
        )
        if supported:
            me = await self._user_repo.get_by_user_id(actor_user_id)
            from_user = me.username if me is not None else actor_user_id
            payload["to_user"] = target["user_ref"]
            payload["from_user"] = from_user
        await self._federation.send_event(
            to_instance_id=target["instance_id"],
            event_type=FederationEventType.APP_SESSION,
            payload=payload,
        )
        await self._know_game(
            app_id, actor_user_id, session_id, target["instance_id"], target
        )
        return session_id

    async def send_message(
        self,
        *,
        app_id: str,
        target: dict,
        session_id: str,
        payload: dict,
        actor_user_id: str,
    ) -> None:
        """Send an app-layer message to a specific *person* (local or remote).

        Mirrors :meth:`open_session`: ``target`` is
        ``{"instance_id": str, "user_ref": str, "is_local": bool}`` as
        returned by :meth:`list_contacts`.

        * ``is_local`` — the target lives on this household; ``user_ref`` is a
          local ``user_id``.  We deliver a ``kind="message"`` frame straight
          to the target *and* the initiator over WebSocket (age-filtered,
          de-duped) — no federation send, no fan-out to every local user.
        * remote — delegate to :meth:`FederationService.send_app_message`,
          which selects the binary ``fed-app-v1`` channel (v_17+ confirmed
          peers) or falls back to the JSON ``APP_MESSAGE`` federation event
          path — in both cases the ``payload`` dict is AES-256-GCM-sealed and
          never sent in plaintext.

        Per-user routing (``to_user`` / ``from_user``) is gated on
        :data:`FederationCapability.MIN_FOR_APP_USER_ROUTING` (v_18) and rides
        both paths, sealed: the JSON ``APP_MESSAGE`` payload or the binary
        frame's envelope metadata (see
        :meth:`FederationService.send_app_message`).
        """
        app = await self._require_enabled(app_id, target)
        await self._assert_age_allowed(app_id, actor_user_id, app=app)
        own = self._federation.own_instance_id
        peer_instance = own if target.get("is_local") else str(target["instance_id"])
        # O(1) per move (C#31): a game this household knows the actor plays
        # with this very person needs no roster scan — only the checks the
        # roster would have applied to that one person.
        known = (
            await self._app_repo.get_pending_session(app_id, actor_user_id, session_id)
            if target.get("user_ref")
            else None
        )
        if known is not None and known.is_with(peer_instance, target["user_ref"]):
            await self._assert_person_allowed(actor_user_id, target)
        else:
            await self._assert_target_allowed(actor_user_id, target)
        await self._mark_played(app_id, actor_user_id, session_id, known, target)

        if target.get("is_local"):
            # Local loopback — deliver the message frame to the addressed
            # person and the initiator only, never a fan-out to all local users.
            recipients = await self._age_filter_recipients(
                app_id, [target["user_ref"], actor_user_id], app=app
            )
            await self._emit_frame(
                recipients,
                app_id=app_id,
                session_id=session_id,
                from_instance=own,
                from_user=actor_user_id,
                kind="message",
                payload=payload,
            )
            if target["user_ref"] in recipients and target["user_ref"] != actor_user_id:
                await self._hold(
                    app_id,
                    target["user_ref"],
                    session_id,
                    from_instance=own,
                    from_user=actor_user_id,
                    kind="message",
                    payload=payload,
                )
            return

        # Remote — include per-user routing fields only for v_18+ peers.
        to_user: str | None = None
        from_user: str | None = None
        if await self._federation.peer_supports(
            target["instance_id"],
            min_version=FederationCapability.MIN_FOR_APP_USER_ROUTING,
        ):
            me = await self._user_repo.get_by_user_id(actor_user_id)
            from_user = me.username if me is not None else actor_user_id
            to_user = target["user_ref"]
        await self._federation.send_app_message(
            to_instance_id=target["instance_id"],
            app_id=app_id,
            session_id=session_id,
            payload=payload,
            to_user=to_user,
            from_user=from_user,
        )

    # ─── Inbound federation dispatch ─────────────────────────────────────────

    async def on_inbound_event(self, event: FederationEvent) -> None:
        """Handle an inbound ``APP_SESSION`` or ``APP_MESSAGE`` JSON event.

        Called by the :class:`FederationService` event registry after the
        §24.11 pipeline has validated and decrypted the envelope.  Extracts
        the app-specific fields and fans out via :meth:`_deliver`.
        """
        if not isinstance(event.payload, dict):
            log.debug("app_federation: inbound event with non-dict payload — dropping")
            return
        app_id = event.payload.get("app_id")
        session_id = event.payload.get("session_id")
        if not app_id or not session_id:
            log.debug(
                "app_federation: inbound event missing app_id/session_id — dropping"
            )
            return

        if event.event_type is FederationEventType.APP_SESSION:
            # Pass the whole payload as the session info dict.
            app_payload: dict = dict(event.payload)
            kind = "session"
            # Idempotency: suppress a re-delivered open (WebRTC + HTTPS
            # fallback / retransmit) so we never seat two invites or raise two
            # notifications for the same challenge.  Scoped to opens — a
            # future ``verb`` ("close") is not a session-creating event.
            if event.payload.get("verb") == "open" and self._seen_open_session(
                event.from_instance, str(session_id)
            ):
                log.debug(
                    "app_federation: duplicate inbound open for session %r — skipping",
                    session_id,
                )
                return
        else:
            # APP_MESSAGE: the application data is nested under "data".
            app_payload = event.payload.get("data") or {}
            kind = "message"

        # Per-user routing (v_18+): a non-empty ``to_user`` addresses one local
        # user.  An empty string is the legacy household-addressed open (a
        # back-compat open maps ``user_ref`` → "") — treat it as absent so it
        # falls back to the household fan-out and is never looked up.
        raw_to_user = event.payload.get("to_user")
        to_user = raw_to_user if isinstance(raw_to_user, str) and raw_to_user else None

        raw_from_user = event.payload.get("from_user")
        from_user = (
            raw_from_user if isinstance(raw_from_user, str) and raw_from_user else None
        )
        # A household that addresses people (v_18+) always names the user on
        # the JSON path; the whole-household fan-out is only for older peers.
        if to_user is None and await self._federation.peer_supports(
            event.from_instance,
            min_version=FederationCapability.MIN_FOR_APP_USER_ROUTING,
        ):
            log.warning(
                "app_federation: %s from %s refused — no addressee (session=%s)",
                kind,
                event.from_instance,
                session_id,
            )
            return

        await self._deliver(
            app_id,
            session_id,
            from_instance=event.from_instance,
            payload=app_payload,
            kind=kind,
            to_user=to_user,
            from_user=from_user,
            notify_open=(kind == "session" and event.payload.get("verb") == "open"),
            withdraw=(kind == "session" and event.payload.get("verb") == "close"),
        )

    async def on_inbound_message(
        self,
        instance_id: str,
        app_id: str,
        session_id: str,
        payload: dict,
        *,
        to_user: str | None = None,
        from_user: str | None = None,
    ) -> None:
        """Handle an inbound binary app frame from ``fed-app-v1``.

        Called by :meth:`FederationService._app_inbound_handler` after the
        §24.11 pipeline validates the envelope and the payload is decrypted.
        ``to_user`` / ``from_user`` come from the frame's sealed metadata
        (v_18+ senders); routed exactly like the JSON ``APP_MESSAGE``. A
        frame without them (an older sender) keeps the household fan-out and
        the app disambiguates by ``session_id``.
        """
        await self._deliver(
            app_id,
            session_id,
            from_instance=instance_id,
            payload=payload,
            kind="message",
            to_user=to_user or None,
            from_user=from_user or None,
        )

    # ─── Internal helpers ─────────────────────────────────────────────────────

    def _seen_open_session(self, from_instance: str, session_id: str) -> bool:
        """Return whether ``session_id`` from ``from_instance`` was already
        handled inbound.

        Keyed per sending household, so one household can never pre-empt
        another's open by sending the same session id first.

        First sight records it (LRU, capped at
        :data:`_SEEN_OPEN_SESSIONS_MAX`, oldest evicted) and returns ``False``;
        a repeat returns ``True`` so the caller can skip both delivery and the
        challenge publish. Synchronous — the OrderedDict mutation is atomic
        under the single-threaded asyncio loop.
        """
        key = (from_instance, session_id)
        if key in self._seen_open_sessions:
            return True
        self._seen_open_sessions[key] = None
        if len(self._seen_open_sessions) > _SEEN_OPEN_SESSIONS_MAX:
            self._seen_open_sessions.popitem(last=False)
        return False

    async def _resolve_remote_initiator(
        self, from_instance: str, from_user: str | None
    ) -> RemoteUser | None:
        """Resolve the remote challenge initiator's local ``RemoteUser`` row.

        Returns ``None`` when ``from_user`` is absent or the remote can't be
        found (legacy/unknown sender) — callers treat that as "can't enforce a
        block, deliver as before" (fail-soft).
        """
        if not from_user:
            return None
        return await self._user_repo.get_remote_by_member(from_instance, from_user)

    async def _remote_open_display(
        self,
        from_instance: str,
        from_user: str | None,
        *,
        remote: RemoteUser | None = None,
    ) -> str:
        """Resolve a human label for a remote challenge initiator.

        Order: the remote user's federated ``display_name`` with their
        household (``"Ben · Familie Okafor"``, :func:`with_household`, when
        ``from_user`` is known) → the peer household's display name → the
        raw ``from_instance`` id as a last resort. Never a stable
        cross-household id beyond the fallback. ``remote`` lets a caller
        that already resolved the initiator (e.g. for a block check) pass
        the row to avoid a second lookup.
        """
        if remote is None and from_user:
            remote = await self._user_repo.get_remote_by_member(
                from_instance, from_user
            )
        household = await self._household_name(from_instance)
        if remote is not None and remote.display_name:
            return with_household(remote.display_name, household)
        return household or from_instance

    async def _household_name(self, instance_id: str) -> str | None:
        """A paired household's name as this household shows it (its local
        alias, else its own display name), or ``None`` if unknown."""
        inst = await self._federation_repo.get_instance(instance_id)
        if inst is None:
            return None
        return inst.effective_display_name.strip() or None

    async def _remote_online(self, instance_id: str, remote_username: str) -> bool:
        """Return whether a remote user has a live session.

        Remote presence is not yet wired into this service — always returns
        ``False`` (fail-soft).  A future task will plumb the presence service
        so cross-household online state can be surfaced in the contact picker.
        """
        _ = instance_id, remote_username  # reserved for future wiring
        return False

    async def _require_enabled(
        self, app_id: str, target: dict | None = None
    ) -> InstalledApp:
        """The installed, enabled app — else raise :class:`AppNotFoundError`
        or :class:`AppNotEnabledError`.

        With *target* (a game with that person), the app must also have the
        permission it takes (:func:`_target_permission`) — else
        :class:`~socialhome.domain.apps.AppPermissionDeniedError`, before
        anything is sent."""
        app = await self._app_repo.get(app_id)
        if app is None:
            raise AppNotFoundError(app_id)
        if not app.enabled:
            raise AppNotEnabledError(app_id)
        if target is not None:
            app.require_permission(_target_permission(target))
        return app

    async def _assert_age_allowed(
        self, app_id: str, actor_user_id: str, *, app: InstalledApp | None = None
    ) -> None:
        """Raise :class:`AppAgeRestrictedError` for protected minors below min_age.

        Fail-closed: if the app is not found or has no age restriction,
        this is a no-op.  Only blocks when the app exists, has a min_age > 0,
        cp_repo is configured, and the user's protection record shows a
        declared_age below the threshold.
        """
        if self._cp_repo is None:
            return
        if app is None:
            app = await self._app_repo.get(app_id)
        if app is None or app.min_age <= 0:
            return
        p = await self._cp_repo.get_user_protection(actor_user_id)
        if p is None:
            return
        if not p.get("child_protection_enabled"):
            return
        declared = int(p.get("declared_age") or 0)
        if declared < app.min_age:
            raise AppAgeRestrictedError(
                f"This app is restricted to ages {app.min_age}+."
            )

    async def _assert_target_allowed(self, actor_user_id: str, target: dict) -> None:
        """Raise :class:`AppContactNotFoundError` if ``target`` is not a contact.

        Closes the Task-5 authorization gap: without this check a crafted
        ``target`` could address an arbitrary local user or remote household
        the actor has no relationship with.  The allowed set is built from the
        *same* source as :meth:`list_contacts` — paired-household members minus
        personal blocks — so the block-list is honoured for free.

        A legacy household-addressed send carries ``user_ref == ""`` (the
        back-compat mapping in the route layer); it predates per-person
        addressing and is allowed through so sub-v_18 peers keep working — the
        household fan-out on the receiver side already scopes by membership.
        """
        if not target.get("user_ref"):
            # Legacy household-addressed path (user_ref == "") — exempt from
            # the roster check, but it reaches everyone there: not across a
            # guardian block of the actor (§CP.F2).
            if await self._guardian_blocks_household(
                actor_user_id, str(target.get("instance_id") or "")
            ):
                raise AppContactNotFoundError(
                    "target is not a contact of the requesting user"
                )
            return
        contacts = await self.list_contacts(self_user_id=actor_user_id)
        key = (
            target.get("instance_id"),
            target.get("user_ref"),
            bool(target.get("is_local")),
        )
        for c in contacts:
            if (c["instance_id"], c["user_ref"], c["is_local"]) == key:
                break
        else:
            raise AppContactNotFoundError(
                "target is not a contact of the requesting user"
            )
        # A local person who blocked the actor — as a remote recipient's
        # household drops a blocked initiator's frames on receipt. Same
        # answer as a non-contact, so the block isn't revealed.
        if target.get("is_local") and await self._user_repo.is_blocked(
            str(target["user_ref"]), actor_user_id
        ):
            raise AppContactNotFoundError(
                "target is not a contact of the requesting user"
            )

    async def _assert_person_allowed(self, actor_user_id: str, target: dict) -> None:
        """:meth:`_assert_target_allowed` for one person, without the
        roster scan: raise :class:`AppContactNotFoundError` unless *target*
        is someone :meth:`list_contacts` would list for the actor (an
        active member here, or a known remote user), neither blocked by
        the actor nor — locally — blocking them, and no guardian block
        between them. A handful of primary-key reads."""
        ref = str(target.get("user_ref") or "")
        refused = AppContactNotFoundError(
            "target is not a contact of the requesting user"
        )
        if target.get("is_local"):
            user = await self._user_repo.get_by_user_id(ref)
            if user is None or not user.is_active() or ref == actor_user_id:
                raise refused
            other = ref
            if await self._user_repo.is_blocked(other, actor_user_id):
                raise refused
        else:
            remote = await self._user_repo.get_remote_by_member(
                str(target.get("instance_id") or ""), ref
            )
            if remote is None or remote.deprovisioned_at is not None:
                raise refused
            other = remote.user_id
        if await self._user_repo.is_blocked(
            actor_user_id, other
        ) or await self._guardian_blocked(actor_user_id, other):
            raise refused

    async def _know_game(
        self,
        app_id: str,
        user_id: str,
        session_id: str,
        peer_instance: str,
        target: dict,
    ) -> None:
        """*user_id* plays *session_id* with *target*: keep the session
        known for them (an ``active`` row), so the other side's moves wait
        in their mailbox and their own moves skip the roster scan. A
        household-addressed (pre-v_18) session names nobody: not kept."""
        if not target.get("user_ref"):
            return
        now = datetime.now(UTC).isoformat()
        await self._app_repo.add_active_session(
            AppPendingSession(
                app_id=app_id,
                user_id=user_id,
                session_id=session_id,
                from_instance=peer_instance,
                from_user=str(target["user_ref"]),
                payload={"app_id": app_id, "session_id": session_id, "verb": "open"},
                created_at=now,
                state=SESSION_ACTIVE,
                updated_at=now,
            )
        )

    async def _mark_played(
        self,
        app_id: str,
        user_id: str,
        session_id: str,
        known: AppPendingSession | None,
        target: dict,
    ) -> None:
        """*user_id* sent a message in *session_id*: an invite that waited
        for them is taken in (they play it), a game from before the mailbox
        becomes known, and a known game's TTL moves on (at most hourly)."""
        if known is None:
            own = self._federation.own_instance_id
            peer = own if target.get("is_local") else str(target["instance_id"])
            await self._know_game(app_id, user_id, session_id, peer, target)
            return
        if known.state == SESSION_INVITED:
            await self._took_in_by_playing(app_id, user_id, session_id)
            return
        now = datetime.now(UTC)
        if known.last_change < (now - _TOUCH_EVERY).isoformat():
            await self._app_repo.touch_session(
                app_id, user_id, session_id, now=now.isoformat()
            )

    async def _hold(
        self,
        app_id: str,
        user_id: str,
        session_id: str,
        *,
        from_instance: str,
        from_user: str | None,
        kind: str,
        payload: dict,
    ) -> bool:
        """The mailbox (D11): keep *payload* as the latest message of
        *session_id* for *user_id* until their app opens — only for a
        session known for them whose other side is ``from_instance`` /
        ``from_user``, and only up to :data:`HELD_MESSAGE_MAX_BYTES`.
        ``True`` when kept."""
        if not from_user:
            return False
        held = {"kind": kind, "payload": payload}
        if len(orjson.dumps(held)) > HELD_MESSAGE_MAX_BYTES:
            log.info(
                "app_federation: %s for session %s too large to hold — live only",
                kind,
                session_id,
            )
            return False
        return await self._app_repo.hold_message(
            app_id,
            user_id,
            session_id,
            from_instance=from_instance,
            from_user=from_user,
            held=held,
            now=datetime.now(UTC).isoformat(),
        )

    async def _emit_frame(
        self,
        recipient_user_ids: list[str],
        *,
        app_id: str,
        session_id: str,
        from_instance: str,
        from_user: str,
        kind: str,
        payload: dict,
    ) -> None:
        """Push an ``app.message`` frame to each recipient over WebSocket.

        Builds the frame once and delivers it per-user via
        :meth:`WebSocketManager.broadcast_to_user` — never a fan-out to all
        local users.  ``from_user`` rides the frame so the recipient's SPA can
        show who initiated the session / sent the message.  ``kind`` is
        ``"session"`` for an open frame, ``"message"`` for an in-session
        message; ``payload`` is the app-specific dict the SPA relays into the
        iframe.  Reused by the local-loopback open and message paths.
        """
        frame = {
            "type": "app.message",
            "app_id": app_id,
            "session_id": session_id,
            "from_instance": from_instance,
            "kind": kind,
            "from_user": from_user,
            "payload": payload,
        }
        for uid in recipient_user_ids:
            await self._ws.broadcast_to_user(uid, frame)

    async def _age_filter_recipients(
        self, app_id: str, user_ids: list[str], *, app: InstalledApp | None = None
    ) -> list[str]:
        """Drop protected minors below the app's ``min_age`` from ``user_ids``.

        Fast path returns the input unchanged when the app has no age
        restriction or no ``cp_repo`` is configured.  Preserves order and
        de-duplicates while keeping first occurrence.
        """
        # De-dupe preserving order.
        seen: set[str] = set()
        ordered: list[str] = []
        for uid in user_ids:
            if uid not in seen:
                seen.add(uid)
                ordered.append(uid)

        if app is None:
            app = await self._app_repo.get(app_id)
        if app is None or app.min_age <= 0 or self._cp_repo is None:
            return ordered

        allowed: list[str] = []
        for uid in ordered:
            p = await self._cp_repo.get_user_protection(uid)
            if p is None or not p.get("child_protection_enabled"):
                allowed.append(uid)
                continue
            declared = int(p.get("declared_age") or 0)
            if declared >= app.min_age:
                allowed.append(uid)
        return allowed

    async def _deliver(
        self,
        app_id: str,
        session_id: str,
        *,
        from_instance: str,
        payload: dict,
        kind: str,
        to_user: str | None = None,
        from_user: str | None = None,
        notify_open: bool = False,
        withdraw: bool = False,
    ) -> None:
        """Route an inbound app message to local users via WebSocket.

        Silently drops (debug log) if the app is not installed or disabled —
        the peer doesn't need to know we don't have this app.

        Parameters
        ----------
        kind:
            ``"session"`` for ``APP_SESSION`` control events,
            ``"message"`` for ``APP_MESSAGE`` and binary ``fed-app-v1``
            data frames.  Forwarded to the SPA so the bridge can relay
            it into the iframe, letting apps distinguish invites from
            in-game moves and route by session.
        to_user:
            Per-user routing hint (v_18+).  When a non-empty string that
            resolves to a local user (by username — the recipient's local
            username equals their ``remote_username`` on their home instance),
            deliver **only** to that user.  When ``None``, empty, or
            unresolvable, fall back to the legacy/best-effort household
            fan-out to every (age-eligible) local user.
        from_user:
            The remote initiator's username. Put on the WS frame as
            ``from_user`` once it resolves to a known user of
            ``from_instance`` (always on the per-user path, which refuses an
            unknown initiator); also resolves the ``from_display`` label on
            the published :class:`AppChallengeReceived`.
        notify_open:
            ``True`` only for an inbound ``APP_SESSION`` *open* — when the
            event also resolves to a single local recipient, raise a bell row +
            push for that recipient. Never set for messages or the legacy
            household fan-out (no specific recipient to notify).
        withdraw:
            ``True`` only for an inbound ``APP_SESSION`` *close* addressed to
            one person: the invite it ends stops waiting — only when the same
            household and person opened it.

        The mailbox (D11): a move or a close for one person is also kept
        as the latest message of that session for them (:meth:`_hold`) —
        only for a session this household knows for them with that very
        sender — and that holds while the app is turned off too (no live
        frame then). An invite to a turned-off app, and anything for the
        whole household, is dropped as before.
        """
        app = await self._app_repo.get(app_id)
        if app is None or (not app.enabled and (notify_open or to_user is None)):
            log.debug(
                "app_federation: dropping inbound for app %r (not installed/enabled)",
                app_id,
            )
            return
        if PERMISSION_FEDERATION not in app.permissions:
            # The household installed this app without "sends to other
            # households": another household's copy can't reach it either.
            log.debug(
                "app_federation: dropping inbound for app %r (no federation permission)",
                app_id,
            )
            return

        frame: dict[str, Any] = {
            "type": "app.message",
            "app_id": app_id,
            "session_id": session_id,
            "from_instance": from_instance,
            "kind": kind,
            "payload": payload,
        }

        # Per-user routing: deliver only to the addressed local user when it
        # resolves.  An empty/absent to_user is already mapped to None by the
        # caller (legacy household fan-out) and is never looked up.
        if to_user is not None:
            recipient = await self._user_repo.get(to_user)
            if recipient is None:
                log.warning(
                    "app_federation: %s from %s refused — addressee %r is not "
                    "a user here (session=%s)",
                    kind,
                    from_instance,
                    to_user,
                    session_id,
                )
                return
            if recipient is not None:
                # Recipient block enforcement (symmetric with DMs,
                # ``dm_service._guard_block_pair``): if the recipient has
                # blocked the remote initiator, drop both the WS delivery and
                # the challenge notification. Fail-soft — when the initiator
                # can't be resolved (no ``from_user`` / unknown remote) we
                # deliver as before (legacy/unknown sender).
                initiator = await self._resolve_remote_initiator(
                    from_instance, from_user
                )
                # A per-user delivery names its initiator (v_18+ senders
                # always ship ``to_user`` with ``from_user``): it must be a
                # user of the sending household, so the recipient's block
                # list can always be applied.
                if initiator is None:
                    log.warning(
                        "app_federation: per-user %s from %s refused — "
                        "initiator %r is not a user of that household "
                        "(session=%s)",
                        kind,
                        from_instance,
                        from_user,
                        session_id,
                    )
                    return
                # Name the sender on the frame, like the same-household path:
                # ``from_instance`` is the signature-proven household and the
                # initiator just resolved as one of its users. The username
                # itself is that household's claim (it vouches for its own
                # people); it is the ``user_ref`` the app answers them by.
                frame["from_user"] = from_user
                if withdraw:
                    await self._withdrawn(
                        app_id,
                        recipient.user_id,
                        session_id,
                        from_instance=from_instance,
                        from_user=from_user,
                    )
                if await self._user_repo.is_blocked(
                    recipient.user_id, initiator.user_id
                ) or await self._guardian_blocked(recipient.user_id, initiator.user_id):
                    log.info(
                        "app_federation: dropping inbound %s for blocked initiator "
                        "(recipient=%s initiator=%s session=%s)",
                        kind,
                        recipient.user_id,
                        initiator.user_id,
                        session_id,
                    )
                    return
                allowed = await self._age_filter_recipients(
                    app_id, [recipient.user_id], app=app
                )
                log.info(
                    "app_federation deliver app=%s session=%s kind=%s routed=%s recipients=%d",
                    app_id,
                    session_id,
                    kind,
                    "user",
                    len(allowed),
                )
                if allowed:
                    if notify_open:
                        # Keep the invite until the recipient plays it ("Play"
                        # in Needs you hands it to the app as the launch
                        # context). Stored UNCONDITIONALLY (not gated on
                        # whether broadcast_to_user reached any sockets): a live
                        # SPA socket does not imply the app is mounted. The
                        # 14-day TTL + hourly prune bound growth.
                        await self._app_repo.add_pending_session(
                            AppPendingSession(
                                app_id=app_id,
                                user_id=recipient.user_id,
                                session_id=session_id,
                                from_instance=from_instance,
                                from_user=from_user,
                                payload=payload,
                                created_at=datetime.now(UTC).isoformat(),
                            )
                        )
                    else:
                        held = await self._hold(
                            app_id,
                            recipient.user_id,
                            session_id,
                            from_instance=from_instance,
                            from_user=from_user,
                            kind=kind,
                            payload=payload,
                        )
                        if held and _is_decline(kind, payload):
                            await self._emit(
                                AppChallengeDeclined(
                                    app_id=app_id,
                                    session_id=session_id,
                                    to_user_id=recipient.user_id,
                                    from_display=await self._remote_open_display(
                                        from_instance, from_user, remote=initiator
                                    ),
                                    from_user_id=initiator.user_id,
                                )
                            )
                    if app.enabled:
                        await self._ws.broadcast_to_user(recipient.user_id, frame)
                    if notify_open:
                        from_display = await self._remote_open_display(
                            from_instance, from_user, remote=initiator
                        )
                        await self._emit(
                            AppChallengeReceived(
                                app_id=app_id,
                                session_id=session_id,
                                to_user_id=recipient.user_id,
                                from_display=from_display,
                                from_user_id=initiator.user_id,
                            )
                        )
                return

        # Legacy / best-effort fan-out to every active, age-eligible local user
        # (soft-deleted/inactive accounts are excluded — same as /friends).
        users = await self._user_repo.list_active()
        user_ids = [u.user_id for u in users]
        if not user_ids:
            return

        # Recipients who blocked the named initiator never get the frame
        # (same rule as the per-user path). An unnamed initiator (an older
        # sender) can't be checked.
        initiator = await self._resolve_remote_initiator(from_instance, from_user)
        if initiator is not None:
            # A named, known sender rides the frame (see the per-user path);
            # an unknown name is not passed on.
            frame["from_user"] = from_user
            user_ids = [
                uid
                for uid in user_ids
                if not await self._user_repo.is_blocked(uid, initiator.user_id)
                and not await self._guardian_blocked(uid, initiator.user_id)
            ]
        else:
            # §CP.F2: an unnamed sender could be anyone there — skip a
            # protected account whose guardian blocked someone on it.
            user_ids = [
                uid
                for uid in user_ids
                if not await self._guardian_blocks_household(uid, from_instance)
            ]

        # Age-gate filter: skip recipients who are protected minors below
        # the app's minimum age.  Fast path when the app has no restriction.
        user_ids = await self._age_filter_recipients(app_id, user_ids, app=app)

        if not user_ids:
            return

        log.info(
            "app_federation deliver app=%s session=%s kind=%s routed=%s recipients=%d",
            app_id,
            session_id,
            kind,
            "broadcast",
            len(user_ids),
        )
        await self._ws.broadcast_to_users(user_ids, frame)

    async def accept_session(
        self, *, app_id: str, session_id: str, actor_user_id: str
    ) -> AppPendingSession:
        """The invitee plays one game invite ("Play" in Needs you).

        Takes that one invite in — and only it: every other invite keeps
        waiting in "Needs you" (opening the app accepts nothing). Its row
        reads as accepted and the invite is returned, so the host can hand
        the game to the app as its launch context. The same one atomic
        step as a decline: a second device, a decline or a withdrawal
        racing it gets :class:`AppChallengeHandledError` — as does an
        invite past its TTL (row ``expired``) or one from someone a
        guardian block separates from the invitee since it arrived (row
        ``done``). A played invite stays known as an ``active`` game: the
        other side's moves wait in its mailbox from now on."""
        app = await self._require_enabled(app_id)
        await self._assert_age_allowed(app_id, actor_user_id, app=app)
        invite = await self._app_repo.activate_pending_session(
            app_id, actor_user_id, session_id, now=datetime.now(UTC).isoformat()
        )
        if invite is None:
            raise AppChallengeHandledError()
        outcome = "accepted"
        cutoff = datetime.now(UTC) - timedelta(seconds=PENDING_SESSION_TTL_SECONDS)
        if invite.created_at < cutoff.isoformat():
            outcome = "expired"
        else:
            # §CP.F2: an invite from someone a guardian block separates
            # from this user since it arrived is not handed to the app.
            sender = await self._invite_sender_id(invite)
            if sender is not None and await self._guardian_blocked(
                actor_user_id, sender
            ):
                outcome = "done"
        await self._emit(
            AppChallengeEnded(
                app_id=app_id,
                user_id=actor_user_id,
                session_ids=(session_id,),
                outcome=outcome,
            )
        )
        if outcome != "accepted":
            await self._app_repo.delete_pending_session(
                app_id, actor_user_id, session_id
            )
            raise AppChallengeHandledError()
        return invite

    async def _took_in_by_playing(
        self, app_id: str, user_id: str, session_id: str
    ) -> None:
        """The invitee sent a message in a game that still waited for them
        (an app that seats live invites itself, before launch contexts):
        they play it, so the invite is accepted and leaves "Needs you"."""
        taken = await self._app_repo.activate_pending_session(
            app_id, user_id, session_id, now=datetime.now(UTC).isoformat()
        )
        if taken is not None:
            await self._emit(
                AppChallengeEnded(
                    app_id=app_id,
                    user_id=user_id,
                    session_ids=(session_id,),
                    outcome="accepted",
                )
            )

    async def take_held(
        self, *, app_id: str, actor_user_id: str
    ) -> list[AppPendingSession]:
        """The person's app opened: the messages their mailbox kept (D11),
        the latest per session, oldest first — cleared as they are handed
        over. Each row's ``held`` is ``{kind, payload}`` and its
        ``from_instance`` / ``from_user`` name the sender. A message from
        someone the person blocked, or a guardian block now separates
        them from, is dropped instead (§CP.F2)."""
        app = await self._require_enabled(app_id)
        await self._assert_age_allowed(app_id, actor_user_id, app=app)
        out: list[AppPendingSession] = []
        for held in await self._app_repo.take_held(app_id, actor_user_id):
            sender = await self._invite_sender_id(held)
            if sender is not None and (
                await self._user_repo.is_blocked(actor_user_id, sender)
                or await self._guardian_blocked(actor_user_id, sender)
            ):
                continue
            out.append(held)
        return out

    async def _invite_sender_id(self, invite: AppPendingSession) -> str | None:
        """The user id of who opened *invite*: a local user id as stored, or
        the ``remote_users`` id of a remote challenger (``None`` unknown)."""
        if not invite.from_user:
            return None
        if invite.from_instance == self._federation.own_instance_id:
            return invite.from_user
        remote = await self._user_repo.get_remote_by_member(
            invite.from_instance, invite.from_user
        )
        return remote.user_id if remote is not None else None

    # ─── Game invites: decline / withdraw ────────────────────────────────

    async def decline_session(
        self, *, app_id: str, session_id: str, actor_user_id: str
    ) -> None:
        """The invitee declines a game invite that waits for them.

        The invite is removed in one step (a second device, the app taking
        it in or a withdrawal racing it: the loser gets
        :class:`AppChallengeHandledError`), its "Needs you" item reads as
        declined, and the challenger's app hears ``{verb: "close", reason:
        "declined"}`` — over WebSocket when they are on this household,
        else as an ``APP_SESSION`` to their household (best effort: the
        decline stands if that send fails). Nothing is sent for an app that
        is no longer enabled here."""
        invite = await self._app_repo.take_pending_session(
            app_id, actor_user_id, session_id
        )
        if invite is None:
            raise AppChallengeHandledError()
        await self._emit(
            AppChallengeEnded(
                app_id=app_id,
                user_id=actor_user_id,
                session_ids=(session_id,),
                outcome="declined",
            )
        )
        app = await self._app_repo.get(app_id)
        if app is None or not app.enabled or not invite.from_user:
            return
        if (
            invite.from_instance != self._federation.own_instance_id
            and PERMISSION_FEDERATION not in app.permissions
        ):
            # The invite is gone here; nothing leaves the household for an
            # app that may no longer send to other households.
            return
        # §CP.F2: an invite from before a guardian block is still declined
        # here, but nothing reaches the person on the other side of it.
        sender = await self._invite_sender_id(invite)
        if sender is not None and await self._guardian_blocked(actor_user_id, sender):
            return
        await self._send_close(
            app_id=app_id,
            session_id=session_id,
            actor_user_id=actor_user_id,
            to_instance=invite.from_instance,
            to_user=invite.from_user,
            reason="declined",
        )

    async def cancel_session(
        self, *, app_id: str, session_id: str, target: dict, actor_user_id: str
    ) -> None:
        """The challenger withdraws a game invite they sent to *target*.

        ``target`` is the same ``{instance_id, user_ref, is_local}`` the
        session was opened with. A local invitee's invite stops waiting
        here (only one this challenger opened); a remote invitee's
        household gets ``APP_SESSION {verb: "close", reason: "withdrawn"}``
        and does the same on its side. Either way the invitee's app hears
        the close."""
        await self._require_enabled(app_id, target)
        await self._assert_target_allowed(actor_user_id, target)
        own = self._federation.own_instance_id
        if target.get("is_local"):
            await self._withdrawn(
                app_id,
                target["user_ref"],
                session_id,
                from_instance=own,
                from_user=actor_user_id,
            )
        await self._send_close(
            app_id=app_id,
            session_id=session_id,
            actor_user_id=actor_user_id,
            to_instance=own if target.get("is_local") else target["instance_id"],
            to_user=target["user_ref"],
            reason="withdrawn",
        )

    async def _withdrawn(
        self,
        app_id: str,
        user_id: str,
        session_id: str,
        *,
        from_instance: str,
        from_user: str | None,
    ) -> None:
        """The challenger (``from_instance`` / ``from_user``) ended an
        invite that waited for ``user_id``: it stops waiting — only when
        they are who opened it."""
        if not from_user:
            return
        taken = await self._app_repo.take_pending_session(
            app_id,
            user_id,
            session_id,
            from_instance=from_instance,
            from_user=from_user,
        )
        if taken is not None:
            await self._emit(
                AppChallengeEnded(
                    app_id=app_id,
                    user_id=user_id,
                    session_ids=(session_id,),
                    outcome="withdrawn",
                )
            )

    async def _send_close(
        self,
        *,
        app_id: str,
        session_id: str,
        actor_user_id: str,
        to_instance: str,
        to_user: str,
        reason: str,
    ) -> None:
        """Tell the other side's app the session ended: ``{verb: "close",
        reason}``. On this household ``to_user`` is a user id and the frame
        goes over WebSocket; on another household it is their username and
        rides ``APP_SESSION`` (v_18+ households only — older ones can't
        address a person, and a close is not worth a household fan-out)."""
        payload: dict[str, Any] = {
            "app_id": app_id,
            "session_id": session_id,
            "verb": "close",
            "reason": reason,
        }
        own = self._federation.own_instance_id
        if to_instance == own:
            recipients = await self._age_filter_recipients(app_id, [to_user])
            await self._emit_frame(
                recipients,
                app_id=app_id,
                session_id=session_id,
                from_instance=own,
                from_user=actor_user_id,
                kind="session",
                payload=payload,
            )
            # The mailbox: the close waits for their app like a remote one.
            if (
                to_user in recipients
                and await self._hold(
                    app_id,
                    to_user,
                    session_id,
                    from_instance=own,
                    from_user=actor_user_id,
                    kind="session",
                    payload=payload,
                )
                and _is_decline("session", payload)
            ):
                me = await self._user_repo.get_by_user_id(actor_user_id)
                await self._emit(
                    AppChallengeDeclined(
                        app_id=app_id,
                        session_id=session_id,
                        to_user_id=to_user,
                        from_display=(
                            me.display_name if me is not None else actor_user_id
                        ),
                        from_user_id=actor_user_id,
                    )
                )
            return
        try:
            if not await self._federation.peer_supports(
                to_instance,
                min_version=FederationCapability.MIN_FOR_APP_USER_ROUTING,
            ):
                return
            me = await self._user_repo.get_by_user_id(actor_user_id)
            payload["to_user"] = to_user
            payload["from_user"] = me.username if me is not None else actor_user_id
            await self._federation.send_event(
                to_instance_id=to_instance,
                event_type=FederationEventType.APP_SESSION,
                payload=payload,
            )
        except Exception as exc:
            # The answer stands on this side; the other app just isn't told.
            log.warning(
                "app_federation: close (%s) for session %s to %s not sent: %s",
                reason,
                session_id,
                to_instance,
                exc,
                exc_info=True,
            )
