"""Tests for socialhome.media_signer."""

from __future__ import annotations

import pytest

from socialhome.media_signer import (
    DEFAULT_TTL_SECONDS,
    MediaUrlSigner,
    derive_signing_key,
    sign_media_urls_in,
    strip_signature_query,
)


@pytest.fixture
def signer() -> MediaUrlSigner:
    return MediaUrlSigner(key=b"\xab" * 32)


def test_sign_round_trip(signer: MediaUrlSigner) -> None:
    """sign(...) → verify(path, exp, sig) returns True for fresh URLs."""
    url = signer.sign("/api/media/abc.webp", now=1_000_000)
    # url shape: /api/media/abc.webp?exp=1003600&sig=<urlsafe-b64>
    assert "exp=" in url
    assert "sig=" in url
    exp = url.split("exp=")[1].split("&")[0]
    sig = url.split("sig=")[1]
    assert signer.verify("/api/media/abc.webp", exp, sig, now=1_000_000)


def test_sign_default_ttl_is_one_hour(signer: MediaUrlSigner) -> None:
    """Default TTL is 3600 s; expiry = now + 3600."""
    assert DEFAULT_TTL_SECONDS == 3600
    url = signer.sign("/api/media/x.webp", now=1_000_000)
    exp = int(url.split("exp=")[1].split("&")[0])
    assert exp == 1_000_000 + 3600


def test_verify_expired_signature_fails(signer: MediaUrlSigner) -> None:
    """A URL whose exp is in the past returns False."""
    url = signer.sign("/api/media/abc.webp", now=1_000_000)
    exp = url.split("exp=")[1].split("&")[0]
    sig = url.split("sig=")[1]
    # Time has advanced past expiry.
    assert not signer.verify("/api/media/abc.webp", exp, sig, now=1_010_000)


def test_verify_expiry_boundary_inclusive(signer: MediaUrlSigner) -> None:
    """A URL is still valid at exactly the expiry instant."""
    url = signer.sign("/api/media/abc.webp", ttl=10, now=1_000_000)
    exp = url.split("exp=")[1].split("&")[0]
    sig = url.split("sig=")[1]
    # ``now == exp`` should still verify (>= boundary).
    assert signer.verify("/api/media/abc.webp", exp, sig, now=1_000_010)
    # One second later: expired.
    assert not signer.verify("/api/media/abc.webp", exp, sig, now=1_000_011)


def test_verify_tampered_sig_fails(signer: MediaUrlSigner) -> None:
    """Flipping any character in sig breaks verification."""
    url = signer.sign("/api/media/abc.webp", now=1_000_000)
    exp = url.split("exp=")[1].split("&")[0]
    sig = url.split("sig=")[1]
    tampered = "A" + sig[1:] if sig[0] != "A" else "B" + sig[1:]
    assert not signer.verify("/api/media/abc.webp", exp, tampered, now=1_000_000)


def test_verify_path_mismatch_fails(signer: MediaUrlSigner) -> None:
    """Sig for one filename can't be reused on another."""
    url = signer.sign("/api/media/abc.webp", now=1_000_000)
    exp = url.split("exp=")[1].split("&")[0]
    sig = url.split("sig=")[1]
    assert not signer.verify("/api/media/other.webp", exp, sig, now=1_000_000)


def test_verify_missing_args_fails(signer: MediaUrlSigner) -> None:
    """Empty exp / sig return False without raising."""
    assert not signer.verify("/api/media/x.webp", "", "anything", now=1)
    assert not signer.verify("/api/media/x.webp", "1", "", now=1)


def test_verify_non_numeric_exp_fails(signer: MediaUrlSigner) -> None:
    """exp must be an integer string; non-numeric returns False (not 500)."""
    assert not signer.verify("/api/media/x.webp", "not-a-number", "abc", now=1)


def test_sign_preserves_existing_query_string(signer: MediaUrlSigner) -> None:
    """``/api/users/{id}/picture?v=<hash>`` keeps the cache buster."""
    url = signer.sign("/api/users/u1/picture?v=abc123", now=1_000_000)
    assert "v=abc123" in url
    assert "exp=" in url
    assert "sig=" in url
    # Sig should validate against the canonical path only (without ?v=...)
    exp = url.split("exp=")[1].split("&")[0]
    sig = url.split("sig=")[1]
    assert signer.verify("/api/users/u1/picture", exp, sig, now=1_000_000)


def test_signing_key_too_short_rejected() -> None:
    """Keys < 16 bytes are rejected at construction time."""
    with pytest.raises(ValueError, match="at least 16 bytes"):
        MediaUrlSigner(key=b"short")


def test_derive_signing_key_deterministic() -> None:
    """derive_signing_key(seed) is stable for the same seed."""
    seed = b"\xff" * 32
    a = derive_signing_key(seed)
    b = derive_signing_key(seed)
    assert a == b
    assert len(a) == 32


def test_derive_signing_key_changes_with_seed() -> None:
    """Different seeds produce different keys (sanity)."""
    a = derive_signing_key(b"\x00" * 32)
    b = derive_signing_key(b"\x01" * 32)
    assert a != b


def test_derive_signing_key_rejects_empty_seed() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        derive_signing_key(b"")


# ── sign_media_urls_in ───────────────────────────────────────────────────


def test_sign_media_urls_in_signs_media_url(signer: MediaUrlSigner) -> None:
    payload = {"id": "p1", "media_url": "/api/media/abc.webp"}
    sign_media_urls_in(payload, signer)
    assert payload["media_url"].startswith("/api/media/abc.webp?exp=")
    assert "sig=" in payload["media_url"]


def test_sign_media_urls_in_signs_picture_url(signer: MediaUrlSigner) -> None:
    payload = {"user_id": "u1", "picture_url": "/api/users/u1/picture?v=hash"}
    sign_media_urls_in(payload, signer)
    # Cache-buster ?v=… is preserved; ?exp=&sig= appended.
    assert "v=hash" in payload["picture_url"]
    assert "exp=" in payload["picture_url"]
    assert "sig=" in payload["picture_url"]


def test_sign_media_urls_in_skips_none(signer: MediaUrlSigner) -> None:
    payload = {"media_url": None, "picture_url": None}
    sign_media_urls_in(payload, signer)
    assert payload == {"media_url": None, "picture_url": None}


def test_sign_media_urls_in_skips_absolute_urls(signer: MediaUrlSigner) -> None:
    """External URLs (e.g. HA-served avatars) aren't ours to sign."""
    payload = {"picture_url": "https://example.com/avatar.png"}
    sign_media_urls_in(payload, signer)
    assert payload["picture_url"] == "https://example.com/avatar.png"


def test_sign_media_urls_in_recurses_into_lists_and_dicts(
    signer: MediaUrlSigner,
) -> None:
    payload = {
        "items": [
            {"media_url": "/api/media/a.webp"},
            {"media_url": "/api/media/b.webp"},
        ],
        "nested": {"author": {"picture_url": "/api/users/u1/picture"}},
    }
    sign_media_urls_in(payload, signer)
    assert "exp=" in payload["items"][0]["media_url"]
    assert "exp=" in payload["items"][1]["media_url"]
    assert "exp=" in payload["nested"]["author"]["picture_url"]


def test_sign_media_urls_in_leaves_unrelated_fields_alone(
    signer: MediaUrlSigner,
) -> None:
    """``url`` / ``thumbnail_url`` are out of scope for this PR."""
    payload = {
        "url": "/api/media/g.webp",
        "thumbnail_url": "/api/media/g-thumb.webp",
    }
    sign_media_urls_in(payload, signer)
    assert payload["url"] == "/api/media/g.webp"
    assert payload["thumbnail_url"] == "/api/media/g-thumb.webp"


def test_sign_media_urls_in_signs_image_urls_list(
    signer: MediaUrlSigner,
) -> None:
    """Bazaar listings expose ``image_urls`` as a list of strings."""
    payload = {
        "id": "l1",
        "image_urls": ["/api/media/a.webp", "/api/media/b.webp"],
    }
    sign_media_urls_in(payload, signer)
    assert payload["image_urls"][0].startswith("/api/media/a.webp?exp=")
    assert "sig=" in payload["image_urls"][0]
    assert payload["image_urls"][1].startswith("/api/media/b.webp?exp=")


def test_sign_media_urls_in_image_urls_preserves_external(
    signer: MediaUrlSigner,
) -> None:
    """Absolute URLs in the list pass through untouched."""
    payload = {"image_urls": ["https://cdn.example.com/x.png"]}
    sign_media_urls_in(payload, signer)
    assert payload["image_urls"] == ["https://cdn.example.com/x.png"]


# ── strip_signature_query ────────────────────────────────────────────────


def test_strip_signature_query_removes_query() -> None:
    assert (
        strip_signature_query("/api/media/x.webp?exp=1&sig=abc")
        == "/api/media/x.webp"
    )


def test_strip_signature_query_passes_through_canonical() -> None:
    assert strip_signature_query("/api/media/x.webp") == "/api/media/x.webp"


def test_strip_signature_query_handles_none_and_non_str() -> None:
    assert strip_signature_query(None) is None
    assert strip_signature_query(123) == 123  # type: ignore[arg-type]
