"""Shared HA provider classes.

The provider Protocols defined in :mod:`socialhome.platform.adapter`
have one HA-shaped implementation each that both :class:`HaAdapter`
(Core) and :class:`~socialhome.platform.haos.HaosAdapter` (Supervisor
add-on) compose. The auth provider is the only piece that varies —
HAOS uses :class:`~socialhome.platform.haos.adapter.HaIngressAuthProvider`
which trusts the Supervisor-injected ``X-Remote-User-Name`` header before
falling back to the bearer flow.

Each provider holds a back-reference to its adapter so it can lazily
access ``adapter._client`` (only available after the adapter's
``on_startup`` has run) and ``adapter._options``.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import AsyncIterable
from typing import TYPE_CHECKING, Any

import orjson

from ..adapter import ExternalUser, _extract_bearer

log = logging.getLogger(__name__)

# A valid Home Assistant service token (the part after ``notify.``). HA
# service names are lowercase ``[a-z0-9_]`` slugs; anything else (dots,
# slashes, spaces, uppercase) is rejected so a user-controlled value can never
# inject into the ``/api/services/{domain}/{service}`` URL path.
#
# ``\A...\Z`` (not ``^...$``): in Python ``$`` also matches just before a
# trailing newline, so ``^[a-z0-9_]+$`` would accept ``"mobile_app_x\n"``.
# The anchored form rejects any embedded/trailing newline.
_VALID_HA_SERVICE = re.compile(r"\A[a-z0-9_]+\Z")

if TYPE_CHECKING:
    from aiohttp import web


def _auth_user_to_external(row: dict) -> ExternalUser | None:
    """Convert an HA ``config/auth/list`` row to :class:`ExternalUser`.

    Returns ``None`` for rows the SH user pipeline can't / shouldn't
    accept:

    * ``system_generated: true`` — the Supervisor's own service
      account, ``Home Assistant Content``, and the cloud / mobile-app
      bridges. They carry ``username: null`` and never log in
      through ingress.
    * ``is_active: false`` — disabled HA accounts; SH shouldn't
      surface them in the wizard picker or honour them when ingress
      forwards their header.
    * ``username: null`` — defensive belt-and-braces for any future
      system row that omits the ``system_generated`` flag.
    """
    if row.get("system_generated", False):
        return None
    if row.get("is_active", True) is False:
        return None
    username = row.get("username")
    if not username:
        return None
    return ExternalUser(
        username=str(username),
        display_name=str(row.get("name") or username),
        # ``config/auth/list`` doesn't carry the picture URL — that
        # lives on the matching ``person.*`` entity. The avatar lifter
        # in :meth:`HaUserDirectory.fetch_picture_bytes` does the cross-
        # reference via ``attributes.user_id`` so the wizard doesn't
        # need it on this code path.
        picture_url=None,
        is_admin=False,
        email=None,
        # The HA-side identifier; persisted on the SH ``users`` row so
        # downstream joins (picture lifter, future presence bridge)
        # don't need to re-resolve username → id on every call.
        external_id=str(row.get("id")) if row.get("id") else None,
    )


def _row_to_owner(row: dict) -> ExternalUser | None:
    """Same as :func:`_auth_user_to_external`, but only returns the
    row when it represents an HA *owner*. Used by the haos wizard to
    pick the initial SH admin without iterating callers."""
    if not row.get("is_owner", False):
        return None
    return _auth_user_to_external(row)


class HaAuthProvider:
    """Resolve a request via ``X-Remote-User-Name`` header or HA bearer token.

    Default for :class:`HaAdapter` (HA Core / non-supervisor mode).
    HAOS swaps in :class:`~socialhome.platform.haos.adapter.HaIngressAuthProvider`,
    which trusts the Supervisor-injected header without bearer fallback.
    """

    __slots__ = ("_adapter",)

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter

    async def authenticate(
        self,
        request: web.Request,
    ) -> ExternalUser | None:
        # Only trust ``X-Remote-User-Name`` when HA Core's ingress proxy
        # is in the path — confirmed by ``X-Hass-Source: core.ingress``
        # (set by direct assignment in ``homeassistant/components/hassio/
        # ingress.py``, overwriting any client-supplied value). Without
        # the marker the request didn't pass Core's auth, so fall
        # through to the bearer path.
        if request.headers.get("X-Hass-Source") == "core.ingress":
            ingress_user = request.headers.get("X-Remote-User-Name")
            if ingress_user:
                return await self._adapter.users.get(ingress_user)
        token = _extract_bearer(request)
        if token:
            return await self._authenticate_bearer(token)
        return None

    async def _authenticate_bearer(self, token: str) -> ExternalUser | None:
        # Tokens are only valid when they live in the local credential
        # store (``platform_tokens``). Those are minted by the setup
        # wizard against a known HA person, so the row carries a real
        # ``username`` we can trust.
        #
        # We deliberately do NOT accept arbitrary HA long-lived access
        # tokens here. ``GET /api/`` only returns ``{"message": "API
        # running."}`` — the REST surface has no way to map a bearer
        # token back to its owning HA user, so any fallback would
        # collapse every LLAT into one shared SH identity. Reject
        # unknown tokens loudly instead.
        credentials = self._adapter._credentials
        if credentials is None:
            return None
        return await credentials.authenticate_bearer(token)


class HaUserDirectory:
    """List / get HA principals from ``config/auth/list`` (WS).

    HA's auth users (``username`` + ``name`` + ``id``) are the
    canonical identity surface. The previous implementation read
    ``person.*`` states and derived ``ExternalUser.username`` from the
    entity slug — which HA Core constructs from the user's
    **display name**, not the auth-provider username (#297). On any
    instance where the operator renamed their account after creation
    the two diverge and the wizard / ingress-auth path 404s.

    Switching to ``config/auth/list`` makes ``ExternalUser.username``
    the authentic credential the operator types at the HA login
    screen — the same string ingress forwards as
    ``X-Remote-User-Name`` — so the rest of the SH user pipeline can
    treat it as a stable key.

    ``enable`` / ``disable`` still raise :class:`NotImplementedError`
    because provisioning goes through the steady-state
    ``/api/admin/ha-users`` routes.
    """

    __slots__ = ("_adapter",)

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter

    async def list_users(self) -> list[ExternalUser]:
        rows = await self._adapter._client.list_auth_users()
        out: list[ExternalUser] = []
        for row in rows:
            user = _auth_user_to_external(row)
            if user is not None:
                out.append(user)
        return out

    async def get(self, username: str) -> ExternalUser | None:
        # ``config/auth/list`` doesn't accept a filter — the list is
        # short (≤ tens of accounts on a typical household instance),
        # and the WS round-trip is cached on the client side, so a
        # linear scan over a recent list is essentially free. Match
        # on the auth-provider username (the credential ingress
        # forwards), not the display name.
        for row in await self._adapter._client.list_auth_users():
            if row.get("username") == username:
                return _auth_user_to_external(row)
        return None

    async def get_owner(self) -> ExternalUser | None:
        """First HA owner from the auth list, or ``None``.

        Used by the haos wizard to pick the initial SH admin without
        a separate Supervisor REST round-trip. An HA instance always
        has exactly one owner.
        """
        for row in await self._adapter._client.list_auth_users():
            user = _row_to_owner(row)
            if user is not None:
                return user
        return None

    async def fetch_picture_bytes(self, username: str) -> bytes | None:
        """Resolve + download the picture for HA auth user ``username``.

        Convenience wrapper around :meth:`fetch_picture_bytes_by_id`
        that resolves the username → HA user_id first. Prefer the
        ``_by_id`` variant when the caller already holds the id
        (e.g. from ``users.external_id`` on the local row) — that
        path skips one WS round-trip.
        """
        target_id = await self._find_ha_user_id(username)
        if not target_id:
            return None
        return await self.fetch_picture_bytes_by_id(target_id)

    async def fetch_picture_bytes_by_id(self, ha_user_id: str) -> bytes | None:
        """Download the ``person.*`` picture matching ``ha_user_id``.

        The id → ``person.*`` join is HA's documented contract
        (``person.attributes.user_id`` references
        ``config/auth/list[].id``) and survives display-name renames
        that shift the entity slug. Callers with the id on hand
        (e.g. the admin-picture sync, which reads it off the local
        ``users`` row) avoid the username→id WS lookup entirely.

        Returns the raw bytes so the caller can run them through the
        ImageProcessor pipeline. ``None`` for any missing link in the
        chain (no matching person, no picture attribute, transport
        error).
        """
        for state in await self._adapter._client.get_states():
            entity_id = state.get("entity_id", "")
            if not entity_id.startswith("person."):
                continue
            attrs: dict = state.get("attributes", {}) or {}
            if attrs.get("user_id") != ha_user_id:
                continue
            entity_picture = attrs.get("entity_picture")
            if not entity_picture:
                return None
            return await self._adapter._client.fetch_path_bytes(entity_picture)
        return None

    async def _find_ha_user_id(self, username: str) -> str | None:
        """Return the HA ``user_id`` for ``username`` (the auth
        provider credential) or ``None``. The ``id`` field is already
        exposed via :attr:`ExternalUser.external_id`; this private
        helper is kept for the by-username picture path where the
        caller hasn't materialised an :class:`ExternalUser` yet."""
        for row in await self._adapter._client.list_auth_users():
            if row.get("username") == username:
                user_id = row.get("id")
                return str(user_id) if user_id else None
        return None

    async def is_enabled(self, username: str) -> bool:
        return (await self.get(username)) is not None

    async def enable(
        self,
        username: str,
        *,
        password: str | None = None,
    ) -> ExternalUser:
        raise NotImplementedError(
            "HA-mode enable goes through ha_users routes; the directory "
            "is read-only as far as HA persons are concerned",
        )

    async def disable(self, username: str) -> None:
        raise NotImplementedError(
            "HA-mode disable goes through ha_users routes",
        )


def _resolve_notify_service(user: Any) -> str | None:
    """The cleaned ``preferences.ha_notify_service`` value for this user,
    or ``None`` when unconfigured / blank / unparseable.

    The value's *interpretation* lives in :meth:`HaPushProvider.send`:

    - ``"notify.<entity>"`` (non-empty suffix) → a notify *entity id*,
      delivered via the ``notify.send_message`` entity action.
    - a bare name (no dot) → a legacy ``notify.<name>`` service.
    - any other ``"domain.service"`` (non-notify domain) → rejected in
      :meth:`HaPushProvider.send`: SH invokes HA with the instance's shared
      token, so only ``notify.*`` targets are ever called.

    There is no auto-derived default: HA's Companion-app notify service is
    named after the *device* (``notify.mobile_app_<device-slug>``), which
    almost never equals the username — so guessing ``mobile_app_<username>``
    silently failed for everyone. The user sets the real target in
    Settings → profile (§25.3).
    """
    raw = getattr(user, "preferences_json", None)
    if not raw:
        return None
    try:
        prefs = orjson.loads(raw)
    except ValueError, TypeError:
        return None
    svc = prefs.get("ha_notify_service") if isinstance(prefs, dict) else None
    if not isinstance(svc, str) or not svc.strip():
        return None
    return svc.strip()


#: Prefix of the Companion ``tag`` Social Home sets (see
#: :func:`companion_tag`).
COMPANION_TAG_PREFIX = "socialhome-"


def companion_tag(tag: str) -> str:
    """The Companion ``tag`` for Social Home's push *tag*.

    Companion contract (companion.home-assistant.io/docs/notifications/
    notifications-basic, "Replacing"): "All subsequent notifications will
    take the place of a notification with the same tag"; ``clear_notification``
    removes one by tag. On iOS the push relay turns ``data.tag`` into the
    ``apns-collapse-id`` header (home-assistant/mobile-apps-fcm-push
    ``functions/ios.js`` @ 72200467), which Apple caps at 64 bytes — a raw
    ``type:link_url`` tag can be longer, so it is hashed to a fixed 43
    characters. Same input, same tag: a repeat replaces itself.
    """
    digest = hashlib.sha256(tag.encode("utf-8")).hexdigest()[:32]
    return f"{COMPANION_TAG_PREFIX}{digest}"


class HaPushProvider:
    """Deliver to the user's HA Companion app via their configured notify
    service (``preferences.ha_notify_service``). No-op when unset.

    ``data`` is Social Home's platform-neutral push data — ``{"url": <SPA
    path>, "tag": <str>}`` — and becomes the Companion keys this provider
    knows the contract for (:meth:`_companion_data`); anything else is
    dropped rather than forwarded to HA unverified.
    """

    __slots__ = ("_adapter",)

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter

    async def send(
        self,
        user: ExternalUser,
        title: str,
        message: str,
        data: dict | None = None,
    ) -> None:
        value = _resolve_notify_service(user)
        if value is None:
            log.debug(
                "HA push: %s has no ha_notify_service configured — skipping. "
                "Set it in Settings → profile (e.g. notify.mobile_app_<device>).",
                getattr(user, "username", "?"),
            )
            return
        body: dict = {"title": title, "message": message}
        data = await self._companion_data(data)
        suffix = value[len("notify.") :] if value.startswith("notify.") else ""
        if (
            suffix.startswith("mobile_app_")
            and _VALID_HA_SERVICE.match(suffix) is not None
        ):
            # mobile_app target → mobile_app's *legacy* per-device service
            # (``notify.mobile_app_<device>``). Only the legacy service accepts
            # the rich ``data`` payload (tap url / actions); HA's strict
            # entity-service schema for ``notify.send_message`` rejects ``data``.
            # The suffix is validated as a real HA service token
            # (``^[a-z0-9_]+$``) so a malicious/mistyped value can never inject
            # into the ``/api/services/{domain}/{service}`` URL path here — any
            # other shape falls through to the send_message body branch below.
            domain, service = "notify", suffix
            if data:
                body["data"] = data
        elif suffix:
            # Any other notify *entity id* (e.g. a notify group with no legacy
            # per-device service) → the ``notify.send_message`` entity action.
            # It takes entity_id + message + optional title ONLY; HA rejects an
            # unsupported ``data`` key, so ``data`` is deliberately omitted. The
            # user-supplied value rides in the request BODY (``entity_id``),
            # never the URL path.
            domain, service = "notify", "send_message"
            body["entity_id"] = value
        elif "." in value:
            # A fully-qualified non-``notify`` ``domain.service`` (every
            # ``notify.*`` value was already handled by the two suffix branches
            # above, so anything reaching here has a non-notify domain). SH
            # calls HA with the *instance's* shared token (operator LLAT /
            # SUPERVISOR_TOKEN), never a per-user one — so honouring an
            # arbitrary user-set domain.service would let any member invoke
            # ``homeassistant.restart`` / ``shell_command.*`` / etc. with that
            # token. Reject it: only ``notify.*`` is supported (the only thing
            # the Settings dropdown ever offers).
            log.warning(
                "HA push: %s has an unsupported notify target %r — only "
                "notify.* services are allowed; pick your device in "
                "Settings → Notifications.",
                getattr(user, "username", "?"),
                value,
            )
            return
        else:
            # A bare name → legacy ``notify.<value>`` service (already-configured
            # users) — legacy call carries the data payload. Validate the value
            # against the service-token regex first so a bare ``notify.<garbage>``
            # call is never built (and the value can't inject into the URL path).
            if _VALID_HA_SERVICE.match(value) is None:
                log.warning(
                    "HA push: %s has an unsupported notify target %r — only "
                    "notify.* services are allowed; pick your device in "
                    "Settings → Notifications.",
                    getattr(user, "username", "?"),
                    value,
                )
                return
            domain, service = "notify", value
            if data:
                body["data"] = data
        result = await self._adapter._client.call_service(domain, service, body)
        if result is None:
            # call_service logs the HTTP failure; say here what it means
            # for push, so a wrong service name is diagnosable instead of
            # silently dropping every notification.
            log.warning(
                "HA push to %s via %s.%s failed — check the notify service "
                "name in Settings → profile (does %s.%s exist in HA?).",
                getattr(user, "username", "?"),
                domain,
                service,
                domain,
                service,
            )

    async def _companion_data(self, data: dict | None) -> dict | None:
        """Map Social Home's push *data* to the Companion app's keys.

        Contract — companion.home-assistant.io/docs/notifications/
        notifications-basic (verified against the push relay,
        home-assistant/mobile-apps-fcm-push ``functions/ios.js`` /
        ``android.js`` @ 72200467):

        * ``url`` (iOS) / ``clickAction`` (Android) — what a tap opens; a
          relative URL opens on the HA server in HA's frontend, a full URL
          as is. The adapter decides which one reaches Social Home
          (``companion_click_url``); without one, no URL is sent.
        * ``tag`` — a later push with the same tag replaces the earlier
          one (:func:`companion_tag`).
        """
        if not data:
            return None
        out: dict[str, str] = {}
        tag = data.get("tag")
        if isinstance(tag, str) and tag:
            out["tag"] = companion_tag(tag)
        path = data.get("url")
        if isinstance(path, str) and path.startswith("/"):
            click = await self._adapter.companion_click_url(path)
            if click:
                out["url"] = click
                out["clickAction"] = click
        return out or None

    async def list_notify_targets(self) -> list[dict]:
        """User-selectable notify targets for the profile dropdown, as
        ``[{"entity_id", "name"}]`` from HA's notify entities."""
        return await self._adapter._client.list_notify_entities()


class HaSTTProvider:
    """Stream PCM16 audio to ``/api/stt/{stt_entity_id}``."""

    __slots__ = ("_adapter",)

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter

    async def transcribe(self, audio: bytes, language: str = "en") -> str:
        async def _single_chunk() -> AsyncIterable[bytes]:
            if audio:
                yield audio

        return await self.stream_transcribe(_single_chunk(), language=language)

    async def stream_transcribe(
        self,
        stream: AsyncIterable[bytes],
        *,
        language: str = "en",
        sample_rate: int = 16000,
        channels: int = 1,
    ) -> str:
        entity_id = self._adapter._options.get("stt_entity_id")
        if not entity_id:
            raise NotImplementedError(
                "HaAdapter: no [homeassistant].stt_entity_id configured — "
                "set it to an HA STT entity id (e.g. "
                "'stt.home_assistant_cloud') to enable transcription."
            )
        payload = await self._adapter._client.stream_stt(
            entity_id,
            stream,
            language=language,
            sample_rate=sample_rate,
            channels=channels,
        )
        if not isinstance(payload, dict) or payload.get("result") != "success":
            return ""
        text = payload.get("text") or ""
        return text if isinstance(text, str) else str(text)


class HaAIProvider:
    """Run HA's ``ai_task.generate_data`` action."""

    __slots__ = ("_adapter",)

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter

    async def generate_data(
        self,
        *,
        task_name: str,
        instructions: str,
    ) -> str:
        body: dict = {"task_name": task_name, "instructions": instructions}
        entity_id = self._adapter._options.get("ai_task_entity_id")
        if entity_id:
            body["entity_id"] = entity_id
        payload = await self._adapter._client.call_service(
            "ai_task",
            "generate_data",
            body,
            return_response=True,
        )
        if payload is None:
            return ""
        service_response = (payload or {}).get("service_response") or {}
        data = service_response.get("data", "")
        if isinstance(data, str):
            return data
        return str(data) if data else ""


class HaEventSink:
    """``POST /api/events/{event_type}`` so HA automations can subscribe."""

    __slots__ = ("_adapter",)

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter

    async def fire(self, event_type: str, data: dict) -> bool:
        return await self._adapter._client.fire_event(event_type, data)
