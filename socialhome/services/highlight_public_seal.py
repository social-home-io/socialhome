"""End-to-end sealing of the public highlight stream (§highlights_public).

A public highlight link is ``https://{gfs}/highlight/{instance}/{highlight}/
{token}#k={link_secret}``. The GFS minted the token, so it could open the
path like any viewer — but the ``#k=`` fragment is never sent to a server
(browsers strip it from requests and from ``Referer``), and every byte of
the highlight is sealed to a key that needs it:

* The author's household derives the link secret from its own identity
  seed, the highlight id and the token
  (:func:`link_secret`), so nothing new is stored and only it can mint one.
  The GFS forwards the token with the offer / relay request; it cannot
  derive the secret.
* The viewer's browser makes an ephemeral P-256 key pair per view and sends
  the public half as ``vk`` (see :mod:`.highlight_public_profile`).
* The household answers with one plaintext ``stream_seal`` frame
  (``seal_suite`` + its own ephemeral ``epk``), then sends every other
  frame as a ``sealed`` frame: the whole inner frame encrypted with
  AES-256-GCM under ``HKDF-SHA256(ikm=ECDH(epk, vk), salt=link_secret,
  info=STREAM_SEAL_INFO)``, a 96-bit big-endian frame counter as the nonce
  and the highlight id as associated data.

Both halves are needed: without the link secret (the GFS, which only ever
sees the path) nothing opens; without the viewer's private key (a passive
observer who has the link) the frames of someone else's view don't open
either. Content-free ``error`` frames before the seal stay plaintext.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from ..crypto import b64url_decode, b64url_encode
from . import highlight_public_framing as framing

#: ECDH P-256 + the link secret → HKDF-SHA256 → AES-256-GCM.
STREAM_SEAL_SUITE_P256_LINK: str = "p256+link-hkdf-sha256-aes256gcm"
SUPPORTED_STREAM_SEAL_SUITES: frozenset[str] = frozenset({STREAM_SEAL_SUITE_P256_LINK})

#: HKDF ``info`` for the per-view stream key.
STREAM_SEAL_INFO: bytes = b"sh-highlight-public-stream:v1"

#: HKDF ``info`` prefix for the per-link secret.
LINK_SECRET_INFO: bytes = b"sh-highlight-link:v1"

#: Fragment parameter the link secret rides in.
LINK_FRAGMENT_PARAM: str = "k"

_NONCE_LEN = 12


class UnsupportedStreamSealSuite(ValueError):
    """A ``stream_seal`` frame names a suite we don't support."""


def link_secret(identity_seed: bytes, *, highlight_id: str, token: str) -> bytes:
    """The 32-byte secret for one share link. Deterministic, so the
    household stores nothing new; only the holder of the identity seed can
    compute it."""
    info = b"\x00".join((LINK_SECRET_INFO, highlight_id.encode(), token.encode()))
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"", info=info).derive(
        identity_seed
    )


def link_with_secret(url: str, secret: bytes) -> str:
    """The share URL with the secret in its fragment (replacing any)."""
    base = url.split("#", 1)[0]
    return f"{base}#{LINK_FRAGMENT_PARAM}={b64url_encode(secret)}"


def _stream_key(
    private: ec.EllipticCurvePrivateKey,
    peer: ec.EllipticCurvePublicKey,
    secret: bytes,
) -> bytes:
    shared = private.exchange(ec.ECDH(), peer)
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=secret, info=STREAM_SEAL_INFO
    ).derive(shared)


def _nonce(counter: int) -> bytes:
    return counter.to_bytes(_NONCE_LEN, "big")


class StreamSealer:
    """Seals one view's frames, in order."""

    __slots__ = ("_aad", "_aead", "_counter", "_epk")

    def __init__(
        self,
        viewer_key: ec.EllipticCurvePublicKey,
        *,
        secret: bytes,
        highlight_id: str,
    ) -> None:
        ephemeral = ec.generate_private_key(ec.SECP256R1())
        self._aead = AESGCM(_stream_key(ephemeral, viewer_key, secret))
        self._epk = ephemeral.public_key().public_bytes(
            Encoding.X962, PublicFormat.UncompressedPoint
        )
        self._aad = highlight_id.encode()
        self._counter = 0

    def opening_frame(self) -> bytes:
        """The plaintext ``stream_seal`` frame that starts the stream."""
        return framing.stream_seal(
            {"seal_suite": STREAM_SEAL_SUITE_P256_LINK, "epk": b64url_encode(self._epk)}
        )

    def seal(self, frame: bytes) -> bytes:
        """One encoded inner frame → one ``sealed`` frame."""
        ciphertext = self._aead.encrypt(_nonce(self._counter), frame, self._aad)
        self._counter += 1
        return framing.sealed(ciphertext)

    async def wrap(self, frames: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        yield self.opening_frame()
        async for frame in frames:
            yield self.seal(frame)


class StreamOpener:
    """Reference opener (the viewer bundle mirrors it in WebCrypto)."""

    __slots__ = ("_aad", "_aead", "_counter")

    def __init__(
        self,
        viewer_private: ec.EllipticCurvePrivateKey,
        header: dict,
        *,
        secret: bytes,
        highlight_id: str,
    ) -> None:
        if header.get("seal_suite") not in SUPPORTED_STREAM_SEAL_SUITES:
            raise UnsupportedStreamSealSuite(str(header.get("seal_suite")))
        epk = ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), b64url_decode(str(header.get("epk") or ""))
        )
        self._aead = AESGCM(_stream_key(viewer_private, epk, secret))
        self._aad = highlight_id.encode()
        self._counter = 0

    def open(self, ciphertext: bytes) -> bytes:
        """Raises ``InvalidTag`` for a tampered, reordered or foreign frame."""
        plain = self._aead.decrypt(_nonce(self._counter), ciphertext, self._aad)
        self._counter += 1
        return plain
