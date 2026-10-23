"""GFS proof of possession — pure, low-level, dependency-light.

A household pins a GFS's ``gfs_instance_id`` + Ed25519 ``public_key`` from
``GET /gfs/info``. That endpoint is unauthenticated and its signed capability
block (:mod:`socialhome.capabilities_sig`) covers only the id + capabilities
— a static statement anyone can copy. So nothing in ``/gfs/info`` proves the
server answering it HOLDS the private key: a host serving a real GFS's
descriptor at another address could be paired and treated as that GFS.

The proof is a challenge/response over ``POST /gfs/proof``:

* the household sends a fresh random 32-byte ``nonce`` (b64url) and nothing
  else — no identity, so the GFS learns no more than a ``/gfs/info`` fetch
  tells it;
* the GFS signs ``b"gfs-proof:v1:" + canonical({base_url, gfs_key, nonce,
  suite})`` with its identity key and returns the signature + its
  ``proof_sig_suite``. The KEY, not the instance id, is what is proven: it is
  the cluster-wide identity, while the id may differ per node;
* the household verifies against the key it is about to pin (pairing) or
  already pinned (reconnect / re-bind), with the URL **it** used.

``base_url`` is the GFS's OWN configured public URL (``[server] base_url``),
never one the requester supplies. That is what defeats a relaying impostor: a
host at another address that forwards the challenge to the real GFS gets back
a signature over the REAL GFS's URL, which fails at the impostor's address.
A household-supplied "audience" would not — the relay would simply pass its
own address through. Both sides compare the URL normalized
(:func:`normalize_gfs_base_url`: case, default port, trailing slash), so a
cosmetic difference never fails a legitimate pairing.

No timestamp is signed: the household's fresh nonce already makes every proof
single-use, and a clock check would only add a way for a skewed GFS clock to
break pairing.

This module sits at the package top level beside :mod:`.capabilities_sig`
and depends ONLY on :mod:`socialhome.crypto`, so the content-blind GFS process
(signer) and the household (verifier) share it without importing each
other's stack.
"""

from __future__ import annotations

import binascii
import ipaddress
import secrets
from urllib.parse import urlsplit

from .canonical_json import canonical_str
from .crypto import (
    b64url_decode,
    b64url_encode,
    ed25519_public_key,
    sign_ed25519,
    verify_ed25519,
)

#: Suite identifier for the proof signature, shipped on the wire as
#: ``proof_sig_suite`` and bound into the signed bytes (crypto-suite rule).
#: Today Ed25519; Phase-2 of ``docs/crypto.md`` adds ``"ed25519+mldsa65"``.
PROOF_SIG_SUITE_ED25519: str = "ed25519"
SUPPORTED_PROOF_SIG_SUITES: frozenset[str] = frozenset({PROOF_SIG_SUITE_ED25519})


class UnsupportedProofSigSuite(ValueError):
    """Raised when a proof names a suite this build doesn't know. Receivers
    MUST reject rather than fall back to a default."""


#: Versioned domain-separation prefix. Not JSON, so a proof can never be
#: confused with a cluster frame (canonical JSON) or a capability block
#: (``gfs-capabilities:v1:``) signed by the same identity key.
PROOF_SIGNING_PREFIX: bytes = b"gfs-proof:v1:"

#: Size of the household's challenge nonce.
PROOF_NONCE_BYTES: int = 32

#: Capability flag a GFS advertises inside its SIGNED ``/gfs/info`` block.
PROOF_CAPABILITY: str = "proof_of_possession"

#: The GFS route that answers the challenge.
PROOF_ROUTE: str = "/gfs/proof"

#: An upper bound on the wire nonce before decoding (32 bytes → 43 chars).
_MAX_NONCE_CHARS = 64

_DEFAULT_PORTS = {"http": 80, "https": 443}


def new_proof_nonce() -> str:
    """A fresh b64url-encoded :data:`PROOF_NONCE_BYTES`-byte challenge."""
    return b64url_encode(secrets.token_bytes(PROOF_NONCE_BYTES))


def parse_proof_nonce(value: object) -> str | None:
    """*value* as a canonical b64url nonce of exactly 32 bytes, else ``None``.

    Re-encoding the decoded bytes makes the signed string canonical, so a
    padded or otherwise re-spelled nonce can't produce a second valid proof
    for the same challenge.
    """
    if not isinstance(value, str) or not value or len(value) > _MAX_NONCE_CHARS:
        return None
    try:
        raw = b64url_decode(value)
    except binascii.Error, ValueError:
        return None
    if len(raw) != PROOF_NONCE_BYTES:
        return None
    return b64url_encode(raw)


def normalize_gfs_base_url(url: object) -> str:
    """``scheme://host[:port][/path]`` lower-cased, default port and trailing
    slash dropped; ``""`` for anything that is not a plain http(s) base URL.

    Only spelling is normalized — a different scheme, host, port or path is a
    different address and stays different.
    """
    if not isinstance(url, str) or not url:
        return ""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return ""
    scheme = parts.scheme.lower()
    host = _ascii_host(parts.hostname or "")
    if scheme not in _DEFAULT_PORTS or not host or parts.query or parts.fragment:
        return ""
    if parts.username is not None or parts.password is not None:
        return ""
    if ":" in host:
        host = f"[{host}]"
    netloc = host if port in (None, _DEFAULT_PORTS[scheme]) else f"{host}:{port}"
    return f"{scheme}://{netloc}{parts.path.rstrip('/')}"


def _ascii_host(host: str) -> str:
    """*host* lower-cased in its ASCII (IDNA / punycode) form, or ``""``.

    So ``bücher.example`` and ``xn--bcher-kva.example`` — the same DNS name
    — compare equal whichever spelling the operator configured and the
    household typed. An IP literal passes through; a name IDNA refuses (or
    one with characters no hostname has) is ``""``.
    """
    host = host.strip().lower()
    if not host:
        return ""
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    try:
        ascii_host = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return ""
    if not all(ch.isalnum() or ch in "-." for ch in ascii_host):
        return ""
    return ascii_host


def bindable_base_url(url: object) -> str:
    """The normalized URL a GFS may bind its proofs to, or ``""``.

    ``""`` for anything :func:`normalize_gfs_base_url` rejects and for an
    unspecified host (``0.0.0.0`` / ``::``): the config fallback infers
    ``http://{host}:{port}`` from the bind address, and a wildcard bind
    address is nobody's address — a proof bound to it would fail for every
    household, so such a GFS simply does not offer the proof.
    """
    normalized = normalize_gfs_base_url(url)
    if not normalized:
        return ""
    host = (urlsplit(normalized).hostname or "").strip("[]")
    try:
        if ipaddress.ip_address(host).is_unspecified:
            return ""
    except ValueError:
        pass
    return normalized


def proof_signing_bytes(
    gfs_key_hex: str,
    nonce: str,
    base_url: str,
    suite: str,
) -> bytes:
    """Canonical, domain-separated bytes for one proof.

    Binds the server's identity KEY (lowercase hex), the household's nonce,
    the server's normalized base URL and the suite (so a signature can't be
    re-labelled under another suite). Deliberately NOT the instance id: the
    key is the cluster-wide identity, while the id may still differ per node
    of a load-balanced cluster (or move to a shared id, signed as
    ``replaces``) — binding it would fail whenever ``/gfs/info`` and
    ``/gfs/proof`` land on different nodes.
    """
    body = canonical_str(
        {
            "base_url": normalize_gfs_base_url(base_url),
            "gfs_key": gfs_key_hex.strip().lower(),
            "nonce": nonce,
            "suite": suite,
        }
    )
    return PROOF_SIGNING_PREFIX + body.encode("utf-8")


def sign_possession_proof(
    seed: bytes,
    nonce: str,
    base_url: str,
) -> tuple[str, str]:
    """Sign a proof with the GFS identity seed → ``(proof_sig, proof_sig_suite)``."""
    suite = PROOF_SIG_SUITE_ED25519
    key_hex = ed25519_public_key(seed).hex()
    sig = sign_ed25519(seed, proof_signing_bytes(key_hex, nonce, base_url, suite))
    return b64url_encode(sig), suite


def verify_possession_proof(
    public_key_hex: str,
    nonce: str,
    base_url: str,
    sig_b64url: str,
    suite: str,
) -> bool:
    """Verify a proof against a GFS key, with the URL the HOUSEHOLD used.

    Raises :class:`UnsupportedProofSigSuite` for an unknown suite (no default
    fallback). Returns ``False`` — never raises — for malformed key or
    signature material or a failed verification: every input here is
    remote-controlled.
    """
    if suite not in SUPPORTED_PROOF_SIG_SUITES:
        raise UnsupportedProofSigSuite(suite)
    try:
        public_key = bytes.fromhex(public_key_hex)
        sig = b64url_decode(sig_b64url)
    except binascii.Error, ValueError, TypeError:
        return False
    return verify_ed25519(
        public_key,
        proof_signing_bytes(public_key_hex, nonce, base_url, suite),
        sig,
    )
