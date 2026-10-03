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
  (:meth:`cert_for_peer` / :meth:`peer_payload_hook`): each payload gets the
  cert of the household it is addressed to and no other, only for a v_49
  peer, always inside the per-peer encrypted envelope;
* **accepts** the cert a seed holder sent us (:meth:`accept`): it must name
  our own identity key, this space, an epoch we hold the content key for,
  and verify against the pinned space key — then it is kept on that epoch's
  ``space_keys`` row (migration 0074);
* **checks** a cert carried by a relayed item (:meth:`check_item_cert`).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ..crypto import b64url_decode, ed25519_public_key
from ..domain.federation_capabilities import FederationCapability
from ..domain.space import SpaceRole
from ..domain.writer_cert import (
    WRITER_SCOPE_COMMENT,
    WRITER_SCOPE_WRITE,
    WriterCert,
    strongest_scope,
)
from ..writer_cert import (
    InvalidWriterCert,
    UnsupportedWriterCertSuite,
    sign_writer_cert,
    verify_writer_cert,
)

if TYPE_CHECKING:
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

PeerPayloadHook = Callable[[str, dict], Awaitable[dict]]


class SpaceWriterCertService:
    """Writer-cert issuer (seed holders) and holder (every household)."""

    __slots__ = (
        "_spaces",
        "_remote_members",
        "_keys",
        "_federation",
        "_own_instance_id",
        "_own_pk",
    )

    def __init__(
        self,
        *,
        space_repo: "AbstractSpaceRepo",
        remote_member_repo: "AbstractSpaceRemoteMemberRepo",
        space_key_repo: "AbstractSpaceKeyRepo",
        own_instance_id: str,
        own_identity_pk: bytes,
    ) -> None:
        self._spaces = space_repo
        self._remote_members = remote_member_repo
        self._keys = space_key_repo
        self._federation: "FederationService | None" = None
        self._own_instance_id = own_instance_id
        self._own_pk = own_identity_pk

    def attach_federation(self, federation_service: "FederationService") -> None:
        """Wire the federation service (peer versions + pinned peer keys)."""
        self._federation = federation_service

    # ── Entitlement ──────────────────────────────────────────────────────

    async def scope_for_instance(self, space: "Space", instance_id: str) -> str | None:
        """The strongest scope household ``instance_id`` holds in ``space``
        by our roster, or ``None`` (no live seat / no comment rights)."""
        if instance_id == self._own_instance_id:
            roles = [str(m.role) for m in await self._spaces.list_members(space.id)]
        else:
            roles = [
                str(r.role)
                for r in await self._remote_members.list_for_instance(
                    space.id, instance_id, include_tombstoned=False
                )
            ]
        scopes: list[str] = []
        for role in roles:
            if role in _WRITE_ROLES:
                scopes.append(WRITER_SCOPE_WRITE)
            elif (
                role == SpaceRole.SUBSCRIBER.value
                and space.features.allow_subscriber_comment
            ):
                scopes.append(WRITER_SCOPE_COMMENT)
        return strongest_scope(scopes)

    async def _instance_pk(self, instance_id: str) -> bytes | None:
        if instance_id == self._own_instance_id:
            return self._own_pk
        if self._federation is None:
            return None
        return await self._federation.peer_identity_public_key(instance_id)

    # ── Issuing ──────────────────────────────────────────────────────────

    async def issue_for_instance(
        self,
        space_id: str,
        instance_id: str,
        *,
        epoch: int | None = None,
    ) -> WriterCert | None:
        """Sign a cert for household ``instance_id`` at ``epoch`` (default:
        our current content epoch). ``None`` when we are not a seed holder,
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
        scope = await self.scope_for_instance(space, instance_id)
        if scope is None:
            return None
        pk = await self._instance_pk(instance_id)
        if pk is None or len(pk) != 32:
            return None
        return sign_writer_cert(
            space_seed=seed,
            space_id=space_id,
            epoch=epoch,
            instance_pk=pk,
            scope=scope,
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
            return {**payload, WRITER_CERT_FIELD: wire}

        return _hook

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

    async def own_cert(self, space_id: str, epoch: int) -> WriterCert | None:
        """The cert our household holds for ``(space, epoch)``: the stored
        one, else a self-issued one when we hold the seed."""
        raw = await self._keys.get_writer_cert(space_id, epoch)
        if raw is not None:
            try:
                return WriterCert.from_wire(json.loads(raw))
            except ValueError:
                log.warning(
                    "writer cert for space %s epoch %d is unreadable", space_id, epoch
                )
                return None
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

    @staticmethod
    def cert_instance_pk(raw: object) -> bytes | None:
        """The household key a wire cert names, or ``None`` if malformed."""
        try:
            return b64url_decode(WriterCert.from_wire(raw).instance_pk)
        except Exception:
            return None
