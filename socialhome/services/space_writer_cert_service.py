"""Issue, deliver, store and check space writer certificates (v_49).

A writer cert (:mod:`socialhome.writer_cert`) is the space AUTHORITY key's
per-epoch statement that one household may write (or only comment). This
service is the one place that

* **derives** what a household is entitled to from the roster the issuer
  already holds — owner / admin / moderator / member seats → ``write``; a
  follower (subscriber) seat → ``comment`` only while the space has
  ``allow_subscriber_comment`` on; several seats → ONE cert at the
  strongest scope. Nothing about issued certs is stored: a seed holder
  re-derives them from the roster at every epoch rotation;
* **issues** them — only a household holding the space seed (the owner, or a
  delegated admin), and only while that seed still matches the pinned space
  key (a demoted admin's leftover seed after a v_44 rotation mints nothing);
* **delivers** them by decorating payloads of channels that already exist
  (:meth:`cert_for_peer` / :meth:`peer_payload_hook`): each per-peer payload
  gets the cert of the household it is addressed to, only for a v_49 peer,
  inside that peer's encrypted envelope. (A cert is not secret: once its
  holder posts, it rides — encrypted — in the item's ``public_relay`` to
  every member and on to subscribers. Delivery is per household so nobody
  is handed a cert for someone else to store.);
* **accepts** the cert a seed holder sent us (:meth:`accept`): it must name
  our own identity key, this space, an epoch we hold the content key for,
  and verify against the pinned space key; a stored cert is replaced only by
  a newer one (``issued_at``, ``write`` preferred on a tie). It is kept on
  that epoch's ``space_keys`` row (migration 0074);
* **checks** a cert carried by a relayed item (:meth:`check_item_cert`, and
  :meth:`check_item` which adds epoch freshness: an item is accepted at the
  newest epoch we hold, or at the previous one for
  :data:`WRITER_CERT_EPOCH_GRACE_S` after the newest key arrived).

**Writer group key (v_50, strict mode).** In a space whose owner set
``gfs_publish_mode = "strict"``, the same per-peer channels also carry the
epoch's writer GROUP key (:mod:`socialhome.writer_key`) under
``writer_key``, next to the cert — only to a v_50 household that holds a
publishing scope (``write`` OR ``comment``: the connection server must not
be able to tell the two apart), never to a non-publisher. Seed holders derive
it from the space seed and store nothing; every other household verifies a
delivered grant against the pinned space key and keeps it KEK-wrapped on that
epoch's ``space_keys`` row (migration 0076). The key rotates with every
content epoch, so the revocation that retires a writer cert (a rotation)
retires the writer key with it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..crypto import b64url_decode, derive_instance_id, ed25519_public_key
from ..domain.federation_capabilities import FederationCapability
from ..domain.gfs_member_publish import GFS_PUBLISH_MODE_STRICT
from ..domain.space import SpaceFeatureAccess, SpaceRole
from ..domain.writer_cert import (
    MAX_WRITER_USERS,
    WRITER_SCOPE_COMMENT,
    WRITER_SCOPE_WRITE,
    WriterCert,
    WriterEntitlement,
    scope_permits,
    strongest_scope,
)
from ..domain.writer_key import WRITER_KEY_FIELD, WriterKeyGrant
from ..writer_cert import (
    InvalidWriterCert,
    UnsupportedWriterCertSuite,
    bind_writer_users,
    sign_writer_cert,
    verify_writer_cert,
)
from ..writer_key import (
    derive_writer_seed,
    issue_writer_key_grant,
    verify_writer_key_grant,
)

if TYPE_CHECKING:
    from ..infrastructure.key_manager import KeyManager
    from ..domain.space import Space
    from ..federation.federation_service import FederationService
    from ..repositories.space_key_repo import AbstractSpaceKeyRepo
    from ..repositories.space_remote_member_repo import AbstractSpaceRemoteMemberRepo
    from ..repositories.space_repo import AbstractSpaceRepo

log = logging.getLogger(__name__)

#: The wire key a writer cert rides under, in every channel that carries one.
WRITER_CERT_FIELD: str = "writer_cert"

#: Seats that may write. Everything else (``subscriber``) is comment-only at
#: best. Compared as strings: the remote mirror stores the role as text.
_WRITE_ROLES: frozenset[str] = frozenset(
    {
        SpaceRole.OWNER.value,
        SpaceRole.ADMIN.value,
        SpaceRole.MODERATOR.value,
        SpaceRole.MEMBER.value,
    }
)

#: Seats that may POST directly, per ``posts`` access level.
_POSTING_ROLES: dict[SpaceFeatureAccess, frozenset[str]] = {
    SpaceFeatureAccess.OPEN: _WRITE_ROLES,
    SpaceFeatureAccess.MODERATED: frozenset(
        {SpaceRole.OWNER.value, SpaceRole.ADMIN.value, SpaceRole.MODERATOR.value}
    ),
    SpaceFeatureAccess.ADMIN_ONLY: frozenset(
        {SpaceRole.OWNER.value, SpaceRole.ADMIN.value}
    ),
}

PeerPayloadHook = Callable[[str, dict], Awaitable[dict]]

#: How long the PREVIOUS content epoch stays open for cert-authorized items
#: after the newest key arrived here. A rotation reaches members within
#: seconds when they are online (the rekey rides the outbox / relay queue),
#: so an author that sealed a post just before it learned the new key gets
#: through; a writer revoked by that rotation is held to the same 10 minutes
#: after an honest receiver learned the new key — a bounded window that
#: needs no seed holder online (an expiry on the cert itself would).
WRITER_CERT_EPOCH_GRACE_S: int = 600

#: Ordering of scopes for "did this household's rights get weaker?".
_SCOPE_RANK: dict[str | None, int] = {
    None: 0,
    WRITER_SCOPE_COMMENT: 1,
    WRITER_SCOPE_WRITE: 2,
}


def scope_weakened(
    before: "str | None | WriterEntitlement", after: "str | None | WriterEntitlement"
) -> bool:
    """True when ``after`` grants less than ``before``: a weaker scope
    (write→comment, any→none), or — for entitlements — the same scope held
    by fewer users (a user's seat ended, or the access level stopped letting
    them post, while the household keeps other seats)."""
    b_scope = before.scope if isinstance(before, WriterEntitlement) else before
    a_scope = after.scope if isinstance(after, WriterEntitlement) else after
    if _SCOPE_RANK[a_scope] < _SCOPE_RANK[b_scope]:
        return True
    if (
        isinstance(before, WriterEntitlement)
        and isinstance(after, WriterEntitlement)
        and a_scope == b_scope
    ):
        return bool(before.user_ids - after.user_ids)
    return False


def _parse_utc(value: object) -> datetime | None:
    """A ``space_keys.created_at`` value as an aware UTC datetime. Both stored
    shapes are UTC: SQLite's naive ``YYYY-MM-DD HH:MM:SS`` and Python's
    ``…+00:00``."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class SpaceWriterCertService:
    """Writer-cert issuer (seed holders) and holder (every household)."""

    __slots__ = (
        "_spaces",
        "_remote_members",
        "_keys",
        "_federation",
        "_own_instance_id",
        "_own_pk",
        "_truncation_warned",
        "_kek",
    )

    def __init__(
        self,
        *,
        space_repo: "AbstractSpaceRepo",
        remote_member_repo: "AbstractSpaceRemoteMemberRepo",
        space_key_repo: "AbstractSpaceKeyRepo",
        own_instance_id: str,
        own_identity_pk: bytes,
        key_manager: "KeyManager | None" = None,
    ) -> None:
        self._spaces = space_repo
        #: KEK that wraps a held writer group key at rest (v_50). Without one
        #: this household neither stores nor uses a delivered writer key.
        self._kek = key_manager
        self._remote_members = remote_member_repo
        self._keys = space_key_repo
        self._federation: "FederationService | None" = None
        self._own_instance_id = own_instance_id
        self._own_pk = own_identity_pk
        #: (space id, instance id) pairs already warned about a binding
        #: truncated to MAX_WRITER_USERS — once each per process.
        self._truncation_warned: set[tuple[str, str]] = set()

    def attach_federation(self, federation_service: "FederationService") -> None:
        """Wire the federation service (peer versions + pinned peer keys)."""
        self._federation = federation_service

    # ── Entitlement ──────────────────────────────────────────────────────

    async def scope_for_instance(self, space: "Space", instance_id: str) -> str | None:
        """The strongest scope household ``instance_id`` holds in ``space``
        by our roster, or ``None`` (no live seat / no comment rights)."""
        return (await self.entitlement_for_instance(space, instance_id)).scope

    async def entitlement_for_instance(
        self, space: "Space", instance_id: str
    ) -> WriterEntitlement:
        """Household ``instance_id``'s scope AND the users holding it (the
        v2 user binding), by our roster and the space's ``posts`` access
        level: a seat writes only where the level lets that role post
        directly — ``ADMIN_ONLY``: owner / admin; ``MODERATED``: content
        authority (owner / admin / moderator), so a plain member's post keeps
        going through the host's review queue; ``OPEN``: every writer role.
        A seat that may not post keeps ``comment`` (comments are judged per
        item by the receivers); a follower seat gets ``comment`` only while
        ``allow_subscriber_comment`` is on."""
        if instance_id == self._own_instance_id:
            seats = [
                (m.user_id, str(m.role))
                for m in await self._spaces.list_members(space.id)
            ]
        else:
            seats = [
                (r.user_id, str(r.role))
                for r in await self._remote_members.list_for_instance(
                    space.id, instance_id, include_tombstoned=False
                )
            ]
        posting = _POSTING_ROLES[space.features.access_level("posts")]
        by_scope: dict[str, set[str]] = {
            WRITER_SCOPE_WRITE: set(),
            WRITER_SCOPE_COMMENT: set(),
        }
        for user_id, role in seats:
            if role in posting:
                by_scope[WRITER_SCOPE_WRITE].add(user_id)
            elif role in _WRITE_ROLES or (
                role == SpaceRole.SUBSCRIBER.value
                and space.features.allow_subscriber_comment
            ):
                by_scope[WRITER_SCOPE_COMMENT].add(user_id)
        scope = strongest_scope(k for k, v in by_scope.items() if v)
        if scope is None:
            return WriterEntitlement(None)
        return WriterEntitlement(scope, frozenset(by_scope[scope]))

    async def verified_instance_pk(
        self, instance_id: str, *, claimed: bytes | None = None
    ) -> bytes | None:
        """Household ``instance_id``'s identity key, or ``None``.

        Ours for ourselves; else the key we pin for it (``remote_instances``,
        the key §24.11 verifies its envelopes against); else — for a
        mesh-only household we hold no row for — a ``claimed`` key that
        derives to its instance id. An instance id IS the fingerprint of its
        identity key (§4.1.2), which is how the v_31 routed-origin check
        verified that household's envelope in the first place."""
        if instance_id == self._own_instance_id:
            return self._own_pk
        if self._federation is not None:
            pinned = await self._federation.peer_identity_public_key(instance_id)
            if pinned is not None:
                return pinned
        if claimed is None or len(claimed) != 32:
            return None
        try:
            if derive_instance_id(claimed) == instance_id:
                return claimed
        except ValueError:
            return None
        return None

    async def peer_is_cert_aware(self, instance_id: str) -> bool:
        """True when ``instance_id`` advertises v_49 — its items must carry a
        cert (a hint without one is a stripped cert, not an older author)."""
        if self._federation is None:
            return False
        return await self._federation.peer_supports(
            instance_id,
            min_version=FederationCapability.MIN_FOR_MEMBER_GFS_PUBLISH,
        )

    # ── Issuing ──────────────────────────────────────────────────────────

    async def issue_for_instance(
        self,
        space_id: str,
        instance_id: str,
        *,
        epoch: int | None = None,
        instance_pk_hint: bytes | None = None,
    ) -> WriterCert | None:
        """Sign a cert for household ``instance_id`` at ``epoch`` (default:
        our current content epoch). ``instance_pk_hint`` is the key a
        mesh-only household's item names (see :meth:`verified_instance_pk`). ``None`` when we are not a seed holder,
        our seed no longer matches the pin, there is no epoch, the household
        holds no entitled seat, or its identity key is unknown to us."""
        space = await self._spaces.get(space_id)
        if space is None:
            return None
        seed = await self._spaces.get_space_seed(space_id)
        if seed is None:
            return None
        if ed25519_public_key(seed).hex() != space.identity_public_key:
            # A seed left behind by a v_44 rotation is no authority any more.
            return None
        if epoch is None:
            latest = await self._keys.get_latest(space_id)
            if latest is None:
                return None
            epoch = latest.epoch
        entitlement = await self.entitlement_for_instance(space, instance_id)
        if entitlement.scope is None:
            return None
        pk = await self.verified_instance_pk(instance_id, claimed=instance_pk_hint)
        if pk is None or len(pk) != 32:
            return None
        cert = sign_writer_cert(
            space_seed=seed,
            space_id=space_id,
            epoch=epoch,
            instance_pk=pk,
            scope=entitlement.scope,
        )
        # v2: bind the household's users holding that scope (a second
        # signature — v1 verifiers still accept the cert unchanged).
        users = sorted(entitlement.user_ids)
        if len(users) > MAX_WRITER_USERS and (
            (space_id, instance_id) not in self._truncation_warned
        ):
            self._truncation_warned.add((space_id, instance_id))
            log.warning(
                "writer cert for household %s in space %s: %d writer users, "
                "binding only the first %d — the rest cannot publish over the "
                "connection server (their posts take the host path)",
                instance_id,
                space_id,
                len(users),
                MAX_WRITER_USERS,
            )
        return bind_writer_users(
            cert, space_seed=seed, user_ids=users[:MAX_WRITER_USERS]
        )

    async def cert_for_peer(
        self,
        space_id: str,
        instance_id: str,
        *,
        epoch: int | None = None,
    ) -> dict | None:
        """The wire cert to deliver to peer ``instance_id`` — gated on the
        peer advertising v_49 — or ``None``."""
        if self._federation is None or instance_id == self._own_instance_id:
            return None
        if not await self._federation.peer_supports(
            instance_id,
            min_version=FederationCapability.MIN_FOR_MEMBER_GFS_PUBLISH,
        ):
            return None
        cert = await self.issue_for_instance(space_id, instance_id, epoch=epoch)
        return cert.to_wire() if cert is not None else None

    def peer_payload_hook(
        self,
        space_id: str,
        *,
        only_instance: str | None = None,
        epoch: int | None = None,
    ) -> PeerPayloadHook:
        """A per-peer payload decorator for a fan-out: each peer's copy gets
        ITS OWN cert under ``writer_cert`` (never another household's). With
        ``only_instance`` the other peers' copies stay untouched."""

        async def _hook(instance_id: str, payload: dict) -> dict:
            if only_instance is not None and instance_id != only_instance:
                return payload
            try:
                wire = await self.cert_for_peer(space_id, instance_id, epoch=epoch)
            except Exception:
                log.exception(
                    "writer cert: issuing for %s in %s failed", instance_id, space_id
                )
                return payload
            if wire is None:
                return payload
            out = {**payload, WRITER_CERT_FIELD: wire}
            try:
                key = await self.writer_key_for_peer(
                    space_id, instance_id, epoch=wire.get("epoch")
                )
            except Exception:
                log.exception(
                    "writer key: issuing for %s in %s failed", instance_id, space_id
                )
                key = None
            if key is not None:
                out[WRITER_KEY_FIELD] = key
            return out

        return _hook

    # ── Writer group key (v_50) ──────────────────────────────────────────

    async def _matching_seed(self, space: "Space") -> bytes | None:
        """Our space seed iff it still matches the pinned authority key."""
        seed = await self._spaces.get_space_seed(space.id)
        if seed is None or ed25519_public_key(seed).hex() != space.identity_public_key:
            return None
        return seed

    async def writer_key_for_peer(
        self,
        space_id: str,
        instance_id: str,
        *,
        epoch: int | None = None,
    ) -> dict | None:
        """The writer group key grant to deliver to peer ``instance_id`` at
        ``epoch`` (default: our current content epoch), or ``None``.

        Only in a STRICT space, only from a seed holder whose seed matches
        the pin, only to a v_50 peer that holds a publishing scope (``write``
        or ``comment`` alike) — never to a non-publisher, never to an older
        household (it gets no key and keeps the host path)."""
        if self._federation is None or instance_id == self._own_instance_id:
            return None
        if not await self._federation.peer_supports(
            instance_id,
            min_version=FederationCapability.MIN_FOR_STRICT_MEMBER_PUBLISH,
        ):
            return None
        space = await self._spaces.get(space_id)
        if space is None or space.features.gfs_publish_mode != GFS_PUBLISH_MODE_STRICT:
            return None
        seed = await self._matching_seed(space)
        if seed is None:
            return None
        if epoch is None:
            latest = await self._keys.get_latest(space_id)
            if latest is None:
                return None
            epoch = latest.epoch
        if (await self.entitlement_for_instance(space, instance_id)).scope is None:
            return None
        return issue_writer_key_grant(
            space_seed=seed, space_id=space_id, epoch=int(epoch)
        ).to_wire()

    @staticmethod
    def _writer_key_aad(space_id: str, epoch: int) -> bytes:
        return f"socialhome-writer-key:{space_id}:{epoch}".encode("utf-8")

    async def accept_writer_key(self, space_id: str, raw: object) -> bool:
        """Verify + store a writer key grant a seed holder delivered to us
        (v_50). It must verify against the pinned space key (its authority
        cert, and the seed matching the pinned writer key) for an epoch we
        hold a content key for; it is stored KEK-wrapped on that epoch's key
        row. ``True`` when stored. WARNING on a bad grant."""
        if raw is None or self._kek is None:
            return False
        space = await self._spaces.get(space_id)
        if space is None:
            return False
        try:
            grant = WriterKeyGrant.from_wire(raw)
            verify_writer_key_grant(
                grant,
                space_pubkey=bytes.fromhex(space.identity_public_key),
                space_id=space_id,
            )
        except ValueError as exc:
            # UnsupportedWriterKeySuite / InvalidWriterKey are ValueErrors.
            log.warning("writer key for space %s refused: %s", space_id, exc)
            return False
        wrapped = self._kek.encrypt(
            json.dumps(grant.to_wire(), sort_keys=True).encode("utf-8"),
            associated_data=self._writer_key_aad(space_id, grant.epoch),
        )
        stored = await self._keys.set_writer_key(space_id, grant.epoch, wrapped)
        if not stored:
            log.info(
                "writer key for space %s epoch %d: no key for that epoch — dropped",
                space_id,
                grant.epoch,
            )
        return stored

    async def own_writer_key(self, space_id: str, epoch: int) -> bytes | None:
        """The writer group key seed our household signs anonymous publishes
        with at ``epoch``, or ``None``: derived when we hold the matching
        seed and the space is strict; else the stored grant while it still
        verifies against the CURRENT pin (a v_44 re-pin retires it)."""
        space = await self._spaces.get(space_id)
        if space is None:
            return None
        seed = await self._matching_seed(space)
        if seed is not None:
            if space.features.gfs_publish_mode != GFS_PUBLISH_MODE_STRICT:
                return None
            return derive_writer_seed(seed, space_id, epoch)
        if self._kek is None:
            return None
        wrapped = await self._keys.get_writer_key(space_id, epoch)
        if wrapped is None:
            return None
        try:
            raw = self._kek.decrypt(
                wrapped, associated_data=self._writer_key_aad(space_id, epoch)
            )
            grant = WriterKeyGrant.from_wire(json.loads(raw))
            if grant.epoch != epoch:
                return None
            return verify_writer_key_grant(
                grant,
                space_pubkey=bytes.fromhex(space.identity_public_key),
                space_id=space_id,
            )
        except Exception as exc:
            log.info(
                "stored writer key for space %s epoch %d no longer holds: %s",
                space_id,
                epoch,
                exc,
            )
            return None

    async def writer_key_cert_wire(self, space_id: str, epoch: int) -> dict | None:
        """The authority-signed ``writer_key_cert`` a seed holder pins at the
        GFS for ``epoch`` (v_50) — only for a STRICT space and while our seed
        matches the pin; else ``None``."""
        space = await self._spaces.get(space_id)
        if space is None or space.features.gfs_publish_mode != GFS_PUBLISH_MODE_STRICT:
            return None
        seed = await self._matching_seed(space)
        if seed is None:
            return None
        return issue_writer_key_grant(
            space_seed=seed, space_id=space_id, epoch=epoch
        ).writer_key_cert.to_wire()

    # ── Holding ──────────────────────────────────────────────────────────

    async def accept(self, space_id: str, raw: object) -> bool:
        """Verify + store a cert a seed holder delivered to us. ``True`` when
        stored. Anything not naming our own household, this space and an
        epoch we hold a key for — or not signed by the pinned space key —
        is refused (WARNING for a bad signature / suite)."""
        if raw is None:
            return False
        space = await self._spaces.get(space_id)
        if space is None:
            return False
        try:
            cert = WriterCert.from_wire(raw)
            verify_writer_cert(
                cert,
                space_pubkey=bytes.fromhex(space.identity_public_key),
                space_id=space_id,
                epoch=cert.epoch,
                author_pk=self._own_pk,
                required_scope=cert.scope,
            )
        except (UnsupportedWriterCertSuite, InvalidWriterCert) as exc:
            log.warning("writer cert for space %s refused: %s", space_id, exc)
            return False
        except ValueError as exc:
            log.warning("writer cert for space %s malformed: %s", space_id, exc)
            return False
        held = await self._stored_valid(space_id, cert.epoch)
        if held is not None and not _supersedes(cert, held):
            log.info(
                "writer cert for space %s epoch %d: keeping the newer / stronger "
                "one we hold",
                space_id,
                cert.epoch,
            )
            return False
        stored = await self._keys.set_writer_cert(
            space_id, cert.epoch, json.dumps(cert.to_wire(), sort_keys=True)
        )
        if not stored:
            log.info(
                "writer cert for space %s epoch %d: no key for that epoch — dropped",
                space_id,
                cert.epoch,
            )
        return stored

    async def _stored_valid(self, space_id: str, epoch: int) -> WriterCert | None:
        """The stored cert for ``(space, epoch)`` iff it still verifies for
        us against the CURRENT pin (a v_44 re-pin retires older certs)."""
        raw = await self._keys.get_writer_cert(space_id, epoch)
        if raw is None:
            return None
        space = await self._spaces.get(space_id)
        if space is None:
            return None
        try:
            cert = WriterCert.from_wire(json.loads(raw))
            verify_writer_cert(
                cert,
                space_pubkey=bytes.fromhex(space.identity_public_key),
                space_id=space_id,
                epoch=epoch,
                author_pk=self._own_pk,
                required_scope=cert.scope,
            )
        except ValueError as exc:
            log.info(
                "stored writer cert for space %s epoch %d no longer holds: %s",
                space_id,
                epoch,
                exc,
            )
            return None
        return cert

    async def own_cert(self, space_id: str, epoch: int) -> WriterCert | None:
        """The cert our household holds for ``(space, epoch)``: the stored
        one while it verifies against the current pin, else a self-issued one
        when we hold the seed, else ``None``."""
        held = await self._stored_valid(space_id, epoch)
        if held is not None:
            return held
        return await self.issue_for_instance(
            space_id, self._own_instance_id, epoch=epoch
        )

    async def current_own_cert_wire(self, space_id: str) -> dict | None:
        """Our cert for the current content epoch, as wire, or ``None``."""
        latest = await self._keys.get_latest(space_id)
        if latest is None:
            return None
        cert = await self.own_cert(space_id, latest.epoch)
        return cert.to_wire() if cert is not None else None

    # ── Checking an item ─────────────────────────────────────────────────

    @staticmethod
    def check_item_cert(
        space: "Space",
        raw: object,
        *,
        epoch: int,
        author_pk: bytes,
        required_scope: str,
        space_pubkey_hex: str | None = None,
    ) -> bool:
        """True iff ``raw`` is a valid cert for ``author_pk`` at
        ``required_scope`` in ``space`` / ``epoch`` against the pinned space
        key (``space_pubkey_hex`` overrides the space row's pin). Logs the
        reason at WARNING on failure."""
        try:
            cert = WriterCert.from_wire(raw)
            verify_writer_cert(
                cert,
                space_pubkey=bytes.fromhex(
                    space_pubkey_hex or space.identity_public_key
                ),
                space_id=space.id,
                epoch=epoch,
                author_pk=author_pk,
                required_scope=required_scope,
            )
        except ValueError as exc:
            # UnsupportedWriterCertSuite / InvalidWriterCert are ValueErrors.
            log.warning("writer cert check failed for space %s: %s", space.id, exc)
            return False
        return True

    async def epoch_is_fresh(
        self, space_id: str, epoch: int, *, now: datetime | None = None
    ) -> bool:
        """True when a cert-authorized item sealed at ``epoch`` may still be
        accepted here: ``epoch`` is the newest we hold, or the one right
        before it while the newest arrived less than
        :data:`WRITER_CERT_EPOCH_GRACE_S` ago (``space_keys.created_at`` is
        when a key was minted or imported here)."""
        latest = await self._keys.get_latest(space_id)
        if latest is None:
            return False
        if epoch == latest.epoch:
            return True
        previous = await self._keys.get_previous(space_id, latest.epoch)
        if previous is None or previous.epoch != epoch:
            return False
        arrived = _parse_utc(latest.created_at)
        if arrived is None:
            return False
        age = ((now or datetime.now(timezone.utc)) - arrived).total_seconds()
        return age <= WRITER_CERT_EPOCH_GRACE_S

    async def check_item(
        self,
        space: "Space",
        raw: object,
        *,
        epoch: int,
        author_pk: bytes,
        required_scope: str,
        space_pubkey_hex: str | None = None,
    ) -> bool:
        """:meth:`check_item_cert` plus epoch freshness (WARNING on a stale
        epoch) — what every receiver of a cert-authorized item runs."""
        if not self.check_item_cert(
            space,
            raw,
            epoch=epoch,
            author_pk=author_pk,
            required_scope=required_scope,
            space_pubkey_hex=space_pubkey_hex,
        ):
            return False
        if not await self.epoch_is_fresh(space.id, epoch):
            log.warning(
                "writer cert for space %s: epoch %d is no longer open here",
                space.id,
                epoch,
            )
            return False
        return True

    @staticmethod
    def cert_instance_pk(raw: object) -> bytes | None:
        """The household key a wire cert names, or ``None`` if malformed."""
        try:
            return b64url_decode(WriterCert.from_wire(raw).instance_pk)
        except Exception:
            return None


def _supersedes(new: WriterCert, held: WriterCert) -> bool:
    """A received cert replaces the one we hold only when it is newer, or as
    new and at least as strong (``write`` wins a tie)."""
    if new.issued_at != held.issued_at:
        return new.issued_at > held.issued_at
    return scope_permits(new.scope, held.scope)
