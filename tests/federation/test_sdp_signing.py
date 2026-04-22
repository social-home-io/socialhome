"""Tests for socialhome.federation.sdp_signing."""

from socialhome.crypto import generate_identity_keypair
from socialhome.federation.sdp_signing import (
    SignedSDP,
    sign_rtc_offer,
    signed_sdp_from_dict,
    signed_sdp_to_dict,
    verify_rtc_offer,
)


def test_sign_and_verify_offer():
    """Signing then verifying an SDP offer succeeds."""
    kp = generate_identity_keypair()
    signed = sign_rtc_offer("v=0\r\no=- ...", "offer", identity_seed=kp.private_key)
    assert signed.sdp_type == "offer"
    assert verify_rtc_offer(signed, remote_public_key=kp.public_key)


def test_sign_and_verify_answer():
    """Signing then verifying an SDP answer succeeds."""
    kp = generate_identity_keypair()
    signed = sign_rtc_offer("v=0\r\nanswer...", "answer", identity_seed=kp.private_key)
    assert verify_rtc_offer(signed, remote_public_key=kp.public_key)


def test_tampered_sdp_rejected():
    """Modified SDP fails verification."""
    kp = generate_identity_keypair()
    signed = sign_rtc_offer("original", "offer", identity_seed=kp.private_key)
    tampered = SignedSDP(sdp="tampered", sdp_type="offer", signature=signed.signature)
    assert not verify_rtc_offer(tampered, remote_public_key=kp.public_key)


def test_wrong_key_rejected():
    """SDP signed by one key fails verification with another."""
    a = generate_identity_keypair()
    b = generate_identity_keypair()
    signed = sign_rtc_offer("sdp", "offer", identity_seed=a.private_key)
    assert not verify_rtc_offer(signed, remote_public_key=b.public_key)


def test_signed_sdp_dict_roundtrip():
    """to_dict/from_dict preserves all fields."""
    kp = generate_identity_keypair()
    signed = sign_rtc_offer("sdp-data", "offer", identity_seed=kp.private_key)
    d = signed_sdp_to_dict(signed)
    restored = signed_sdp_from_dict(d)
    assert restored.sdp == signed.sdp
    assert restored.sdp_type == signed.sdp_type
    assert restored.signature == signed.signature
    assert verify_rtc_offer(restored, remote_public_key=kp.public_key)
