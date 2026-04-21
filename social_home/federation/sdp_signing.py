"""SDP signature primitive — proves an offered/answered SDP came from
the claimed instance identity (§25.6 S-14).

Both household calls (§26) and the WebRTC federation transport
(§24.12.5) sign the SDP they offer or answer with the instance's
Ed25519 identity key — the same key that signs federation envelopes.
The verifier on the other side checks the signature with the peer's
public key from ``remote_instances.remote_identity_pk``, which thwarts
an on-path attacker swapping the SDP for one that points at their
own DTLS endpoint.

Lives in :mod:`federation` rather than its own ``transport`` package
because the threat it defends against is a federation-layer one and
the keys are federation-managed.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..crypto import b64url_decode, b64url_encode, sign_ed25519, verify_ed25519


@dataclass(slots=True, frozen=True)
class SignedSDP:
    """An SDP offer or answer plus an Ed25519 signature.

    The signature covers ``sdp_type + ":" + sdp`` as UTF-8 bytes so
    verifiers can confirm the SDP has not been tampered with in transit.
    """

    sdp: str
    sdp_type: str  # "offer" | "answer"
    signature: str  # b64url Ed25519


def sign_rtc_offer(
    sdp: str,
    sdp_type: str,
    *,
    identity_seed: bytes,
) -> SignedSDP:
    """Sign an SDP offer or answer with the instance's Ed25519 key.

    Returns a :class:`SignedSDP` that can be serialised into a federation
    event payload.
    """
    payload = f"{sdp_type}:{sdp}".encode("utf-8")
    sig = sign_ed25519(identity_seed, payload)
    return SignedSDP(
        sdp=sdp,
        sdp_type=sdp_type,
        signature=b64url_encode(sig),
    )


def verify_rtc_offer(
    signed: SignedSDP,
    *,
    remote_public_key: bytes,
) -> bool:
    """Verify that a received SDP was signed by the claimed remote instance.

    Returns ``True`` iff the signature is valid. Does NOT check the SDP
    content for semantic validity — that is left to the WebRTC stack.
    """
    payload = f"{signed.sdp_type}:{signed.sdp}".encode("utf-8")
    return verify_ed25519(remote_public_key, payload, b64url_decode(signed.signature))


def signed_sdp_to_dict(signed: SignedSDP) -> dict:
    """Serialise for inclusion in a federation event payload."""
    return {
        "sdp": signed.sdp,
        "sdp_type": signed.sdp_type,
        "signature": signed.signature,
    }


def signed_sdp_from_dict(d: dict) -> SignedSDP:
    """Deserialise from a federation event payload."""
    return SignedSDP(
        sdp=d["sdp"],
        sdp_type=d["sdp_type"],
        signature=d["signature"],
    )
