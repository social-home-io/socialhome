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
  writes in (:meth:`subscribe_member_spaces`), so it receives other members'
  items live;
* decodes / encodes the encrypted item the GFS relays
  (:func:`build_item_plaintext` / :func:`parse_item_plaintext`).

**Never anything identified to a GFS without the capability.** Every send is
gated on the GFS's SIGNED ``member_publish_trusted`` capability
(:meth:`GfsConnectionService.member_publish_trusted_supported`). Without it
the post takes today's path: the normal member federation, from which a seed
holder relays it.

**What a member publish carries.** ``payload`` is the space-content-key
ciphertext of ``{"item_type": "post", "inner": <author-signed inner +
writer_cert>}`` — the inner is the same ``build_signed_author_inner`` object
the host relay carries, so receivers verify it with the same code. The real
type never leaves the ciphertext (the outer type is always ``space_item``).
PR 3 adds comment / reaction / edit / delete item types; their type must then
be bound inside the author signature.

**Host dedupe rule.** Before the member broadcast goes out, the author names
the connection servers it is about to publish to (``gfs_published`` — their
``gfs_instance_id``s, outside the author signature, in the encrypted
``public_relay`` hint). A seed holder relaying that post skips exactly those
servers (``publish_space_event(skip_gfs_instance_ids=…)``) and relays to the
rest — so a v_49 author's post is not published twice, while a v_48 author
(no field) is relayed as before. If the author's own publish to one of them
later fails permanently, subscribers there catch the post up through space
sync; a duplicate is harmless either way (receivers dedupe by post id).

Retries ride a second :class:`GfsPublishRetryQueue` (#808 machinery): a
transient failure (transport, 408, 429, 5xx, a busy GFS's 503) is retried
with backoff. An identified item is RE-SIGNED with a fresh ``ts`` on every
attempt (the request is only valid for ±300 s); the queued item holds only
what the household can rebuild it from.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import aiohttp

from ..authority_sig import AUTHORITY_EVENT_SPACE_EPOCH_NOTICE, sign_authority_event
from ..crypto import b64url_encode, sign_ed25519
from ..domain.events import SpaceMemberJoined
from ..domain.federation import GfsConnection
from ..domain.gfs_member_publish import (
    MEMBER_PUBLISH_ROUTE,
    SPACE_ITEM_EVENT_TYPE,
    MemberPublishRequest,
    owner_epoch_notice_signing_payload,
)
from ..domain.space import PUBLIC_SPACE_TIERS, SpaceRole
from ..domain.writer_cert import WRITER_SCOPE_WRITE, WriterCert, scope_permits
from .gfs_publish_retry import (
    GfsPublish,
    GfsPublishRetryQueue,
    PublishOutcome,
    classify_publish_status,
)
from .space_writer_cert_service import WRITER_CERT_FIELD

if TYPE_CHECKING:
    from ..domain.space import Space
    from ..infrastructure.event_bus import EventBus
    from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
    from ..repositories.space_repo import AbstractSpaceRepo
    from .gfs_connection_service import GfsConnectionService
    from .space_crypto_service import SpaceContentEncryption
    from .space_writer_cert_service import SpaceWriterCertService

log = logging.getLogger(__name__)

#: The only item type PR 2 carries over the member relay.
ITEM_TYPE_POST: str = "post"
SUPPORTED_ITEM_TYPES: frozenset[str] = frozenset({ITEM_TYPE_POST})

#: Field of the ``public_relay`` hint naming the connection servers the
#: author published the post to itself (see "Host dedupe rule").
GFS_PUBLISHED_FIELD: str = "gfs_published"

#: How long a "space X is listed on GFS Y" answer is trusted (seconds).
LISTING_TTL_S: float = 600.0
LISTING_NEGATIVE_TTL_S: float = 60.0

#: Bound on one first publish attempt, which runs before the member
#: broadcast so the author names only servers that accepted the post.
FIRST_ATTEMPT_TIMEOUT_S: float = 3.0

#: Queue-item event types of the member retry queue.
_KIND_ITEM = SPACE_ITEM_EVENT_TYPE
_KIND_NOTICE = "space_epoch_notice"

_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10)


def _cert_lets_post(cert: dict, author_user_id: str) -> bool:
    """Whether our wire cert lets ``author_user_id`` post: ``write`` scope
    and a v2 user binding naming them."""
    users = cert.get("writer_user_ids")
    return (
        scope_permits(str(cert.get("scope") or ""), WRITER_SCOPE_WRITE)
        and bool(cert.get("users_sig"))
        and isinstance(users, list)
        and author_user_id in users
    )


def build_item_plaintext(item_type: str, inner: dict) -> bytes:
    """The bytes a member publish encrypts: the real type + the inner."""
    return json.dumps({"item_type": item_type, "inner": inner}).encode("utf-8")


def parse_item_plaintext(plaintext: bytes) -> tuple[str, dict] | None:
    """``(item_type, inner)`` from decrypted bytes, or ``None`` if malformed
    or of a type this build does not carry."""
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
        "_conn_repo",
        "_crypto",
        "_gfs",
        "_listing",
        "_own_identity_seed",
        "_own_instance_id",
        "_retry",
        "_spaces",
        "_subscribed",
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
        #: conn id → (space ids in its public directory, monotonic time).
        self._listing: dict[str, tuple[frozenset[str], float]] = {}
        self._retry = GfsPublishRetryQueue(self._retry_send)
        #: (conn id, space id) pairs this process already subscribed.
        self._subscribed: set[tuple[str, str]] = set()

    def wire(self, bus: "EventBus") -> None:
        """Subscribe a newly seated local writer to the GFS fan-out at once
        (the reconnect hook would otherwise only catch it next connect)."""
        bus.subscribe(SpaceMemberJoined, self._on_member_joined)

    async def _on_member_joined(self, event: SpaceMemberJoined) -> None:
        try:
            await self.ensure_subscribed(event.space_id)
        except Exception:
            log.exception(
                "gfs.member_publish: subscribing after a join failed for %s",
                event.space_id,
            )

    async def start(self) -> None:
        await self._retry.start()

    async def stop(self) -> None:
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

    async def _listed(self, conn: GfsConnection, space_id: str) -> bool:
        """Whether *space_id* is in *conn*'s public directory
        (``GET /gfs/spaces``, the WHOLE listing, cached per connection).
        Never a space-specific probe — asking the server about one space
        would tell it which spaces this household cares about — and over
        the cookie-less publish session, so it links to nothing."""
        cached = self._listing.get(conn.id)
        now = time.monotonic()
        if cached is not None:
            listed_ids, at = cached
            if now - at < (LISTING_TTL_S if listed_ids else LISTING_NEGATIVE_TTL_S):
                return space_id in listed_ids
        client = self._gfs.publish_client()
        if client is None:
            return False
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
            return False
        spaces = body.get("spaces") if isinstance(body, dict) else None
        listed_ids = frozenset(
            str(sp.get("space_id"))
            for sp in (spaces if isinstance(spaces, list) else [])
            if isinstance(sp, dict) and sp.get("space_id")
        )
        self._listing[conn.id] = (listed_ids, now)
        return space_id in listed_ids

    async def _capable_listed(self, space_id: str) -> list[GfsConnection]:
        """Active connections that list *space_id* and proved
        ``member_publish_trusted`` under a valid signature."""
        own = {c.id for c in await self._conn_repo.list_gfs_for_space(space_id)}
        out: list[GfsConnection] = []
        for conn in await self._conn_repo.list_active():
            if not await self._gfs.member_publish_trusted_supported(conn):
                continue
            if conn.id in own or await self._listed(conn, space_id):
                out.append(conn)
        return out

    async def plan_post(
        self, space_id: str, author_user_id: str
    ) -> list[GfsConnection]:
        """The connection servers this household will publish
        ``author_user_id``'s post in *space_id* to, or ``[]`` — not
        public/readable, a seed holder (it relays with the authority
        signature instead), or our cert for the current epoch does not let
        THIS author post (no cert, ``comment`` scope, or the v2 user binding
        does not name them — e.g. a plain member of a moderated space, whose
        post must wait in the host's queue). ``[]`` means today's host path."""
        space = await self._spaces.get(space_id)
        if not self._publicly_readable(space):
            return []
        if await self._holds_seed(space_id):
            return []
        cert = await self._writer_certs.current_own_cert_wire(space_id)
        if cert is None or not _cert_lets_post(cert, author_user_id):
            return []
        targets = await self._capable_listed(space_id)
        # A writer that publishes wants the other members' items too.
        await self._subscribe(targets, space_id)
        return targets

    # ── Publishing an item ────────────────────────────────────────────────

    async def publish_post(
        self,
        space_id: str,
        inner: dict,
        targets: list[GfsConnection],
        *,
        first_attempt_timeout_s: float = FIRST_ATTEMPT_TIMEOUT_S,
    ) -> list[GfsConnection]:
        """Encrypt the author-signed post *inner* (built with its bound
        ``item_type`` / ``item_target``) with our writer cert for the epoch
        it is sealed under and publish it to *targets*, concurrently and
        each first attempt bounded by ``first_attempt_timeout_s``. Returns
        the servers that ACCEPTED it on that first attempt — the only ones
        the author may name in the host-dedupe hint. A transient failure or
        a timeout goes to the retry queue (the host then relays there too;
        a duplicate is harmless — receivers dedupe by post id)."""
        if not targets:
            return []
        sealed = await self._seal(space_id, inner)
        if sealed is None:
            return []
        epoch, cert, ciphertext = sealed
        data = {"epoch": epoch, "writer_cert": cert.to_wire(), "payload": ciphertext}
        item = GfsPublish(space_id=space_id, event_type=_KIND_ITEM, payload=data)
        results = await asyncio.gather(
            *(
                self._bounded_first_attempt(conn, item, first_attempt_timeout_s)
                for conn in targets
            )
        )
        return [conn for conn, ok in zip(targets, results, strict=True) if ok]

    async def _bounded_first_attempt(
        self, conn: GfsConnection, item: GfsPublish, timeout_s: float
    ) -> bool:
        try:
            return await asyncio.wait_for(self._first_attempt(conn, item), timeout_s)
        except TimeoutError:
            self._retry.enqueue(conn.id, item)
            return False

    async def _seal(
        self, space_id: str, inner: dict
    ) -> tuple[int, WriterCert, str] | None:
        body = {
            k: v
            for k, v in inner.items()
            if k not in (WRITER_CERT_FIELD, GFS_PUBLISHED_FIELD)
        }
        for _attempt in range(2):
            epoch = await self._crypto.get_current_epoch(space_id)
            if epoch is None:
                return None
            cert = await self._writer_certs.own_cert(space_id, epoch)
            if cert is None or not scope_permits(cert.scope, WRITER_SCOPE_WRITE):
                log.info(
                    "gfs.member_publish: no write cert for space %s epoch %d",
                    space_id,
                    epoch,
                )
                return None
            plaintext = build_item_plaintext(
                ITEM_TYPE_POST, {**body, WRITER_CERT_FIELD: cert.to_wire()}
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
        body = self._signed_item_body(conn, space_id, data)
        return await self._post(
            self._gfs.client(), f"{conn.inbox_url}{MEMBER_PUBLISH_ROUTE}", body, conn
        )

    # ── Epoch notices ─────────────────────────────────────────────────────

    async def announce_epoch(self, space_id: str, *, only: str | None = None) -> int:
        """Tell every capable GFS listing *space_id* its current content
        epoch — at every rotation (before the subscriber re-seal), after
        every authority re-pin, and on every GFS (re)connect. The OWNER signs
        with its household key (the GFS confirms any jump); a delegated admin
        signs with the space seed (+1 at most). ``only`` limits it to one
        connection. No-op without the seed or for a space that is not
        publicly readable. Returns how many accepted."""
        space = await self._spaces.get(space_id)
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
        if data.get("owner"):
            ts = datetime.now(timezone.utc).isoformat()
            payload = owner_epoch_notice_signing_payload(
                owning_instance=self._own_instance_id,
                gfs_instance_id=conn.gfs_instance_id,
                space_id=space_id,
                epoch=int(data["epoch"]),
                ts=ts,
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
        if not await self._gfs.member_publish_trusted_supported(conn):
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

    async def subscribe_member_spaces(self, gfs_id: str) -> int:
        """Subscribe this household to *gfs_id*'s fan-out for every publicly
        readable space it holds a writer seat in (and no seed — a seed holder
        receives its members' posts over federation and relays them itself),
        so other members' items arrive live. Run on every GFS-WS (re)connect;
        the GFS subscribe is an upsert. Fail-soft per space. Returns how many
        were (re)subscribed."""
        conn = await self._conn_repo.get(gfs_id)
        if conn is None or conn.status != "active":
            return 0
        if not await self._gfs.member_publish_trusted_supported(conn):
            return 0
        done = 0
        for space in await self._spaces.list_all():
            if not self._publicly_readable(space):
                continue
            if await self._holds_seed(space.id):
                continue
            if not await self._local_writer(space.id):
                continue
            if not await self._listed(conn, space.id):
                continue
            # A (re)connect re-subscribes even what this process already
            # did: the GFS may have dropped the seat meanwhile (upsert).
            self._subscribed.discard((conn.id, space.id))
            done += await self._subscribe([conn], space.id)
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
