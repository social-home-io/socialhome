"""Idempotent instance-identity bootstrap (§4.1, §5.2).

Run on every app startup before :mod:`socialhome.ha_bootstrap`. If the
``instance_identity`` row is missing, generate a fresh Ed25519 keypair,
KEK-encrypt the seed via the supplied :class:`KeyManager`, and insert
the row. Otherwise read and decrypt the existing values.

Post-quantum identity material (ML-DSA-65) is minted on demand. When
:func:`ensure_instance_identity` is called with ``sig_suite`` including
``mldsa65`` and the row lacks a ``pq_public_key`` value, a fresh ML-DSA
keypair is generated and persisted on the same row. Switching a
deployment from classical to hybrid therefore happens transparently on
next startup — no manual migration required.

Returned tuple is consumed by :func:`socialhome.app.create_app` to wire
the :class:`FederationService` with the real identity material.

Why this isn't part of ``ha_bootstrap``: ``ha_bootstrap`` is HA-specific
(it depends on ``SUPERVISOR_TOKEN``); identity provisioning must run in
standalone mode too.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from .crypto import (
    b64url_encode,
    derive_instance_id,
    derive_user_id,
    generate_identity_keypair,
    generate_routing_secret,
    generate_x25519_keypair,
    sign_ed25519,
)
from .db import AsyncDatabase
from .federation.crypto_suite import parse_suite
from .federation.pq_signer import PqSigner
from .infrastructure import KeyManager

log = logging.getLogger(__name__)


class IdentityMaterial:
    """Return value from :func:`ensure_instance_identity`.

    Bundles the classical Ed25519 seed + public key, optional PQ
    (ML-DSA-65) seed + public key, and the per-instance X25519 *key-wrap*
    keypair (Phase 5b foundation — used to seal a payload to another
    household's published key-wrap pubkey). Uses ``__slots__`` so it's
    cheap and readonly-in-spirit.
    """

    __slots__ = (
        "identity_seed",
        "identity_public_key",
        "instance_id",
        "pq_seed",
        "pq_public_key",
        "keywrap_private_key",
        "keywrap_public_key",
        "keywrap_sig",
    )

    def __init__(
        self,
        identity_seed: bytes,
        identity_public_key: bytes,
        instance_id: str,
        *,
        pq_seed: bytes | None = None,
        pq_public_key: bytes | None = None,
        keywrap_private_key: bytes,
        keywrap_public_key: bytes,
        keywrap_sig: str,
    ) -> None:
        self.identity_seed = identity_seed
        self.identity_public_key = identity_public_key
        self.instance_id = instance_id
        self.pq_seed = pq_seed
        self.pq_public_key = pq_public_key
        self.keywrap_private_key = keywrap_private_key
        self.keywrap_public_key = keywrap_public_key
        #: ``b64url(sign_ed25519(identity_seed, keywrap_public_key))`` — binds
        #: the key-wrap pubkey to this identity so a remote sealer can verify
        #: the GFS-served key-wrap key end-to-end (``verify_keywrap_binding``)
        #: and never trust a substituted value.
        self.keywrap_sig = keywrap_sig


async def ensure_instance_identity(
    db: AsyncDatabase,
    key_manager: KeyManager,
    *,
    display_name: str = "My Home",
    sig_suite: str = "ed25519",
) -> IdentityMaterial:
    """Return the full identity material for this instance.

    The classical keypair is always present. Post-quantum material is
    present iff ``sig_suite`` names an algorithm beyond ``ed25519``.

    On first start the row does not exist — classical + optional PQ
    keys are generated. On every subsequent start the existing row is
    decrypted and returned; if the row lacks PQ material but the
    requested suite needs it, the PQ half is minted in-place.
    """
    needs_pq = "mldsa65" in parse_suite(sig_suite)

    row = await db.fetchone(
        "SELECT identity_private_key, identity_public_key, instance_id, "
        "       pq_algorithm, pq_private_key, pq_public_key, "
        "       keywrap_private_key, keywrap_public_key, keywrap_sig "
        "FROM instance_identity WHERE id='self'",
    )
    if row is not None:
        seed = key_manager.decrypt(row["identity_private_key"])
        if len(seed) != 32:
            raise RuntimeError(
                f"instance_identity: decrypted seed must be 32 bytes, got {len(seed)}"
            )
        public_key_hex = row["identity_public_key"]
        public_key = bytes.fromhex(public_key_hex)
        if len(public_key) != 32:
            raise RuntimeError(
                f"instance_identity: public_key must be 32 bytes, got {len(public_key)}"
            )
        instance_id = row["instance_id"]

        pq_seed: bytes | None = None
        pq_pk: bytes | None = None
        existing_pq_pk_hex = row["pq_public_key"]
        if existing_pq_pk_hex:
            pq_seed = key_manager.decrypt(row["pq_private_key"])
            pq_pk = bytes.fromhex(existing_pq_pk_hex)
        elif needs_pq:
            # Upgrade path: row exists but no PQ yet. Mint + persist.
            pq_seed, pq_pk = PqSigner.generate_keypair()
            await db.enqueue(
                "UPDATE instance_identity "
                "   SET pq_algorithm=?, pq_private_key=?, pq_public_key=? "
                " WHERE id='self'",
                ("mldsa65", key_manager.encrypt(pq_seed), pq_pk.hex()),
            )
            log.info(
                "instance_identity: minted PQ keypair (mldsa65) for instance_id=%s",
                instance_id,
            )
        # Key-wrap keypair: present on a 5b+ row; lazily minted on a
        # pre-5b row (mirrors the PQ upgrade path above).
        kw_priv: bytes
        kw_pub: bytes
        kw_sig: str
        existing_kw_pub_hex = row["keywrap_public_key"]
        existing_kw_sig = row["keywrap_sig"]
        if existing_kw_pub_hex:
            kw_priv = key_manager.decrypt(row["keywrap_private_key"])
            kw_pub = bytes.fromhex(existing_kw_pub_hex)
            if existing_kw_sig:
                kw_sig = existing_kw_sig
            else:
                # Pre-safeguard row: key-wrap key present but unsigned. Sign it
                # in-place with the identity seed (binds it for remote sealers).
                kw_sig = await _mint_keywrap_sig(
                    db, seed=seed, keywrap_pub=kw_pub, instance_id=instance_id
                )
        else:
            kw_priv, kw_pub = await _mint_keywrap_keypair(
                db, key_manager, instance_id=instance_id, upgrade=True
            )
            kw_sig = await _mint_keywrap_sig(
                db, seed=seed, keywrap_pub=kw_pub, instance_id=instance_id
            )

        return IdentityMaterial(
            seed,
            public_key,
            instance_id,
            pq_seed=pq_seed,
            pq_public_key=pq_pk,
            keywrap_private_key=kw_priv,
            keywrap_public_key=kw_pub,
            keywrap_sig=kw_sig,
        )

    # First-start path: mint fresh classical + (optionally) PQ keypairs +
    # the per-instance X25519 key-wrap keypair.
    keypair = generate_identity_keypair()
    instance_id = derive_instance_id(keypair.public_key)
    encrypted_seed = key_manager.encrypt(keypair.private_key)
    routing_secret = generate_routing_secret()
    keywrap = generate_x25519_keypair()
    encrypted_keywrap_priv = key_manager.encrypt(keywrap.private_key)
    keywrap_sig = b64url_encode(sign_ed25519(keypair.private_key, keywrap.public_key))

    pq_algorithm: str | None = None
    pq_private_key_enc: str | None = None
    pq_public_key_hex: str | None = None
    pq_seed_bytes: bytes | None = None
    pq_pk_bytes: bytes | None = None
    if needs_pq:
        pq_seed_bytes, pq_pk_bytes = PqSigner.generate_keypair()
        pq_algorithm = "mldsa65"
        pq_private_key_enc = key_manager.encrypt(pq_seed_bytes)
        pq_public_key_hex = pq_pk_bytes.hex()

    await db.enqueue(
        """
        INSERT INTO instance_identity(
            id, instance_id, display_name,
            identity_private_key, identity_public_key,
            key_format,
            pq_algorithm, pq_private_key, pq_public_key,
            keywrap_private_key, keywrap_public_key, keywrap_sig,
            routing_secret, created_at
        ) VALUES('self', ?, ?, ?, ?, 'encrypted', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            instance_id,
            display_name,
            encrypted_seed,
            keypair.public_key.hex(),
            pq_algorithm,
            pq_private_key_enc,
            pq_public_key_hex,
            encrypted_keywrap_priv,
            keywrap.public_key.hex(),
            keywrap_sig,
            routing_secret,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    log.info(
        "instance_identity: generated new identity instance_id=%s display=%s "
        "sig_suite=%s pq=%s",
        instance_id,
        display_name,
        sig_suite,
        pq_algorithm or "none",
    )
    return IdentityMaterial(
        keypair.private_key,
        keypair.public_key,
        instance_id,
        pq_seed=pq_seed_bytes,
        pq_public_key=pq_pk_bytes,
        keywrap_private_key=keywrap.private_key,
        keywrap_public_key=keywrap.public_key,
        keywrap_sig=keywrap_sig,
    )


async def _mint_keywrap_sig(
    db: AsyncDatabase,
    *,
    seed: bytes,
    keywrap_pub: bytes,
    instance_id: str,
) -> str:
    """Sign + persist the key-wrap pubkey with the identity seed.

    Used on the upgrade paths for a row that has a key-wrap key but no
    signature (or whose key was just lazily minted). Returns the b64url
    signature so the caller can put it on the returned material.
    """
    sig = b64url_encode(sign_ed25519(seed, keywrap_pub))
    await db.enqueue(
        "UPDATE instance_identity SET keywrap_sig=? WHERE id='self'",
        (sig,),
    )
    log.info(
        "instance_identity: signed key-wrap pubkey for instance_id=%s",
        instance_id,
    )
    return sig


async def _mint_keywrap_keypair(
    db: AsyncDatabase,
    key_manager: KeyManager,
    *,
    instance_id: str,
    upgrade: bool,
) -> tuple[bytes, bytes]:
    """Mint + persist a fresh X25519 key-wrap keypair on the ``'self'`` row.

    Used both on the first-start path (via the INSERT above) and on the
    upgrade path for a pre-5b row whose key-wrap columns are NULL — the
    private half is KEK-wrapped, the public half stored as hex. Returns
    ``(private_key, public_key)`` raw bytes.
    """
    keywrap = generate_x25519_keypair()
    await db.enqueue(
        "UPDATE instance_identity "
        "   SET keywrap_private_key=?, keywrap_public_key=? "
        " WHERE id='self'",
        (key_manager.encrypt(keywrap.private_key), keywrap.public_key.hex()),
    )
    if upgrade:
        log.info(
            "instance_identity: minted X25519 key-wrap keypair for instance_id=%s",
            instance_id,
        )
    return keywrap.private_key, keywrap.public_key


async def derive_local_user_id(db: AsyncDatabase, username: str) -> str:
    """Return the cryptographic ``user_id`` for a *username-anchored* local user.

    The ONE place the first-boot admin paths mint a ``user_id``. Every local
    user id is a :func:`~socialhome.crypto.derive_user_id` of this household's
    Ed25519 identity public key plus a derivation input, because that is
    exactly what a remote household re-computes to self-certify an author
    (``derive_user_id(author_pk, anchor) == author_user_id`` — see
    :mod:`socialhome.services.space_public_author`). An id that is not derived
    can never verify, so its author's content is dropped by every subscriber.

    The derivation input here is the **username** (not a uuid4
    ``identity_anchor`` like :meth:`UserService.provision`): the admin-mirror
    paths are re-run on every boot / wizard replay and must stay deterministic,
    and it matches both the migration-0041 backfill and the HAOS owner. Callers
    therefore also store ``identity_anchor = username`` so the row keeps the
    ``user_id == derive_user_id(pk, identity_anchor)`` invariant.

    Raises :class:`RuntimeError` when the instance identity has not been
    bootstrapped yet — deriving from a missing key is not something to paper
    over. :func:`ensure_instance_identity` runs in ``_on_startup`` before the
    platform adapter's own ``on_startup`` and before any request is served, so
    every caller is safely downstream of it.
    """
    row = await db.fetchone(
        "SELECT identity_public_key FROM instance_identity WHERE id='self'",
    )
    if row is None or not row["identity_public_key"]:
        raise RuntimeError(
            "derive_local_user_id: instance_identity not initialised — "
            "ensure_instance_identity() must run before any user is minted"
        )
    return derive_user_id(bytes.fromhex(row["identity_public_key"]), username)
