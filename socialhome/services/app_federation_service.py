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

Inbound routing: when an inbound JSON event carries a non-empty ``to_user``
that resolves to a local user, :meth:`_deliver` delivers only to that user;
otherwise (legacy/empty/unresolvable, and the binary ``fed-app-v1`` path which
carries no routing slot in v1) it falls back to the household fan-out and the
app's ``session_id`` scopes the semantics for the SPA.

Authorization: both outbound entry points call :meth:`_assert_target_allowed`,
which rejects (``AppContactNotFoundError``) any target that is not in the
actor's challengeable roster (the same block-aware set as
:meth:`list_contacts`).  Legacy household-addressed sends (``user_ref == ""``)
are exempt for back-compat.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from datetime import datetime, timezone
from uuid import uuid4
from typing import TYPE_CHECKING, Any

from ..domain.apps import (
    AppAgeRestrictedError,
    AppContactNotFoundError,
    AppNotEnabledError,
    AppNotFoundError,
    AppPendingSession,
)
from ..domain.events import AppChallengeReceived
from ..domain.federation import FederationEvent, FederationEventType
from ..domain.federation_capabilities import FederationCapability

if TYPE_CHECKING:
    from ..domain.events import DomainEvent
    from ..domain.user import RemoteUser
    from ..infrastructure.event_bus import EventBus
    from ..repositories.app_repo import AbstractAppRepo
    from ..repositories.cp_repo import AbstractCpRepo
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.user_repo import AbstractUserRepo
    from ..infrastructure.ws_manager import WebSocketManager

#: Cap on the bounded inbound de-dupe of ``APP_SESSION`` opens — keeps memory
#: flat (LRU eviction of the oldest session id) while still suppressing the
#: realistic double-delivery window (WebRTC + HTTPS fallback, retransmits).
_SEEN_OPEN_SESSIONS_MAX = 4096

log = logging.getLogger(__name__)


class AppFederationService:
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
        "_user_repo",
        "_ws",
        "_federation",
        "_federation_repo",
        "_cp_repo",
        "_bus",
        "_seen_open_sessions",
    )

    def __init__(
        self,
        *,
        app_repo: AbstractAppRepo,
        user_repo: AbstractUserRepo,
        ws: WebSocketManager,
        federation: Any,
        federation_repo: AbstractFederationRepo,
        cp_repo: "AbstractCpRepo | None" = None,
        bus: "EventBus | None" = None,
    ) -> None:
        self._app_repo = app_repo
        self._user_repo = user_repo
        self._ws = ws
        self._federation = federation
        self._federation_repo = federation_repo
        self._cp_repo = cp_repo
        self._bus = bus
        #: Bounded LRU of inbound ``APP_SESSION`` open ``session_id``s already
        #: handled — guards against a double federation delivery (WebRTC +
        #: HTTPS fallback / retransmit) seating two invites + two notifications
        #: for the same challenge. Scoped to inbound only; outbound
        #: ``open_session`` always mints a fresh uuid so it never dedupes.
        self._seen_open_sessions: OrderedDict[str, None] = OrderedDict()

    async def _emit(self, event: "DomainEvent") -> None:
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
        ``online``.

        Remote online presence is not yet wired in — ``online`` is always
        ``False`` for remote contacts (deferred to a future wiring task).
        """
        own = self._federation.own_instance_id
        connected = self._ws.connected_users()

        # Personal blocks (§Privacy) — same filter as /friends and DM roster.
        blocked_ids = {
            uid for uid, _ in await self._user_repo.list_blocked(self_user_id)
        }

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
                }
            )

        for r in await self._user_repo.list_all_known_remote():
            if r.user_id in blocked_ids:
                continue
            out.append(
                {
                    "instance_id": r.instance_id,
                    "user_ref": r.remote_username,
                    "display_name": r.display_name or r.remote_username,
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
          straight to the target *and* the initiator over WebSocket — no
          federation send, no fan-out to every local user.
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
        await self._require_enabled(app_id)
        await self._assert_age_allowed(app_id, actor_user_id)
        await self._assert_target_allowed(actor_user_id, target)
        session_id = uuid4().hex
        own = self._federation.own_instance_id

        if target.get("is_local"):
            # Local loopback — deliver the open frame to the addressed person
            # and the initiator only, never a fan-out to all local users.
            recipients = await self._age_filter_recipients(
                app_id, [target["user_ref"], actor_user_id]
            )
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
            if target["user_ref"] in recipients:
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
                    )
                )
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
        the JSON ``APP_MESSAGE`` path only — the binary fast-path frame format
        (v1) has no routing slot, so a binary send stays household-scoped and
        the receiver disambiguates by ``session_id`` (documented v1
        limitation; see :meth:`FederationService.send_app_message`).
        """
        await self._require_enabled(app_id)
        await self._assert_age_allowed(app_id, actor_user_id)
        await self._assert_target_allowed(actor_user_id, target)
        own = self._federation.own_instance_id

        if target.get("is_local"):
            # Local loopback — deliver the message frame to the addressed
            # person and the initiator only, never a fan-out to all local users.
            recipients = await self._age_filter_recipients(
                app_id, [target["user_ref"], actor_user_id]
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
                str(session_id)
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

        await self._deliver(
            app_id,
            session_id,
            from_instance=event.from_instance,
            payload=app_payload,
            kind=kind,
            to_user=to_user,
            from_user=from_user,
            notify_open=(kind == "session" and event.payload.get("verb") == "open"),
        )

    async def on_inbound_message(
        self,
        instance_id: str,
        app_id: str,
        session_id: str,
        payload: dict,
    ) -> None:
        """Handle an inbound binary app frame from ``fed-app-v1``.

        Called by :meth:`FederationService._app_inbound_handler` after the
        §24.11 pipeline validates the envelope and the payload is decrypted.
        """
        # The binary ``fed-app-v1`` frame format (v1) carries no ``to_user``
        # routing slot, so binary inbound always uses the household fan-out;
        # the receiver disambiguates by ``session_id``.
        await self._deliver(
            app_id,
            session_id,
            from_instance=instance_id,
            payload=payload,
            kind="message",
            to_user=None,
        )

    # ─── Internal helpers ─────────────────────────────────────────────────────

    def _seen_open_session(self, session_id: str) -> bool:
        """Return whether ``session_id`` was already handled inbound.

        First sight records it (LRU, capped at
        :data:`_SEEN_OPEN_SESSIONS_MAX`, oldest evicted) and returns ``False``;
        a repeat returns ``True`` so the caller can skip both delivery and the
        challenge publish. Synchronous — the OrderedDict mutation is atomic
        under the single-threaded asyncio loop.
        """
        if session_id in self._seen_open_sessions:
            return True
        self._seen_open_sessions[session_id] = None
        if len(self._seen_open_sessions) > _SEEN_OPEN_SESSIONS_MAX:
            self._seen_open_sessions.popitem(last=False)
        return False

    async def _resolve_remote_initiator(
        self, from_instance: str, from_user: str | None
    ) -> "RemoteUser | None":
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
        remote: "RemoteUser | None" = None,
    ) -> str:
        """Resolve a human label for a remote challenge initiator.

        Order: the remote user's federated ``display_name`` (when ``from_user``
        is known) → the peer instance's display name → the raw
        ``from_instance`` id as a last resort. Never a stable cross-household
        id beyond the fallback. ``remote`` lets a caller that already resolved
        the initiator (e.g. for a block check) pass the row to avoid a second
        lookup.
        """
        if remote is None and from_user:
            remote = await self._user_repo.get_remote_by_member(
                from_instance, from_user
            )
        if remote is not None and remote.display_name:
            return remote.display_name
        inst = await self._federation_repo.get_instance(from_instance)
        if inst is not None:
            return inst.effective_display_name
        return from_instance

    async def _remote_online(self, instance_id: str, remote_username: str) -> bool:
        """Return whether a remote user has a live session.

        Remote presence is not yet wired into this service — always returns
        ``False`` (fail-soft).  A future task will plumb the presence service
        so cross-household online state can be surfaced in the contact picker.
        """
        _ = instance_id, remote_username  # reserved for future wiring
        return False

    async def _require_enabled(self, app_id: str) -> None:
        """Raise :class:`AppNotFoundError` or :class:`AppNotEnabledError`."""
        app = await self._app_repo.get(app_id)
        if app is None:
            raise AppNotFoundError(app_id)
        if not app.enabled:
            raise AppNotEnabledError(app_id)

    async def _assert_age_allowed(self, app_id: str, actor_user_id: str) -> None:
        """Raise :class:`AppAgeRestrictedError` for protected minors below min_age.

        Fail-closed: if the app is not found or has no age restriction,
        this is a no-op.  Only blocks when the app exists, has a min_age > 0,
        cp_repo is configured, and the user's protection record shows a
        declared_age below the threshold.
        """
        if self._cp_repo is None:
            return
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
            # Legacy household-addressed path (user_ref == "") — exempt.
            return
        contacts = await self.list_contacts(self_user_id=actor_user_id)
        key = (
            target.get("instance_id"),
            target.get("user_ref"),
            bool(target.get("is_local")),
        )
        for c in contacts:
            if (c["instance_id"], c["user_ref"], c["is_local"]) == key:
                return
        raise AppContactNotFoundError("target is not a contact of the requesting user")

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
        self, app_id: str, user_ids: list[str]
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
            The remote initiator's username, used to resolve the
            ``from_display`` label on the published :class:`AppChallengeReceived`.
        notify_open:
            ``True`` only for an inbound ``APP_SESSION`` *open* — when the
            event also resolves to a single local recipient, raise a bell row +
            push for that recipient. Never set for messages or the legacy
            household fan-out (no specific recipient to notify).
        """
        app = await self._app_repo.get(app_id)
        if app is None or not app.enabled:
            log.debug(
                "app_federation: dropping inbound for app %r (not installed/enabled)",
                app_id,
            )
            return

        frame = {
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
                if initiator is not None and await self._user_repo.is_blocked(
                    recipient.user_id, initiator.user_id
                ):
                    log.info(
                        "app_federation: dropping inbound %s for blocked initiator "
                        "(recipient=%s initiator=%s session=%s)",
                        kind,
                        recipient.user_id,
                        initiator.user_id,
                        session_id,
                    )
                    return
                allowed = await self._age_filter_recipients(app_id, [recipient.user_id])
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
                        # Persist the invite so it survives until the recipient
                        # opens the app. Stored UNCONDITIONALLY (not gated on
                        # whether broadcast_to_user reached any sockets): a live
                        # SPA socket does not imply the chess app is mounted, so
                        # the durable invite must always be written — the app
                        # drains+replays it on open and dedupes by session_id,
                        # so a redundant replay when already online is harmless.
                        # The drain's 14-day TTL + prune bound growth.
                        await self._app_repo.add_pending_session(
                            AppPendingSession(
                                app_id=app_id,
                                user_id=recipient.user_id,
                                session_id=session_id,
                                from_instance=from_instance,
                                from_user=from_user,
                                payload=payload,
                                created_at=datetime.now(timezone.utc).isoformat(),
                            )
                        )
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
                            )
                        )
                return

        # Legacy / best-effort fan-out to every active, age-eligible local user
        # (soft-deleted/inactive accounts are excluded — same as /friends).
        users = await self._user_repo.list_active()
        user_ids = [u.user_id for u in users]
        if not user_ids:
            return

        # Age-gate filter: skip recipients who are protected minors below
        # the app's minimum age.  Fast path when the app has no restriction.
        user_ids = await self._age_filter_recipients(app_id, user_ids)

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

    async def drain_pending_sessions(
        self, app_id: str, user_id: str
    ) -> list["AppPendingSession"]:
        """Return + clear this user's pending session invites for the app
        (replayed by the app on open)."""
        return await self._app_repo.drain_pending_sessions(app_id, user_id)
