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
from aiohttp import web

from socialhome.authority_sig import (
    AUTHORITY_EVENT_SPACE_MEDIA_DELETE,
    sign_authority_event,
)
from socialhome.crypto import (
    b64url_encode,
    counting_ed25519_verifies,
    ed25519_public_key,
    sign_ed25519,
)
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
    SPACE_MEDIA_MAX_PER_MINUTE_PER_IP,
    SPACE_MEDIA_TS_SKEW_S,
    GfsSpaceMediaAuth,
    build_space_media_rate_limit,
    is_space_media_write,
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


def test_only_the_routed_writes_are_shed_per_ip():
    assert is_space_media_write("PUT", "/gfs/media/{blob_id}")
    assert is_space_media_write("POST", "/gfs/media/{blob_id}/delete")
    assert not is_space_media_write("GET", "/gfs/media/{blob_id}")
    assert not is_space_media_write("POST", "/gfs/media/{blob_id}")
    assert not is_space_media_write("PUT", "/gfs/spaces/{space_id}/epoch")
    assert not is_space_media_write("PUT", None)


async def test_the_write_limiter_takes_a_node_share():
    """Cluster-wide: on 2 live nodes each allows half the per-IP budget."""
    limiter = build_space_media_rate_limit(_FixedIp(), nodes=lambda: 2)
    allowed = 0
    for _ in range(SPACE_MEDIA_MAX_PER_MINUTE_PER_IP):
        resp = await limiter(_RoutedRequest("PUT", "/gfs/media/{blob_id}"), _ok)
        allowed += resp.status == 200
    assert allowed == SPACE_MEDIA_MAX_PER_MINUTE_PER_IP // 2


class _FixedIp:
    def bucket(self, request) -> str:
        return "203.0.113.9"


class _Resource:
    def __init__(self, canonical):
        self.canonical = canonical


class _Route:
    def __init__(self, canonical):
        self.resource = _Resource(canonical) if canonical is not None else None


class _MatchInfo:
    def __init__(self, canonical):
        self.route = _Route(canonical)


class _RoutedRequest:
    def __init__(self, method, canonical):
        self.method = method
        self.match_info = _MatchInfo(canonical)


async def _ok(request):
    return web.Response(status=200)


def test_the_real_clock_is_the_default():
    assert GfsSpaceMediaAuth.__init__.__kwdefaults__["clock"] is time.time


# ── Timing: every outcome of a kind costs the same verifications ─────────

#: Ed25519 verifications a successful upload of each kind makes.
_VERIFIES = {
    "authority": 1,
    "writer_cert": 2,
    "writer_key": 1,
    "channel": 1,
    "channel_cert": 2,
    "channel_writer_key": 1,
}


async def _case(env, kind: str, case: str) -> SpaceMediaUploadAuth:
    """Build the upload for (*kind*, *case*), setting up state as needed."""
    _, epoch_repo, channel_repo = env
    other = "1e" * 16
    if kind == "authority":
        return {
            "ok": lambda: _authority(_claim()),
            "wrong_key": lambda: _authority(_claim(), seed=os.urandom(32)),
            "unlisted": lambda: _authority(_claim("sp-nope")),
            "banned": lambda: _authority(_claim("sp-banned")),
            "no_pinned_key": lambda: _authority(_claim("sp-nokey")),
            "suite": lambda: SpaceMediaUploadAuth(
                claim=_claim(), kind=AUTH_AUTHORITY, sig="x" * 86, sig_suite="pq"
            ),
        }[case]()
    if kind == "writer_cert":
        if case == "strict":
            await _pin_writer_key(epoch_repo)
        if case == "closed_epoch":
            await epoch_repo.confirm(SPACE, 9, now=NOW)
        if case == "suite":
            good = _writer_cert(_claim())
            return SpaceMediaUploadAuth(
                claim=good.claim,
                kind=good.kind,
                sig=good.sig,
                sig_suite="pq",
                epoch=good.epoch,
                writer_cert=good.writer_cert,
            )
        if case == "bad_sig_encoding":
            good = _writer_cert(_claim())
            return SpaceMediaUploadAuth(
                claim=good.claim,
                kind=good.kind,
                sig="!!!",
                sig_suite=good.sig_suite,
                epoch=good.epoch,
                writer_cert=good.writer_cert,
            )
        return {
            "ok": lambda: _writer_cert(_claim()),
            "other_signer": lambda: _writer_cert(_claim(), signer=os.urandom(32)),
            "foreign_cert": lambda: _writer_cert(_claim(), cert_seed=os.urandom(32)),
            "strict": lambda: _writer_cert(_claim()),
            "closed_epoch": lambda: _writer_cert(_claim(), epoch=3),
            "unlisted": lambda: _writer_cert(_claim("sp-nope")),
            "banned": lambda: _writer_cert(_claim("sp-banned")),
        }[case]()
    if kind == "writer_key":
        seed = os.urandom(32)
        if case in ("ok", "wrong_key", "wrong_epoch", "suite", "bad_sig_encoding"):
            seed = await _pin_writer_key(epoch_repo)
        if case == "bad_sig_encoding":
            good = _writer_key(_claim(), seed)
            return SpaceMediaUploadAuth(
                claim=good.claim,
                kind=good.kind,
                sig="!!!",
                sig_suite="ed25519",
                epoch=3,
            )
        return {
            "ok": lambda: _writer_key(_claim(), seed),
            "no_pin": lambda: _writer_key(_claim(), seed),
            "wrong_key": lambda: _writer_key(_claim(), os.urandom(32)),
            "wrong_epoch": lambda: _writer_key(_claim(), seed, epoch=4),
            "suite": lambda: _writer_key(_claim(), seed, suite="pq"),
            "unlisted": lambda: _writer_key(_claim("sp-nope"), seed),
            "banned": lambda: _writer_key(_claim("sp-banned"), seed),
        }[case]()
    if kind == "channel":
        return {
            "ok": lambda: _channel_key(_ch_claim()),
            "wrong_key": lambda: _channel_key(_ch_claim(), seed=os.urandom(32)),
            "unknown": lambda: _channel_key(_claim(other, scope=SCOPE_CHANNEL)),
            "suite": lambda: _channel_key(_ch_claim(), suite="pq"),
        }[case]()
    if kind == "channel_cert":
        if case == "strict":
            await _pin_channel_writer_key(channel_repo)
        return {
            "ok": lambda: _channel_cert(_ch_claim()),
            "other_signer": lambda: _channel_cert(_ch_claim(), signer=os.urandom(32)),
            "foreign_cert": lambda: _channel_cert(
                _ch_claim(), cert_seed=os.urandom(32)
            ),
            "closed_epoch": lambda: _channel_cert(_ch_claim(), epoch=9),
            "strict": lambda: _channel_cert(_ch_claim()),
            "unknown": lambda: _channel_cert(_claim(other, scope=SCOPE_CHANNEL)),
        }[case]()
    # channel_writer_key
    seed = os.urandom(32)
    if case in ("ok", "wrong_key", "wrong_epoch", "suite"):
        seed = await _pin_channel_writer_key(channel_repo)
    return {
        "ok": lambda: _writer_key(_ch_claim(), seed),
        "no_pin": lambda: _writer_key(_ch_claim(), seed),
        "wrong_key": lambda: _writer_key(_ch_claim(), os.urandom(32)),
        "wrong_epoch": lambda: _writer_key(_ch_claim(), seed, epoch=4),
        "suite": lambda: _writer_key(_ch_claim(), seed, suite="pq"),
        "unknown": lambda: _writer_key(_claim(other, scope=SCOPE_CHANNEL), seed),
    }[case]()


_CASES = (
    [
        ("authority", c)
        for c in ("ok", "wrong_key", "unlisted", "banned", "no_pinned_key", "suite")
    ]
    + [
        ("writer_cert", c)
        for c in (
            "ok",
            "other_signer",
            "foreign_cert",
            "suite",
            "bad_sig_encoding",
            "strict",
            "closed_epoch",
            "unlisted",
            "banned",
        )
    ]
    + [
        ("writer_key", c)
        for c in (
            "ok",
            "no_pin",
            "wrong_key",
            "wrong_epoch",
            "suite",
            "bad_sig_encoding",
            "unlisted",
            "banned",
        )
    ]
    + [("channel", c) for c in ("ok", "wrong_key", "unknown", "suite")]
    + [
        ("channel_cert", c)
        for c in (
            "ok",
            "other_signer",
            "foreign_cert",
            "closed_epoch",
            "strict",
            "unknown",
        )
    ]
    + [
        ("channel_writer_key", c)
        for c in ("ok", "no_pin", "wrong_key", "wrong_epoch", "suite", "unknown")
    ]
)


@pytest.mark.parametrize(("kind", "case"), _CASES, ids=[f"{k}-{c}" for k, c in _CASES])
async def test_every_outcome_of_a_kind_costs_the_same_verifications(env, kind, case):
    """Timing must not tell which spaces are listed or banned, which channels
    exist, which keys are pinned or which mode a space is in: a refusal at
    any step costs the Ed25519 verifications a success does."""
    auth, _, _ = env
    a = await _case(env, kind, case)
    with counting_ed25519_verifies() as done:
        try:
            await _upload(auth, a)
            outcome = "ok"
        except PermissionError:
            outcome = "refused"
    assert outcome == ("ok" if case == "ok" else "refused")
    assert done[0] == _VERIFIES[kind]


@pytest.mark.parametrize(
    ("scope", "case"),
    [
        ("space", "ok"),
        ("space", "wrong_key"),
        ("space", "unlisted"),
        ("channel", "ok"),
        ("channel", "wrong_key"),
        ("channel", "unknown"),
        ("space", "stale"),
    ],
)
async def test_every_outcome_of_a_delete_costs_one_verification(env, scope, case):
    auth, _, _ = env
    ts = NOW - SPACE_MEDIA_TS_SKEW_S - 1 if case == "stale" else NOW
    if scope == "space":
        scope_id = "sp-nope" if case == "unlisted" else SPACE
        sig = _space_delete_sig(
            ts, seed=os.urandom(32) if case == "wrong_key" else SEED
        )
    else:
        scope_id = "1e" * 16 if case == "unknown" else CHANNEL
        sig = _channel_delete_sig(
            ts, seed=os.urandom(32) if case == "wrong_key" else CHANNEL_SEED
        )
    with counting_ed25519_verifies() as done:
        try:
            await auth.authorize_delete(scope, scope_id, BID, ts=ts, **sig)
        except PermissionError:
            pass
    assert done[0] == 1
