"""Household side of trusted-mode member publish over the GFS (v_49).

A member household — any writer, including one seated from an invite link
with no pairing to the host — publishes its OWN space posts to every
connection server (GFS) the space is listed on, without the host signing
them (``POST /gfs/member-publish``; wire codec in
:mod:`socialhome.domain.gfs_member_publish`, server side in
:mod:`socialhome.global_server.member_publish`). It also:

* sends the **content-epoch notice** a seed holder owes every GFS at each
  rotation (:meth:`GfsMemberPublishService.announce_epoch`): the space OWNER
  signs it with its household key (it may jump), a delegated admin signs it
  with the space authority seed (the GFS takes it as +1 at most);
* **auto-subscribes** a member household to the GFS fan-out of the spaces it
  writes in (:meth:`member_subscription_ids`, merged into the shuffled
  reconnect batch), so it receives other members'
  items live;
* decodes / encodes the encrypted item the GFS relays
  (:func:`build_item_plaintext` / :func:`parse_item_plaintext`).

**Never anything identified to a GFS without the capability.** Every send is
gated on the GFS's SIGNED ``member_publish_trusted`` capability
(:meth:`GfsConnectionService.member_publish_trusted_supported`). Without it
the post takes today's path: the normal member federation, from which a seed
holder relays it.

**What a member publish carries.** ``payload`` is the space-content-key
ciphertext of ``{"item_type": <type>, "inner": <author-signed inner +
writer_cert>}``. A ``post`` / ``post_edit`` inner is the
``build_signed_author_inner`` object the host relay carries (receivers
verify it with the same code); comments, comment edits / deletes, post
deletes and reactions carry the generic inner of
:mod:`socialhome.services.space_item_author`. Either way the real type and
its target are bound inside the author signature, and never leave the
ciphertext (the outer type is always ``space_item``). Which types exist and
the scope each needs: :mod:`socialhome.domain.space_item`. A seed holder
publishes no posts here (it relays them with the authority signature) but
does publish its other items, which no other path carries to followers.

**No host dedupe.** The host keeps relaying the member's post to every
GFS (``space_post_public``) exactly as before, because followers on an older
build read only that copy; receivers that understand ``space_item`` get the
post twice and drop the second copy by post id. The member publish itself
runs in the background (:meth:`GfsMemberPublishService.schedule_post`) —
post creation never waits on a connection server. Revisit once followers
advertise ``space_item`` support.

Retries ride a second :class:`GfsPublishRetryQueue` (#808 machinery): a
transient failure (transport, 408, 429, 5xx, a busy GFS's 503) is retried
with backoff. An identified item is RE-SIGNED with a fresh ``ts`` on every
attempt (the request is only valid for ±300 s); the queued item holds only
what the household can rebuild it from.

**Strict mode (v_50).** When this household holds the space's writer GROUP
key for the epoch it seals under (:meth:`SpaceWriterCertService
.own_writer_key` — delivered only in a space whose owner chose
``gfs_publish_mode = "strict"``), the item goes to ``POST
/gfs/member-publish-anon`` instead: no ``instance_id``, no household
signature, no plaintext cert (it rides inside the ciphertext as always), a
fresh ``nonce`` + ``ts`` and a ``writer_sig`` under the group key — over the
COOKIE-LESS publish session, never the identified one, and only to a server
whose signed block proves ``member_publish_strict``. In a strict space
without that key (an older or not-yet-rekeyed household) or without such a
server, NOTHING goes to a connection server: the item takes the host path
(the member broadcast, which always runs). Strict publishing never
subscribes on the spot (an identified subscribe right before an anonymous
publish would link the two); the auto-subscribe runs only on a seat and on
every (re)connect, with the SAME signed body a follower's subscribe and
re-subscribe carry — nothing in it says "writer".
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import aiohttp

from ..authority_sig import AUTHORITY_EVENT_SPACE_EPOCH_NOTICE, sign_authority_event
from ..crypto import b64url_encode, sign_ed25519
from ..domain.events import (
    SpaceConfigChanged,
    SpaceContentKeyImported,
    SpaceMemberJoined,
)
from ..domain.federation import GfsConnection
from ..domain.gfs_member_publish import (
    GFS_PUBLISH_MODE_STRICT,
    MEMBER_PUBLISH_ANON_ROUTE,
    MEMBER_PUBLISH_ROUTE,
    SPACE_ITEM_EVENT_TYPE,
    MemberPublishAnonRequest,
    MemberPublishRequest,
    owner_epoch_notice_signing_payload,
)
from ..domain.writer_key import WRITER_KEY_SUITE_ED25519
from ..domain.space import PUBLIC_SPACE_TIERS, SpaceRole, SpaceType
from ..domain.space_item import ITEM_TYPE_POST, SUPPORTED_ITEM_TYPES, required_scope
from ..domain.writer_cert import WriterCert, scope_permits
from .gfs_publish_retry import (
    GfsPublish,
    GfsPublishRetryQueue,
    PublishOutcome,
    classify_publish_status,
)
from ..writer_key import sign_with_writer_key
from .space_writer_cert_service import WRITER_CERT_FIELD

if TYPE_CHECKING:
    from ..domain.space import Space
    from ..infrastructure.event_bus import EventBus
    from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
    from ..repositories.space_repo import AbstractSpaceRepo
    from .gfs_channel_service import GfsChannelService
    from .gfs_connection_service import GfsConnectionService
    from .space_crypto_service import SpaceContentEncryption
    from .space_writer_cert_service import SpaceWriterCertService

log = logging.getLogger(__name__)


#: How long a "space X is listed on GFS Y" answer is trusted (seconds).
LISTING_TTL_S: float = 600.0
LISTING_NEGATIVE_TTL_S: float = 60.0

#: Background publishes in flight at once; past it a new one is dropped
#: (logged) — the host's copy still reaches every follower.
MAX_PENDING_PUBLISHES: int = 64

#: Queue-item event types of the member retry queue.
_KIND_ITEM = SPACE_ITEM_EVENT_TYPE
_KIND_ANON = "space_item_anon"
_KIND_NOTICE = "space_epoch_notice"

_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10)


def _cert_lets(cert: dict, author_user_id: str, item_type: str) -> bool:
    """Whether our wire cert lets ``author_user_id`` publish ``item_type``:
    the scope the type needs (:func:`required_scope` — ``write`` for a post
    and its edit / delete, ``comment`` for comments and reactions) and a v2
    user binding naming them. The binding names the users holding the
    cert's scope, so a comment-only user of a household holding a ``write``
    cert is not bound and keeps the host path."""
    users = cert.get("writer_user_ids")
    return (
        scope_permits(str(cert.get("scope") or ""), required_scope(item_type))
        and bool(cert.get("users_sig"))
        and isinstance(users, list)
        and author_user_id in users
    )


#: Plaintext sizes a member item is padded up to before encryption, so the
#: ciphertext length tells a connection server (or anyone on the wire) only
#: the bucket — a reaction, a comment and a short post all look alike. The
#: largest stays well under the GFS payload cap once encrypted and base64'd.
ITEM_SIZE_BUCKETS: tuple[int, ...] = (1024, 4096, 16384, 65536, 131072)

#: The padding field. A JSON key, so the padding sits INSIDE the AEAD
#: (authenticated with the item) and a receiver from before padding — which
#: reads ``item_type`` / ``inner`` off the object and ignores other keys —
#: still parses a padded item.
_PAD_FIELD: str = "_pad"


#: Random offset (seconds, either way) on an anonymous request's ``ts``. Well
#: inside the server's ±300 s window, wide enough that the stamp says nothing
#: about this household's clock.
ANON_TS_JITTER_S: int = 60


def _anon_ts() -> str:
    """The ``ts`` of an anonymous publish: whole seconds (a microsecond clock
    offset would fingerprint the household across requests) plus a random
    jitter of up to :data:`ANON_TS_JITTER_S` either way."""
    now = int(time.time())
    jitter = secrets.randbelow(2 * ANON_TS_JITTER_S + 1) - ANON_TS_JITTER_S
    return datetime.fromtimestamp(now + jitter, tz=timezone.utc).isoformat()


def build_item_plaintext(item_type: str, inner: dict) -> bytes:
    """The bytes a member publish encrypts: the real type + the inner,
    padded with ASCII ``0`` in :data:`_PAD_FIELD` to exactly the smallest
    :data:`ITEM_SIZE_BUCKETS` size that fits. An item larger than the
    largest bucket is left unpadded (its size is then its own)."""
    body = {"item_type": item_type, "inner": inner, _PAD_FIELD: ""}
    base = json.dumps(body).encode("utf-8")
    bucket = next((b for b in ITEM_SIZE_BUCKETS if b >= len(base)), None)
    if bucket is None:
        return base
    body[_PAD_FIELD] = "0" * (bucket - len(base))
    return json.dumps(body).encode("utf-8")


def parse_item_plaintext(plaintext: bytes) -> tuple[str, dict] | None:
    """``(item_type, inner)`` from decrypted bytes, or ``None`` if malformed
    or of a type this build does not carry. The size padding is ignored."""
    try:
        body = json.loads(plaintext)
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
    """Publish this household's own items over the GFS; announce epochs;
    keep member subscriptions."""

    __slots__ = (
        "_channels",
        "_conn_repo",
        "_crypto",
        "_gfs",
        "_listing",
        "_own_identity_seed",
        "_own_instance_id",
        "_retry",
        "_spaces",
        "_subscribed",
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
        space_crypto: "SpaceContentEncryption",
        writer_certs: "SpaceWriterCertService",
        own_instance_id: str,
        own_identity_seed: bytes,
    ) -> None:
        self._gfs = gfs
        self._conn_repo = conn_repo
        self._spaces = space_repo
        self._crypto = space_crypto
        self._writer_certs = writer_certs
        self._own_instance_id = own_instance_id
        self._own_identity_seed = own_identity_seed
        #: conn id → ({space id: publish mode} of its public directory,
        #: monotonic time). Cleared on every key import and config change.
        self._listing: dict[str, tuple[dict[str, str], float]] = {}
        self._retry = GfsPublishRetryQueue(self._retry_send)
        #: (conn id, space id) pairs this process already subscribed.
        self._subscribed: set[tuple[str, str]] = set()
        #: Background publishes in flight (strong refs) and the stop flag.
        self._tasks: set[asyncio.Task[None]] = set()
        self._stopping = False
        #: v_51 — opaque channels for PRIVATE spaces with link-joined
        #: members. ``None`` → private spaces always take the host path.
        self._channels: "GfsChannelService | None" = None

    def attach_channels(self, channels: "GfsChannelService") -> None:
        """Wire the private-space channel service (v_51)."""
        self._channels = channels

    async def _private(self, space_id: str) -> bool:
        space = await self._spaces.get(space_id)
        return space is not None and space.space_type is SpaceType.PRIVATE

    async def channel_space(self, space_id: str) -> bool:
        """Whether ``space_id`` is a PRIVATE space using an opaque channel
        here (v_51) — its local writes are offered to the member publisher,
        which then decides by our grant and cert."""
        return (
            self._channels is not None
            and await self._private(space_id)
            and await self._channels.has_channel(space_id)
        )

    async def reconcile_channel(self, space_id: str) -> None:
        """Before an authority-rotation bundle goes out: let the channel
        service start a fresh channel for the new seed (and announce it), so
        the bundle carries grants for it. No-op for other spaces."""
        if self._channels is not None and await self._private(space_id):
            await self._channels.on_rotation(space_id)

    def wire(self, bus: "EventBus") -> None:
        """Subscribe a newly seated local writer to the GFS fan-out at once
        (the reconnect hook would otherwise only catch it next connect)."""
        bus.subscribe(SpaceMemberJoined, self._on_member_joined)
        # v_50 — a key import (a rotation, which a switch to strict causes)
        # or a config change (the mode itself) re-reads each server's
        # listing before the next decision.
        bus.subscribe(SpaceContentKeyImported, self._on_space_state_changed)
        bus.subscribe(SpaceConfigChanged, self._on_space_state_changed)

    async def _on_space_state_changed(
        self, event: SpaceContentKeyImported | SpaceConfigChanged
    ) -> None:
        self.forget_directories()

    async def _on_member_joined(self, event: SpaceMemberJoined) -> None:
        try:
            await self.ensure_subscribed(event.space_id)
        except Exception:
            log.exception(
                "gfs.member_publish: subscribing after a join failed for %s",
                event.space_id,
            )

    async def start(self) -> None:
        self._stopping = False
        await self._retry.start()

    async def stop(self) -> None:
        """Let in-flight background publishes finish (their failures land in
        the retry queue, stopped right after), then stop the retry loop."""
        self._stopping = True
        await self.wait_idle()
        await self._retry.stop()

    # ── Eligibility + targets ─────────────────────────────────────────────

    async def _holds_seed(self, space_id: str) -> bool:
        try:
            return await self._spaces.get_space_seed(space_id) is not None
        except RuntimeError:
            return False

    @staticmethod
    def _publicly_readable(space: "Space | None") -> bool:
        return (
            space is not None
            and space.space_type in PUBLIC_SPACE_TIERS
            and bool(space.features.allow_subscribers)
        )

    async def _directory(self, conn: GfsConnection) -> dict[str, str] | None:
        """*conn*'s public directory as ``{space id: publish mode}``
        (``GET /gfs/spaces``, the WHOLE listing, cached per connection), or
        ``None`` when it can't be fetched. Never a space-specific probe —
        asking the server about one space would tell it which spaces this
        household cares about — and over the cookie-less publish session, so
        it links to nothing. ``member_publish_mode`` (v_50) is absent on an
        older server, which reads as ``trusted``."""
        cached = self._listing.get(conn.id)
        now = time.monotonic()
        if cached is not None:
            listed, at = cached
            if now - at < (LISTING_TTL_S if listed else LISTING_NEGATIVE_TTL_S):
                return listed
        client = self._gfs.publish_client()
        if client is None:
            return None
        try:
            async with client.get(
                f"{conn.inbox_url}/gfs/spaces",
                allow_redirects=False,
                timeout=_HTTP_TIMEOUT,
            ) as resp:
                body = await resp.json() if resp.status == 200 else {}
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            log.info(
                "gfs.member_publish: directory fetch from %s failed: %s", conn.id, exc
            )
            return None
        spaces = body.get("spaces") if isinstance(body, dict) else None
        listed = {
            str(sp.get("space_id")): (
                GFS_PUBLISH_MODE_STRICT
                if sp.get("member_publish_mode") == GFS_PUBLISH_MODE_STRICT
                else "trusted"
            )
            for sp in (spaces if isinstance(spaces, list) else [])
            if isinstance(sp, dict) and sp.get("space_id")
        }
        self._listing[conn.id] = (listed, now)
        return listed

    async def _listed(self, conn: GfsConnection, space_id: str) -> bool:
        """Whether *space_id* is in *conn*'s public directory."""
        listed = await self._directory(conn)
        return listed is not None and space_id in listed

    async def _listed_strict(self, conn: GfsConnection, space_id: str) -> bool:
        """Whether *conn*'s public directory says *space_id* is in strict
        mode (v_50) — then nothing identified may go there, whatever our own
        copy of the setting says (it may lag the owner's switch)."""
        listed = await self._directory(conn)
        return listed is not None and listed.get(space_id) == GFS_PUBLISH_MODE_STRICT

    def forget_directories(self, *_: object) -> None:
        """Drop the cached directories, so the next decision re-reads each
        server's listing — on a key import (a rotation, which a switch to
        strict causes) and on a config change."""
        self._listing.clear()

    async def _capable_listed(
        self, space_id: str, *, strict: bool = False, identified_items: bool = False
    ) -> list[GfsConnection]:
        """Active connections that list *space_id* and proved
        ``member_publish_trusted`` (with ``strict``: ``member_publish_strict``)
        under a valid signature. With ``identified_items`` a server whose
        listing says the space is strict is left out — never for epoch
        notices or subscribes, which must still reach it (the owner's notice
        is how a strict space goes back to trusted)."""
        own = {c.id for c in await self._conn_repo.list_gfs_for_space(space_id)}
        out: list[GfsConnection] = []
        for conn in await self._conn_repo.list_active():
            if strict:
                if not await self._gfs.member_publish_strict_supported(conn):
                    continue
            elif not await self._gfs.member_publish_trusted_supported(conn):
                continue
            elif identified_items and await self._listed_strict(conn, space_id):
                # Identified publishing to a server that lists the space as
                # strict would be refused — and seen. Never.
                continue
            if conn.id in own or await self._listed(conn, space_id):
                out.append(conn)
        return out

    async def plan_post(
        self, space_id: str, author_user_id: str
    ) -> list[GfsConnection]:
        """:meth:`plan_item` for a post. A seed holder publishes no posts
        here — it relays them with the authority signature instead."""
        return await self.plan_item(space_id, author_user_id, ITEM_TYPE_POST)

    async def plan_item(
        self, space_id: str, author_user_id: str, item_type: str
    ) -> list[GfsConnection]:
        """The connection servers this household will publish
        ``author_user_id``'s ``item_type`` item in *space_id* to, or ``[]`` —
        not public/readable; a POST from a seed holder (it relays posts with
        the authority signature; nothing else reaches followers, so a seed
        holder's comments, reactions and own edits / deletes do go here); or
        our cert for the current epoch does not let THIS author publish THIS
        type (no cert, too weak a scope, or the v2 user binding does not name
        them — e.g. a plain member of a moderated space, whose post must
        wait in the host's queue). ``[]`` means today's host path.

        Strict mode (v_50): holding the writer group key for the current
        epoch → the servers proving ``member_publish_strict`` (no subscribe
        here); a strict space without the key → ``[]`` — never the identified
        path."""
        space = await self._spaces.get(space_id)
        if (
            space is not None
            and space.space_type is SpaceType.PRIVATE
            and self._channels is not None
        ):
            # v_51 — a private space publishes only through its opaque
            # channel, and only when our cert lets THIS author post THIS type
            # (the cert rides inside the ciphertext, as always).
            cert = await self._writer_certs.current_own_cert_wire(space_id)
            if cert is None or not _cert_lets(cert, author_user_id, item_type):
                return []
            return await self._channels.plan(space_id)
        if not self._publicly_readable(space):
            return []
        assert space is not None
        seed_holder = await self._holds_seed(space_id)
        if item_type == ITEM_TYPE_POST and seed_holder:
            return []
        cert = await self._writer_certs.current_own_cert_wire(space_id)
        if cert is None or not _cert_lets(cert, author_user_id, item_type):
            return []
        epoch = await self._crypto.get_current_epoch(space_id)
        if epoch is not None and (
            await self._writer_certs.own_writer_key(space_id, epoch) is not None
        ):
            return await self._capable_listed(space_id, strict=True)
        if space.features.gfs_publish_mode == GFS_PUBLISH_MODE_STRICT:
            log.info(
                "gfs.member_publish: strict space %s and no writer key for the "
                "current epoch — the host path carries it",
                space_id,
            )
            return []
        targets = await self._capable_listed(space_id, identified_items=True)
        # A writer that publishes wants the other members' items too (a seed
        # holder receives them over federation).
        if not seed_holder:
            await self._subscribe(targets, space_id)
        return targets

    # ── Publishing an item ────────────────────────────────────────────────

    def schedule_post(self, space_id: str, author_user_id: str, inner: dict) -> bool:
        """:meth:`schedule_item` for a post."""
        return self.schedule_item(space_id, author_user_id, ITEM_TYPE_POST, inner)

    def schedule_item(
        self, space_id: str, author_user_id: str, item_type: str, inner: dict
    ) -> bool:
        """Plan and publish ``author_user_id``'s ``item_type`` item in the
        BACKGROUND, so the local write never waits on a connection server.
        Returns whether a task was started (``False`` when stopping or too
        many are pending — the host path still carries the write)."""
        if self._stopping or len(self._tasks) >= MAX_PENDING_PUBLISHES:
            log.warning(
                "gfs.member_publish: background publish for space %s skipped "
                "(%s) — the host path carries it",
                space_id,
                "stopping" if self._stopping else "too many pending",
            )
            return False
        task = asyncio.create_task(
            self._plan_and_publish(space_id, author_user_id, item_type, inner),
            name=f"gfs-member-publish-{space_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def _plan_and_publish(
        self, space_id: str, author_user_id: str, item_type: str, inner: dict
    ) -> None:
        try:
            targets = await self.plan_item(space_id, author_user_id, item_type)
            if targets:
                await self.publish_item(space_id, item_type, inner, targets)
        except Exception:
            log.exception(
                "gfs.member_publish: background publish failed for %s", space_id
            )

    async def wait_idle(self) -> None:
        """Wait for every background publish (tests, graceful stop)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def publish_post(
        self,
        space_id: str,
        inner: dict,
        targets: list[GfsConnection],
    ) -> list[GfsConnection]:
        """:meth:`publish_item` for a post."""
        return await self.publish_item(space_id, ITEM_TYPE_POST, inner, targets)

    async def publish_item(
        self,
        space_id: str,
        item_type: str,
        inner: dict,
        targets: list[GfsConnection],
    ) -> list[GfsConnection]:
        """Encrypt the author-signed *inner* (built with its bound
        ``item_type`` / ``item_target``) as ``item_type`` with our writer
        cert for the epoch it is sealed under, and publish it to *targets*.
        Returns the servers that accepted it on the first attempt; transient
        failures go to the retry queue."""
        if not targets:
            return []
        sealed = await self._seal(space_id, item_type, inner)
        if sealed is None:
            return []
        epoch, cert, ciphertext = sealed
        if self._channels is not None and await self._private(space_id):
            return await self._channels.publish_sealed(
                space_id, epoch, ciphertext, targets
            )
        item = await self._item_for(space_id, epoch, cert, ciphertext)
        if item is None:
            return []
        accepted = []
        for conn in targets:
            if item.event_type == _KIND_ANON and not (
                await self._gfs.member_publish_strict_supported(conn)
            ):
                continue
            if await self._first_attempt(conn, item):
                accepted.append(conn)
        return accepted

    async def _item_for(
        self, space_id: str, epoch: int, cert: WriterCert, ciphertext: str
    ) -> GfsPublish | None:
        """The queue item for an item sealed at ``epoch``: ANONYMOUS when we
        hold the writer group key for that epoch (the cert stays inside the
        ciphertext only); ``None`` in a strict space without it (host path —
        never identified); else IDENTIFIED with the v1 cert fields in the
        clear (the user binding stays inside the ciphertext — it names this
        household's users)."""
        if await self._writer_certs.own_writer_key(space_id, epoch) is not None:
            return GfsPublish(
                space_id=space_id,
                event_type=_KIND_ANON,
                payload={"epoch": epoch, "payload": ciphertext},
            )
        space = await self._spaces.get(space_id)
        if space is None or space.features.gfs_publish_mode == GFS_PUBLISH_MODE_STRICT:
            log.info(
                "gfs.member_publish: strict space %s, no writer key for epoch %d "
                "— the host path carries it",
                space_id,
                epoch,
            )
            return None
        data = {
            "epoch": epoch,
            "writer_cert": cert.v1().to_wire(),
            "payload": ciphertext,
        }
        return GfsPublish(space_id=space_id, event_type=_KIND_ITEM, payload=data)

    async def _seal(
        self, space_id: str, item_type: str, inner: dict
    ) -> tuple[int, WriterCert, str] | None:
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
            plaintext = build_item_plaintext(
                item_type, {**body, WRITER_CERT_FIELD: cert.to_wire()}
            )
            try:
                sealed_epoch, ct = await self._crypto.encrypt(space_id, plaintext)
            except RuntimeError:
                return None
            if sealed_epoch == epoch:
                return epoch, cert, ct
        log.warning(
            "gfs.member_publish: content epoch kept moving for space %s — not published",
            space_id,
        )
        return None

    def _signed_item_body(self, conn: GfsConnection, space_id: str, data: dict) -> dict:
        req = MemberPublishRequest(
            instance_id=self._own_instance_id,
            gfs_instance_id=conn.gfs_instance_id,
            ts=datetime.now(timezone.utc).isoformat(),
            signature="",
            target=space_id,
            epoch=int(data["epoch"]),
            writer_cert=WriterCert.from_wire(data["writer_cert"]),
            payload=str(data["payload"]),
        )
        sig = sign_ed25519(self._own_identity_seed, req.signing_bytes())
        return replace(req, signature=b64url_encode(sig)).to_wire()

    async def _post_item(
        self, conn: GfsConnection, space_id: str, data: dict
    ) -> PublishOutcome:
        space = await self._spaces.get(space_id)
        if space is None or space.features.gfs_publish_mode == GFS_PUBLISH_MODE_STRICT:
            # The space went strict after this was queued: an identified
            # attempt is exactly what strict mode withholds. Dropped — the
            # host path carries the item.
            return PublishOutcome.permanent()
        if await self._listed_strict(conn, space_id):
            # The server's listing already says strict (our copy of the
            # setting lags the owner's switch): an identified request would
            # be refused there — and seen. The host path carries it.
            log.info(
                "gfs.member_publish: GFS %s lists space %s as strict — no "
                "identified publish",
                conn.id,
                space_id,
            )
            return PublishOutcome.permanent()
        body = self._signed_item_body(conn, space_id, data)
        return await self._post(
            self._gfs.client(), f"{conn.inbox_url}{MEMBER_PUBLISH_ROUTE}", body, conn
        )

    async def _post_anon_item(
        self, conn: GfsConnection, space_id: str, data: dict
    ) -> PublishOutcome:
        """One anonymous attempt: signed afresh (``ts`` + ``nonce``) with the
        writer group key for the item's epoch — re-read now, so an attempt
        after the key is gone is dropped, never downgraded — over the
        COOKIE-LESS publish session (nothing ties it to our identified
        session)."""
        epoch = int(data["epoch"])
        writer_seed = await self._writer_certs.own_writer_key(space_id, epoch)
        client = self._gfs.publish_client()
        if writer_seed is None or client is None:
            return PublishOutcome.permanent()
        req = MemberPublishAnonRequest(
            gfs_instance_id=conn.gfs_instance_id,
            ts=_anon_ts(),
            nonce=b64url_encode(secrets.token_bytes(16)),
            target=space_id,
            epoch=epoch,
            payload=str(data["payload"]),
            writer_sig="",
            writer_sig_suite=WRITER_KEY_SUITE_ED25519,
        )
        body = replace(
            req, writer_sig=sign_with_writer_key(writer_seed, req.signing_bytes())
        ).to_wire()
        return await self._post(
            client, f"{conn.inbox_url}{MEMBER_PUBLISH_ANON_ROUTE}", body, conn
        )

    # ── Epoch notices ─────────────────────────────────────────────────────

    async def announce_epoch(self, space_id: str, *, only: str | None = None) -> int:
        """Tell every capable GFS listing *space_id* its current content
        epoch — at every rotation (before the subscriber re-seal), after
        every authority re-pin, and on every GFS (re)connect. The OWNER signs
        with its household key (the GFS confirms any jump); a delegated admin
        signs with the space seed (+1 at most). ``only`` limits it to one
        connection. No-op without the seed or for a space that is not
        publicly readable. Returns how many accepted.

        v_51 — a PRIVATE space announces to its opaque channel instead (the
        owner first reconciles the channel, so a space that lost its last
        link-joined member retires it before this rotation's rekey)."""
        space = await self._spaces.get(space_id)
        if (
            space is not None
            and space.space_type is SpaceType.PRIVATE
            and self._channels is not None
        ):
            if only is not None:
                return await self._channels.announce_epoch(space_id, only=only)
            return await self._channels.on_rotation(space_id)
        if not self._publicly_readable(space):
            return 0
        assert space is not None
        try:
            seed = await self._spaces.get_space_seed(space_id)
        except RuntimeError:
            seed = None
        if seed is None:
            return 0
        epoch = await self._crypto.get_current_epoch(space_id)
        if epoch is None:
            return 0
        is_owner = space.owner_instance_id == self._own_instance_id
        data: dict = {"epoch": epoch, "owner": is_owner}
        # v_50 — the strict-mode statements, sent only to a server proving
        # ``member_publish_strict`` (see :meth:`_post_notice`): the owner's
        # mode, and the writer key pin for this epoch (strict spaces only).
        if is_owner:
            data["publish_mode"] = space.features.gfs_publish_mode
        wkc = await self._writer_certs.writer_key_cert_wire(space_id, epoch)
        if wkc is not None:
            data["writer_key_cert"] = wkc
        if not is_owner:
            data.update(
                sign_authority_event(
                    event_type=AUTHORITY_EVENT_SPACE_EPOCH_NOTICE,
                    space_id=space_id,
                    payload={"space_id": space_id, "epoch": epoch},
                    space_seed=seed,
                )
            )
        item = GfsPublish(space_id=space_id, event_type=_KIND_NOTICE, payload=data)
        done = 0
        for conn in await self._capable_listed(space_id):
            if only is not None and conn.id != only:
                continue
            if await self._first_attempt(conn, item):
                done += 1
        return done

    async def _post_notice(
        self, conn: GfsConnection, space_id: str, data: dict
    ) -> PublishOutcome:
        url = f"{conn.inbox_url}/gfs/spaces/{space_id}/epoch"
        # v_50 fields only where the server verifies them: an older server
        # checks the owner signature WITHOUT them and would refuse it.
        strict_ok = await self._gfs.member_publish_strict_supported(conn)
        wkc = data.get("writer_key_cert") if strict_ok else None
        if data.get("owner"):
            ts = datetime.now(timezone.utc).isoformat()
            mode = data.get("publish_mode") if strict_ok else None
            payload = owner_epoch_notice_signing_payload(
                owning_instance=self._own_instance_id,
                gfs_instance_id=conn.gfs_instance_id,
                space_id=space_id,
                epoch=int(data["epoch"]),
                ts=ts,
                publish_mode=mode,
                writer_key_cert=wkc,
            )
            canonical = json.dumps(
                payload, separators=(",", ":"), sort_keys=True
            ).encode("utf-8")
            body = {
                "owning_instance": self._own_instance_id,
                "gfs_instance_id": conn.gfs_instance_id,
                "epoch": int(data["epoch"]),
                "ts": ts,
                "signature": b64url_encode(
                    sign_ed25519(self._own_identity_seed, canonical)
                ),
            }
            if mode is not None:
                body["publish_mode"] = mode
            if wkc is not None:
                body["writer_key_cert"] = wkc
            return await self._post(self._gfs.client(), url, body, conn)
        # Seed-only form: anonymous — authorized by the authority signature
        # alone, so it rides the cookie-less publish session.
        client = self._gfs.publish_client()
        if client is None:
            return PublishOutcome.permanent()
        body = {
            "epoch": int(data["epoch"]),
            "authority_sig": data["authority_sig"],
            "authority_sig_suite": data["authority_sig_suite"],
        }
        if wkc is not None:
            body["writer_key_cert"] = wkc
        return await self._post(client, url, body, conn)

    # ── Transport + retries ───────────────────────────────────────────────

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
                        "gfs.member_publish: GFS %s answered HTTP %d (%s)",
                        conn.id,
                        resp.status,
                        outcome.kind,
                    )
                return outcome
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning(
                "gfs.member_publish: GFS %s unreachable: %s",
                conn.id,
                exc or type(exc).__name__,
            )
            return PublishOutcome.transient()

    async def _send(self, conn: GfsConnection, item: GfsPublish) -> PublishOutcome:
        if item.event_type == _KIND_ANON:
            return await self._post_anon_item(conn, item.space_id, item.payload)
        if item.event_type == _KIND_ITEM:
            return await self._post_item(conn, item.space_id, item.payload)
        return await self._post_notice(conn, item.space_id, item.payload)

    async def _first_attempt(self, conn: GfsConnection, item: GfsPublish) -> bool:
        if self._retry.pending(conn.id):
            self._retry.enqueue(conn.id, item)
            return False
        outcome = await self._send(conn, item)
        if outcome.kind == "transient":
            if not self._retry.enqueue(
                conn.id, item, retry_after_s=outcome.retry_after_s
            ):
                log.warning(
                    "gfs.member_publish: %s for space %s to GFS %s is lost — "
                    "the retry queue refused it",
                    item.event_type,
                    item.space_id,
                    conn.id,
                )
        return outcome.kind == "delivered"

    async def _retry_send(self, conn_id: str, item: GfsPublish) -> PublishOutcome:
        """One retry: the connection must still be active and still prove the
        capability, or the item is dropped (never sent identified to a GFS
        that cannot take it)."""
        conn = await self._conn_repo.get(conn_id)
        if conn is None or conn.status != "active":
            return PublishOutcome.permanent()
        if item.event_type == _KIND_ANON:
            if not await self._gfs.member_publish_strict_supported(conn):
                return PublishOutcome.permanent()
        elif not await self._gfs.member_publish_trusted_supported(conn):
            return PublishOutcome.permanent()
        return await self._send(conn, item)

    # ── Auto-subscribe ────────────────────────────────────────────────────

    async def ensure_subscribed(self, space_id: str) -> int:
        """Subscribe to *space_id*'s fan-out on every capable GFS listing it,
        when this household is a local writer there without the seed."""
        space = await self._spaces.get(space_id)
        if not self._publicly_readable(space) or await self._holds_seed(space_id):
            return 0
        if not await self._local_writer(space_id):
            return 0
        return await self._subscribe(await self._capable_listed(space_id), space_id)

    async def _subscribe(self, conns: list[GfsConnection], space_id: str) -> int:
        done = 0
        for conn in conns:
            if (conn.id, space_id) in self._subscribed:
                continue
            try:
                await self._gfs.subscribe_to_gfs_space(space_id, conn.id)
            except Exception as exc:  # GfsConnectionError and transport errors
                log.info(
                    "gfs.member_publish: subscribing space %s on %s failed: %s",
                    space_id,
                    conn.id,
                    exc,
                )
                continue
            self._subscribed.add((conn.id, space_id))
            done += 1
        return done

    async def member_subscription_ids(self, gfs_id: str) -> list[str]:
        """The spaces this household should hold a fan-out seat in on
        *gfs_id* because it writes there (publicly readable, a local writer
        seat, no seed, listed there) — for the reconnect batch, which merges
        them with the followed spaces and shuffles the lot
        (:meth:`GfsSpaceMirrorService.resubscribe_all`). ``[]`` for an
        inactive or incapable connection."""
        conn = await self._conn_repo.get(gfs_id)
        if conn is None or conn.status != "active":
            return []
        if not await self._gfs.member_publish_trusted_supported(conn):
            return []
        out: list[str] = []
        for space in await self._spaces.list_all():
            if not self._publicly_readable(space):
                continue
            if await self._holds_seed(space.id):
                continue
            if not await self._local_writer(space.id):
                continue
            if await self._listed(conn, space.id):
                out.append(space.id)
        return out

    async def subscribe_member_spaces(self, gfs_id: str) -> int:
        """Subscribe this household to *gfs_id*'s fan-out for every publicly
        readable space it holds a writer seat in (and no seed — a seed holder
        receives its members' posts over federation and relays them itself),
        so other members' items arrive live. The GFS subscribe is an upsert.
        Fail-soft per space. Returns how many were (re)subscribed. (The
        reconnect hook instead merges :meth:`member_subscription_ids` into
        the shuffled follower batch.)"""
        conn = await self._conn_repo.get(gfs_id)
        if conn is None:
            return 0
        done = 0
        for space_id in await self.member_subscription_ids(gfs_id):
            # Re-subscribes even what this process already did: the GFS may
            # have dropped the seat meanwhile (upsert).
            self._subscribed.discard((conn.id, space_id))
            done += await self._subscribe([conn], space_id)
        return done

    async def _local_writer(self, space_id: str) -> bool:
        local = set(await self._spaces.list_local_member_user_ids(space_id))
        if not local:
            return False
        return any(
            m.user_id in local and m.role != SpaceRole.SUBSCRIBER.value
            for m in await self._spaces.list_members(space_id)
        )

    async def announce_held_epochs(self, gfs_id: str) -> int:
        """On a GFS (re)connect: re-announce the current epoch of every space
        this household holds the seed of and that GFS lists — the GFS forgets
        on a re-pin, and an owner's notice confirms what a delegated admin
        rotated meanwhile."""
        conn = await self._conn_repo.get(gfs_id)
        if conn is None or conn.status != "active":
            return 0
        done = 0
        if self._channels is not None:
            # v_51 — private-space channels: the owner re-registers /
            # creates and re-announces, members re-subscribe.
            try:
                done += await self._channels.heal(gfs_id)
            except Exception:
                log.exception("gfs.member_publish: channel heal failed")
        for space in await self._spaces.list_all():
            if not self._publicly_readable(space) or not await self._holds_seed(
                space.id
            ):
                continue
            try:
                done += await self.announce_epoch(space.id, only=gfs_id)
            except Exception:
                log.exception(
                    "gfs.member_publish: epoch announce failed for %s", space.id
                )
        return done
