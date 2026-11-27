"""Household side of member publish over the GFS — the opaque channel only.

A member household — any writer, including one seated from an invite link
with no pairing to the host, the OWNER's own posts included — publishes its
OWN space items into the space's opaque channel
(:mod:`socialhome.services.gfs_channel_service`): a PRIVATE space whose owner
turned ``private_gfs`` on (v_51), or a GLOBAL space that stores a channel
here (v_96). The item is the space-content-key ciphertext of
``{"item_type": <type>, "inner": <author-signed inner + writer_cert>}``
(:func:`build_item_plaintext` / :func:`parse_item_plaintext`); the channel
frame names no space and no household. A ``post`` / ``post_edit`` inner is
the ``build_signed_author_inner`` object (receivers verify it with the same
code); every other type carries the generic inner of
:mod:`socialhome.services.space_item_author`. The real type and its target
are bound inside the author signature and never leave the ciphertext. Which
types exist and the scope each needs: :mod:`socialhome.domain.space_item`.

An item goes into the channel only when our writer cert for the current
epoch lets THIS author publish THIS type (:func:`_cert_lets`: the scope, a
v2 user binding naming them, and for a follower's ``reader`` cert the
matching switch). Otherwise — or for a space with no channel here —
nothing goes to a connection server: the write takes the host path (the
member broadcast, which always runs) and readers catch up from the host by
sync. The space-keyed ``/gfs/member-publish-anon`` and
``/gfs/spaces/{id}/epoch`` requests are gone (v_96).

It also announces a space's content epoch into its channel at each rotation
(:meth:`GfsMemberPublishService.announce_epoch`) and heals the channels on a
GFS (re)connect (:meth:`GfsMemberPublishService.announce_held_epochs`).
Publishing runs in the background (:meth:`GfsMemberPublishService
.schedule_item`) — a local write never waits on a connection server.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import orjson

from ..domain.federation import FederationEventType, GfsConnection
from ..domain.federation_retention import supersede_key
from ..domain.gfs_channel import RING_CLASS_DURABLE
from ..domain.space import SpaceFeatures, SpaceType
from ..domain.space_item import (
    BUDGET_CONTENT,
    BUDGET_LOCATION,
    EVENT_ITEM_SIZE_BUCKETS,
    ITEM_SIZE_BUCKETS,
    ITEM_SPECS,
    ITEM_TYPE_POST,
    MAX_MENTION_NOTES,
    SUPPORTED_ITEM_TYPES,
    V1_CHANNEL_EVENT_TYPES,
    SealedItem,
    pad_json_object,
    reader_cert_permits,
    required_scope,
)
from ..domain.space_item_outbox import UNSENT_SEAL_FAILED
from ..domain.writer_cert import WriterCert, scope_permits
from ..task_drain import wait_tasks_done
from .gfs_channel_service import CHANNEL_TIERS
from .space_item_author import build_signed_event_item
from .space_item_budget import SenderBudget
from .space_writer_cert_service import WRITER_CERT_FIELD

if TYPE_CHECKING:
    from ..domain.space import Space
    from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
    from ..repositories.instance_config_repo import AbstractInstanceConfigRepo
    from ..repositories.space_remote_member_repo import (
        AbstractSpaceRemoteMemberRepo,
    )
    from ..repositories.space_repo import AbstractSpaceRepo
    from .gfs_channel_service import GfsChannelService
    from .space_crypto_service import SpaceContentEncryption
    from .space_item_outbox import SpaceItemOutbox
    from .space_mentions import SpaceMentionResolver
    from .space_writer_cert_service import SpaceWriterCertService

log = logging.getLogger(__name__)


#: Background publishes in flight at once; past it a new one is dropped
#: (logged) — the host path still carries the write.
MAX_PENDING_PUBLISHES: int = 64


def _cert_lets(
    cert: dict, author_user_id: str, item_type: str, features: SpaceFeatures
) -> bool:
    """Whether our wire cert lets ``author_user_id`` publish ``item_type``:
    the scope the type needs (:func:`required_scope` — ``write`` for a post
    and its edit / delete, ``comment`` for comments and reactions) and a v2
    user binding naming them. The binding names the users holding the
    cert's scope, so a comment-only user of a household holding a ``write``
    cert is not bound and keeps the host path. A follower's ``reader`` cert
    (v_96) also needs the matching follower switch in our pinned config
    (:func:`reader_cert_permits`) — receivers refuse it otherwise."""
    users = cert.get("writer_user_ids")
    try:
        parsed = WriterCert.from_wire(cert)
    except ValueError:
        return False
    return (
        scope_permits(parsed.scope, required_scope(item_type))
        and bool(parsed.users_sig)
        and isinstance(users, list)
        and author_user_id in users
        and reader_cert_permits(parsed, item_type, features)
    )


#: The payload fields that name the acting local person, in order (a chat
#: message's ``author``, a sticky's ``actor_user_id``, a vote's
#: ``voter_user_id`` …). An item naming nobody (a task delete) has no
#: author to tell: its failure expires unseen after a week.
_AUTHOR_KEYS: tuple[str, ...] = (
    "user_id",
    "author_user_id",
    "author",
    "actor_user_id",
    "voter_user_id",
    "created_by",
    "updated_by",
    "deleted_by",
)


def _author_of(payload: dict) -> str:
    """The local person a change's payload names (the budget and the
    "Couldn't send" notice are theirs); ``""`` when none."""
    for key in _AUTHOR_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


#: v_97 (owner Q6 A) — how long the legacy event still goes to every
#: member after this household first put a space's change on its channel.
LEGACY_WINDOW: timedelta = timedelta(days=14)
LEGACY_WINDOW_KEY_PREFIX: str = "space_items_channel_since:"


def build_item_plaintext(
    item_type: str, inner: dict, *, buckets: tuple[int, ...] = ITEM_SIZE_BUCKETS
) -> bytes:
    """The bytes a channel item encrypts: the real type + the inner, padded
    JSON (:func:`~socialhome.domain.space_item.pad_json_object`) — ASCII
    ``0`` in :data:`~socialhome.domain.space_item.PAD_FIELD` to exactly the
    smallest :data:`~socialhome.domain.space_item.ITEM_SIZE_BUCKETS` size
    that fits (an item larger than the largest bucket is left unpadded)."""
    return pad_json_object({"item_type": item_type, "inner": inner}, buckets=buckets)


def parse_item_plaintext(plaintext: bytes) -> tuple[str, dict] | None:
    """``(item_type, inner)`` from decrypted bytes, or ``None`` if malformed
    or of a type this build does not carry. The size padding is ignored."""
    try:
        body = orjson.loads(plaintext)
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    item_type = body.get("item_type")
    inner = body.get("inner")
    if item_type not in SUPPORTED_ITEM_TYPES or not isinstance(inner, dict):
        return None
    return str(item_type), inner


class GfsMemberPublishService:
    """Publish this household's own items into a space's opaque channel;
    announce epochs there; heal the channels on a reconnect."""

    __slots__ = (
        "_budget",
        "_channels",
        "_config",
        "_conn_repo",
        "_crypto",
        "_identity",
        "_mentions",
        "_outbox",
        "_remote_members",
        "_spaces",
        "_stopping",
        "_tasks",
        "_writer_certs",
    )

    def __init__(
        self,
        *,
        conn_repo: AbstractGfsConnectionRepo,
        space_repo: AbstractSpaceRepo,
        space_crypto: SpaceContentEncryption,
        writer_certs: SpaceWriterCertService,
    ) -> None:
        self._conn_repo = conn_repo
        self._spaces = space_repo
        self._crypto = space_crypto
        self._writer_certs = writer_certs
        #: Background publishes in flight (strong refs) and the stop flag.
        self._tasks: set[asyncio.Task[None]] = set()
        self._stopping = False
        #: The opaque channels (v_51 private, v_96 global). ``None`` → every
        #: write takes the host path.
        self._channels: GfsChannelService | None = None
        #: v_97 — ``(instance_id, identity_seed, identity_pk)``: the origin
        #: that signs this household's event-shaped space items.
        self._identity: tuple[str, bytes, bytes] | None = None
        #: v_97 — when a person's chat / location item may first go.
        self._budget = SenderBudget()
        #: v_97 — the legacy-window start per space, the mention resolver
        #: and the roster for mention notes (``attach_items``).
        self._config: AbstractInstanceConfigRepo | None = None
        self._mentions: SpaceMentionResolver | None = None
        self._remote_members: AbstractSpaceRemoteMemberRepo | None = None
        #: v_97 — the durable queue a GFS space's items wait in.
        self._outbox: SpaceItemOutbox | None = None

    def attach_outbox(self, outbox: SpaceItemOutbox) -> None:
        """v_97 — the durable item queue (owner: retry, never a fallback)."""
        self._outbox = outbox

    def attach_items(
        self,
        *,
        config: AbstractInstanceConfigRepo,
        mentions: SpaceMentionResolver,
        remote_members: AbstractSpaceRemoteMemberRepo,
    ) -> None:
        """v_97 — what a GFS space's items need beyond sealing."""
        self._config = config
        self._mentions = mentions
        self._remote_members = remote_members

    def attach_identity(
        self, *, instance_id: str, identity_seed: bytes, identity_pk: bytes
    ) -> None:
        """v_97 — the household identity that signs its space items."""
        self._identity = (instance_id, identity_seed, identity_pk)

    def attach_channels(self, channels: GfsChannelService) -> None:
        """Wire the channel service (v_51 / v_96)."""
        self._channels = channels

    async def _channel_tier(self, space_id: str) -> bool:
        """A tier whose channel this household may hold: PRIVATE, or GLOBAL
        since v_96 (the channel service retires / reconciles both)."""
        space = await self._spaces.get(space_id)
        return space is not None and space.space_type in CHANNEL_TIERS

    async def _on_channel(self, space: Space | None) -> bool:
        """Whether *space*'s items go through its opaque channel: a PRIVATE
        space (the channel service decides whether one runs), or a GLOBAL
        space once it stores a channel here (v_96 — the owner registered
        one, or we hold a grant)."""
        if space is None or self._channels is None:
            return False
        if space.space_type is SpaceType.PRIVATE:
            return True
        return (
            space.space_type is SpaceType.GLOBAL
            and await self._channels.has_channel(space.id)
        )

    async def channel_space(self, space_id: str) -> bool:
        """Whether ``space_id`` is a PRIVATE (v_51) or GLOBAL (v_96) space
        using an opaque channel here — its local writes are offered to the
        member publisher, which then decides by our grant and cert."""
        return (
            self._channels is not None
            and await self._channel_tier(space_id)
            and await self._channels.has_channel(space_id)
        )

    async def retire_channel(self, space_id: str) -> None:
        """Before an authority rotation swaps the seed: unregister a private
        space's channel while its key can still be signed for."""
        if self._channels is not None and await self._channel_tier(space_id):
            await self._channels.retire(space_id)

    async def enable_channel(self, space_id: str) -> None:
        """The owner turned a private space's ``private_gfs`` ON: start (or
        keep) its channel, announce it and distribute the grants."""
        if self._channels is not None and await self._channel_tier(space_id):
            await self._channels.enable(space_id)

    async def reconcile_channel(self, space_id: str) -> None:
        """Before an authority-rotation bundle goes out: let the channel
        service start a fresh channel for the new seed (and announce it), so
        the bundle carries grants for it. No-op for other spaces."""
        if self._channels is not None and await self._channel_tier(space_id):
            await self._channels.on_rotation(space_id)

    async def channel_rekey_sent(self, space_id: str) -> None:
        """The member rekey of a content-key rotation we ran went out: a
        private space's channel service renews our own seat now, as the
        members renew theirs on that rekey
        (:meth:`GfsChannelService.on_rekey_sent`). No-op for other spaces."""
        if self._channels is not None and await self._channel_tier(space_id):
            await self._channels.on_rekey_sent(space_id)

    async def start(self) -> None:
        self._stopping = False

    async def stop(self) -> None:
        """Let in-flight background publishes (and held items) finish."""
        self._stopping = True
        await self.wait_idle()

    # ── Publishing an item ────────────────────────────────────────────────

    async def plan_post(
        self, space_id: str, author_user_id: str
    ) -> list[GfsConnection]:
        """:meth:`plan_item` for a post."""
        return await self.plan_item(space_id, author_user_id, ITEM_TYPE_POST)

    async def plan_item(
        self, space_id: str, author_user_id: str, item_type: str
    ) -> list[GfsConnection]:
        """The connection servers this household will publish
        ``author_user_id``'s ``item_type`` item in *space_id* to, or ``[]``:
        the space has no channel here, or our cert for the current epoch
        does not let THIS author publish THIS type (no cert, too weak a
        scope, the v2 user binding does not name them, or a follower switch
        is off). ``[]`` means the host path alone."""
        space = await self._spaces.get(space_id)
        if self._channels is None or not await self._on_channel(space):
            return []
        assert space is not None
        cert = await self._writer_certs.current_own_cert_wire(space_id)
        if cert is None or not _cert_lets(
            cert, author_user_id, item_type, space.features
        ):
            return []
        return await self._channels.plan(space_id)

    def schedule_post(
        self,
        space_id: str,
        author_user_id: str,
        inner: dict,
        *,
        on_published: Callable[[], Awaitable[None]] | None = None,
    ) -> bool:
        """:meth:`schedule_item` for a post."""
        return self.schedule_item(
            space_id,
            author_user_id,
            ITEM_TYPE_POST,
            inner,
            on_published=on_published,
        )

    def schedule_item(
        self,
        space_id: str,
        author_user_id: str,
        item_type: str,
        inner: dict,
        *,
        on_published: Callable[[], Awaitable[None]] | None = None,
    ) -> bool:
        """Plan and publish ``author_user_id``'s ``item_type`` item in the
        BACKGROUND, so the local write never waits on a connection server.
        Returns whether a task was started (``False`` when stopping or too
        many are pending — the host path still carries the write).
        ``on_published`` runs once at least one server took the item —
        never when none did (no cert, no capable server, every send
        refused)."""
        if self._stopping or len(self._tasks) >= MAX_PENDING_PUBLISHES:
            log.warning(
                "gfs.member_publish: background publish for space %s skipped "
                "(%s) — the host path carries it",
                space_id,
                "stopping" if self._stopping else "too many pending",
            )
            return False
        task = asyncio.create_task(
            self._plan_and_publish(
                space_id, author_user_id, item_type, inner, on_published
            ),
            name=f"gfs-member-publish-{space_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def _plan_and_publish(
        self,
        space_id: str,
        author_user_id: str,
        item_type: str,
        inner: dict,
        on_published: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        try:
            targets = await self.plan_item(space_id, author_user_id, item_type)
            if not targets:
                return
            accepted = await self.publish_item(space_id, item_type, inner, targets)
            if accepted and on_published is not None:
                await on_published()
        except Exception:
            log.exception(
                "gfs.member_publish: background publish failed for %s", space_id
            )

    async def wait_idle(self) -> None:
        """Wait for every background publish (tests, graceful stop)."""
        await wait_tasks_done(lambda: self._tasks)

    async def publish_item(
        self,
        space_id: str,
        item_type: str,
        inner: dict,
        targets: list[GfsConnection],
    ) -> list[GfsConnection]:
        """Encrypt the author-signed *inner* (built with its bound
        ``item_type`` / ``item_target``) as ``item_type`` with our writer
        cert for the epoch it is sealed under, and put it into the space's
        channel on *targets*. Returns the servers that accepted it on the
        first attempt (the channel service retries the rest)."""
        if not targets or self._channels is None:
            return []
        if not await self._on_channel(await self._spaces.get(space_id)):
            return []
        sealed = await self._seal(space_id, item_type, inner)
        if sealed is None:
            return []
        epoch, ciphertext = sealed
        return await self._channels.publish_sealed(space_id, epoch, ciphertext, targets)

    async def _seal(
        self, space_id: str, item_type: str, inner: dict
    ) -> tuple[int, str] | None:
        """``(epoch, ciphertext)`` of the item sealed (padded JSON — a
        channel carries JSON only) with our writer cert for the epoch it is
        encrypted under."""
        body = {k: v for k, v in inner.items() if k != WRITER_CERT_FIELD}
        needed = required_scope(item_type)
        for _attempt in range(2):
            epoch = await self._crypto.get_current_epoch(space_id)
            if epoch is None:
                return None
            cert = await self._writer_certs.own_cert(space_id, epoch)
            if cert is None or not scope_permits(cert.scope, needed):
                log.info(
                    "gfs.member_publish: no %s cert for space %s epoch %d",
                    needed,
                    space_id,
                    epoch,
                )
                return None
            item = {**body, WRITER_CERT_FIELD: cert.to_wire()}
            try:
                sealed_epoch, ct = await self._crypto.encrypt(
                    space_id, build_item_plaintext(item_type, item)
                )
            except RuntimeError:
                return None
            if sealed_epoch == epoch:
                return epoch, ct
        log.warning(
            "gfs.member_publish: content epoch kept moving for space %s — not published",
            space_id,
        )
        return None

    # ── v_97 event-shaped items ──────────────────────────────────────────

    async def seal_event_item(
        self,
        space_id: str,
        event_type: FederationEventType,
        payload: dict,
        *,
        whole_space: bool = True,
    ) -> SealedItem | None:
        """This household's change ``event_type`` / ``payload`` sealed as
        ONE v_97 space item (``docs/protocol/space-items.md``): the v2 inner
        signed by our identity key, our writer cert for the epoch inside,
        padded, under the space content key.

        * a **mesh space**: returned for ``SPACE_ITEM``;
        * a **GFS space** (a channel here), for the whole space and a type
          no v1 item carries: stored in the DURABLE item queue
          (:mod:`.space_item_outbox` — retried per planned GFS until
          accepted, its first try at the sender budget's slot) and
          returned ``via_channel`` with the mention notes. Never ``None``
          (owner 2026-11-27: GFS-space content is never handed to the
          legacy per-household fan-out): an item that can't be sealed or
          stored is a failure its author sees ("Couldn't send"), logged at
          WARNING, and the result still says ``via_channel``.

        ``None`` (the legacy event goes to everyone) only where the legacy
        event IS the path: no identity, no person item, a narrowed
        audience or a v1-carried type in a GFS space, latest-only state or
        no cert / key for the epoch in a mesh space."""
        if self._identity is None or event_type.value not in ITEM_SPECS:
            return None
        author = _author_of(payload)
        try:
            on_channel = await self.channel_space(space_id)
        except Exception:
            # Which kind of space this is can't be told: never a legacy
            # fan-out of what may be GFS-space content — a bug, surfaced.
            log.warning(
                "space items: could not tell whether space %s is a GFS space"
                " — a %s was not sent; its author is told",
                space_id,
                event_type.value,
                exc_info=True,
            )
            return await self._unsent(space_id, event_type, author)
        if on_channel:
            if not whole_space or event_type.value in V1_CHANNEL_EVENT_TYPES:
                return None
            try:
                return await self._channel_item(space_id, event_type, payload, author)
            except Exception:
                log.warning(
                    "space items: sealing a %s for GFS space %s failed — not"
                    " sent; its author is told",
                    event_type.value,
                    space_id,
                    exc_info=True,
                )
                return await self._unsent(space_id, event_type, author)
        try:
            if (
                supersede_key(event_type, space_id=space_id, payload=payload)
                is not None
            ):
                # Latest-only state (a pin, a poll selection) keeps its event
                # on the mesh: the outbox holds one row per (peer, key) for
                # it, and a queued SPACE_ITEM would not (no movement trail
                # for an offline household, an opt-out still replaces a
                # queued pin).
                return None
            sealed = await self._seal_event(space_id, event_type, payload)
        except Exception:
            # A mesh space: the legacy event is the durable per-household
            # path there — it carries the change.
            log.warning(
                "space items: sealing a %s for mesh space %s failed — the event"
                " goes as before",
                event_type.value,
                space_id,
                exc_info=True,
            )
            return None
        if sealed is None:
            return None
        epoch, ct, _item_id = sealed
        return SealedItem(space_id=space_id, epoch=epoch, item=ct)

    async def _channel_item(
        self,
        space_id: str,
        event_type: FederationEventType,
        payload: dict,
        author: str,
    ) -> SealedItem:
        """A GFS space's item: sealed, size-checked and stored in the
        durable queue for every planned GFS (none planned yet: planned when
        due)."""
        if self._outbox is None:
            log.warning(
                "space items: no durable item queue — a %s of GFS space %s"
                " was not sent",
                event_type.value,
                space_id,
            )
            return await self._unsent(space_id, event_type, author)
        sealed = await self._seal_event(space_id, event_type, payload)
        if sealed is None:
            log.warning(
                "space items: no writer cert or content key for the current"
                " epoch of GFS space %s — a %s was not sent; its author is told",
                space_id,
                event_type.value,
            )
            return await self._unsent(space_id, event_type, author)
        epoch, ct, item_id = sealed
        spec = ITEM_SPECS[event_type.value]
        # A location "off" must outlive the pin it removes: kept like any
        # durable item (a household that fetches later still sees it) and
        # never held behind the pin pacing — but it replaces a pin still
        # waiting (the same latest-only key).
        off = payload.get("mode") == "off"
        location = spec.budget == BUDGET_LOCATION
        targets = await self._channels.plan(space_id) if self._channels else []
        queued = await self._outbox.queue(
            space_id=space_id,
            epoch=epoch,
            payload=ct,
            event_type=event_type.value,
            ring_class=RING_CLASS_DURABLE if off else spec.ring_class,
            author_user_id=author,
            gfs_ids=[t.id for t in targets],
            delay_s=self._budget.delay(
                space_id, BUDGET_CONTENT if off else spec.budget, author
            ),
            supersede_key=f"location:{author}" if location else None,
        )
        if not queued:
            # Too large, or the space's queue is full: its author was told.
            return SealedItem(space_id=space_id, epoch=epoch, item="", via_channel=True)
        wire = {"space_id": space_id, "epoch": epoch, "item": ct}
        return SealedItem(
            space_id=space_id,
            epoch=epoch,
            item=ct,
            via_channel=True,
            legacy_window=await self._in_legacy_window(space_id),
            item_id=item_id,
            # Only a new message / item mentions anew (an edit would ring
            # the same people twice).
            notes=tuple(
                (iid, wire)
                for iid in (
                    await self._mentioned_households(space_id, payload)
                    if event_type.value.endswith("_created")
                    else []
                )
            ),
        )

    async def _unsent(
        self, space_id: str, event_type: FederationEventType, author: str
    ) -> SealedItem:
        """A GFS-space item that could not be sealed or stored: its author
        sees it (no bytes kept — Discard only); nothing goes to a v_97
        household (the result says ``via_channel``)."""
        if self._outbox is not None:
            try:
                await self._outbox.fail(
                    space_id=space_id,
                    event_type=event_type.value,
                    author_user_id=author,
                    error=UNSENT_SEAL_FAILED,
                )
            except Exception:
                log.warning(
                    "space items: could not record an unsent %s",
                    event_type.value,
                    exc_info=True,
                )
        return SealedItem(space_id=space_id, epoch=0, item="", via_channel=True)

    async def _seal_event(
        self, space_id: str, event_type: FederationEventType, payload: dict
    ) -> tuple[int, str, str] | None:
        """``(epoch, ciphertext, item_id)`` or ``None``."""
        assert self._identity is not None
        instance_id, seed, pk = self._identity
        needed = required_scope(event_type.value)
        for _attempt in range(2):
            epoch = await self._crypto.get_current_epoch(space_id)
            if epoch is None:
                return None
            cert = await self._writer_certs.own_cert(space_id, epoch)
            if cert is None:
                # The seed holder (the owner) holds no cert from anyone:
                # it signs its own.
                cert = await self._writer_certs.issue_for_instance(
                    space_id, instance_id, epoch=epoch, instance_pk_hint=pk
                )
            if cert is None or not scope_permits(cert.scope, needed):
                return None
            inner = build_signed_event_item(
                item_type=event_type.value,
                item_id=secrets.token_hex(16),
                space_id=space_id,
                epoch=epoch,
                origin_instance_id=instance_id,
                identity_seed=seed,
                identity_pk=pk,
                ts=datetime.now(UTC).isoformat(),
                body=payload,
            )
            inner[WRITER_CERT_FIELD] = cert.to_wire()
            try:
                sealed_epoch, ct = await self._crypto.encrypt(
                    space_id,
                    build_item_plaintext(
                        event_type.value, inner, buckets=EVENT_ITEM_SIZE_BUCKETS
                    ),
                )
            except RuntimeError:
                return None
            if sealed_epoch == epoch:
                return epoch, ct, str(inner["item_id"])
        return None

    async def _in_legacy_window(self, space_id: str) -> bool:
        """v_97 (owner Q6 A) — for 14 days after this household first put a
        space's change on its CURRENT channel, the legacy event still goes
        to every member; the start is kept in ``instance_config`` per
        channel, so a space that left the GFS and came back (a fresh
        channel) gets a fresh window."""
        if self._config is None:
            return False
        stored = await self._spaces.get_gfs_channel(space_id)
        key = f"{LEGACY_WINDOW_KEY_PREFIX}{space_id}:{stored[0] if stored else ''}"
        now = datetime.now(UTC)
        raw = await self._config.get(key)
        if raw is None:
            await self._config.set(key, now.isoformat())
            return True
        try:
            since = datetime.fromisoformat(raw)
        except ValueError:
            return False
        return now - since < LEGACY_WINDOW

    async def _mentioned_households(self, space_id: str, payload: dict) -> list[str]:
        """The other households whose people ``payload``'s text @-mentions
        (≤ :data:`MAX_MENTION_NOTES`): they get a ``SPACE_MENTION_NOTE`` so
        the mention notifies at once. ``@here`` notifies watchers live and
        everyone else by badge (owner F7) — no notes."""
        if self._mentions is None or self._remote_members is None:
            return []
        content = payload.get("content")
        if not isinstance(content, str) or "@" not in content:
            return []
        author = (
            payload.get("author_user_id")
            or payload.get("author")
            or payload.get("user_id")
        )
        mentions = await self._mentions.resolve(
            space_id, content, author_id=str(author) if author else None
        )
        users = {m.user_id for m in mentions if m.user_id}
        if not users:
            return []
        out: list[str] = []
        for seat in await self._remote_members.list_for_space(space_id):
            if seat.user_id in users and seat.instance_id not in out:
                out.append(seat.instance_id)
        return out[:MAX_MENTION_NOTES]

    # ── Epoch notices ─────────────────────────────────────────────────────

    async def announce_epoch(self, space_id: str, *, only: str | None = None) -> int:
        """Announce *space_id*'s current content epoch into its opaque
        channel — at every rotation and after every authority re-pin. The
        owner first reconciles the channel (a space that lost its last remote
        member, or whose owner turned ``private_gfs`` off, retires it before
        this rotation's rekey). ``only`` limits it to one connection. No-op
        for a space with no channel tier or no channel service. Returns how
        many accepted."""
        if self._channels is None or not await self._channel_tier(space_id):
            return 0
        if only is not None:
            return await self._channels.announce_epoch(space_id, only=only)
        return await self._channels.on_rotation(space_id)

    async def announce_held_epochs(self, gfs_id: str) -> int:
        """On a GFS (re)connect: heal the channels on that server — the
        owner re-registers / creates and re-announces, members re-subscribe
        (:meth:`GfsChannelService.heal`)."""
        conn = await self._conn_repo.get(gfs_id)
        if conn is None or conn.status != "active" or self._channels is None:
            return 0
        try:
            return await self._channels.heal(gfs_id)
        except Exception:
            log.exception("gfs.member_publish: channel heal failed")
            return 0
