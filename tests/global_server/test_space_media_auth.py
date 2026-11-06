"""Who may put a blob into the space media cache: a space-authority
signature, a trusted-mode writer cert (signed by the household key it names)
or a strict-mode writer group key — the checks ``/gfs/publish`` and the member
publishes already make, reused. Every refusal is a ``PermissionError``."""

from __future__ import annotations

import os
import time

import pytest

from socialhome.authority_sig import (
    AUTHORITY_EVENT_SPACE_MEDIA_DELETE,
    sign_authority_event,
)
from socialhome.crypto import b64url_encode, ed25519_public_key, sign_ed25519
from socialhome.domain.gfs_space_media import (
    AUTH_AUTHORITY,
    AUTH_WRITER_CERT,
    AUTH_WRITER_KEY,
    SpaceMediaClaim,
    SpaceMediaUploadAuth,
    space_media_delete_payload,
)
from socialhome.global_server.domain import ClientInstance, GlobalSpace
from socialhome.global_server.federation import GfsFederationService
from socialhome.global_server.member_publish import GfsMemberPublishService
from socialhome.global_server.repositories import (
    SqliteGfsFederationRepo,
    SqliteGfsSpaceEpochRepo,
)
from socialhome.global_server.space_media_auth import (
    SPACE_MEDIA_TS_SKEW_S,
    GfsSpaceMediaAuth,
)
from socialhome.writer_cert import sign_writer_cert
from socialhome.writer_key import (
    derive_writer_seed,
    sign_with_writer_key,
)

SPACE = "sp-1"
SEED = os.urandom(32)
PK = ed25519_public_key(SEED)
BID = "cd" * 32
NOW = 1_800_000_000


class _Clock:
    def __init__(self) -> None:
        self.now = float(NOW)

    def __call__(self) -> float:
        return self.now


@pytest.fixture
async def env(gfs_db):
    fed_repo = SqliteGfsFederationRepo(gfs_db)
    epoch_repo = SqliteGfsSpaceEpochRepo(gfs_db)
    await fed_repo.upsert_instance(
        ClientInstance(instance_id="owner", display_name="O", public_key="aa" * 32)
    )
    for sid, status in (
        (SPACE, "active"),
        ("sp-banned", "active"),
        ("sp-nokey", "active"),
    ):
        await fed_repo.upsert_space(
            GlobalSpace(
                space_id=sid,
                owning_instance="owner",
                status=status,
                identity_public_key="" if sid == "sp-nokey" else PK.hex(),
            )
        )
    await fed_repo.set_space_status("sp-banned", "banned")
    federation = GfsFederationService(fed_repo, epoch_repo=epoch_repo)
    clock = _Clock()
    member = GfsMemberPublishService(
        federation=federation,
        fed_repo=fed_repo,
        epoch_repo=epoch_repo,
        relay=None,  # type: ignore[arg-type]  # never reached by these checks
        gfs_instance_id="gfs-test",
        clock=clock,
    )
    auth = GfsSpaceMediaAuth(federation=federation, member_publish=member, clock=clock)
    return auth, epoch_repo, clock


def _claim(space=SPACE, ts=NOW, **over) -> SpaceMediaClaim:
    return SpaceMediaClaim(
        space_id=space,
        blob_id=over.get("blob_id", BID),
        size=over.get("size", 65_564),
        expires_at=over.get("expires_at", NOW + 3600),
        ts=ts,
    )


def _authority(claim: SpaceMediaClaim, seed: bytes = SEED) -> SpaceMediaUploadAuth:
    sig = sign_ed25519(seed, claim.authority_signing_bytes())
    return SpaceMediaUploadAuth(
        claim=claim, kind=AUTH_AUTHORITY, sig=b64url_encode(sig), sig_suite="ed25519"
    )


def _writer_cert(claim, *, epoch=3, holder=None, signer=None, cert_seed=SEED):
    holder = holder or os.urandom(32)
    cert = sign_writer_cert(
        space_seed=cert_seed,
        space_id=claim.space_id,
        epoch=epoch,
        instance_pk=ed25519_public_key(holder),
        scope="comment",
    )
    sig = sign_ed25519(
        signer or holder, claim.writer_signing_bytes(kind=AUTH_WRITER_CERT, epoch=epoch)
    )
    return SpaceMediaUploadAuth(
        claim=claim,
        kind=AUTH_WRITER_CERT,
        sig=b64url_encode(sig),
        sig_suite="ed25519",
        epoch=epoch,
        writer_cert=cert,
    )


async def _pin_writer_key(epoch_repo, epoch: int = 3) -> bytes:
    writer_seed = derive_writer_seed(SEED, SPACE, epoch)
    await epoch_repo.confirm(SPACE, epoch, now=NOW)
    await epoch_repo.set_publish_mode(SPACE, "strict", at=NOW)
    await epoch_repo.pin_writer_key(
        SPACE, epoch, b64url_encode(ed25519_public_key(writer_seed)), replace=True
    )
    return writer_seed


def _writer_key(claim, writer_seed: bytes, *, epoch=3, suite="ed25519"):
    sig = sign_with_writer_key(
        writer_seed, claim.writer_signing_bytes(kind=AUTH_WRITER_KEY, epoch=epoch)
    )
    return SpaceMediaUploadAuth(
        claim=claim, kind=AUTH_WRITER_KEY, sig=sig, sig_suite=suite, epoch=epoch
    )


# ── Space authority ──────────────────────────────────────────────────────


async def test_an_authority_signed_upload_is_authorized(env):
    auth, _, _ = env
    await auth.authorize_upload(_authority(_claim()), space_id=SPACE, blob_id=BID)


@pytest.mark.parametrize(
    "case",
    [
        "wrong_key",
        "unknown_space",
        "banned",
        "no_pinned_key",
        "path_space",
        "path_blob",
    ],
)
async def test_authority_refusals(env, case):
    auth, _, _ = env
    space, blob = SPACE, BID
    if case == "wrong_key":
        a = _authority(_claim(), seed=os.urandom(32))
    elif case == "unknown_space":
        a, space = _authority(_claim(space="sp-nope")), "sp-nope"
    elif case == "banned":
        a, space = _authority(_claim(space="sp-banned")), "sp-banned"
    elif case == "no_pinned_key":
        a, space = _authority(_claim(space="sp-nokey")), "sp-nokey"
    elif case == "path_space":
        a, space = _authority(_claim()), "sp-banned"
    else:
        a, blob = _authority(_claim()), "ef" * 32
    with pytest.raises(PermissionError):
        await auth.authorize_upload(a, space_id=space, blob_id=blob)


@pytest.mark.parametrize(
    "skew", [SPACE_MEDIA_TS_SKEW_S + 1, -SPACE_MEDIA_TS_SKEW_S - 1]
)
async def test_a_stale_or_future_timestamp_is_refused(env, skew):
    auth, _, _ = env
    a = _authority(_claim(ts=NOW + skew))
    with pytest.raises(PermissionError):
        await auth.authorize_upload(a, space_id=SPACE, blob_id=BID)


async def test_a_signature_for_another_claim_does_not_carry_over(env):
    """Any change to the claim (here the size) breaks the signature."""
    auth, _, _ = env
    a = _authority(_claim())
    forged = SpaceMediaUploadAuth(
        claim=_claim(size=131_100), kind=a.kind, sig=a.sig, sig_suite=a.sig_suite
    )
    with pytest.raises(PermissionError):
        await auth.authorize_upload(forged, space_id=SPACE, blob_id=BID)


async def test_an_unknown_authority_suite_is_refused(env):
    auth, _, _ = env
    a = _authority(_claim())
    odd = SpaceMediaUploadAuth(
        claim=a.claim, kind=a.kind, sig=a.sig, sig_suite="ed25519+mldsa65"
    )
    with pytest.raises(PermissionError):
        await auth.authorize_upload(odd, space_id=SPACE, blob_id=BID)


# ── Trusted-mode writer cert ─────────────────────────────────────────────


async def test_a_writer_cert_holder_is_authorized(env):
    auth, _, _ = env
    await auth.authorize_upload(_writer_cert(_claim()), space_id=SPACE, blob_id=BID)


@pytest.mark.parametrize("case", ["other_signer", "foreign_cert", "suite", "strict"])
async def test_writer_cert_refusals(env, case):
    auth, epoch_repo, _ = env
    if case == "other_signer":
        a = _writer_cert(_claim(), signer=os.urandom(32))
    elif case == "foreign_cert":
        a = _writer_cert(_claim(), cert_seed=os.urandom(32))
    elif case == "suite":
        good = _writer_cert(_claim())
        a = SpaceMediaUploadAuth(
            claim=good.claim,
            kind=good.kind,
            sig=good.sig,
            sig_suite="ed25519+mldsa65",
            epoch=good.epoch,
            writer_cert=good.writer_cert,
        )
    else:
        await _pin_writer_key(epoch_repo)
        a = _writer_cert(_claim())
    with pytest.raises(PermissionError):
        await auth.authorize_upload(a, space_id=SPACE, blob_id=BID)


async def test_a_writer_cert_of_a_closed_epoch_is_refused(env):
    auth, epoch_repo, _ = env
    await epoch_repo.confirm(SPACE, 9, now=NOW)
    with pytest.raises(PermissionError):
        await auth.authorize_upload(
            _writer_cert(_claim(), epoch=3), space_id=SPACE, blob_id=BID
        )


# ── Strict-mode writer key ───────────────────────────────────────────────


async def test_a_writer_key_holder_is_authorized(env):
    auth, epoch_repo, _ = env
    writer_seed = await _pin_writer_key(epoch_repo)
    await auth.authorize_upload(
        _writer_key(_claim(), writer_seed), space_id=SPACE, blob_id=BID
    )


@pytest.mark.parametrize("case", ["no_pin", "wrong_key", "wrong_epoch", "suite"])
async def test_writer_key_refusals(env, case):
    auth, epoch_repo, _ = env
    writer_seed = os.urandom(32)
    if case != "no_pin":
        writer_seed = await _pin_writer_key(epoch_repo)
    if case == "wrong_key":
        a = _writer_key(_claim(), os.urandom(32))
    elif case == "wrong_epoch":
        a = _writer_key(_claim(), writer_seed, epoch=4)
    elif case == "suite":
        a = _writer_key(_claim(), writer_seed, suite="ed25519+mldsa65")
    else:
        a = _writer_key(_claim(), writer_seed)
    with pytest.raises(PermissionError):
        await auth.authorize_upload(a, space_id=SPACE, blob_id=BID)


# ── Delete (space authority only) ────────────────────────────────────────


def _delete_sig(ts: int, seed: bytes = SEED, blob_id: str = BID) -> dict:
    return sign_authority_event(
        event_type=AUTHORITY_EVENT_SPACE_MEDIA_DELETE,
        space_id=SPACE,
        payload=space_media_delete_payload(SPACE, blob_id, ts),
        space_seed=seed,
    )


async def test_the_space_authority_may_delete(env):
    auth, _, _ = env
    sig = _delete_sig(NOW)
    await auth.authorize_delete(
        SPACE,
        BID,
        ts=NOW,
        authority_sig=sig["authority_sig"],
        authority_sig_suite=sig["authority_sig_suite"],
    )


@pytest.mark.parametrize("case", ["stale", "wrong_key", "other_blob", "bad_ts"])
async def test_delete_refusals(env, case):
    auth, _, _ = env
    ts: object = NOW
    if case == "stale":
        ts = NOW - SPACE_MEDIA_TS_SKEW_S - 1
        sig = _delete_sig(ts)
    elif case == "wrong_key":
        sig = _delete_sig(NOW, seed=os.urandom(32))
    elif case == "other_blob":
        sig = _delete_sig(NOW, blob_id="ef" * 32)
    else:
        ts, sig = "now", _delete_sig(NOW)
    with pytest.raises(PermissionError):
        await auth.authorize_delete(
            SPACE,
            BID,
            ts=ts,
            authority_sig=sig["authority_sig"],
            authority_sig_suite=sig["authority_sig_suite"],
        )


def test_the_real_clock_is_the_default(env):
    assert GfsSpaceMediaAuth.__init__.__kwdefaults__["clock"] is time.time
