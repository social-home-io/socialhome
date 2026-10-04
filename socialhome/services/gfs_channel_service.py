"""Household side of opaque connection-server channels for PRIVATE spaces (v_51).

A private space whose members include households seated through an invite
link (``InstanceSource.SPACE_SESSION`` — they reach the host only over the
connection server's ``/gfs/envelope``) used to depend on the host for every
item: a link-joined member's post reached the other members only once the
host was online to relay it. This service gives those spaces member
publishing over the connection server through an **opaque channel** the
server knows only by a random id and a channel key — never the space id,
name or authority key (wire shapes: :mod:`socialhome.domain.gfs_channel`;
keys: :mod:`socialhome.gfs_channel`; server: ``global_server/channels.py``).

**Which spaces get one (the owner's explicit option).** A PRIVATE space this
household owns (seed matching the pin) whose owner turned
``SpaceFeatures.private_gfs`` ON ("use the connection server for this
space"), that has at least one live remote member household, while at least
one active connection server proves ``private_channels`` in its signed
``/gfs/info`` (:meth:`GfsChannelService.reconcile`). Checked when the owner
turns the option on (:meth:`GfsChannelService.enable`), when a remote seat
goes live (:class:`SpaceRemoteSeatLive`), at every content-key rotation
(before the member rekey, so the rekey carries the grants) and on every GFS
(re)connect. A space that stops qualifying (the option turned off, its last
remote member left, it is no longer private) has its channel unregistered
and forgotten; its members simply get no grant for the next epoch, which
ends their use of it. A private space with the option OFF never gets channel
material, and never touches a connection server.

**The channel.** A random 128-bit id (``new_channel_id``) and a key
HKDF-derived from the space seed with its own domain separation, registered
ANONYMOUSLY over the cookie-less publish session (channel-key-signed — no
household identity). When the seed changes (a v_44 authority rotation) the
owner unregisters the old channel before the seed swap and starts a FRESH
one — there is no re-pin: a revoked seed holder still holds the old channel
key and could race any re-pin chained to it.

**Grants.** Every seed holder derives the channel key and issues, per member
household and epoch, a :class:`~socialhome.domain.gfs_channel.GfsChannelGrant`
(channel cert for a writer in a trusted space, channel writer key in a
strict space, and a seat pass for EVERY member household — link-joined,
paired and mesh-only: the owner's ``private_gfs`` opt-in), bound to the
space by the space authority key. It rides INSIDE the per-peer encrypted payloads of
the four channels that already carry writer certs — never to a household
below v_51 (:data:`FederationCapability.MIN_FOR_PRIVATE_CHANNELS`), never to
ourselves. The owner itself holds no grant and never subscribes: it receives
everything over federation.

**Members** verify a grant against the pinned space key, keep it
KEK-wrapped on that epoch's key row, subscribe with its pass on every server
it names that they are connected to (identified, like a follower — the
accepted residual: the server learns the member households connected to
it), and publish items to it: trusted with the channel cert (identified),
strict with the channel writer key (anonymous). A member without a usable
seat (not connected to the channel's server, or a v_51 owner's publish-only
grant) catches the others' items up from the host by §25.6 sync when the
host is back (it re-advertises its capabilities on startup). Without a grant
for the current epoch, nothing goes to any server — the host path always
carries the item.

**Self-heal.** The server answers the owner's notice with what it holds;
when another channel-key holder moved the channel past the owner's CURRENT
epoch (or pinned another writer key for it), the owner starts a fresh
channel and re-grants its members. The answer is untrusted: it counts only
after the owner has been online and connected to that server for
:data:`HEAL_GRACE_S` (a rotation missed while offline lands first), a
missing or malformed field is no information, and replacements are capped
at one per space per :data:`HEAL_COOLDOWN_S`, persisted.

**Epoch notices.** Every seed holder announces the content epoch to the
channel's servers at each rotation, before the member rekey (channel-key
signed, with the publish mode and, in a strict space, the writer key pin).
The server bounds inflation by time (one epoch per minute); a notice that
comes too fast gets a ``429`` and lands from the retry queue.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Protocol

import aiohttp

from ..crypto import b64url_decode, b64url_encode, ed25519_public_key, sign_ed25519
from ..domain.events import (
    PeerCapabilitiesAdvertised,
    PeerTransportChanged,
    SpaceRemoteSeatLive,
)
from ..domain.federation import FederationEventType, GfsConnection, InstanceSource
from ..domain.federation_capabilities import FederationCapability
from ..domain.gfs_channel import (
    CHANNEL_EPOCH_ROUTE,
    CHANNEL_PUBLISH_ANON_ROUTE,
    CHANNEL_PUBLISH_ROUTE,
    CHANNEL_REGISTER_ROUTE,
    CHANNEL_SUBSCRIBE_ROUTE,
    CHANNEL_UNREGISTER_ROUTE,
    CHANNEL_UNSUBSCRIBE_ROUTE,
    MAX_GRANT_GFS_IDS,
    ChannelItemFrame,
    ChannelPublishAnonRequest,
    ChannelPublishRequest,
    ChannelSubscribeRequest,
    ChannelUnsubscribeRequest,
    GfsChannelGrant,
    InvalidChannelWire,
    canonical,
)
from ..domain.gfs_member_publish import GFS_PUBLISH_MODE_STRICT
from ..domain.space import SpaceType
from ..domain.writer_key import WRITER_KEY_SUITE_ED25519
from ..gfs_channel import (
    bind_grant,
    channel_pk_of,
    derive_channel_epoch_offset,
    derive_channel_seed,
    issue_channel_cert,
    issue_channel_pass,
    issue_channel_writer_key,
    new_channel_id,
    sign_notice,
    sign_publish_anon,
    sign_register,
    sign_unregister,
    verify_grant,
)
from .gfs_publish_retry import (
    GfsPublish,
    GfsPublishRetryQueue,
    PublishOutcome,
    classify_publish_status,
)

if TYPE_CHECKING:
    from ..domain.space import Space
    from ..federation.federation_service import FederationService
    from ..infrastructure.event_bus import EventBus
    from ..infrastructure.key_manager import KeyManager
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
    from ..repositories.space_key_repo import AbstractSpaceKeyRepo
    from ..repositories.space_remote_member_repo import AbstractSpaceRemoteMemberRepo
    from ..repositories.space_repo import AbstractSpaceRepo
    from .gfs_connection_service import GfsConnectionService
    from .space_crypto_service import SpaceContentEncryption
    from .space_public_inbound import SpacePublicInbound
    from .space_writer_cert_service import SpaceWriterCertService

log = logging.getLogger(__name__)


class _SyncScheduler(Protocol):
    async def enqueue_sync_for_space(
        self, *, space_id: str, peer_instance_id: str
    ) -> None: ...


_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10)

#: Queue-item kinds of the channel retry queue.
_KIND_ITEM = "channel_item"
_KIND_ANON = "channel_item_anon"
_KIND_NOTICE = "channel_epoch_notice"

#: Random offset (seconds, either way) on an anonymous request's ``ts`` — the
#: same as strict member publish: whole seconds, so a sub-second clock offset
#: can't link a household's anonymous requests.
ANON_TS_JITTER_S: int = 60

#: How long a publish-only member waits after its host came back before it
#: asks for the catch-up sync (the host drains its relay queue first).
HOST_RETURN_SYNC_DELAY_S: float = 15.0

#: Background tasks (subscribe / unsubscribe / channel creation) in flight.
MAX_PENDING_TASKS: int = 64

#: At most one take-over replacement (self-heal or a squatted id) per space
#: per this window, persisted on the space row: the evidence is untrusted
#: server output, so a lying server can cost at most one fresh channel a day.
HEAL_COOLDOWN_S: int = 24 * 3600

#: How long the owner must have been online (and, per connection server,
#: connected) before a notice answer can count as a take-over: the rekeys
#: and the sync it missed while away (a delegated admin's rotation) land
#: first, so a server ahead of a stale epoch is catch-up, not a take-over.
HEAL_GRACE_S: float = 300.0

#: The largest epoch a server can hold (the GFS caps notices at 2^62).
_MAX_WIRE_EPOCH = 2**62


def _monotonic() -> float:
    return time.monotonic()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _anon_ts() -> str:
    now = int(time.time())
    jitter = secrets.randbelow(2 * ANON_TS_JITTER_S + 1) - ANON_TS_JITTER_S
    return datetime.fromtimestamp(now + jitter, tz=timezone.utc).isoformat()


def _grant_aad(space_id: str, epoch: int) -> bytes:
    return f"socialhome-gfs-channel:{space_id}:{epoch}".encode("utf-8")


class GfsChannelService:
    """Create, announce and use opaque private-space channels."""

    __slots__ = (
        "_catching_up",
        "_conn_repo",
        "_crypto",
        "_federation",
        "_federation_repo",
        "_gfs",
        "_connected_at",
        "_healing",
        "_inbound",
        "_kek",
        "_keys",
        "_own_instance_id",
        "_own_pk",
        "_own_seed",
        "_registered",
        "_remote_members",
        "_retry",
        "_space_service",
        "_sync",
        "_spaces",
        "_started_at",
        "_stop",
        "_stopping",
        "_tasks",
        "_writer_certs",
    )

    def __init__(
        self,
        *,
        gfs: "GfsConnectionService",
        conn_repo: "AbstractGfsConnectionRepo",
        space_repo: "AbstractSpaceRepo",
        space_key_repo: "AbstractSpaceKeyRepo",
        remote_member_repo: "AbstractSpaceRemoteMemberRepo",
        federation_repo: "AbstractFederationRepo | None",
        space_crypto: "SpaceContentEncryption",
        writer_certs: "SpaceWriterCertService",
        own_instance_id: str,
        own_identity_seed: bytes,
        key_manager: "KeyManager | None",
    ) -> None:
        self._gfs = gfs
        self._conn_repo = conn_repo
        self._spaces = space_repo
        self._keys = space_key_repo
        self._remote_members = remote_member_repo
        self._federation_repo = federation_repo
        self._crypto = space_crypto
        self._writer_certs = writer_certs
        self._own_instance_id = own_instance_id
        self._own_seed = own_identity_seed
        self._own_pk = ed25519_public_key(own_identity_seed)
        self._kek = key_manager
        self._federation: "FederationService | None" = None
        self._space_service: object | None = None
        self._inbound: "SpacePublicInbound | None" = None
        self._sync: "_SyncScheduler | None" = None
        self._retry = GfsPublishRetryQueue(self._retry_send)
        #: channel id → ``gfs_instance_id``s that confirmed our registration
        #: in this process (the owner's grants name only these).
        self._registered: dict[str, set[str]] = {}
        #: Spaces with a take-over replacement in flight (the persisted
        #: per-space cooldown bounds them across restarts).
        self._healing: set[str] = set()
        #: When this process started, and when each connection (by id) last
        #: (re)connected — the take-over grace runs from the later one.
        self._started_at = _monotonic()
        self._connected_at: dict[str, float] = {}
        #: Spaces with a host-return catch-up sync pending.
        self._catching_up: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._stopping = False
        self._stop = asyncio.Event()

    # ── Wiring + lifecycle ───────────────────────────────────────────────

    def attach_federation(self, federation: "FederationService") -> None:
        """Peer versions (the v_51 gate on every grant), and a second
        ``SPACE_CONFIG_CHANGED`` handler — registered after the inbound
        service's, so it reads the config that handler just applied (never
        the event itself): a member drops its seat when the owner turned
        ``private_gfs`` off, and takes it when the owner turned it on."""
        self._federation = federation
        registry = getattr(federation, "_event_registry", None)
        if registry is not None:
            registry.register(
                FederationEventType.SPACE_CONFIG_CHANGED, self._on_config_event
            )

    def attach_space_service(self, space_service: object) -> None:
        """The roster-snapshot sender that distributes a new channel's
        grants (``send_roster_snapshot``)."""
        self._space_service = space_service

    def attach_inbound(self, inbound: "SpacePublicInbound") -> None:
        """Where a decrypted-later channel item goes (the same checks as a
        public ``space_item``)."""
        self._inbound = inbound

    def attach_sync_scheduler(self, scheduler: "_SyncScheduler") -> None:
        """The §25.6 scheduler a publish-only member asks for a catch-up
        from the host when the host comes back."""
        self._sync = scheduler

    def wire(self, bus: "EventBus") -> None:
        bus.subscribe(SpaceRemoteSeatLive, self._on_seat_live)
        bus.subscribe(PeerTransportChanged, self._on_peer_transport)
        bus.subscribe(PeerCapabilitiesAdvertised, self._on_peer_advertised)

    async def start(self) -> None:
        self._stopping = False
        self._stop.clear()
        self._started_at = _monotonic()
        await self._retry.start()

    async def stop(self) -> None:
        self._stopping = True
        self._stop.set()
        await self.wait_idle()
        await self._retry.stop()

    async def wait_idle(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def _spawn(self, coro, name: str) -> None:
        if self._stopping or len(self._tasks) >= MAX_PENDING_TASKS:
            coro.close()
            log.warning("gfs.channel: background %s skipped (busy / stopping)", name)
            return
        task = asyncio.create_task(coro, name=f"gfs-channel-{name}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ── Helpers ──────────────────────────────────────────────────────────

    async def _matching_seed(self, space: "Space") -> bytes | None:
        try:
            seed = await self._spaces.get_space_seed(space.id)
        except RuntimeError:
            return None
        if seed is None or ed25519_public_key(seed).hex() != space.identity_public_key:
            return None
        return seed

    async def _capable(self) -> list[GfsConnection]:
        out: list[GfsConnection] = []
        for conn in await self._conn_repo.list_active():
            if conn.status != "active" or not conn.gfs_instance_id:
                continue
            if await self._gfs.private_channels_supported(conn):
                out.append(conn)
        return out

    async def _capable_in(self, gfs_ids: tuple[str, ...]) -> list[GfsConnection]:
        wanted = set(gfs_ids)
        return [c for c in await self._capable() if c.gfs_instance_id in wanted]

    async def _live_seat(self, space_id: str, instance_id: str) -> bool:
        return bool(
            await self._remote_members.list_for_instance(
                space_id, instance_id, include_tombstoned=False
            )
        )

    async def _is_link_joined(self, space_id: str, instance_id: str) -> bool:
        if self._federation_repo is None:
            return False
        inst = await self._federation_repo.get_instance(instance_id)
        return (
            inst is not None
            and inst.source is InstanceSource.SPACE_SESSION
            and await self._live_seat(space_id, instance_id)
        )

    async def _channel_seed(self, space: "Space") -> tuple[str, bytes] | None:
        """``(channel_id, channel_seed)`` when we hold the matching seed and
        the stored channel key is the one it derives; else ``None``."""
        seed = await self._matching_seed(space)
        stored = await self._spaces.get_gfs_channel(space.id)
        if seed is None or stored is None:
            return None
        channel_id, channel_pk = stored
        channel_seed = derive_channel_seed(seed, space.id, channel_id)
        if channel_pk_of(channel_seed) != channel_pk:
            return None
        return channel_id, channel_seed

    # ── Owner: create / keep / retire ────────────────────────────────────

    async def eligible(self, space: "Space", *, joining: str | None = None) -> bool:
        """A PRIVATE space we own whose owner turned ``private_gfs`` ON, our
        seed matching the pin, with at least one live remote member household
        — link-joined, paired or mesh-only (``joining`` counts too: the
        seat-live event fires before its ``space_instances`` row lands). A
        space with the option OFF never gets a channel."""
        if (
            space.space_type is not SpaceType.PRIVATE
            or not space.features.private_gfs
            or space.owner_instance_id != self._own_instance_id
            or await self._matching_seed(space) is None
        ):
            return False
        candidates = set(await self._spaces.list_member_instances(space.id))
        if joining:
            candidates.add(joining)
        candidates.discard(self._own_instance_id)
        for inst in candidates:
            if await self._live_seat(space.id, inst):
                return True
        return False

    async def enable(self, space_id: str) -> str:
        """The owner just turned ``private_gfs`` ON: create the channel when
        the space now qualifies, announce it and hand every member household
        its grant (with a seat pass) in a roster snapshot. Returns the
        :meth:`reconcile` outcome."""
        result = await self.reconcile(space_id)
        if result in ("created", "kept"):
            await self.announce_epoch(space_id)
            await self.distribute(space_id)
        return result

    async def reconcile(
        self, space_id: str, *, joining: str | None = None, fresh: bool = False
    ) -> str:
        """Create, keep or retire this space's channel (owner only). Returns
        ``"created"``, ``"kept"``, ``"retired"`` or ``"none"``."""
        space = await self._spaces.get(space_id)
        if space is None or space.owner_instance_id != self._own_instance_id:
            return "none"
        stored = await self._spaces.get_gfs_channel(space_id)
        if not await self.eligible(space, joining=joining):
            if stored is not None:
                await self._retire(space, stored[0])
                return "retired"
            return "none"
        conns = await self._capable()
        if not conns:
            return "none"
        current = await self._channel_seed(space)
        if current is not None and not fresh:
            _done, squatted = await self._register_all(current[0], current[1], conns)
            if not squatted:
                return "kept"
            if not await self._claim_heal(space_id, current[0]):
                return "kept"
            # Another key holds our id on some server (a member pre-registered
            # it where our registration had not landed): never fight over it —
            # start a fresh channel, which the next grants carry.
            log.warning(
                "gfs.channel: channel %s is pinned to another key on a "
                "connection server — starting a fresh one",
                current[0],
            )
        seed = await self._matching_seed(space)
        if seed is None:
            return "none"
        # First channel, our seed changed (v_44), or the old id was squatted:
        # a fresh one. An old channel we can no longer sign for idles out.
        channel_id = new_channel_id()
        channel_seed = derive_channel_seed(seed, space_id, channel_id)
        done, _squatted = await self._register_all(channel_id, channel_seed, conns)
        if not done:
            log.info(
                "gfs.channel: no connection server took the channel for a "
                "private space — its items keep the host path"
            )
            return "none"
        if not await self._spaces.set_gfs_channel(
            space_id, channel_id, channel_pk_of(channel_seed)
        ):
            return "none"
        log.info(
            "gfs.channel: started channel %s for private space %s", channel_id, space_id
        )
        return "created"

    async def _register_all(
        self, channel_id: str, channel_seed: bytes, conns: list[GfsConnection]
    ) -> tuple[set[str], bool]:
        """Register (or refresh) the channel on every connection. Returns the
        ``gfs_instance_id``s that confirmed it — the only servers a grant
        names — and whether any server holds the id under ANOTHER key
        (``409``). Remembered in memory: a restart re-registers on the next
        connect before any grant names a server again."""
        client = self._gfs.publish_client()
        done: set[str] = set()
        squatted = False
        if client is None:
            return done, squatted
        for conn in conns:
            body = sign_register(
                channel_seed=channel_seed,
                channel_id=channel_id,
                gfs_instance_id=conn.gfs_instance_id,
                ts=_anon_ts(),
            ).to_wire()
            status = await self._post_status(
                client, f"{conn.inbox_url}{CHANNEL_REGISTER_ROUTE}", body, conn
            )
            if status in (200, 201):
                done.add(conn.gfs_instance_id)
            elif status == 409:
                squatted = True
        registered = self._registered.setdefault(channel_id, set())
        registered.update(done)
        return done, squatted

    async def _post_status(
        self,
        client: aiohttp.ClientSession,
        url: str,
        body: dict,
        conn: GfsConnection,
    ) -> int | None:
        try:
            async with client.post(
                url, json=body, allow_redirects=False, timeout=_HTTP_TIMEOUT
            ) as resp:
                if resp.status not in (200, 201):
                    log.warning(
                        "gfs.channel: GFS %s answered HTTP %d on register",
                        conn.id,
                        resp.status,
                    )
                return resp.status
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning(
                "gfs.channel: GFS %s unreachable: %s",
                conn.id,
                exc or type(exc).__name__,
            )
            return None

    async def retire(self, space_id: str) -> bool:
        """Unregister and forget this space's channel now (owner, seed still
        matching) — before an authority rotation swaps the seed. ``True``
        when there was one."""
        space = await self._spaces.get(space_id)
        if space is None or space.owner_instance_id != self._own_instance_id:
            return False
        stored = await self._spaces.get_gfs_channel(space_id)
        if stored is None:
            return False
        await self._retire(space, stored[0])
        return True

    async def _retire(self, space: "Space", channel_id: str) -> None:
        """Unregister the channel everywhere we can sign for it, and forget
        it. Members get no grant for the next epoch."""
        seed = await self._matching_seed(space)
        client = self._gfs.publish_client()
        if seed is not None and client is not None:
            channel_seed = derive_channel_seed(seed, space.id, channel_id)
            for conn in await self._capable():
                body = sign_unregister(
                    channel_seed=channel_seed,
                    channel_id=channel_id,
                    gfs_instance_id=conn.gfs_instance_id,
                    ts=_anon_ts(),
                ).to_wire()
                await self._post(
                    client, f"{conn.inbox_url}{CHANNEL_UNREGISTER_ROUTE}", body, conn
                )
        await self._spaces.set_gfs_channel(space.id, None, None)
        log.info(
            "gfs.channel: retired channel %s — private space %s no longer uses "
            "the connection server",
            channel_id,
            space.id,
        )

    async def distribute(self, space_id: str, *, also: str | None = None) -> int:
        """Send every member household a roster snapshot, which carries its
        grant (v_51 peers only) — after a channel was created outside a
        rotation. ``also`` names a household whose ``space_instances`` row
        may not have landed yet (the seat that triggered the creation)."""
        sender = getattr(self._space_service, "send_roster_snapshot", None)
        if sender is None:
            return 0
        done = 0
        targets = list(await self._spaces.list_member_instances(space_id))
        if also is not None and also not in targets:
            targets.append(also)
        for inst in targets:
            if inst == self._own_instance_id:
                continue
            try:
                if await sender(space_id, to_instance_id=inst):
                    done += 1
            except Exception:
                log.exception("gfs.channel: snapshot to %s failed", inst)
        return done

    async def _on_config_event(self, event: object) -> None:
        """After an inbound ``SPACE_CONFIG_CHANGED`` was applied: re-read the
        STORED space (the inbound service already validated and pinned it)
        and follow the owner's ``private_gfs`` on a space we are a member
        of."""
        payload = getattr(event, "payload", None)
        space_id = payload.get("space_id") if isinstance(payload, dict) else None
        if not isinstance(space_id, str) or not space_id:
            return
        space = await self._spaces.get(space_id)
        if (
            space is None
            or space.space_type is not SpaceType.PRIVATE
            or space.owner_instance_id == self._own_instance_id
        ):
            return
        if space.features.private_gfs:
            self._spawn(self.subscribe(space_id), "subscribe")
        else:
            await self.drop_stale(space_id)

    async def drop_stale(self, space_id: str) -> bool:
        """Member side: leave a channel we may no longer use — the owner
        turned ``private_gfs`` off, or the current epoch came without a grant
        though an earlier one carried one (the owner retired the channel or
        removed us from it). Unsubscribes at the servers the last grant
        named and forgets the channel. ``True`` when a seat was dropped."""
        space = await self._spaces.get(space_id)
        if (
            space is None
            or space.space_type is not SpaceType.PRIVATE
            or space.owner_instance_id == self._own_instance_id
        ):
            return False
        stored = await self._spaces.get_gfs_channel(space_id)
        if stored is None:
            return False
        grant = await self.current_grant(space_id)
        if grant is not None and space.features.private_gfs:
            return False  # still ours to use
        if grant is None:
            grant = await self._previous_grant(space_id)
        gfs_ids = grant.gfs_ids if grant is not None else ()
        await self._spaces.set_gfs_channel(space_id, None, None)
        if gfs_ids:
            await self._unsubscribe_all(stored[0], gfs_ids)
        log.info(
            "gfs.channel: left channel %s of private space %s (no grant for "
            "the current epoch, or the owner turned the connection server off)",
            stored[0],
            space_id,
        )
        return True

    async def _previous_grant(self, space_id: str) -> GfsChannelGrant | None:
        """The newest stored grant below the current epoch that still names
        the stored channel."""
        epoch = await self._crypto.get_current_epoch(space_id)
        if epoch is None:
            return None
        found = await self._keys.latest_gfs_channel_before(space_id, epoch)
        if found is None:
            return None
        return await self.own_grant(space_id, found[0])

    async def _on_seat_live(self, event: SpaceRemoteSeatLive) -> None:
        self._spawn(self._after_seat(event.space_id, event.instance_id), "seat")

    async def _on_peer_transport(self, event: PeerTransportChanged) -> None:
        """A peer's DataChannel just opened (a reconnect after a blip)."""
        if event.transport == "rtc":
            await self._host_back(event.instance_id)

    async def _on_peer_advertised(self, event: PeerCapabilitiesAdvertised) -> None:
        """A peer re-advertised its capabilities — it does on every startup:
        a host that was down is back."""
        await self._host_back(event.instance_id)

    async def _host_back(self, host: str) -> None:
        """When ``host`` is the host of a private space where we hold a grant
        but no usable seat (a publish-only grant, or a pass for servers we
        are not connected to), ask it for a catch-up sync shortly — that is
        how the other members' items, which only reached the host while it
        was away, reach us. One pending catch-up per space."""
        if self._sync is None:
            return
        for space in await self._spaces.list_all():
            if (
                space.space_type is not SpaceType.PRIVATE
                or space.owner_instance_id != host
                or space.id in self._catching_up
            ):
                continue
            grant = await self.current_grant(space.id)
            if grant is None:
                continue
            if grant.channel_pass is not None and await self._capable_in(grant.gfs_ids):
                # We hold a seat on a server we are connected to: the
                # others' items reached us live.
                continue
            log.info(
                "gfs.channel: host of %s is back — catch-up sync in %.0f s",
                space.id,
                HOST_RETURN_SYNC_DELAY_S,
            )
            self._catching_up.add(space.id)
            self._spawn(self._catch_up_later(space.id, host), "catch-up")

    async def _catch_up_later(self, space_id: str, host: str) -> None:
        # Let the host drain its relay queue (the link-joined members' items)
        # before it serves the sync.
        try:
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=HOST_RETURN_SYNC_DELAY_S
                )
                return  # stopping
            except TimeoutError:
                pass
            if self._sync is not None:
                await self._sync.enqueue_sync_for_space(
                    space_id=space_id, peer_instance_id=host
                )
        finally:
            self._catching_up.discard(space_id)

    async def _after_seat(self, space_id: str, instance_id: str) -> None:
        """A remote seat went live: create the channel if this made the space
        qualify, announce it, then hand every member household its grant in
        a roster snapshot. A joiner of a space whose channel already exists
        got its grant in the redeem ACK (built once its seat exists), so a
        snapshot here would only race the ACK it follows."""
        try:
            if await self.reconcile(space_id, joining=instance_id) == "created":
                await self.announce_epoch(space_id)
                await self.distribute(space_id, also=instance_id)
        except Exception:
            log.exception("gfs.channel: reconcile after a seat failed")

    async def on_rotation(self, space_id: str) -> int:
        """At every content-key rotation, BEFORE the member rekey: the owner
        reconciles (so a space that lost its last remote member, or whose
        owner turned ``private_gfs`` off, retires its channel, and a newly
        qualifying one gets it in this rekey), then any seed holder announces
        the new epoch. Never raises."""
        try:
            await self.reconcile(space_id)
            return await self.announce_epoch(space_id)
        except Exception:
            log.exception("gfs.channel: rotation hook failed")
            return 0

    async def heal(self, gfs_id: str) -> int:
        """On a GFS (re)connect: the owner reconciles every private space it
        owns (re-registering is idempotent; a newly capable server or an
        upgrade gets channels created and distributed) and re-announces;
        members re-subscribe. Fail-soft per space."""
        conn = await self._conn_repo.get(gfs_id)
        if conn is None or conn.status != "active":
            return 0
        # A (re)connect restarts this server's take-over grace: what it
        # answers before we caught up is never evidence.
        self._connected_at[conn.id] = _monotonic()
        if not await self._gfs.private_channels_supported(conn):
            return 0
        done = 0
        for space in await self._spaces.list_all():
            if space.space_type is not SpaceType.PRIVATE:
                continue
            try:
                if space.owner_instance_id == self._own_instance_id:
                    result = await self.reconcile(space.id)
                    if result in ("created", "kept"):
                        await self.announce_epoch(space.id, only=gfs_id)
                        done += 1
                    if result == "created":
                        await self.distribute(space.id)
                elif await self.subscribe(space.id, only=gfs_id):
                    done += 1
            except Exception:
                log.exception("gfs.channel: heal failed for a private space")
        return done

    # ── Epoch notices (any seed holder) ──────────────────────────────────

    async def _channel_gfs_ids(self, space: "Space") -> tuple[str, ...]:
        """Where the channel lives: the owner's capable servers; a delegated
        admin reuses the servers its own grant names."""
        if space.owner_instance_id == self._own_instance_id:
            stored = await self._spaces.get_gfs_channel(space.id)
            if stored is None:
                return ()
            confirmed = self._registered.get(stored[0], set())
            return tuple(
                sorted(
                    c.gfs_instance_id
                    for c in await self._capable()
                    if c.gfs_instance_id in confirmed
                )
            )
        latest = await self._keys.get_latest(space.id)
        if latest is None:
            return ()
        grant = await self.own_grant(space.id, latest.epoch)
        return grant.gfs_ids if grant is not None else ()

    async def announce_epoch(self, space_id: str, *, only: str | None = None) -> int:
        """Tell the channel's servers the current content epoch (and mode,
        and the strict writer key pin). Any seed holder whose seed matches
        the pin; no-op without a channel. Returns how many accepted on the
        first attempt (a 429 lands later from the retry queue)."""
        space = await self._spaces.get(space_id)
        if space is None or space.space_type is not SpaceType.PRIVATE:
            return 0
        if await self._channel_seed(space) is None:
            return 0
        epoch = await self._crypto.get_current_epoch(space_id)
        if epoch is None:
            return 0
        item = GfsPublish(
            space_id=space_id, event_type=_KIND_NOTICE, payload={"epoch": epoch}
        )
        done = 0
        for conn in await self._capable_in(await self._channel_gfs_ids(space)):
            if only is not None and conn.id != only:
                continue
            if await self._first_attempt(conn, item):
                done += 1
        return done

    async def _post_notice(
        self, conn: GfsConnection, space_id: str, data: dict
    ) -> PublishOutcome:
        space = await self._spaces.get(space_id)
        if space is None:
            return PublishOutcome.permanent()
        seed = await self._matching_seed(space)
        found = await self._channel_seed(space)
        client = self._gfs.publish_client()
        if seed is None or found is None or client is None:
            return PublishOutcome.permanent()
        channel_id, channel_seed = found
        # The wire epoch: content epoch + the channel's secret offset.
        offset = derive_channel_epoch_offset(seed, space_id, channel_id)
        epoch = int(data["epoch"]) + offset
        strict = space.features.gfs_publish_mode == GFS_PUBLISH_MODE_STRICT
        wkc = (
            issue_channel_writer_key(
                space_seed=seed, space_id=space_id, channel_id=channel_id, epoch=epoch
            ).writer_key_cert
            if strict
            else None
        )
        body = sign_notice(
            channel_seed=channel_seed,
            channel_id=channel_id,
            gfs_instance_id=conn.gfs_instance_id,
            # Exact, not jittered: the server orders the mode by it (after
            # the epoch), and only seed holders send notices.
            ts=datetime.now(timezone.utc).isoformat(),
            epoch=epoch,
            publish_mode=space.features.gfs_publish_mode,
            writer_key_cert=wkc,
        ).to_wire()
        outcome, held = await self._post_reading(
            client, f"{conn.inbox_url}{CHANNEL_EPOCH_ROUTE}", body, conn
        )
        if (
            held is None
            or space.owner_instance_id != self._own_instance_id
            or not self._caught_up(conn)
        ):
            return outcome
        # Compare against the CURRENT epoch, not the (possibly stale) one
        # this notice was queued with: a rotation that landed since moved us.
        current = await self._crypto.get_current_epoch(space_id)
        if current is not None and current + offset > epoch:
            epoch = current + offset
            wkc = (
                issue_channel_writer_key(
                    space_seed=seed,
                    space_id=space_id,
                    channel_id=channel_id,
                    epoch=epoch,
                ).writer_key_cert
                if strict
                else None
            )
        if self._channel_taken_over(
            held, epoch=epoch, writer_pk=wkc.writer_pk if wkc else None
        ):
            # Another channel-key holder (a seed holder) moved the channel
            # past us — stepping its epoch, or pinning another writer key —
            # which locks our members out. Never fight: a fresh channel.
            log.warning(
                "gfs.channel: channel %s was moved past this owner's epoch on "
                "GFS %s — starting a fresh one",
                channel_id,
                conn.id,
            )
            self._spawn(self._self_heal(space_id, channel_id), "self-heal")
        return outcome

    def _caught_up(self, conn: GfsConnection) -> bool:
        """Whether this process has been running, and connected to ``conn``,
        for the take-over grace."""
        since = max(self._started_at, self._connected_at.get(conn.id, 0.0))
        return _monotonic() - since >= HEAL_GRACE_S

    @staticmethod
    def _channel_taken_over(held: dict, *, epoch: int, writer_pk: str | None) -> bool:
        """Whether the server's answer to our own notice shows another key
        holder got there first: an epoch above ours, or (strict) a writer key
        pin for exactly our epoch that is not ours. A missing or malformed
        field is no information — never a take-over."""
        server_epoch = held.get("epoch")
        if (
            not isinstance(server_epoch, int)
            or isinstance(server_epoch, bool)
            or not 0 <= server_epoch <= _MAX_WIRE_EPOCH
        ):
            return False
        if server_epoch > epoch:
            return True
        held_pk = held.get("writer_pk")
        return (
            writer_pk is not None
            and server_epoch == epoch
            and isinstance(held_pk, str)
            and held_pk != ""
            and held_pk != writer_pk
        )

    async def _claim_heal(self, space_id: str, channel_id: str) -> bool:
        """Take this space's take-over replacement slot: at most one per
        :data:`HEAL_COOLDOWN_S`, persisted on the space row so a restart
        does not reset it. ``False`` (with a WARNING) inside the cooldown."""
        now = _utcnow()
        last = await self._spaces.get_gfs_channel_healed_at(space_id)
        if last is not None:
            try:
                since = now - datetime.fromisoformat(last)
            except TypeError, ValueError:
                since = timedelta(0)
            if since < timedelta(seconds=HEAL_COOLDOWN_S):
                log.warning(
                    "gfs.channel: channel %s looks taken over or squatted, but "
                    "this space already replaced its channel %s ago — keeping "
                    "it (cooldown %d s)",
                    channel_id,
                    since,
                    HEAL_COOLDOWN_S,
                )
                return False
        await self._spaces.set_gfs_channel_healed_at(space_id, now.isoformat())
        return True

    async def _self_heal(self, space_id: str, channel_id: str) -> None:
        """Replace a channel another key holder took over (rate-limited per
        space by :meth:`_claim_heal`)."""
        if space_id in self._healing:
            return
        self._healing.add(space_id)
        try:
            stored = await self._spaces.get_gfs_channel(space_id)
            if stored is None or stored[0] != channel_id:
                return
            if not await self._claim_heal(space_id, channel_id):
                return
            if await self.reconcile(space_id, fresh=True) == "created":
                await self.announce_epoch(space_id)
                await self.distribute(space_id)
        except Exception:
            log.exception("gfs.channel: self-heal failed")
        finally:
            self._healing.discard(space_id)

    async def _post_reading(
        self,
        client: aiohttp.ClientSession,
        url: str,
        body: dict,
        conn: GfsConnection,
    ) -> tuple[PublishOutcome, dict | None]:
        """:meth:`_post`, also returning a 200's JSON body."""
        try:
            async with client.post(
                url, json=body, allow_redirects=False, timeout=_HTTP_TIMEOUT
            ) as resp:
                outcome = classify_publish_status(
                    resp.status, resp.headers.get("Retry-After")
                )
                held: dict | None = None
                if resp.status == 200:
                    try:
                        parsed = await resp.json()
                    except aiohttp.ContentTypeError, ValueError:
                        parsed = None
                    held = parsed if isinstance(parsed, dict) else None
                if outcome.kind != "delivered":
                    log.warning(
                        "gfs.channel: GFS %s answered HTTP %d (%s) on epoch",
                        conn.id,
                        resp.status,
                        outcome.kind,
                    )
                return outcome, held
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning(
                "gfs.channel: GFS %s unreachable: %s",
                conn.id,
                exc or type(exc).__name__,
            )
            return PublishOutcome.transient(), None

    # ── Grants (any seed holder) ─────────────────────────────────────────

    async def grant_for_peer(
        self, space_id: str, instance_id: str, *, epoch: int | None = None
    ) -> dict | None:
        """The channel grant to deliver to peer ``instance_id`` at ``epoch``
        (default: our current content epoch), or ``None`` — not v_51, not a
        private space with a channel we can sign for, no live seat, or no
        known identity key. Never to the space's OWNER household: it must not
        hold a grant, or it would subscribe (and publish) and name itself."""
        if self._federation is None or instance_id == self._own_instance_id:
            return None
        # A mesh-only member (no ``remote_instances`` row) is judged by the
        # version it claimed over the mesh; an unknown one gets nothing.
        if not await self._federation.space_member_supports(
            instance_id, min_version=FederationCapability.MIN_FOR_PRIVATE_CHANNELS
        ):
            return None
        space = await self._spaces.get(space_id)
        if (
            space is None
            or space.space_type is not SpaceType.PRIVATE
            or instance_id == space.owner_instance_id
        ):
            return None
        found = await self._channel_seed(space)
        seed = await self._matching_seed(space)
        if found is None or seed is None:
            return None
        if not await self._live_seat(space_id, instance_id):
            return None
        if epoch is None:
            latest = await self._keys.get_latest(space_id)
            if latest is None:
                return None
            epoch = latest.epoch
        pk = await self._writer_certs.verified_instance_pk(instance_id)
        if pk is None or len(pk) != 32:
            return None
        gfs_ids = (await self._channel_gfs_ids(space))[:MAX_GRANT_GFS_IDS]
        if not gfs_ids:
            return None
        channel_id, channel_seed = found
        offset = derive_channel_epoch_offset(seed, space_id, channel_id)
        wire_epoch = int(epoch) + offset
        strict = space.features.gfs_publish_mode == GFS_PUBLISH_MODE_STRICT
        scope = (
            await self._writer_certs.entitlement_for_instance(space, instance_id)
        ).scope
        # Trusted mode publishes with the cert; strict mode with the writer
        # key alone (a strict member holds no identified credential).
        cert = (
            issue_channel_cert(
                channel_seed=channel_seed,
                channel_id=channel_id,
                epoch=wire_epoch,
                instance_pk=pk,
                scope=scope,
            )
            if scope is not None and not strict
            else None
        )
        writer_key = (
            issue_channel_writer_key(
                space_seed=seed,
                space_id=space_id,
                channel_id=channel_id,
                epoch=wire_epoch,
            )
            if scope is not None and strict
            else None
        )
        # Who takes a seat: a link-joined household always (it reaches the
        # host only over the server anyway). With the owner's ``private_gfs``
        # ON, every member household too — paired and mesh-only — so all of
        # them receive each other's items while the host is offline: the
        # owner's explicit opt-in to the server learning those households.
        # Without the option a paired member's grant would be publish-only
        # (#815) — but a space with the option OFF has no channel at all.
        seated = space.features.private_gfs or await self._is_link_joined(
            space_id, instance_id
        )
        if not seated and cert is None and writer_key is None:
            return None
        grant = GfsChannelGrant(
            channel_suite="ed25519",
            space_id=space_id,
            channel_id=channel_id,
            channel_pk=channel_pk_of(channel_seed),
            epoch=int(epoch),
            epoch_offset=offset,
            gfs_ids=tuple(gfs_ids),
            binding_sig_suite="ed25519",
            binding_sig="",
            channel_pass=(
                issue_channel_pass(
                    channel_seed=channel_seed,
                    channel_id=channel_id,
                    epoch=wire_epoch,
                    instance_pk=pk,
                )
                if seated
                else None
            ),
            channel_cert=cert,
            writer_key=writer_key,
        )
        return bind_grant(grant, space_seed=seed).to_wire()

    # ── Holding (member households) ──────────────────────────────────────

    async def accept_grant(self, space_id: str, raw: object) -> bool:
        """Verify + store a grant a seed holder delivered to us; switch to
        its channel (unsubscribing an old one) and subscribe when it is for
        the current epoch. ``True`` when stored. WARNING on a bad grant."""
        if raw is None or self._kek is None:
            return False
        space = await self._spaces.get(space_id)
        if (
            space is None
            or space.space_type is not SpaceType.PRIVATE
            or space.owner_instance_id == self._own_instance_id
        ):
            # The owner never holds a grant (a delegated admin's rekey may
            # carry one for it): it would subscribe and name itself.
            return False
        try:
            grant = GfsChannelGrant.from_wire(raw)
            verify_grant(
                grant,
                space_pubkey=bytes.fromhex(space.identity_public_key),
                space_id=space_id,
                own_pk=self._own_pk,
            )
        except ValueError as exc:
            log.warning("gfs.channel: grant for space %s refused: %s", space_id, exc)
            return False
        # No first-come claim on the id: another space's owner could bind a
        # grant to our space's channel id and block the real one. Several
        # spaces may name an id; inbound frames try each (the content key
        # decides).
        # Store first, switch second: a grant that can't be kept (no key for
        # its epoch yet) must not move us off the channel we use.
        wrapped = self._kek.encrypt(
            canonical(grant.to_wire()),
            associated_data=_grant_aad(space_id, grant.epoch),
        )
        stored = await self._keys.set_gfs_channel(space_id, grant.epoch, wrapped)
        if not stored:
            log.info(
                "gfs.channel: grant for space %s epoch %d: no key for that epoch",
                space_id,
                grant.epoch,
            )
            return False
        previous = await self._spaces.get_gfs_channel(space_id)
        current_epoch = await self._crypto.get_current_epoch(space_id)
        if (
            previous is not None
            and previous[0] != grant.channel_id
            and current_epoch is not None
            and grant.epoch < current_epoch
        ):
            # A late grant for an older epoch never moves us back to a
            # channel the owner has since replaced; it is kept for its epoch.
            return True
        if (
            previous is None or previous[0] != grant.channel_id
        ) and not await self._spaces.set_gfs_channel(
            space_id, grant.channel_id, grant.channel_pk
        ):
            log.warning(
                "gfs.channel: switching space %s to its channel failed", space_id
            )
            return False
        if previous is not None and previous[0] != grant.channel_id:
            self._spawn(
                self._unsubscribe_all(previous[0], grant.gfs_ids), "unsubscribe"
            )
        if grant.epoch == current_epoch and grant.channel_pass is not None:
            self._spawn(self.subscribe(space_id), "subscribe")
        return True

    async def own_grant(self, space_id: str, epoch: int) -> GfsChannelGrant | None:
        """The stored grant for ``(space, epoch)`` while it still verifies
        against the CURRENT pin and names the space's current channel."""
        if self._kek is None:
            return None
        wrapped = await self._keys.get_gfs_channel(space_id, epoch)
        space = await self._spaces.get(space_id)
        current = await self._spaces.get_gfs_channel(space_id)
        if wrapped is None or space is None or current is None:
            return None
        try:
            raw = self._kek.decrypt(
                wrapped, associated_data=_grant_aad(space_id, epoch)
            )
            grant = GfsChannelGrant.from_wire(json.loads(raw))
            verify_grant(
                grant,
                space_pubkey=bytes.fromhex(space.identity_public_key),
                space_id=space_id,
                own_pk=self._own_pk,
            )
        except Exception as exc:
            log.info(
                "gfs.channel: stored grant for space %s no longer holds: %s",
                space_id,
                exc,
            )
            return None
        if grant.epoch != epoch or grant.channel_id != current[0]:
            return None
        return grant

    async def current_grant(self, space_id: str) -> GfsChannelGrant | None:
        epoch = await self._crypto.get_current_epoch(space_id)
        if epoch is None:
            return None
        return await self.own_grant(space_id, epoch)

    async def has_channel(self, space_id: str) -> bool:
        """Whether this private space currently uses a channel here (the
        owner's own, or a member's grant)."""
        return await self._spaces.get_gfs_channel(space_id) is not None

    # ── Seats (identified, like a follower) ──────────────────────────────

    async def subscribe(self, space_id: str, *, only: str | None = None) -> int:
        """Take our fan-out seat on the channel's servers with the current
        grant's pass. Fail-soft per server. Returns how many accepted."""
        space = await self._spaces.get(space_id)
        if space is None or not space.features.private_gfs:
            # Defence in depth: never a seat for a space whose stored option
            # is OFF, whatever grant we hold.
            return 0
        grant = await self.current_grant(space_id)
        if grant is None or grant.channel_pass is None:
            # A publish-only member never takes a seat.
            return 0
        done = 0
        for conn in await self._capable_in(grant.gfs_ids):
            if only is not None and conn.id != only:
                continue
            req = ChannelSubscribeRequest(
                instance_id=self._own_instance_id,
                gfs_instance_id=conn.gfs_instance_id,
                channel_id=grant.channel_id,
                ts=datetime.now(timezone.utc).isoformat(),
                signature="",
                channel_pass=grant.channel_pass,
            )
            sig = sign_ed25519(self._own_seed, canonical(req.signing_payload()))
            body = replace(req, signature=b64url_encode(sig)).to_wire()
            outcome = await self._post(
                self._gfs.client(),
                f"{conn.inbox_url}{CHANNEL_SUBSCRIBE_ROUTE}",
                body,
                conn,
            )
            if outcome.kind == "delivered":
                done += 1
        return done

    async def _unsubscribe_all(self, channel_id: str, gfs_ids: tuple[str, ...]) -> None:
        for conn in await self._capable_in(gfs_ids):
            req = ChannelUnsubscribeRequest(
                instance_id=self._own_instance_id,
                gfs_instance_id=conn.gfs_instance_id,
                channel_id=channel_id,
                ts=datetime.now(timezone.utc).isoformat(),
                signature="",
            )
            sig = sign_ed25519(self._own_seed, canonical(req.signing_payload()))
            await self._post(
                self._gfs.client(),
                f"{conn.inbox_url}{CHANNEL_UNSUBSCRIBE_ROUTE}",
                replace(req, signature=b64url_encode(sig)).to_wire(),
                conn,
            )

    # ── Publishing an item (called by the member publisher) ──────────────

    async def plan(self, space_id: str) -> list[GfsConnection]:
        """The servers our item in private ``space_id`` goes to, or ``[]``:
        no grant for the current epoch; a strict space without the channel
        writer key (never identified there); or a trusted grant without a
        channel cert (a reader seat). ``[]`` means the host path."""
        space = await self._spaces.get(space_id)
        if (
            space is None
            or space.space_type is not SpaceType.PRIVATE
            or not space.features.private_gfs
        ):
            return []
        grant = await self.current_grant(space_id)
        if grant is None:
            return []
        if grant.writer_key is None:
            if space.features.gfs_publish_mode == GFS_PUBLISH_MODE_STRICT:
                return []
            if grant.channel_cert is None:
                return []
        return await self._capable_in(grant.gfs_ids)

    async def publish_sealed(
        self,
        space_id: str,
        epoch: int,
        ciphertext: str,
        targets: list[GfsConnection],
    ) -> list[GfsConnection]:
        """Publish an item already sealed at ``epoch`` (the member publisher
        sealed it with our space writer cert inside) to ``targets``:
        anonymous with the channel writer key when the grant carries one,
        else identified with the channel cert — never identified in a strict
        space. Returns the servers that accepted on the first attempt."""
        grant = await self.own_grant(space_id, epoch)
        space = await self._spaces.get(space_id)
        if grant is None or space is None or not space.features.private_gfs:
            return []
        if grant.writer_key is not None:
            kind = _KIND_ANON
        elif (
            space.features.gfs_publish_mode == GFS_PUBLISH_MODE_STRICT
            or grant.channel_cert is None
        ):
            return []
        else:
            kind = _KIND_ITEM
        item = GfsPublish(
            space_id=space_id,
            event_type=kind,
            payload={"epoch": epoch, "payload": ciphertext},
        )
        accepted: list[GfsConnection] = []
        for conn in targets:
            if conn.gfs_instance_id not in grant.gfs_ids:
                continue
            if await self._first_attempt(conn, item):
                accepted.append(conn)
        return accepted

    async def _post_item(
        self, conn: GfsConnection, space_id: str, data: dict, *, anon: bool
    ) -> PublishOutcome:
        """One attempt, re-read from the stored grant (an attempt after the
        grant is gone is dropped, never downgraded) and signed afresh."""
        epoch = int(data["epoch"])
        grant = await self.own_grant(space_id, epoch)
        space = await self._spaces.get(space_id)
        if grant is None or space is None or not space.features.private_gfs:
            return PublishOutcome.permanent()
        if anon:
            client = self._gfs.publish_client()
            if grant.writer_key is None or client is None:
                return PublishOutcome.permanent()
            req = sign_publish_anon(
                ChannelPublishAnonRequest(
                    gfs_instance_id=conn.gfs_instance_id,
                    channel_id=grant.channel_id,
                    ts=_anon_ts(),
                    nonce=b64url_encode(secrets.token_bytes(16)),
                    epoch=grant.channel_epoch,
                    payload=str(data["payload"]),
                    writer_sig="",
                    writer_sig_suite=WRITER_KEY_SUITE_ED25519,
                ),
                writer_seed=b64url_decode(grant.writer_key.writer_seed),
            )
            return await self._post(
                client,
                f"{conn.inbox_url}{CHANNEL_PUBLISH_ANON_ROUTE}",
                req.to_wire(),
                conn,
            )
        if (
            space.features.gfs_publish_mode == GFS_PUBLISH_MODE_STRICT
            or grant.channel_cert is None
        ):
            # The space went strict after this was queued: never identified.
            return PublishOutcome.permanent()
        pub = ChannelPublishRequest(
            instance_id=self._own_instance_id,
            gfs_instance_id=conn.gfs_instance_id,
            channel_id=grant.channel_id,
            ts=datetime.now(timezone.utc).isoformat(),
            signature="",
            epoch=grant.channel_epoch,
            channel_cert=grant.channel_cert,
            payload=str(data["payload"]),
        )
        sig = sign_ed25519(self._own_seed, pub.signing_bytes())
        return await self._post(
            self._gfs.client(),
            f"{conn.inbox_url}{CHANNEL_PUBLISH_ROUTE}",
            replace(pub, signature=b64url_encode(sig)).to_wire(),
            conn,
        )

    # ── Inbound ──────────────────────────────────────────────────────────

    async def handle_frame(self, frame: dict) -> None:
        """A channel fan-out frame: route it to its space by the channel id
        and run the very checks a public ``space_item`` gets (decrypt, the
        real type, author signature, writer cert from inside the ciphertext
        against the pinned space key, epoch freshness, roster). A frame for
        an unknown channel is dropped."""
        try:
            item = ChannelItemFrame.from_wire(frame)
        except InvalidChannelWire:
            log.warning("gfs.channel: malformed channel frame — dropped")
            return
        if self._inbound is None:
            return
        candidates = await self._spaces.spaces_for_gfs_channel(item.channel_id)
        if not candidates:
            log.debug("gfs.channel: frame for an unknown channel — dropped")
            return
        # Normally exactly one. Should another space's owner have bound a
        # grant to this id, each candidate is tried: the item decrypts only
        # under the space it was sealed for, and its signed ``space_id``
        # must match — so it can never land in the wrong space.
        for space_id in candidates:
            space = await self._spaces.get(space_id)
            if space is None or space.space_type is not SpaceType.PRIVATE:
                continue
            # The frame carries the channel epoch: shift it back by this
            # space's channel offset (from our current grant).
            grant = await self.current_grant(space_id)
            if grant is None or grant.channel_id != item.channel_id:
                continue
            content_epoch = item.epoch - grant.epoch_offset
            if content_epoch < 0:
                continue
            await self._inbound.handle_channel_item(
                space_id, epoch=content_epoch, payload=item.payload
            )

    # ── Transport + retries ──────────────────────────────────────────────

    async def _post(
        self,
        client: aiohttp.ClientSession,
        url: str,
        body: dict,
        conn: GfsConnection,
    ) -> PublishOutcome:
        try:
            async with client.post(
                url, json=body, allow_redirects=False, timeout=_HTTP_TIMEOUT
            ) as resp:
                outcome = classify_publish_status(
                    resp.status, resp.headers.get("Retry-After")
                )
                if outcome.kind != "delivered":
                    log.warning(
                        "gfs.channel: GFS %s answered HTTP %d (%s) on %s",
                        conn.id,
                        resp.status,
                        outcome.kind,
                        url.rsplit("/", 1)[-1],
                    )
                return outcome
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning(
                "gfs.channel: GFS %s unreachable: %s",
                conn.id,
                exc or type(exc).__name__,
            )
            return PublishOutcome.transient()

    async def _send(self, conn: GfsConnection, item: GfsPublish) -> PublishOutcome:
        if item.event_type == _KIND_NOTICE:
            return await self._post_notice(conn, item.space_id, item.payload)
        return await self._post_item(
            conn, item.space_id, item.payload, anon=item.event_type == _KIND_ANON
        )

    async def _first_attempt(self, conn: GfsConnection, item: GfsPublish) -> bool:
        if self._retry.pending(conn.id):
            self._retry.enqueue(conn.id, item)
            return False
        outcome = await self._send(conn, item)
        if outcome.kind == "transient" and not self._retry.enqueue(
            conn.id, item, retry_after_s=outcome.retry_after_s
        ):
            log.warning(
                "gfs.channel: %s to GFS %s is lost — the retry queue refused it",
                item.event_type,
                conn.id,
            )
        return outcome.kind == "delivered"

    async def _retry_send(self, conn_id: str, item: GfsPublish) -> PublishOutcome:
        conn = await self._conn_repo.get(conn_id)
        if conn is None or conn.status != "active":
            return PublishOutcome.permanent()
        if not await self._gfs.private_channels_supported(conn):
            return PublishOutcome.permanent()
        return await self._send(conn, item)
