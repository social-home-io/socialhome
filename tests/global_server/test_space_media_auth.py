"""Who may put a blob into the space media cache — per scope, the checks the
GFS already makes for that scope, reused. Every refusal is a
``PermissionError``.

* space: a space-authority signature, a trusted-mode writer cert (signed by
  the household key it names) or a strict-mode writer group key;
* channel (a private space's opaque v_51 channel): the channel key, a
  channel cert (signed by the household key it names) or the channel writer
  key."""

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
    AUTH_CHANNEL,
    AUTH_CHANNEL_CERT,
    AUTH_WRITER_CERT,
    AUTH_WRITER_KEY,
    CHANNEL_MEDIA_DELETE_PREFIX,
    SCOPE_CHANNEL,
    SCOPE_SPACE,
    SpaceMediaClaim,
    SpaceMediaUploadAuth,
    media_delete_payload,
)
from socialhome.gfs_channel import (
    channel_pk_of,
    issue_channel_cert,
    signing_bytes,
)
from socialhome.global_server.channels import GfsChannelService
from socialhome.global_server.domain import ClientInstance, GlobalSpace
from socialhome.global_server.federation import GfsFederationService
from socialhome.global_server.member_publish import GfsMemberPublishService
from socialhome.global_server.repositories import (
    SqliteGfsChannelRepo,
    SqliteGfsFederationRepo,
    SqliteGfsSpaceEpochRepo,
)
from socialhome.global_server.space_media_auth import (
    SPACE_MEDIA_TS_SKEW_S,
    GfsSpaceMediaAuth,
    is_space_media_write_path,
)
from socialhome.writer_cert import sign_writer_cert
from socialhome.writer_key import derive_writer_seed, sign_with_writer_key

SPACE = "sp-1"
SEED = os.urandom(32)
PK = ed25519_public_key(SEED)
CHANNEL = "0f" * 16
CHANNEL_SEED = os.urandom(32)
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
    channel_repo = SqliteGfsChannelRepo(gfs_db)
    await fed_repo.upsert_instance(
        ClientInstance(instance_id="owner", display_name="O", public_key="aa" * 32)
    )
    for sid in (SPACE, "sp-banned", "sp-nokey"):
        await fed_repo.upsert_space(
            GlobalSpace(
                space_id=sid,
                owning_instance="owner",
                status="active",
                identity_public_key="" if sid == "sp-nokey" else PK.hex(),
            )
        )
    await fed_repo.set_space_status("sp-banned", "banned")
    await channel_repo.register(
        CHANNEL,
        channel_suite="ed25519",
        channel_pk=channel_pk_of(CHANNEL_SEED),
        now=NOW,
    )
    await channel_repo.set_epoch(CHANNEL, 3, expected=None, now=NOW)
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
    channels = GfsChannelService(
        federation=federation,
        channel_repo=channel_repo,
        member_publish=member,
        gfs_instance_id="gfs-test",
        clock=clock,
    )
    auth = GfsSpaceMediaAuth(
        federation=federation, member_publish=member, channels=channels, clock=clock
    )
    return auth, epoch_repo, channel_repo


def _claim(scope_id=SPACE, ts=NOW, scope=SCOPE_SPACE, **over) -> SpaceMediaClaim:
    return SpaceMediaClaim(
        scope=scope,
        scope_id=scope_id,
        blob_id=over.get("blob_id", BID),
        size=over.get("size", 65_564),
        expires_at=over.get("expires_at", NOW + 3600),
        ts=ts,
    )


def _ch_claim(**over) -> SpaceMediaClaim:
    return _claim(scope_id=CHANNEL, scope=SCOPE_CHANNEL, **over)


async def _upload(auth, a: SpaceMediaUploadAuth) -> None:
    await auth.authorize_upload(
        a, scope_kind=a.claim.scope, scope_id=a.claim.scope_id, blob_id=a.claim.blob_id
    )


def _authority(claim: SpaceMediaClaim, seed: bytes = SEED) -> SpaceMediaUploadAuth:
    sig = sign_ed25519(seed, claim.authority_signing_bytes())
    return SpaceMediaUploadAuth(
        claim=claim, kind=AUTH_AUTHORITY, sig=b64url_encode(sig), sig_suite="ed25519"
    )


def _channel_key(
    claim: SpaceMediaClaim, seed: bytes = CHANNEL_SEED, suite: str = "ed25519"
) -> SpaceMediaUploadAuth:
    sig = sign_ed25519(seed, claim.channel_signing_bytes())
    return SpaceMediaUploadAuth(
        claim=claim, kind=AUTH_CHANNEL, sig=b64url_encode(sig), sig_suite=suite
    )


def _writer_cert(claim, *, epoch=3, holder=None, signer=None, cert_seed=SEED):
    holder = holder or os.urandom(32)
    cert = sign_writer_cert(
        space_seed=cert_seed,
        space_id=claim.scope_id,
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


def _channel_cert(claim, *, epoch=3, signer=None, cert_seed=CHANNEL_SEED):
    holder = os.urandom(32)
    cert = issue_channel_cert(
        channel_seed=cert_seed,
        channel_id=claim.scope_id,
        epoch=epoch,
        instance_pk=ed25519_public_key(holder),
        scope="comment",
    )
    sig = sign_ed25519(
        signer or holder,
        claim.writer_signing_bytes(kind=AUTH_CHANNEL_CERT, epoch=epoch),
    )
    return SpaceMediaUploadAuth(
        claim=claim,
        kind=AUTH_CHANNEL_CERT,
        sig=b64url_encode(sig),
        sig_suite="ed25519",
        epoch=epoch,
        channel_cert=cert,
    )


async def _pin_writer_key(epoch_repo, epoch: int = 3) -> bytes:
    writer_seed = derive_writer_seed(SEED, SPACE, epoch)
    await epoch_repo.confirm(SPACE, epoch, now=NOW)
    await epoch_repo.set_publish_mode(SPACE, "strict", at=NOW)
    await epoch_repo.pin_writer_key(
        SPACE, epoch, b64url_encode(ed25519_public_key(writer_seed)), replace=True
    )
    return writer_seed


async def _pin_channel_writer_key(channel_repo, epoch: int = 3) -> bytes:
    writer_seed = os.urandom(32)
    await channel_repo.set_publish_mode(CHANNEL, "strict", at=NOW)
    await channel_repo.pin_writer_key(CHANNEL, epoch, channel_pk_of(writer_seed))
    return writer_seed


def _writer_key(claim, writer_seed: bytes, *, epoch=3, suite="ed25519"):
    sig = sign_with_writer_key(
        writer_seed, claim.writer_signing_bytes(kind=AUTH_WRITER_KEY, epoch=epoch)
    )
    return SpaceMediaUploadAuth(
        claim=claim, kind=AUTH_WRITER_KEY, sig=sig, sig_suite=suite, epoch=epoch
    )


# ── Space scope: authority ───────────────────────────────────────────────


async def test_an_authority_signed_upload_is_authorized(env):
    auth, _, _ = env
    await _upload(auth, _authority(_claim()))


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
    scope_id, blob = SPACE, BID
    if case == "wrong_key":
        a = _authority(_claim(), seed=os.urandom(32))
    elif case == "unknown_space":
        a, scope_id = _authority(_claim("sp-nope")), "sp-nope"
    elif case == "banned":
        a, scope_id = _authority(_claim("sp-banned")), "sp-banned"
    elif case == "no_pinned_key":
        a, scope_id = _authority(_claim("sp-nokey")), "sp-nokey"
    elif case == "path_space":
        a, scope_id = _authority(_claim()), "sp-banned"
    else:
        a, blob = _authority(_claim()), "ef" * 32
    with pytest.raises(PermissionError):
        await auth.authorize_upload(
            a, scope_kind=SCOPE_SPACE, scope_id=scope_id, blob_id=blob
        )


@pytest.mark.parametrize(
    "skew", [SPACE_MEDIA_TS_SKEW_S + 1, -SPACE_MEDIA_TS_SKEW_S - 1]
)
async def test_a_stale_or_future_timestamp_is_refused(env, skew):
    auth, _, _ = env
    with pytest.raises(PermissionError):
        await _upload(auth, _authority(_claim(ts=NOW + skew)))
    with pytest.raises(PermissionError):
        await _upload(auth, _channel_key(_ch_claim(ts=NOW + skew)))


async def test_a_signature_for_another_claim_does_not_carry_over(env):
    auth, _, _ = env
    a = _authority(_claim())
    forged = SpaceMediaUploadAuth(
        claim=_claim(size=131_100), kind=a.kind, sig=a.sig, sig_suite=a.sig_suite
    )
    with pytest.raises(PermissionError):
        await _upload(auth, forged)


async def test_an_unknown_authority_suite_is_refused(env):
    auth, _, _ = env
    a = _authority(_claim())
    odd = SpaceMediaUploadAuth(
        claim=a.claim, kind=a.kind, sig=a.sig, sig_suite="ed25519+mldsa65"
    )
    with pytest.raises(PermissionError):
        await _upload(auth, odd)


async def test_a_kind_from_the_other_scope_is_refused(env):
    """A channel-key upload sent to a space path (or the reverse) never
    authorizes: each scope takes only its own kinds."""
    auth, _, _ = env
    a = _channel_key(_ch_claim())
    with pytest.raises(PermissionError):
        await auth.authorize_upload(
            a, scope_kind=SCOPE_SPACE, scope_id=CHANNEL, blob_id=BID
        )


# ── Space scope: trusted-mode writer cert ────────────────────────────────


async def test_a_writer_cert_holder_is_authorized(env):
    auth, _, _ = env
    await _upload(auth, _writer_cert(_claim()))


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
        await _upload(auth, a)


async def test_a_writer_cert_of_a_closed_epoch_is_refused(env):
    auth, epoch_repo, _ = env
    await epoch_repo.confirm(SPACE, 9, now=NOW)
    with pytest.raises(PermissionError):
        await _upload(auth, _writer_cert(_claim(), epoch=3))


# ── Space scope: strict-mode writer key ──────────────────────────────────


async def test_a_writer_key_holder_is_authorized(env):
    auth, epoch_repo, _ = env
    writer_seed = await _pin_writer_key(epoch_repo)
    await _upload(auth, _writer_key(_claim(), writer_seed))


@pytest.mark.parametrize(
    "case", ["no_pin", "wrong_key", "wrong_epoch", "suite", "banned"]
)
async def test_writer_key_refusals(env, case):
    auth, epoch_repo, _ = env
    writer_seed = os.urandom(32)
    if case != "no_pin":
        writer_seed = await _pin_writer_key(epoch_repo)
    claim = _claim()
    if case == "wrong_key":
        a = _writer_key(claim, os.urandom(32))
    elif case == "wrong_epoch":
        a = _writer_key(claim, writer_seed, epoch=4)
    elif case == "suite":
        a = _writer_key(claim, writer_seed, suite="ed25519+mldsa65")
    elif case == "banned":
        a = _writer_key(_claim("sp-banned"), writer_seed)
    else:
        a = _writer_key(claim, writer_seed)
    with pytest.raises(PermissionError):
        await _upload(auth, a)


# ── Channel scope: channel key ───────────────────────────────────────────


async def test_a_channel_key_signed_upload_is_authorized(env):
    auth, _, _ = env
    await _upload(auth, _channel_key(_ch_claim()))


@pytest.mark.parametrize("case", ["wrong_key", "unknown_channel", "suite"])
async def test_channel_key_refusals(env, case):
    auth, _, _ = env
    if case == "wrong_key":
        a = _channel_key(_ch_claim(), seed=os.urandom(32))
    elif case == "unknown_channel":
        a = _channel_key(_claim(scope_id="1e" * 16, scope=SCOPE_CHANNEL))
    else:
        a = _channel_key(_ch_claim(), suite="ed25519+mldsa65")
    with pytest.raises(PermissionError):
        await _upload(auth, a)


# ── Channel scope: channel cert ──────────────────────────────────────────


async def test_a_channel_cert_holder_is_authorized(env):
    auth, _, _ = env
    await _upload(auth, _channel_cert(_ch_claim()))


@pytest.mark.parametrize(
    "case", ["other_signer", "foreign_cert", "closed_epoch", "strict"]
)
async def test_channel_cert_refusals(env, case):
    auth, _, channel_repo = env
    if case == "other_signer":
        a = _channel_cert(_ch_claim(), signer=os.urandom(32))
    elif case == "foreign_cert":
        a = _channel_cert(_ch_claim(), cert_seed=os.urandom(32))
    elif case == "closed_epoch":
        a = _channel_cert(_ch_claim(), epoch=9)
    else:
        await _pin_channel_writer_key(channel_repo)
        a = _channel_cert(_ch_claim())
    with pytest.raises(PermissionError):
        await _upload(auth, a)


# ── Channel scope: channel writer key ────────────────────────────────────


async def test_a_channel_writer_key_holder_is_authorized(env):
    auth, _, channel_repo = env
    writer_seed = await _pin_channel_writer_key(channel_repo)
    await _upload(auth, _writer_key(_ch_claim(), writer_seed))


@pytest.mark.parametrize("case", ["no_pin", "wrong_key", "wrong_epoch", "suite"])
async def test_channel_writer_key_refusals(env, case):
    auth, _, channel_repo = env
    writer_seed = os.urandom(32)
    if case != "no_pin":
        writer_seed = await _pin_channel_writer_key(channel_repo)
    if case == "wrong_key":
        a = _writer_key(_ch_claim(), os.urandom(32))
    elif case == "wrong_epoch":
        a = _writer_key(_ch_claim(), writer_seed, epoch=4)
    elif case == "suite":
        a = _writer_key(_ch_claim(), writer_seed, suite="ed25519+mldsa65")
    else:
        a = _writer_key(_ch_claim(), writer_seed)
    with pytest.raises(PermissionError):
        await _upload(auth, a)


# ── Delete (the scope's seed holder only) ────────────────────────────────


def _space_delete_sig(ts: int, seed: bytes = SEED, blob_id: str = BID) -> dict:
    signed = sign_authority_event(
        event_type=AUTHORITY_EVENT_SPACE_MEDIA_DELETE,
        space_id=SPACE,
        payload=media_delete_payload(SCOPE_SPACE, SPACE, blob_id, ts),
        space_seed=seed,
    )
    return {"sig": signed["authority_sig"], "sig_suite": signed["authority_sig_suite"]}


def _channel_delete_sig(ts: int, seed: bytes = CHANNEL_SEED) -> dict:
    body = media_delete_payload(SCOPE_CHANNEL, CHANNEL, BID, ts)
    sig = sign_ed25519(seed, signing_bytes(CHANNEL_MEDIA_DELETE_PREFIX, body))
    return {"sig": b64url_encode(sig), "sig_suite": "ed25519"}


async def test_the_scope_seed_holder_may_delete(env):
    auth, _, _ = env
    await auth.authorize_delete(
        SCOPE_SPACE, SPACE, BID, ts=NOW, **_space_delete_sig(NOW)
    )
    await auth.authorize_delete(
        SCOPE_CHANNEL, CHANNEL, BID, ts=NOW, **_channel_delete_sig(NOW)
    )


@pytest.mark.parametrize(
    "case",
    ["stale", "wrong_key", "other_blob", "bad_ts", "bad_sig", "channel_key", "scope"],
)
async def test_delete_refusals(env, case):
    auth, _, _ = env
    ts: object = NOW
    scope, scope_id = SCOPE_SPACE, SPACE
    if case == "stale":
        ts = NOW - SPACE_MEDIA_TS_SKEW_S - 1
        sig = _space_delete_sig(ts)
    elif case == "wrong_key":
        sig = _space_delete_sig(NOW, seed=os.urandom(32))
    elif case == "other_blob":
        sig = _space_delete_sig(NOW, blob_id="ef" * 32)
    elif case == "bad_ts":
        ts, sig = "now", _space_delete_sig(NOW)
    elif case == "bad_sig":
        sig = {"sig": 5, "sig_suite": "ed25519"}
    elif case == "channel_key":
        scope, scope_id = SCOPE_CHANNEL, CHANNEL
        sig = _channel_delete_sig(NOW, seed=os.urandom(32))
    else:
        scope, sig = "room", _space_delete_sig(NOW)
    with pytest.raises(PermissionError):
        await auth.authorize_delete(scope, scope_id, BID, ts=ts, **sig)


def test_only_the_write_paths_are_shed_per_ip():
    assert is_space_media_write_path(f"/gfs/spaces/s/media/{'a' * 64}")
    assert is_space_media_write_path(f"/gfs/channels/{CHANNEL}/media/{'a' * 64}")
    assert is_space_media_write_path(f"/gfs/spaces/s/media/{'a' * 64}/delete")
    assert not is_space_media_write_path(f"/gfs/media/{'a' * 64}")
    assert not is_space_media_write_path("/gfs/spaces/s/epoch")
    assert not is_space_media_write_path("/gfs/channels/publish")
    assert not is_space_media_write_path(f"/gfs/spaces/s/media/{'a' * 64}/other")


def test_the_real_clock_is_the_default():
    assert GfsSpaceMediaAuth.__init__.__kwdefaults__["clock"] is time.time
