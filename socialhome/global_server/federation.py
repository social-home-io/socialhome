"""GFS federation service — instance registration and the space directory.

Business logic only — all SQL lives in :mod:`.repositories`. Crypto
helpers are reused from :mod:`socialhome.crypto` (no duplication).

Since v_96 this server keeps no space-id-keyed relay: a space's content
reaches it only through the opaque channels of :mod:`.channels`, and the
space rows here are the listing a household browses (``GET /gfs/spaces``),
its pinned authority key and its invite links.
"""

from __future__ import annotations

import hashlib
import itertools
import logging
import time
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ..authority_cert import (
    InvalidAuthorityCert,
    UnsupportedAuthorityCertSuite,
    authority_cert_epoch,
    verify_authority_cert,
)
from ..canonical_json import canonical_bytes
from ..crypto import b64url_decode, derive_instance_id, verify_ed25519
from ..domain.media_constraints import SPACE_IMAGE_DATA_URI_MAX_CHARS
from ..domain.space import (
    normalize_category,
    normalize_join_mode,
)
from ..domain.space_listing_counts import parse_listing_count
from ..domain.space_place import parse_place_pin_wire
from ..gfs_client_status import (
    CLIENT_STATUSES,
    StatusRequest,
    verify_status_request,
)
from ..space_join_contact import parse_join_contact, verify_join_contact
from .domain import (
    ClientInstance,
    GlobalSpace,
    normalize_counts_ts,
)
from .repositories import AbstractGfsFederationRepo

if TYPE_CHECKING:
    from .addressee import GfsAddressee
    from .repositories import AbstractGfsInviteRepo

log = logging.getLogger(__name__)

#: Storage cap for a published space's ``about_markdown``. Generous for a
#: real "about" blurb; bounds DB growth + the public-page render cost from
#: an oversized publish. The public renderer applies its own (smaller) cap.
MAX_ABOUT_MARKDOWN_CHARS: int = 8000

#: Max display_name length accepted by ``update_instance`` (chars). Mirrors
#: the household-name bound on the HFS side.
MAX_DISPLAY_NAME_CHARS: int = 80


#: Default lifetime of a :class:`SeenPayloadCache` entry (seconds) — the
#: ±300 s freshness window every replay guard here uses.
PUBLISH_REPLAY_TTL_S: float = 300.0

#: Default cap on remembered digests. 10 000 × (32-byte key + float) is well
#: under a megabyte; past the cap the oldest (closest-to-expiring) entries go.
PUBLISH_REPLAY_MAX_ENTRIES: int = 10_000

#: Verify key used on every unknown-instance branch (the ``/gfs/ws`` hello,
#: :mod:`.routes.ws`; the approval-state answer of ``POST /gfs/proof``) so
#: that branch does the same Ed25519 verification work as the
#: registered-instance branch — otherwise an unknown instance returns
#: measurably sooner and the endpoint becomes a timing oracle for "is this
#: household registered here?". The Ed25519 public key for the all-zero seed;
#: nothing is ever signed with it, and a signature can never verify under it.
TIMING_UNIFORM_DUMMY_KEY_HEX: str = (
    "3b6a27bcceb6a42d62a3a8d02a6f0d73653215771de243a63ac048a18b59da29"
)

#: Fixed, well-formed (64-byte) Ed25519 signature fed to the dummy verify on
#: every early rejection. A real Ed25519 verification of a wrong-length
#: signature fails on the length check and costs nothing, so the burn has to
#: use a full-length value to match the work a genuine bad-signature rejection
#: does. It can never verify under any key.
TIMING_UNIFORM_DUMMY_SIG: bytes = bytes(64)

#: Freshness window for the signed instance-update timestamp (seconds). A
#: ``ts`` further than this from now is treated as a replay and rejected —
#: same ±300 s tolerance the §24.11 inbound pipeline uses.
INSTANCE_UPDATE_TS_SKEW_SECONDS: int = 300


def _bounded_image_uri(uri: str | None, *, space_id: str, kind: str) -> str | None:
    """*uri*, or ``""`` when it exceeds ``SPACE_IMAGE_DATA_URI_MAX_CHARS``.

    Applied AFTER the publish signature verifies over the full value. Logged
    at WARNING so an operator can see why a listing has no art.
    """
    if uri and len(uri) > SPACE_IMAGE_DATA_URI_MAX_CHARS:
        log.warning(
            "GFS: dropping oversized %s for space %s (%d chars > %d)",
            kind,
            space_id,
            len(uri),
            SPACE_IMAGE_DATA_URI_MAX_CHARS,
        )
        return ""
    return uri


def _served_join_contact(
    raw: object,
    *,
    space_id: str,
    owning_instance: str,
    join_mode: str,
    allow_subscribers: bool,
    space_public_key_hex: str,
) -> dict | None:
    """The join contact this listing serves (v_84), or ``None``.

    Applied AFTER the publish signature verified over the value as sent.
    Served only for a space that takes requests or follows (v_96: invite-only
    with ``allow_subscribers`` on — a follow is a join for the read-only
    role, sealed to the same contact), when it names the owning
    instance and its space-authority signature verifies against the key this
    server pinned for the space — so a listing never carries a contact a
    reader would refuse anyway. A dropped one is logged at WARNING.
    """
    if raw is None or (join_mode not in ("request", "open") and not allow_subscribers):
        return None
    contact = parse_join_contact(raw)
    if (
        contact is None
        or contact["instance_id"] != owning_instance
        or not space_public_key_hex
        or not verify_join_contact(
            contact, space_id=space_id, space_public_key_hex=space_public_key_hex
        )
    ):
        log.warning(
            "GFS: space %s published a join contact that does not verify; dropped",
            space_id,
        )
        return None
    return contact


def certified_authority_repin(
    space_id: str,
    *,
    owning_instance: str,
    owner_pk_hex: str,
    offered_pk: str,
    cert: dict | None,
    stored_cert: dict | None,
) -> bool:
    """Whether ``cert`` authorizes pinning ``offered_pk`` (v_44).

    True only when the cert verifies for this space and this owner —
    signed by the owner's REGISTERED key, which derives to
    ``owning_instance`` — names exactly ``offered_pk``, and is newer than
    the cert stored here. Never raises; a refusal is logged.
    """
    if cert is None:
        return False
    try:
        verified = verify_authority_cert(
            cert,
            space_id=space_id,
            owner_instance_id=owning_instance,
            known_owner_pk_hex=owner_pk_hex,
        )
    except (InvalidAuthorityCert, UnsupportedAuthorityCertSuite) as exc:
        log.warning("GFS: authority cert for space %s refused: %s", space_id, exc)
        return False
    if verified.authority_pk_hex != (offered_pk or "").lower():
        log.warning(
            "GFS: authority cert for space %s names another key than the "
            "one published — refused",
            space_id,
        )
        return False
    if (
        stored_cert is not None
        and verified.key_epoch == authority_cert_epoch(stored_cert)
        and str(stored_cert.get("authority_pk") or "").lower()
        == verified.authority_pk_hex
    ):
        # The key already pinned at the epoch already stored, sent again:
        # the owner re-publishes with its cert on every reconnect and
        # metadata change until this server confirmed the pin. Nothing to
        # move, nothing refused.
        return False
    if verified.key_epoch <= authority_cert_epoch(stored_cert):
        log.warning(
            "GFS: authority cert for space %s is not newer than the stored "
            "one (epoch %d) — refused",
            space_id,
            verified.key_epoch,
        )
        return False
    return True


class SeenPayloadCache:
    """Bounded, TTL'd set of recently-seen request digests (replay guards).

    The content-blind GFS cannot dedupe on an item id — that id lives inside
    the encrypted payload. It CAN dedupe on the bytes it already holds:
    hashing them leaks nothing it does not already have, and mutating any
    field (the signature included) changes the digest.

    In-memory and per-process on purpose: no table, no migration. A GFS
    restart or a second cluster node simply forgets, which costs one extra
    copy of a replayed request and never correctness (households dedupe by
    the item id inside the ciphertext).
    """

    __slots__ = ("_cap", "_seen", "_ttl")

    def __init__(
        self,
        *,
        ttl_s: float = PUBLISH_REPLAY_TTL_S,
        cap: int = PUBLISH_REPLAY_MAX_ENTRIES,
    ) -> None:
        self._ttl = ttl_s
        self._cap = cap
        # key → monotonic expiry. Insertion order IS expiry order because the
        # TTL is constant, which is what lets the prune stop at the first
        # unexpired entry.
        self._seen: dict[bytes, float] = {}

    @staticmethod
    def digest(payload: object) -> bytes:
        """Return the 32-byte BLAKE2b digest of *payload*'s canonical JSON."""
        canonical = canonical_bytes(payload)
        return hashlib.blake2b(canonical, digest_size=32).digest()

    def seen(self, key: bytes, *, now: float | None = None) -> bool:
        """Return whether *key* was recorded within the TTL."""
        stamp = time.monotonic() if now is None else now
        expires = self._seen.get(key)
        if expires is None:
            return False
        if expires <= stamp:
            del self._seen[key]
            return False
        return True

    def record(self, key: bytes, *, now: float | None = None) -> None:
        """Remember *key* for the TTL, pruning expired + overflowing entries."""
        stamp = time.monotonic() if now is None else now
        self._seen.pop(key, None)  # re-insert at the end to keep expiry order
        self._seen[key] = stamp + self._ttl
        self._prune(stamp)

    def _prune(self, stamp: float) -> None:
        # Front-pop rather than a scan over a COPY of the dict: this runs on
        # every ``record`` (i.e. every accepted publish) and copying up to
        # ``_cap`` items each time made the common case — nothing expired —
        # O(n) in the cache size. Insertion order is expiry order (constant
        # TTL), so the first unexpired entry ends the loop.
        while self._seen:
            key = next(iter(self._seen))
            if self._seen[key] > stamp:
                break
            del self._seen[key]
        overflow = len(self._seen) - self._cap
        if overflow > 0:
            # Only the overflowing prefix is materialised, never the whole dict.
            for key in list(itertools.islice(self._seen, overflow)):
                del self._seen[key]

    def __len__(self) -> int:
        return len(self._seen)


class SpaceNotListedError(LookupError):
    """A ``withdrawn`` (pin-only) publish named a space this GFS holds no row for."""


class InstanceRegistrationRefused(Exception):
    """``/gfs/register`` refused: ``status`` + a stable ``error`` code."""

    def __init__(self, status: int, error: str) -> None:
        super().__init__(error)
        self.status = status
        self.error = error


class GfsFederationService:
    """Registration and the space directory for the GFS process.

    Responsible for:
    * Registering/updating client household instances.
    * Verifying household-signed requests (:meth:`verify_signed_request`).
    * Listing, publishing and withdrawing global spaces.
    """

    __slots__ = (
        "_addressee",
        "_invite_repo",
        "_repo",
    )

    def __init__(
        self,
        repo: AbstractGfsFederationRepo,
        invite_repo: AbstractGfsInviteRepo | None = None,
    ) -> None:
        self._repo = repo
        # Optional: only :meth:`hide_space` touches it, to take a withdrawn
        # space's public invite pages down with the listing. ``None`` in the
        # tests / embedders that predate invite links; production always wires
        # it (``server.py``).
        self._invite_repo = invite_repo
        # This server as an addressee (its identity key): a request that
        # binds ``gfs_key`` into its signed bytes must name exactly it.
        # Wired by ``server.py``; without it such a request is refused.
        self._addressee: GfsAddressee | None = None

    async def client_status(
        self, nonce: str, request: StatusRequest, *, gfs_key: str
    ) -> str:
        """The approval state a household asked for over ``POST /gfs/proof``
        (:mod:`socialhome.gfs_client_status`): ``active`` / ``pending`` /
        ``banned`` when *request* verifies against the key its instance
        registered, as a question for THIS server (*gfs_key*, its identity
        key — a question meant for another GFS is no question here), else
        ``unknown``.

        *request* already passed :func:`parse_status_request` (supported
        suite, 64-byte signature, current ``ts``): ``unknown`` means only "no
        such registration" or "the signature did not verify". An unknown
        instance verifies against :data:`TIMING_UNIFORM_DUMMY_KEY_HEX`, so it
        answers no sooner than a bad signature for a registered one: only the
        key holder learns whether — and how — it is registered here (the same
        rule as the WebSocket hello). A rejected household's row is deleted,
        so it reads ``unknown`` too."""
        instance = await self._repo.get_instance(request.instance_id)
        key_hex = (
            instance.public_key
            if instance is not None
            else TIMING_UNIFORM_DUMMY_KEY_HEX
        )
        verified = verify_status_request(key_hex, gfs_key, nonce, request)
        if instance is None or not verified or instance.status not in CLIENT_STATUSES:
            return "unknown"
        return instance.status

    async def check_client_identity(self, instance_id: str, public_key: str) -> None:
        """Before a ``/gfs/register`` spends its pairing token: the id must
        be the one the key derives (``derive_instance_id``) — so nobody can
        register an id they do not hold the key of, such as another
        household's or an anonymous mailbox's. A row that already exists
        under exactly this (id, key) pair is let through as is
        (grandfathered; every household has derived its id since the first
        release, so none is expected). Raises
        :class:`InstanceRegistrationRefused`."""
        try:
            raw = bytes.fromhex(public_key)
        except ValueError, TypeError:
            raise InstanceRegistrationRefused(400, "invalid_public_key") from None
        if len(raw) != 32:
            raise InstanceRegistrationRefused(400, "invalid_public_key")
        if derive_instance_id(raw) == instance_id:
            return
        existing = await self._repo.get_instance(instance_id)
        if existing is not None and existing.public_key.lower() == public_key.lower():
            return
        raise InstanceRegistrationRefused(400, "instance_id_mismatch")

    async def register_client(
        self,
        instance_id: str,
        public_key: str,
        *,
        display_name: str = "",
        auto_accept: bool = False,
        keywrap_public_key: str = "",
        kem_suite: str = "",
        keywrap_sig: str = "",
    ) -> None:
        """A household's own ``/gfs/register`` (after
        :meth:`check_client_identity`): written in one statement that never
        lets a DIFFERENT key take an existing row over and never inserts an
        id a mailbox holds. Raises :class:`InstanceRegistrationRefused`."""
        written = await self._repo.register_instance_pinned(
            ClientInstance(
                instance_id=instance_id,
                display_name=display_name,
                public_key=public_key,
                status="active" if auto_accept else "pending",
                auto_accept=auto_accept,
                keywrap_public_key=keywrap_public_key,
                kem_suite=kem_suite,
                keywrap_sig=keywrap_sig,
            )
        )
        if written:
            log.debug("GFS: registered instance %s", instance_id)
            return
        existing = await self._repo.get_instance(instance_id)
        if (
            existing is not None
            and existing.status == "banned"
            and existing.public_key.lower() == public_key.lower()
        ):
            # Banned: nothing changed and only an operator lifts it. The
            # caller answers exactly as for any registration, so
            # ``/gfs/register`` is no way to learn whether one is banned
            # (every other route already refuses a banned household).
            return
        if existing is not None:
            raise InstanceRegistrationRefused(409, "instance_key_mismatch")
        raise InstanceRegistrationRefused(409, "id_in_use")

    async def register_instance(
        self,
        instance_id: str,
        public_key: str,
        *,
        display_name: str = "",
        auto_accept: bool = False,
        keywrap_public_key: str = "",
        kem_suite: str = "",
        keywrap_sig: str = "",
    ) -> None:
        """Register or update a client household instance.

        ``keywrap_public_key`` + ``kem_suite`` carry the household's published
        X25519 key-wrap pubkey (Phase 5b foundation) — empty for an older HFS
        that ships none, in which case that household simply can't be
        sealed-to yet. ``keywrap_sig`` is the household's self-signature over
        that key-wrap pubkey so a remote sealer can bind it to the household
        identity end-to-end (``verify_keywrap_binding``) and never trust this
        GFS-served value; empty for an older HFS (unsealable, graceful).
        """
        await self._repo.upsert_instance(
            ClientInstance(
                instance_id=instance_id,
                display_name=display_name,
                public_key=public_key,
                status="active" if auto_accept else "pending",
                auto_accept=auto_accept,
                keywrap_public_key=keywrap_public_key,
                kem_suite=kem_suite,
                keywrap_sig=keywrap_sig,
            )
        )
        log.debug("GFS: registered instance %s", instance_id)

    @staticmethod
    def _assert_fresh_ts(ts: object) -> None:
        """Raise ``PermissionError`` unless *ts* is a fresh timestamp.

        Fresh means a tz-aware ISO 8601 string within ±300 s of now. A
        missing, non-string, unparseable or naive value is untrusted and
        rejected — a missing UTC offset can't be interpreted safely, so it
        is treated exactly like a stale one.
        """
        if not isinstance(ts, str) or not ts:
            raise PermissionError("Stale timestamp")
        try:
            parsed = datetime.fromisoformat(ts)
        except (ValueError, TypeError) as exc:
            raise PermissionError("Stale timestamp") from exc
        if parsed.tzinfo is None:
            raise PermissionError("Stale timestamp")
        now = datetime.now(UTC)
        if abs((now - parsed).total_seconds()) > INSTANCE_UPDATE_TS_SKEW_SECONDS:
            raise PermissionError("Stale timestamp")

    async def _verify_signed_request(
        self,
        instance_id: str,
        payload: dict[str, object],
        *,
        signature: str,
    ) -> ClientInstance:
        """Verify a self-signed, replay-guarded request from *instance_id*.

        Shared by every ``{instance_id, ..., ts}``-shaped GFS request
        (``update_instance``, ``hide_space``, the channel seats, invite
        links). The Ed25519
        *signature* is checked over the canonical JSON of *payload* against
        the instance's REGISTERED public key — because ``instance_id`` is
        part of the signed payload, a caller can only act as **itself**.
        The freshness check reads ``payload["ts"]`` — the very timestamp
        the signature covers, so a caller can never have a signature
        verified over one timestamp and freshness-checked against another.
        The signed ``ts`` must be a fresh, tz-aware ISO 8601 timestamp
        within ±300 s of now; unparseable / naive timestamps are rejected
        (a missing offset is treated as untrusted).

        Fails closed with :class:`PermissionError` on an unknown instance,
        a missing / malformed / non-verifying signature, or a stale ``ts``.
        Returns the looked-up :class:`ClientInstance` so callers can reuse
        it without a second read.
        """
        inst = await self._repo.get_instance(instance_id)
        if inst is None:
            raise PermissionError(f"Unknown instance: {instance_id}")

        # Signature is REQUIRED here (unlike publish_space's optional-sig
        # branch): an empty or invalid signature is a hard PermissionError.
        if not signature:
            raise PermissionError("Invalid Ed25519 signature")
        canonical = canonical_bytes(payload)
        try:
            # A malformed stored pubkey is just as unverifiable as a
            # malformed signature — fail closed with the same
            # PermissionError instead of escaping the handler as a 500.
            raw_key = bytes.fromhex(inst.public_key)
            raw_sig = b64url_decode(signature)
        except (ValueError, TypeError) as exc:
            raise PermissionError("Invalid Ed25519 signature") from exc
        if not verify_ed25519(raw_key, canonical, raw_sig):
            raise PermissionError("Invalid Ed25519 signature")

        # ``.get`` (not ``[...]``): a future caller that forgets ``ts`` must
        # fail closed as a 403, never escape the handler as a KeyError/500 —
        # the freshness contract enforces itself.
        self._assert_fresh_ts(payload.get("ts"))
        return inst

    async def verify_signed_request(
        self,
        instance_id: str,
        payload: dict[str, object],
        *,
        signature: str,
    ) -> ClientInstance:
        """Public entry point onto :meth:`_verify_signed_request`.

        Sibling services built on the same registered-instance identity —
        :class:`.invites.GfsInviteService` today — authenticate through this
        rather than reaching into the private method or growing a second
        signature verifier that could drift from this one (a duplicate is how
        a missing ``action`` discriminator or a dropped freshness check gets
        shipped). Same contract, same fail-closed
        :class:`PermissionError`.
        """
        return await self._verify_signed_request(
            instance_id,
            payload,
            signature=signature,
        )

    async def list_spaces(
        self,
        *,
        status: str | None = None,
    ) -> list[GlobalSpace]:
        """Return global/public spaces known to this GFS node.

        The public ``GET /gfs/spaces`` endpoint passes ``status='active'``
        to hide pending + banned rows. Internal callers (admin, tests)
        can pass ``status=None`` to see everything.
        """
        return await self._repo.list_spaces(status=status)

    async def get_space(self, space_id: str) -> GlobalSpace | None:
        """Single-space lookup — used by ``GET /gfs/spaces/{id}`` so SH
        clients fetching the metadata for a discovery-link can mirror
        the space row locally before subscribing."""
        return await self._repo.get_space(space_id)

    def attach_addressee(self, addressee: GfsAddressee) -> None:
        """Wire this server's addressee (its identity key), built after the
        service — see :meth:`_assert_addressed_here`."""
        self._addressee = addressee

    def _assert_addressed_here(self, gfs_key: str | None, *, required: bool) -> None:
        """Refuse a signed request addressed to another server.

        ``gfs_key`` is the server key a household bound into the signed
        bytes of a listing request (capability ``withdrawn_repin``). The
        owner-signed listing bodies otherwise name no server, so one sent to
        GFS A could be replayed by A, inside the ``ts`` window, to any other
        GFS the owner is registered on — and a withdrawal or a pin-only
        publish replayed that way takes a LIVE listing and its invite pages
        down there. A request that carries the field must name this
        server's key; *required* makes the field mandatory (the pin-only
        publish, which only households of this revision send). Absent and
        not required: an older household, judged as before."""
        if gfs_key is None:
            if required:
                raise PermissionError("gfs_key is required")
            return
        own = self._addressee
        if own is None or not own.accepts(own.instance_id, gfs_key):
            raise PermissionError("request is addressed to another server")

    async def hide_space(
        self,
        space_id: str,
        owning_instance: str,
        ts: str,
        signature: str,
        *,
        gfs_key: str | None = None,
    ) -> None:
        """Withdraw a space listing at its OWNER's signed request.

        Drives ``POST|DELETE /gfs/spaces/{id}/unpublish``. Authenticated
        by :meth:`_verify_signed_request`: an Ed25519
        signature over the canonical ``{action: "unpublish", owning_instance,
        space_id, ts}`` JSON (plus ``gfs_key``, the addressed server's key,
        when the household bound it — then it must be this server's, see
        :meth:`_assert_addressed_here`), verified against *owning_instance*'s registered
        public key and replay-guarded (±300 s). The ``action`` is inside the
        signed bytes, so a captured signature of another request can never
        be replayed as a delisting.

        Authentication alone is not enough: a signature only proves WHICH
        registered household is calling, so the caller must additionally BE
        the space's ``owning_instance`` — otherwise any paired household that
        learned a space id (they travel in discovery links) could delist
        someone else's space.

        Sets the reversible ``withdrawn`` flag and NEVER touches ``status``:
        ``banned`` is the GFS moderator's verdict and is deliberately sticky
        against re-publish, so writing it here permanently locked an owner out
        of its own listing. The row itself survives (audit trail, TOFU-pinned
        authority pubkey) and the owner's next signed publish clears the
        flag. Withdrawal affects DISCOVERY only (``docs/protocol/discovery.md``).

        Fails closed with :class:`PermissionError` on an unknown instance, a
        missing / malformed / invalid signature, a stale ``ts``, or a caller
        that is not the owner. An unknown space stays a silent no-op (the
        unpublish fan-out must be idempotent) — but only AFTER the signature
        verifies, so space existence is never leaked to an unsigned caller.
        """
        signed: dict[str, object] = {
            "action": "unpublish",
            "owning_instance": owning_instance,
            "space_id": space_id,
            "ts": ts,
        }
        if gfs_key is not None:
            signed["gfs_key"] = gfs_key
        await self._verify_signed_request(owning_instance, signed, signature=signature)
        self._assert_addressed_here(gfs_key, required=False)
        existing = await self._repo.get_space(space_id)
        if existing is None:
            return
        if existing.owning_instance != owning_instance:
            raise PermissionError("not the owner of this space")
        await self._withdraw_listing(space_id)
        log.info("GFS: owner %s withdrew space %s", owning_instance, space_id)

    async def _withdraw_listing(self, space_id: str) -> None:
        """Flag a listing ``withdrawn`` and take its invite pages down.

        The one withdrawal step, shared by :meth:`hide_space` and a
        ``withdrawn`` publish so the two cannot drift.
        """
        await self._repo.set_space_withdrawn(space_id, True)
        # Take the space's public invite pages down with the listing. The
        # ``withdrawn`` flag itself is reversible, but an invite link is a
        # STANDING public URL already sitting in other people's chats — leaving
        # it live would keep a working side door into a listing its owner
        # deliberately delisted. Re-publishing restores the listing, not the
        # old links; the owner mints fresh ones.
        if self._invite_repo is not None:
            await self._invite_repo.delete_for_space(space_id)

    async def _apply_certified_repin(
        self, space_id: str, *, new_pk: str, cert: dict
    ) -> bool:
        """Move a space's authority pin to ``new_pk`` under a cert the caller
        already verified with :func:`certified_authority_repin`.

        The one re-pin step, shared by a normal and a ``withdrawn`` publish
        so the two cannot drift. Compare-and-set against the row as stored
        right now; the repo forgets the old key's epoch state and writer-key
        pins and bumps ``authority_rotation_seq`` in that same statement.
        Returns whether the pin moved.
        """
        stored = await self._repo.get_space(space_id)
        if stored is None or not await self._repo.set_space_authority(
            space_id,
            expected_pk=stored.identity_public_key,
            expected_cert=stored.authority_cert,
            new_pk=new_pk,
            cert=cert,
        ):
            return False
        log.info(
            "GFS: space %s authority key re-pinned by its owner (epoch %d)",
            space_id,
            authority_cert_epoch(cert),
        )
        return True

    async def publish_space(
        self,
        *,
        space_id: str,
        owning_instance: str,
        name: str,
        description: str | None = None,
        about_markdown: str | None = None,
        cover_url: str | None = None,
        icon_url: str | None = None,
        min_age: int = 0,
        category: str = "general",
        join_mode: str = "",
        allow_subscribers: bool | None = None,
        accent_color: str = "#D2542A",
        primary_color: str = "#D2542A",
        identity_public_key: str = "",
        signature: str = "",
        ts: str = "",
        authority_cert: dict | None = None,
        place: object = None,
        join_contact: object = None,
        withdrawn: bool | None = None,
        gfs_key: str | None = None,
        member_count: object = None,
        follower_count: object = None,
    ) -> GlobalSpace:
        """Register / refresh a space row from the owning instance.

        Drives the ``POST /gfs/spaces/{id}/publish`` route. The publish
        body is signed by the owning HFS so a malicious peer can't
        flip another household's space metadata; signature is verified
        against the registered ``ClientInstance.public_key``.
        Auto-accepted clients land as ``status='active'`` (visible on
        ``GET /gfs/spaces``); pending clients stay pending until the
        GFS admin flips them.

        ``ts`` is an OPTIONAL tz-aware ISO 8601 timestamp. When present it is
        part of the signed canonical body and replay-guarded (±300 s), which
        makes the publish a *fresh* statement of intent — only such a publish
        may clear an owner's earlier ``withdrawn`` flag. A publish without
        ``ts`` (an older household) still registers and refreshes metadata,
        but CANNOT restore a withdrawn listing: its body is replayable
        forever, so honouring it would let anyone holding one historical
        publish body re-list a space its owner deliberately delisted.

        ``join_mode`` and ``allow_subscribers`` are OPTIONAL on the wire for
        the same mixed-version reason, and each is folded into the signed
        canonical body only when present (an older household's signature
        covers no such field). ``join_mode`` is the MEMBERSHIP gate shown on
        the listing; an empty or unknown value stores the fail-closed
        ``invite_only``. ``allow_subscribers`` is the READABILITY opt-in —
        ``None`` (an older household that sends no flag) stores the
        fail-closed ``False``.

        ``authority_cert`` (v_44) is the owner's cert for a ROTATED space
        authority key. It rides inside the signed body, and a body carrying
        one must carry a fresh signed ``ts``. The pin moves only when the
        cert verifies against the owner's REGISTERED key (and derives to
        ``owning_instance``), names exactly the offered key, and has a higher
        ``key_epoch`` than the cert stored here (0 for none) — see
        :func:`certified_authority_repin`. Anything else keeps the pin and logs.

        ``withdrawn`` (advertised as the ``withdrawn_repin`` capability) makes
        the publish a PIN-ONLY re-pin of a listing its owner withdrew. An
        owner that rotated the authority key must be able to move the pin
        here without re-listing the space — otherwise a withdrawn listing
        keeps the old pin forever, and whoever still holds the old key's seed
        can still sign the space's join contact here. Signed (folded in only
        when true) and it requires a fresh signed ``ts``. Such a publish
        stores NO metadata (the household sends it blank on purpose: a space
        that left the public tier must not leak its new name), never touches
        ``status``, never creates a row (:class:`SpaceNotListedError`, raised
        only after the signature verified) and leaves the listing
        ``withdrawn`` — setting the flag if it was not set yet. The pin moves
        under exactly the cert rule above. Because it withdraws, it MUST name
        the server it is for: ``gfs_key`` (this server's identity key, inside
        the signed bytes) is mandatory with ``withdrawn`` and optional
        otherwise (:meth:`_assert_addressed_here`).

        ``member_count`` / ``follower_count`` (advertised as the
        ``listing_counts`` capability) are the space's counts as its owner
        declares them, raw off the wire. Each is folded into the signed bytes
        only when sent (an older household signs a body without them), so a
        relay can neither add, change nor strip one. After the signature they
        are re-validated with :func:`parse_listing_count`; an invalid one is
        stored as ``None`` ("no number") and the listing still publishes.
        They are stored with the publish's signed ``ts`` as the
        last-writer-wins stamp (``upsert_space``); a publish without them
        clears them. A pin-only ``withdrawn`` publish never touches them.
        """
        inst = await self._repo.get_instance(owning_instance)
        if inst is None:
            raise PermissionError(
                f"Unknown owning_instance: {owning_instance}",
            )
        # Signature is MANDATORY (same trust model as update_instance): an
        # empty / malformed / invalid signature is a hard PermissionError, so
        # a registered peer can't overwrite another household's space listing.
        if not signature:
            raise PermissionError("Invalid Ed25519 signature")
        signed: dict[str, object] = {
            "space_id": space_id,
            "owning_instance": owning_instance,
            "name": name,
            "description": description or "",
            "about_markdown": about_markdown or "",
            "cover_url": cover_url or "",
            "icon_url": icon_url or "",
            "min_age": min_age,
            "category": category,
            "accent_color": accent_color,
            "primary_color": primary_color,
            "identity_public_key": identity_public_key or "",
        }
        # ``join_mode`` rides inside the signed bytes when the household sent
        # one, so it cannot be flipped in transit (a relay can't turn an
        # invite-only listing into a publicly readable one). An older
        # household sends none and signs a body without the key — folding an
        # empty value in unconditionally would break every such signature.
        # Stripping it is safe in the other direction: the missing value
        # normalises to the MORE restrictive ``invite_only``.
        if join_mode:
            signed["join_mode"] = join_mode
        # Same mixed-version rule for the readability opt-in: ``None`` means
        # the household sent no such key and signed a body without it, so
        # folding a value in unconditionally would break every older
        # signature. Its absence normalises to the MORE restrictive False.
        if allow_subscribers is not None:
            signed["allow_subscribers"] = bool(allow_subscribers)
        # v_44 — the authority cert rides inside the signed bytes (a relay
        # cannot bolt one onto an owner's publish), and a body that carries
        # one is a re-pin request: it must be a FRESH statement, so ``ts`` is
        # mandatory for it.
        if authority_cert is not None:
            if not ts:
                raise PermissionError("authority_cert requires a signed ts")
            signed["authority_cert"] = authority_cert
        # The pin-only flag rides inside the signed bytes when the household
        # sent it, so a relay can neither turn an owner's normal publish into
        # one that withdraws the listing nor strip it and re-list the space
        # under blank metadata. An older household sends none and signs a
        # body without the key. It must be a FRESH statement, like a cert.
        if withdrawn:
            if not ts:
                raise PermissionError("withdrawn requires a signed ts")
            signed["withdrawn"] = True
        # The addressed server's key, inside the signed bytes when sent.
        if gfs_key is not None:
            signed["gfs_key"] = gfs_key
        # The owner's optional place pin (``place: {lat, lon}``, advertised
        # as the ``place_pin`` capability) rides inside the signed bytes when
        # sent, so a relay can neither add, move nor strip one. Absent ⇒ the
        # listing has no pin (an older household never sends one; an owner
        # who removed it sends none either).
        if place is not None:
            signed["place"] = place
        # v_84 — the owner's join contact rides inside the signed bytes too
        # (an older household sends none and signs a body without it).
        if join_contact is not None:
            signed["join_contact"] = join_contact
        # The owner-declared counts ride inside the signed bytes too, as
        # sent, only when present (an older household signs without them).
        if member_count is not None:
            signed["member_count"] = member_count
        if follower_count is not None:
            signed["follower_count"] = follower_count
        # The signed ``ts`` is OPTIONAL, for backward compatibility:
        # ``publish_space`` has shipped production
        # callers, so hard-requiring ``ts`` would 403 every older household
        # against an upgraded GFS during a mixed-version window. This is the
        # documented "first-revision payloads missing the field default to the
        # single supported value" shape — with the security-relevant half
        # (clearing ``withdrawn``) gated on the replay-guarded variant.
        #
        # MIGRATION TRIPWIRE: once every household ships ``ts`` (it has been
        # sent by ``GfsConnectionService._build_publish_body`` since this
        # revision), delete the no-``ts`` branch below and make ``ts``
        # mandatory — verify it the way ``_verify_signed_request`` does.
        has_ts = bool(ts)
        if has_ts:
            signed["ts"] = ts
        canonical = canonical_bytes(signed)
        try:
            # A malformed stored pubkey is just as unverifiable as a malformed
            # signature — fail closed with the same PermissionError instead of
            # escaping the handler as a 500 (``register_instance`` never
            # validates that ``public_key`` is hex).
            raw_key = bytes.fromhex(inst.public_key)
            raw_sig = b64url_decode(signature)
        except (ValueError, TypeError) as exc:
            raise PermissionError("Invalid Ed25519 signature") from exc
        if not verify_ed25519(raw_key, canonical, raw_sig):
            raise PermissionError("Invalid Ed25519 signature")
        if has_ts:
            # Freshness is checked over the very ``ts`` the signature covers,
            # so a captured publish body can't be replayed to un-do the
            # owner's later withdrawal.
            self._assert_fresh_ts(ts)
        # After the signature: only the owner's own statement is judged.
        self._assert_addressed_here(gfs_key, required=bool(withdrawn))
        # Bound the stored ``about_markdown`` (verified above against the
        # full value, so the signature still holds). The public page caps
        # rendering too; capping at storage avoids DB bloat from a paired
        # instance publishing an oversized blob. Truncate rather than
        # reject so a slightly-long About still publishes.
        if about_markdown and len(about_markdown) > MAX_ABOUT_MARKDOWN_CHARS:
            about_markdown = about_markdown[:MAX_ABOUT_MARKDOWN_CHARS]
        # Same for the embedded cover / icon ``data:`` URIs, against the bound
        # the household side embeds under (``SPACE_IMAGE_EMBED_MAX_BYTES``).
        # A truncated data URI is a broken image, so an oversized one is
        # dropped instead — the space still lists, just without that art.
        cover_url = _bounded_image_uri(cover_url, space_id=space_id, kind="cover")
        icon_url = _bounded_image_uri(icon_url, space_id=space_id, kind="icon")
        existing = await self._repo.get_space(space_id)
        # Owner is immutable after first publish. space_id is a public,
        # owner-chosen UUID that travels in discovery links, so a registered
        # peer that learns it could otherwise re-publish the row with
        # owning_instance=itself — validly signed by its OWN key — and seize
        # the listing. Reject any publish whose owner differs from the stored
        # one (first-publisher wins; only that instance can refresh it).
        if existing is not None and existing.owning_instance != owning_instance:
            raise PermissionError("space already owned by another instance")
        # TOFU-pin the space's authority public key. The FIRST publish that
        # carries one pins it; thereafter it is IMMUTABLE — a later publish
        # offering a DIFFERENT pubkey keeps the pinned one (and logs a warning
        # so a swap attempt is diagnosable). This is what lets the GFS trust an
        # authority-signed relay from a non-owner seed-holder: the verify key
        # is established once by the owner and can't be silently rotated by a
        # later (possibly compromised-transport) publish. A first publish with
        # an empty pubkey leaves it NULL — that space can't use authority-signed
        # relay until a pubkey is pinned. The owner is already immutable
        # (checked above), so only the legit owner could ever pin/refresh.
        # v_44: the only way to MOVE a pin is an owner cert for the offered
        # key (``_certified_repin``, applied after the upsert below).
        pinned_pubkey = identity_public_key or ""
        certified = certified_authority_repin(
            space_id,
            owning_instance=owning_instance,
            owner_pk_hex=inst.public_key,
            offered_pk=pinned_pubkey,
            cert=authority_cert,
            stored_cert=existing.authority_cert if existing is not None else None,
        )
        if existing is not None and existing.identity_public_key:
            if (
                pinned_pubkey
                and pinned_pubkey != existing.identity_public_key
                and not certified
            ):
                log.warning(
                    "GFS: ignoring attempt to change pinned authority pubkey "
                    "for space %s (pinned=%s…, offered=%s…) — no valid owner "
                    "cert for it",
                    space_id,
                    existing.identity_public_key[:8],
                    pinned_pubkey[:8],
                )
            pinned_pubkey = existing.identity_public_key
        if withdrawn:
            # Pin-only from here on: nothing of the (blank) metadata above is
            # stored, the status is left alone and no seat is purged. Reached
            # only after the signature, freshness and owner checks, so an
            # unsigned caller never learns whether the space exists.
            if existing is None:
                raise SpaceNotListedError(space_id)
            if certified and authority_cert is not None:
                await self._apply_certified_repin(
                    space_id, new_pk=identity_public_key, cert=authority_cert
                )
            if not existing.withdrawn:
                await self._withdraw_listing(space_id)
                log.info(
                    "GFS: owner %s withdrew space %s with a pin-only publish",
                    owning_instance,
                    space_id,
                )
            repinned = await self._repo.get_space(space_id)
            if repinned is None:
                raise SpaceNotListedError(space_id)
            return repinned
        # Preserve posts_per_week / published_at from the existing row —
        # those are GFS-side bookkeeping, not the owner's to declare. Only
        # the owner's name / description / cover travel with the publish.
        # ``subscriber_count`` is inert since v_96 (no relay seats; GFS
        # migration 0033): carried as stored, never served.
        next_status = "active" if inst.auto_accept else "pending"
        if existing is not None and existing.status in ("banned", "active"):
            # A moderator's decision sticks: a ban stays a ban, and an
            # accepted space stays accepted — a routine re-publish must not
            # demote it to pending (its media uploads would all be 403; a
            # GFS that doesn't auto-accept relies on this, fourth review G2).
            # Only a NEW row (or one still pending) starts from the
            # household's auto-accept setting.
            next_status = existing.status
        # Never trust the wire: an unknown / missing value is stored as the
        # fail-closed ``invite_only`` rather than verbatim.
        normalized_join_mode = normalize_join_mode(join_mode)
        # Never trust the wire here either: absent ⇒ not publicly readable.
        readable = bool(allow_subscribers)
        # Verified above over the value as sent; stored re-validated to 4 dp.
        # A malformed point lists the space unpinned rather than failing the
        # publish — the pin only ever sorts.
        pin = parse_place_pin_wire(place)
        if place is not None and pin is None:
            log.warning("GFS: space %s published a malformed place; ignored", space_id)
        # Verified above over the values as sent; stored re-validated. An
        # invalid count is shown as no number, never refused — the counts
        # only ever inform.
        members = parse_listing_count(member_count)
        followers = parse_listing_count(follower_count)
        for field, raw, parsed in (
            ("member_count", member_count, members),
            ("follower_count", follower_count, followers),
        ):
            if raw is not None and parsed is None:
                log.warning(
                    "GFS: space %s published an invalid %s; stored as none",
                    space_id,
                    field,
                )
        fresh_counts = has_ts and (
            member_count is not None or follower_count is not None
        )
        # The contact is judged against the key this publish leaves pinned:
        # a re-pinning publish carries one signed with the NEW key (the old
        # seed is retired). Judged against the key pinned BEFORE the re-pin
        # it was dropped, and the listing lost the contact and with it the
        # owner's cert — a household that had pinned the space then saw a
        # new key without a chain and refused the GFS route for good.
        repinning = bool(certified and authority_cert is not None)
        contact = _served_join_contact(
            join_contact,
            space_id=space_id,
            owning_instance=owning_instance,
            join_mode=normalized_join_mode,
            allow_subscribers=readable,
            space_public_key_hex=identity_public_key if repinning else pinned_pubkey,
        )
        space = GlobalSpace(
            space_id=space_id,
            owning_instance=owning_instance,
            name=name,
            description=description,
            about_markdown=about_markdown,
            cover_url=cover_url,
            icon_url=icon_url,
            min_age=min_age,
            category=normalize_category(category),
            join_mode=normalized_join_mode,
            allow_subscribers=readable,
            accent_color=accent_color,
            primary_color=primary_color,
            status=next_status,
            subscriber_count=existing.subscriber_count if existing else 0,
            posts_per_week=existing.posts_per_week if existing else 0.0,
            published_at=existing.published_at if existing else "",
            identity_public_key=pinned_pubkey,
            authority_cert=existing.authority_cert if existing is not None else None,
            # A FRESH signed publish from the owner (one carrying a signed,
            # replay-guarded ``ts``) is the RECOVERY path for an earlier
            # withdrawal — it restores discoverability. A legacy publish with
            # no ``ts`` is replayable, so it preserves the current flag
            # instead. A moderator ``banned`` status is handled above and
            # stays sticky either way.
            withdrawn=(existing.withdrawn if existing and not has_ts else False),
            place_lat=pin.lat if pin is not None else None,
            place_lon=pin.lon if pin is not None else None,
            # The hint is inside the same signed ``place`` object.
            place_radius_km=pin.radius_km if pin is not None else None,
            join_contact=contact,
            # Counts change only on a FRESH publish that declares them: the
            # signed, freshness-checked ``ts`` stamps them for last-writer-
            # wins. A publish without counts (an older household, or one
            # sent before this server's ``listing_counts`` was known) and a
            # replayable no-``ts`` body leave the stored counts as they are.
            # (``upsert_space`` keeps the stored ones for a row without a
            # stamp, in the same statement.)
            member_count=members if fresh_counts else None,
            follower_count=followers if fresh_counts else None,
            counts_ts=normalize_counts_ts(ts) if fresh_counts else None,
        )
        if existing is not None and existing.withdrawn and not has_ts:
            log.info(
                "GFS: publish for withdrawn space %s carried no signed ts — "
                "listing stays withdrawn (upgrade the household so its "
                "publish body includes a timestamp)",
                space_id,
            )
        await self._repo.upsert_space(space)
        if repinning and authority_cert is not None:
            if await self._apply_certified_repin(
                space_id, new_pk=identity_public_key, cert=authority_cert
            ):
                space = replace(
                    space,
                    identity_public_key=identity_public_key,
                    authority_cert=authority_cert,
                )
            elif contact is not None:
                # The pin did not move here (a concurrent publish won the
                # compare-and-set). Keep the contact only if the row now
                # pins the key it was judged against — the other publish
                # carried the same cert — else it is not served.
                stored = await self._repo.get_space(space_id)
                if stored is None or stored.identity_public_key != identity_public_key:
                    space = replace(space, join_contact=None)
                    await self._repo.upsert_space(space)
                else:
                    space = replace(
                        space,
                        identity_public_key=stored.identity_public_key,
                        authority_cert=stored.authority_cert,
                    )
        log.info(
            "GFS: published space %s (owner=%s, status=%s, join_mode=%s, "
            "allow_subscribers=%s)",
            space_id,
            owning_instance,
            next_status,
            normalized_join_mode,
            readable,
        )
        return space

    async def update_instance(
        self,
        instance_id: str,
        display_name: str,
        ts: str,
        signature: str,
    ) -> None:
        """Update a registered instance's display_name. Signed by the
        instance and verified against its registered public key (same
        trust model as publish_space — a peer can't rename another
        household). Rejects unknown instances, bad signatures, and stale
        timestamps (replay guard)."""
        await self._verify_signed_request(
            instance_id,
            {
                "instance_id": instance_id,
                "display_name": display_name,
                "ts": ts,
            },
            signature=signature,
        )

        cleaned = display_name.strip()
        if not cleaned or len(cleaned) > MAX_DISPLAY_NAME_CHARS:
            raise ValueError("display_name must be 1-80 chars")

        await self._repo.set_instance_display_name(instance_id, cleaned)
        log.info("GFS: instance %s renamed to %r", instance_id, cleaned)
