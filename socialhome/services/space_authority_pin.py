"""Apply an owner-signed space authority cert on a receiving household (v_44).

The ONE place a household that does not host a space moves its pinned
space authority key (``spaces.identity_public_key``). Every inbound path
that can carry a cert funnels here — the ``SPACE_AUTHORITY_ROTATED`` bundle,
``SPACE_ADMIN_KEY_SHARE``, a config / invite / redeem-ACK ``space_meta``,
the roster snapshot, and the GFS listing a subscriber heals from — so the
rules below are enforced identically everywhere:

1. The cert must verify (:func:`socialhome.authority_cert.verify_authority_cert`):
   known suites only, bound to THIS space and to the space's owner
   instance, ``derive_instance_id(owner_pk) == owner_instance_id``, and the
   owner key equal to the one we stored for the host if we stored one.
2. It applies only when its epoch is HIGHER than the epoch we hold. The
   same epoch with the same key is a no-op; the same epoch with a different
   key, or a lower epoch, is dropped at WARNING. A receiver that missed
   rotations jumps straight to the latest.
3. The household that hosts the space ignores certs for it — its own pin is
   the authority.
4. Applying is one compare-and-set write: pin + epoch move together and any
   seed we hold (necessarily for the retired key) is cleared.

Once the pin moved, every existing verifier reads the new key from the row,
so a signature made with the old key fails everywhere automatically.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from ..authority_cert import (
    InvalidAuthorityCert,
    UnsupportedAuthorityCertSuite,
    VerifiedAuthorityCert,
    sign_authority_cert,
    verify_authority_cert,
)

if TYPE_CHECKING:
    from ..domain.space import Space
    from ..repositories.space_repo import AbstractSpaceRepo

log = logging.getLogger(__name__)


class AuthorityPinRefresher(Protocol):
    """Heals a subscriber's pin from a GFS listing's owner cert (v_44).

    Implemented by
    :class:`~socialhome.services.gfs_space_mirror_service.GfsSpaceMirrorService`;
    the GFS-relay consumers call it when an authority check fails.
    """

    async def refresh_authority_pin(self, space_id: str) -> bool: ...


def owner_authority_cert(
    space: "Space",
    *,
    own_instance_id: str | None,
    owner_seed: object,
    owner_pk: object,
) -> dict | None:
    """The cert for ``space``'s CURRENT authority key, signed by this
    household — or ``None`` when there is nothing to certify.

    ``None`` unless this household hosts the space and the key has been
    rotated at least once (epoch 0 is the creation-time key, TOFU-pinned,
    no cert). The owner re-signs on demand rather than storing the cert:
    only the epoch and the key it names matter, and both live on the row.
    ``owner_seed`` / ``owner_pk`` are the household identity key; anything
    that is not raw 32-byte key material (an unwired test double) yields
    ``None`` rather than a cert nobody could verify.
    """
    if space.authority_key_epoch < 1:
        return None
    if not own_instance_id or space.owner_instance_id != own_instance_id:
        return None
    if not isinstance(owner_seed, bytes) or not isinstance(owner_pk, bytes):
        return None
    if len(owner_seed) != 32 or len(owner_pk) != 32:
        return None
    return sign_authority_cert(
        space_id=space.id,
        owner_instance_id=own_instance_id,
        owner_seed=owner_seed,
        owner_pk_hex=owner_pk.hex(),
        authority_pk_hex=space.identity_public_key,
        key_epoch=space.authority_key_epoch,
    )


def owner_authority_cert_via(federation: object, space: "Space") -> dict | None:
    """:func:`owner_authority_cert` with the identity read off a
    :class:`FederationService` (``None`` when federation is not wired)."""
    if federation is None:
        return None
    return owner_authority_cert(
        space,
        own_instance_id=getattr(federation, "own_instance_id", None),
        owner_seed=getattr(federation, "own_identity_seed", None),
        owner_pk=getattr(federation, "own_identity_pk", None),
    )


class AuthorityCertOutcome(StrEnum):
    """What :func:`apply_authority_cert` did with a cert."""

    #: No cert was offered (an epoch-0 space, or an older sender).
    ABSENT = "absent"
    #: The pin moved to the cert's key at a higher epoch.
    APPLIED = "applied"
    #: Same epoch, same key — we already hold this pin.
    CURRENT = "current"
    #: Older epoch, or same epoch with another key — dropped.
    STALE = "stale"
    #: Failed verification or carries an unknown suite — dropped.
    REJECTED = "rejected"
    #: We host the space; our own pin is authoritative.
    OWN_SPACE = "own_space"

    @property
    def consistent(self) -> bool:
        """The cert names the key we now pin, at the epoch we now hold."""
        return self in (AuthorityCertOutcome.APPLIED, AuthorityCertOutcome.CURRENT)


async def _remember(
    space_repo: "AbstractSpaceRepo",
    space_id: str,
    verified: VerifiedAuthorityCert,
    cert: object,
) -> None:
    """Keep the cert of the key we now pin (v_46): the proof our authority
    epoch echo shows the owner. Skipped when it is already stored, so a
    cert repeated on every config / snapshot costs one read, not a write."""
    if not isinstance(cert, dict):
        return
    stored = await space_repo.get_authority_cert(space_id)
    if stored is not None and stored.get("key_epoch") == verified.key_epoch:
        return
    await space_repo.remember_authority_cert(
        space_id,
        key_epoch=verified.key_epoch,
        public_key_hex=verified.authority_pk_hex,
        cert=cert,
    )


async def apply_authority_cert(
    space_repo: "AbstractSpaceRepo",
    space: "Space",
    cert: object,
    *,
    own_instance_id: str,
    known_owner_pk_hex: str | None = None,
) -> AuthorityCertOutcome:
    """Verify ``cert`` for ``space`` and re-pin if it is newer.

    ``known_owner_pk_hex`` is the owner household's identity key when the
    caller holds one (e.g. the confirmed peer row); otherwise the host key
    recorded on the stub (``host_identity_pk``) is used when present. Never
    raises for a bad cert — the outcome says what happened.
    """
    if cert is None:
        return AuthorityCertOutcome.ABSENT
    if own_instance_id and space.owner_instance_id == own_instance_id:
        return AuthorityCertOutcome.OWN_SPACE
    if known_owner_pk_hex is None:
        known_owner_pk_hex = await space_repo.get_host_identity_pk(space.id)
    try:
        verified = verify_authority_cert(
            cert,
            space_id=space.id,
            owner_instance_id=space.owner_instance_id,
            known_owner_pk_hex=known_owner_pk_hex,
        )
    except UnsupportedAuthorityCertSuite as exc:
        log.warning(
            "authority cert for space %s carries an unknown suite (%s) — dropped",
            space.id,
            exc,
        )
        return AuthorityCertOutcome.REJECTED
    except InvalidAuthorityCert as exc:
        log.warning("authority cert for space %s refused: %s", space.id, exc)
        return AuthorityCertOutcome.REJECTED
    held_epoch = space.authority_key_epoch
    held_pk = (space.identity_public_key or "").lower()
    if verified.key_epoch < held_epoch or (
        verified.key_epoch == held_epoch and verified.authority_pk_hex != held_pk
    ):
        log.warning(
            "authority cert for space %s is stale or conflicting (cert epoch %d, "
            "held epoch %d) — keeping the current pin",
            space.id,
            verified.key_epoch,
            held_epoch,
        )
        return AuthorityCertOutcome.STALE
    if verified.key_epoch == held_epoch:
        await _remember(space_repo, space.id, verified, cert)
        return AuthorityCertOutcome.CURRENT
    if await space_repo.adopt_authority_key(
        space.id, verified.authority_pk_hex, verified.key_epoch
    ):
        log.info(
            "space %s: adopted the owner-certified authority key at epoch %d",
            space.id,
            verified.key_epoch,
        )
        await _remember(space_repo, space.id, verified, cert)
        return AuthorityCertOutcome.APPLIED
    # Lost a compare-and-set race against a concurrent apply: judge the
    # cert against what is stored now.
    fresh = await space_repo.get(space.id)
    if (
        fresh is not None
        and fresh.authority_key_epoch == verified.key_epoch
        and (fresh.identity_public_key or "").lower() == verified.authority_pk_hex
    ):
        return AuthorityCertOutcome.CURRENT
    return AuthorityCertOutcome.STALE
