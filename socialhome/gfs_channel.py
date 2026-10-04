"""Opaque GFS channels — key derivation, signing and verification (v_51).

The wire shapes live in :mod:`socialhome.domain.gfs_channel`. Like
:mod:`socialhome.writer_key` this module depends only on
:mod:`socialhome.crypto` (and ``cryptography``'s HKDF), so the content-blind
GFS process imports it.

**Keys.** Both derived from the space AUTHORITY seed, so every seed holder
derives the same ones and nobody stores them::

    channel_seed = HKDF-SHA256(ikm=space_seed,
                               salt=b"socialhome-gfs-channel-key:v1",
                               info=b"<space_id>:<channel_id>", length=32)
    channel_writer_seed = HKDF-SHA256(ikm=space_seed,
                               salt=b"socialhome-gfs-channel-writer-key:v1",
                               info=b"<space_id>:<channel_id>:<epoch>", length=32)

The salts differ from the writer group key's (``socialhome-writer-key:v1``)
and from each other, so neither key equals or relates to the space key, the
space's writer group keys (which a connection server sees for a PUBLIC space)
or each other. HKDF is one-way: a connection server holding ``channel_pk`` —
even one that also learned the space public key some other way (a space that
was public before, an invite link's page for a listed space) — cannot link
the two without the seed. An authority-key rotation (v_44) changes the seed,
so the household starts a FRESH channel rather than re-pinning (a revoked
seed holder still holds the old channel key and could race any re-pin).

**Signing bytes.** ``prefix + canonical_json(statement minus its signature)``
with one prefix per statement, so no signature can be lifted onto another:

=================================  ===========================================
``gfs-channel-register:v1:``       registration (by the key being pinned)
``gfs-channel-epoch:v1:``          epoch notice
``gfs-channel-unregister:v1:``     unregister
``gfs-channel-cert:v1:``           :class:`ChannelCert`
``gfs-channel-pass:v1:``           :class:`ChannelPass`
``gfs-channel-writer-key-cert:v1:`` :class:`ChannelWriterKeyCert`
``gfs-channel-publish-anon:v1:``   strict publish (by the channel writer key)
``gfs-channel-binding:v1:``        grant binding (by the SPACE authority key)
=================================  ===========================================
"""

from __future__ import annotations

import secrets
import time
from dataclasses import replace

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .crypto import (
    b64url_decode,
    b64url_encode,
    ed25519_public_key,
    sign_ed25519,
    verify_ed25519,
)
from .domain.gfs_channel import (
    CHANNEL_BINDING_SUITE_ED25519,
    CHANNEL_SUITE_ED25519,
    SUPPORTED_CHANNEL_BINDING_SUITES,
    SUPPORTED_CHANNEL_SUITES,
    ChannelCert,
    ChannelEpochNotice,
    ChannelPass,
    ChannelPublishAnonRequest,
    ChannelRegisterRequest,
    ChannelUnregisterRequest,
    ChannelWriterKeyCert,
    ChannelWriterKeyGrant,
    GfsChannelGrant,
    UnsupportedChannelSuite,
    canonical,
)
from .domain.writer_cert import MAX_WRITER_CERT_EPOCH, WRITER_SCOPES, scope_permits
from .domain.writer_key import (
    SUPPORTED_WRITER_KEY_SUITES,
    WRITER_KEY_SUITE_ED25519,
    UnsupportedWriterKeySuite,
)

_KEY_SALT: bytes = b"socialhome-gfs-channel-key:v1"
_WRITER_SALT: bytes = b"socialhome-gfs-channel-writer-key:v1"
_OFFSET_SALT: bytes = b"socialhome-gfs-channel-epoch-offset:v1"

REGISTER_PREFIX: bytes = b"gfs-channel-register:v1:"
NOTICE_PREFIX: bytes = b"gfs-channel-epoch:v1:"
UNREGISTER_PREFIX: bytes = b"gfs-channel-unregister:v1:"
CERT_PREFIX: bytes = b"gfs-channel-cert:v1:"
PASS_PREFIX: bytes = b"gfs-channel-pass:v1:"
WRITER_KEY_CERT_PREFIX: bytes = b"gfs-channel-writer-key-cert:v1:"
ANON_PREFIX: bytes = b"gfs-channel-publish-anon:v1:"
BINDING_PREFIX: bytes = b"gfs-channel-binding:v1:"


class InvalidChannelSignature(ValueError):
    """A channel statement is bound to another channel / epoch / household,
    lacks the scope, or its signature does not verify."""


def new_channel_id() -> str:
    """A fresh channel id: 128 random bits as 32 lowercase hex chars."""
    return secrets.token_hex(16)


def _check_seed(space_seed: bytes) -> None:
    if len(space_seed) != 32:
        raise ValueError("space seed must be 32 bytes")


def _check_epoch(epoch: int) -> int:
    if isinstance(epoch, bool) or not 0 <= int(epoch) <= MAX_WRITER_CERT_EPOCH:
        raise ValueError("epoch out of range")
    return int(epoch)


def derive_channel_seed(space_seed: bytes, space_id: str, channel_id: str) -> bytes:
    """The 32-byte Ed25519 seed of the channel key (module docstring)."""
    _check_seed(space_seed)
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_KEY_SALT,
        info=f"{space_id}:{channel_id}".encode("utf-8"),
    ).derive(space_seed)


def derive_channel_writer_seed(
    space_seed: bytes, space_id: str, channel_id: str, epoch: int
) -> bytes:
    """The 32-byte Ed25519 seed of the channel writer key at ``epoch``."""
    _check_seed(space_seed)
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_WRITER_SALT,
        info=f"{space_id}:{channel_id}:{_check_epoch(epoch)}".encode("utf-8"),
    ).derive(space_seed)


def derive_channel_epoch_offset(
    space_seed: bytes, space_id: str, channel_id: str
) -> int:
    """The channel's secret epoch offset (40 bits): wire epoch = content
    epoch + offset, so a server cannot match channel epochs to the space's
    content epochs it may have seen elsewhere. Deterministic for every seed
    holder."""
    _check_seed(space_seed)
    raw = HKDF(
        algorithm=hashes.SHA256(),
        length=5,
        salt=_OFFSET_SALT,
        info=f"{space_id}:{channel_id}".encode("utf-8"),
    ).derive(space_seed)
    return int.from_bytes(raw, "big")


def channel_pk_of(channel_seed: bytes) -> str:
    """The b64url public half of a channel (or channel writer) seed."""
    return b64url_encode(ed25519_public_key(channel_seed))


def signing_bytes(prefix: bytes, body: dict) -> bytes:
    """``prefix + canonical_json(body)``."""
    return prefix + canonical(body)


def _sign(seed: bytes, prefix: bytes, body: dict) -> str:
    return b64url_encode(sign_ed25519(seed, signing_bytes(prefix, body)))


def _check_suite(suite: str) -> None:
    if suite not in SUPPORTED_CHANNEL_SUITES:
        raise UnsupportedChannelSuite(suite)


def _pk_bytes(pk_b64: str) -> bytes:
    try:
        raw = b64url_decode(pk_b64)
    except Exception as exc:
        raise InvalidChannelSignature("key is not base64url") from exc
    if len(raw) != 32:
        raise InvalidChannelSignature("key is not 32 bytes")
    return raw


def verify_signature(
    *, prefix: bytes, body: dict, signature: str, public_key: bytes, suite: str
) -> None:
    """Verify one channel-key signature. Raises
    :class:`UnsupportedChannelSuite` / :class:`InvalidChannelSignature`."""
    _check_suite(suite)
    if len(public_key) != 32:
        raise InvalidChannelSignature("public key is not 32 bytes")
    try:
        sig = b64url_decode(signature)
    except Exception as exc:
        raise InvalidChannelSignature("signature is not base64url") from exc
    if not verify_ed25519(public_key, signing_bytes(prefix, body), sig):
        raise InvalidChannelSignature("signature does not verify")


# ─── Cert / pass / writer key ────────────────────────────────────────────


def issue_channel_cert(
    *,
    channel_seed: bytes,
    channel_id: str,
    epoch: int,
    instance_pk: bytes,
    scope: str,
    issued_at: int | None = None,
) -> ChannelCert:
    if scope not in WRITER_SCOPES:
        raise ValueError(f"unknown scope {scope!r}")
    if len(instance_pk) != 32:
        raise ValueError("instance_pk must be 32 bytes")
    unsigned = ChannelCert(
        channel_suite=CHANNEL_SUITE_ED25519,
        channel_id=channel_id,
        epoch=_check_epoch(epoch),
        instance_pk=b64url_encode(instance_pk),
        scope=scope,
        issued_at=int(time.time()) if issued_at is None else int(issued_at),
        cert_sig="",
    )
    return replace(
        unsigned, cert_sig=_sign(channel_seed, CERT_PREFIX, unsigned.signing_body())
    )


def verify_channel_cert(
    cert: ChannelCert,
    *,
    channel_pk: bytes,
    channel_id: str,
    epoch: int,
    author_pk: bytes,
    required_scope: str,
) -> None:
    """``cert`` authorizes ``author_pk`` at ``required_scope`` in
    ``channel_id`` during ``epoch``, under the pinned ``channel_pk``."""
    verify_signature(
        prefix=CERT_PREFIX,
        body=cert.signing_body(),
        signature=cert.cert_sig,
        public_key=channel_pk,
        suite=cert.channel_suite,
    )
    if cert.channel_id != channel_id:
        raise InvalidChannelSignature("cert is for another channel")
    if cert.epoch != epoch:
        raise InvalidChannelSignature("cert is for another epoch")
    if _pk_bytes(cert.instance_pk) != author_pk:
        raise InvalidChannelSignature("cert names another household")
    if required_scope not in WRITER_SCOPES or not scope_permits(
        cert.scope, required_scope
    ):
        raise InvalidChannelSignature(
            f"scope {cert.scope!r} does not permit {required_scope!r}"
        )


def issue_channel_pass(
    *,
    channel_seed: bytes,
    channel_id: str,
    epoch: int,
    instance_pk: bytes,
    issued_at: int | None = None,
) -> ChannelPass:
    if len(instance_pk) != 32:
        raise ValueError("instance_pk must be 32 bytes")
    unsigned = ChannelPass(
        channel_suite=CHANNEL_SUITE_ED25519,
        channel_id=channel_id,
        epoch=_check_epoch(epoch),
        instance_pk=b64url_encode(instance_pk),
        issued_at=int(time.time()) if issued_at is None else int(issued_at),
        pass_sig="",
    )
    return replace(
        unsigned, pass_sig=_sign(channel_seed, PASS_PREFIX, unsigned.signing_body())
    )


def verify_channel_pass(
    channel_pass: ChannelPass,
    *,
    channel_pk: bytes,
    channel_id: str,
    instance_pk: bytes,
) -> None:
    """``channel_pass`` names ``instance_pk`` in ``channel_id`` under the
    pinned ``channel_pk`` (the epoch is the caller's freshness check)."""
    verify_signature(
        prefix=PASS_PREFIX,
        body=channel_pass.signing_body(),
        signature=channel_pass.pass_sig,
        public_key=channel_pk,
        suite=channel_pass.channel_suite,
    )
    if channel_pass.channel_id != channel_id:
        raise InvalidChannelSignature("pass is for another channel")
    if _pk_bytes(channel_pass.instance_pk) != instance_pk:
        raise InvalidChannelSignature("pass names another household")


def issue_channel_writer_key(
    *,
    space_seed: bytes,
    space_id: str,
    channel_id: str,
    epoch: int,
) -> ChannelWriterKeyGrant:
    """Derive the channel writer key at ``epoch`` and pin it with the channel
    key — the grant a publish-capable member household is handed."""
    channel_seed = derive_channel_seed(space_seed, space_id, channel_id)
    writer_seed = derive_channel_writer_seed(space_seed, space_id, channel_id, epoch)
    unsigned = ChannelWriterKeyCert(
        writer_key_suite=WRITER_KEY_SUITE_ED25519,
        channel_suite=CHANNEL_SUITE_ED25519,
        channel_id=channel_id,
        epoch=_check_epoch(epoch),
        writer_pk=channel_pk_of(writer_seed),
        cert_sig="",
    )
    cert = replace(
        unsigned,
        cert_sig=_sign(channel_seed, WRITER_KEY_CERT_PREFIX, unsigned.signing_body()),
    )
    return ChannelWriterKeyGrant(
        writer_key_suite=WRITER_KEY_SUITE_ED25519,
        channel_id=channel_id,
        epoch=cert.epoch,
        writer_seed=b64url_encode(writer_seed),
        writer_key_cert=cert,
    )


def verify_channel_writer_key_cert(
    cert: ChannelWriterKeyCert, *, channel_pk: bytes, channel_id: str, epoch: int
) -> bytes:
    """Verify the pin (its ``channel_suite`` names the signature's suite,
    ``writer_key_suite`` the pinned key's); returns the 32-byte writer
    public key."""
    if cert.writer_key_suite not in SUPPORTED_WRITER_KEY_SUITES:
        raise UnsupportedWriterKeySuite(cert.writer_key_suite)
    verify_signature(
        prefix=WRITER_KEY_CERT_PREFIX,
        body=cert.signing_body(),
        signature=cert.cert_sig,
        public_key=channel_pk,
        suite=cert.channel_suite,
    )
    if cert.channel_id != channel_id:
        raise InvalidChannelSignature("writer key cert is for another channel")
    if cert.epoch != epoch:
        raise InvalidChannelSignature("writer key cert is for another epoch")
    return _pk_bytes(cert.writer_pk)


def verify_channel_writer_key_grant(
    grant: ChannelWriterKeyGrant, *, channel_pk: bytes, channel_id: str, epoch: int
) -> bytes:
    """Verify a delivered writer key: its pin under ``channel_pk`` and the
    seed's public half equal to the pinned key. Returns the 32-byte seed."""
    if grant.writer_key_suite not in SUPPORTED_WRITER_KEY_SUITES:
        raise UnsupportedWriterKeySuite(grant.writer_key_suite)
    if grant.channel_id != channel_id or grant.epoch != epoch:
        raise InvalidChannelSignature("writer key is for another channel / epoch")
    writer_pk = verify_channel_writer_key_cert(
        grant.writer_key_cert, channel_pk=channel_pk, channel_id=channel_id, epoch=epoch
    )
    try:
        seed = b64url_decode(grant.writer_seed)
    except Exception as exc:
        raise InvalidChannelSignature("writer seed is not base64url") from exc
    if len(seed) != 32 or ed25519_public_key(seed) != writer_pk:
        raise InvalidChannelSignature("writer seed does not match its pin")
    return seed


# ─── Requests ────────────────────────────────────────────────────────────


def sign_register(
    *,
    channel_seed: bytes,
    channel_id: str,
    gfs_instance_id: str,
    ts: str,
) -> ChannelRegisterRequest:
    unsigned = ChannelRegisterRequest(
        channel_suite=CHANNEL_SUITE_ED25519,
        channel_id=channel_id,
        channel_pk=channel_pk_of(channel_seed),
        gfs_instance_id=gfs_instance_id,
        ts=ts,
        nonce=b64url_encode(secrets.token_bytes(16)),
        channel_sig="",
    )
    return replace(
        unsigned,
        channel_sig=_sign(channel_seed, REGISTER_PREFIX, unsigned.signing_body()),
    )


def verify_register(req: ChannelRegisterRequest) -> bytes:
    """Proof of possession: ``channel_sig`` verifies under the very
    ``channel_pk`` being registered. Returns that key's 32 bytes."""
    pk = _pk_bytes(req.channel_pk)
    verify_signature(
        prefix=REGISTER_PREFIX,
        body=req.signing_body(),
        signature=req.channel_sig,
        public_key=pk,
        suite=req.channel_suite,
    )
    return pk


def sign_notice(
    *,
    channel_seed: bytes,
    channel_id: str,
    gfs_instance_id: str,
    ts: str,
    epoch: int,
    publish_mode: str,
    writer_key_cert: ChannelWriterKeyCert | None = None,
) -> ChannelEpochNotice:
    unsigned = ChannelEpochNotice(
        channel_suite=CHANNEL_SUITE_ED25519,
        channel_id=channel_id,
        gfs_instance_id=gfs_instance_id,
        ts=ts,
        nonce=b64url_encode(secrets.token_bytes(16)),
        epoch=_check_epoch(epoch),
        publish_mode=publish_mode,
        channel_sig="",
        writer_key_cert=writer_key_cert,
    )
    return replace(
        unsigned,
        channel_sig=_sign(channel_seed, NOTICE_PREFIX, unsigned.signing_body()),
    )


def verify_notice(notice: ChannelEpochNotice, *, channel_pk: bytes) -> None:
    verify_signature(
        prefix=NOTICE_PREFIX,
        body=notice.signing_body(),
        signature=notice.channel_sig,
        public_key=channel_pk,
        suite=notice.channel_suite,
    )


def sign_unregister(
    *, channel_seed: bytes, channel_id: str, gfs_instance_id: str, ts: str
) -> ChannelUnregisterRequest:
    unsigned = ChannelUnregisterRequest(
        channel_suite=CHANNEL_SUITE_ED25519,
        channel_id=channel_id,
        gfs_instance_id=gfs_instance_id,
        ts=ts,
        nonce=b64url_encode(secrets.token_bytes(16)),
        channel_sig="",
    )
    return replace(
        unsigned,
        channel_sig=_sign(channel_seed, UNREGISTER_PREFIX, unsigned.signing_body()),
    )


def verify_unregister(req: ChannelUnregisterRequest, *, channel_pk: bytes) -> None:
    verify_signature(
        prefix=UNREGISTER_PREFIX,
        body=req.signing_body(),
        signature=req.channel_sig,
        public_key=channel_pk,
        suite=req.channel_suite,
    )


def sign_publish_anon(
    req: ChannelPublishAnonRequest, *, writer_seed: bytes
) -> ChannelPublishAnonRequest:
    """``req`` with ``writer_sig`` made by the channel writer key."""
    return replace(req, writer_sig=_sign(writer_seed, ANON_PREFIX, req.signing_body()))


def verify_publish_anon(req: ChannelPublishAnonRequest, *, writer_pk: bytes) -> None:
    if req.writer_sig_suite not in SUPPORTED_WRITER_KEY_SUITES:
        raise UnsupportedWriterKeySuite(req.writer_sig_suite)
    verify_signature(
        prefix=ANON_PREFIX,
        body=req.signing_body(),
        signature=req.writer_sig,
        public_key=writer_pk,
        suite=CHANNEL_SUITE_ED25519,
    )


# ─── Grant binding (space authority key) ─────────────────────────────────


def bind_grant(grant: GfsChannelGrant, *, space_seed: bytes) -> GfsChannelGrant:
    """``grant`` with the space authority's ``binding_sig``."""
    unsigned = replace(
        grant, binding_sig_suite=CHANNEL_BINDING_SUITE_ED25519, binding_sig=""
    )
    return replace(
        unsigned,
        binding_sig=_sign(space_seed, BINDING_PREFIX, unsigned.binding_body()),
    )


def verify_grant_binding(grant: GfsChannelGrant, *, space_pubkey: bytes) -> None:
    """The grant's binding verifies under the pinned SPACE key."""
    if grant.binding_sig_suite not in SUPPORTED_CHANNEL_BINDING_SUITES:
        raise UnsupportedChannelSuite(grant.binding_sig_suite)
    if len(space_pubkey) != 32:
        raise InvalidChannelSignature("space public key is not 32 bytes")
    try:
        sig = b64url_decode(grant.binding_sig)
    except Exception as exc:
        raise InvalidChannelSignature("binding is not base64url") from exc
    if not verify_ed25519(
        space_pubkey, signing_bytes(BINDING_PREFIX, grant.binding_body()), sig
    ):
        raise InvalidChannelSignature("binding does not verify against the space key")


def verify_grant(
    grant: GfsChannelGrant,
    *,
    space_pubkey: bytes,
    space_id: str,
    own_pk: bytes,
) -> bytes | None:
    """Everything a member household checks on a delivered grant: the space
    binding, a supported channel suite, the pass (if any) and cert naming
    ``own_pk`` for this channel and epoch under ``channel_pk``, and a writer
    key that matches its pin. Returns the writer seed (or ``None``)."""
    _check_suite(grant.channel_suite)
    if grant.space_id != space_id:
        raise InvalidChannelSignature("grant is for another space")
    verify_grant_binding(grant, space_pubkey=space_pubkey)
    channel_pk = _pk_bytes(grant.channel_pk)
    wire_epoch = grant.channel_epoch
    if (
        grant.channel_pass is None
        and grant.channel_cert is None
        and (grant.writer_key is None)
    ):
        raise InvalidChannelSignature("grant grants nothing")
    if grant.channel_pass is not None:
        if grant.channel_pass.epoch != wire_epoch:
            raise InvalidChannelSignature("pass is for another epoch")
        verify_channel_pass(
            grant.channel_pass,
            channel_pk=channel_pk,
            channel_id=grant.channel_id,
            instance_pk=own_pk,
        )
    if grant.channel_cert is not None:
        verify_channel_cert(
            grant.channel_cert,
            channel_pk=channel_pk,
            channel_id=grant.channel_id,
            epoch=wire_epoch,
            author_pk=own_pk,
            required_scope=grant.channel_cert.scope,
        )
    if grant.writer_key is None:
        return None
    return verify_channel_writer_key_grant(
        grant.writer_key,
        channel_pk=channel_pk,
        channel_id=grant.channel_id,
        epoch=wire_epoch,
    )


def channel_pk_bytes(pk_b64: str) -> bytes:
    """The 32 bytes of a b64url channel / writer public key, else
    :class:`InvalidChannelSignature`."""
    return _pk_bytes(pk_b64)
