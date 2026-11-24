"""Who may put a blob into the space media cache — the checks the GFS
already makes for a channel, reused. Every refusal is a ``PermissionError``.

Since v_96 (P7, owner decision Q5) the one scope is a space's opaque
channel: the channel key or the channel writer key. The ``space`` scope
(space authority, space writer group key) is refused."""

from __future__ import annotations

import os
import time

import pytest
from aiohttp import web

from socialhome.crypto import (
    b64url_encode,
    counting_ed25519_verifies,
    ed25519_public_key,
    sign_ed25519,
)
from socialhome.domain.gfs_space_media import (
    AUTH_CHANNEL,
    AUTH_WRITER_KEY,
    CHANNEL_MEDIA_DELETE_PREFIX,
    SCOPE_CHANNEL,
    SpaceMediaClaim,
    SpaceMediaUploadAuth,
    media_delete_payload,
)
from socialhome.gfs_channel import channel_pk_of, signing_bytes
from socialhome.global_server.channels import GfsChannelService
from socialhome.global_server.domain import ClientInstance, GlobalSpace
from socialhome.global_server.fan_out_workers import GfsFanOutWorkers
from socialhome.global_server.federation import GfsFederationService
from socialhome.global_server.repositories import (
    SqliteGfsChannelRepo,
    SqliteGfsFederationRepo,
)
from socialhome.global_server.space_media_auth import (
    SPACE_MEDIA_MAX_PER_MINUTE_PER_IP,
    SPACE_MEDIA_TS_SKEW_S,
    GfsSpaceMediaAuth,
    build_space_media_rate_limit,
    is_space_media_write,
)
from socialhome.writer_key import sign_with_writer_key

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
    channel_repo = SqliteGfsChannelRepo(gfs_db)
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="owner",
            display_name="O",
            public_key="aa" * 32,
            status="active",
        )
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
    # In use: two member households hold a seat (security review F1, N2).
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="member2",
            display_name="M",
            public_key="cc" * 32,
            status="active",
        )
    )
    await channel_repo.add_subscriber(CHANNEL, "owner", pass_epoch=3, now=NOW)
    await channel_repo.add_subscriber(CHANNEL, "member2", pass_epoch=3, now=NOW)
    federation = GfsFederationService(fed_repo)
    clock = _Clock()
    channels = GfsChannelService(
        federation=federation,
        channel_repo=channel_repo,
        # Never reached by these checks.
        fan_out=GfsFanOutWorkers(relay=None),  # type: ignore[arg-type]
        gfs_instance_id="gfs-test",
        clock=clock,
    )
    auth = GfsSpaceMediaAuth(channels=channels, clock=clock)
    return auth, None, channel_repo


def _claim(scope_id=CHANNEL, ts=NOW, scope=SCOPE_CHANNEL, **over) -> SpaceMediaClaim:
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


def _channel_key(
    claim: SpaceMediaClaim, seed: bytes = CHANNEL_SEED, suite: str = "ed25519"
) -> SpaceMediaUploadAuth:
    sig = sign_ed25519(seed, claim.channel_signing_bytes())
    return SpaceMediaUploadAuth(
        claim=claim, kind=AUTH_CHANNEL, sig=b64url_encode(sig), sig_suite=suite
    )


async def _pin_channel_writer_key(channel_repo, epoch: int = 3) -> bytes:
    writer_seed = os.urandom(32)
    await channel_repo.pin_writer_key(CHANNEL, epoch, channel_pk_of(writer_seed))
    return writer_seed


def _writer_key(claim, writer_seed: bytes, *, epoch=3, suite="ed25519"):
    sig = sign_with_writer_key(
        writer_seed, claim.writer_signing_bytes(kind=AUTH_WRITER_KEY, epoch=epoch)
    )
    return SpaceMediaUploadAuth(
        claim=claim, kind=AUTH_WRITER_KEY, sig=sig, sig_suite=suite, epoch=epoch
    )


@pytest.mark.parametrize(
    "skew", [SPACE_MEDIA_TS_SKEW_S + 1, -SPACE_MEDIA_TS_SKEW_S - 1]
)
async def test_a_stale_or_future_timestamp_is_refused(env, skew):
    auth, _, _ = env
    with pytest.raises(PermissionError):
        await _upload(auth, _channel_key(_ch_claim(ts=NOW + skew)))


async def test_a_signature_for_another_claim_does_not_carry_over(env):
    auth, _, _ = env
    a = _channel_key(_ch_claim())
    forged = SpaceMediaUploadAuth(
        claim=_ch_claim(size=131_100), kind=a.kind, sig=a.sig, sig_suite=a.sig_suite
    )
    with pytest.raises(PermissionError):
        await _upload(auth, forged)


@pytest.mark.parametrize("kind", ["authority", "writer_cert", "channel_cert"])
async def test_a_retired_kind_is_refused(env, kind):
    """The space authority kind left with the ``space`` scope (v_96), the
    writer-cert / channel-cert kinds with the identified member publish
    (v_90): a header naming one never authorizes."""
    auth, _, _ = env
    a = SpaceMediaUploadAuth(
        claim=_ch_claim(), kind=kind, sig="x" * 86, sig_suite="ed25519", epoch=3
    )
    with pytest.raises(PermissionError):
        await _upload(auth, a)


async def test_the_space_scope_is_refused(env):
    """v_96 (P7) — a claim naming the retired ``space`` scope never
    authorizes an upload or a delete, whatever it carries."""
    auth, _, _ = env
    claim = _claim(scope_id="sp-1", scope="space")
    for kind in ("channel", "writer_key", "authority"):
        a = SpaceMediaUploadAuth(
            claim=claim, kind=kind, sig="x" * 86, sig_suite="ed25519", epoch=3
        )
        with pytest.raises(PermissionError):
            await auth.authorize_upload(
                a, scope_kind="space", scope_id="sp-1", blob_id=BID
            )
    with pytest.raises(PermissionError):
        await auth.authorize_delete(
            "space", "sp-1", BID, ts=NOW, sig="x" * 86, sig_suite="ed25519"
        )


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


def _channel_delete_sig(
    ts: int, seed: bytes = CHANNEL_SEED, blob_id: str = BID
) -> dict:
    body = media_delete_payload(SCOPE_CHANNEL, CHANNEL, blob_id, ts)
    sig = sign_ed25519(seed, signing_bytes(CHANNEL_MEDIA_DELETE_PREFIX, body))
    return {"sig": b64url_encode(sig), "sig_suite": "ed25519"}


async def test_the_scope_seed_holder_may_delete(env):
    auth, _, _ = env
    await auth.authorize_delete(
        SCOPE_CHANNEL, CHANNEL, BID, ts=NOW, **_channel_delete_sig(NOW)
    )


async def test_a_replayed_delete_is_refused_at_one_verification(env):
    """Security review F5: a captured delete replayed inside the ±300 s
    window is refused (it could otherwise drop a claim the scope made again
    since) — after its signature verified, so it costs what a success does."""
    auth, _, _ = env
    sig = _channel_delete_sig(NOW)
    await auth.authorize_delete(SCOPE_CHANNEL, CHANNEL, BID, ts=NOW, **sig)
    with counting_ed25519_verifies() as done, pytest.raises(PermissionError):
        await auth.authorize_delete(SCOPE_CHANNEL, CHANNEL, BID, ts=NOW, **sig)
    assert done[0] == 1
    # A fresh one (another ts) still goes through.
    await auth.authorize_delete(
        SCOPE_CHANNEL, CHANNEL, BID, ts=NOW + 1, **_channel_delete_sig(NOW + 1)
    )


@pytest.mark.parametrize(
    "case",
    ["stale", "wrong_key", "other_blob", "bad_ts", "bad_sig", "scope"],
)
async def test_delete_refusals(env, case):
    auth, _, _ = env
    ts: object = NOW
    scope, scope_id = SCOPE_CHANNEL, CHANNEL
    if case == "stale":
        ts = NOW - SPACE_MEDIA_TS_SKEW_S - 1
        sig = _channel_delete_sig(ts)
    elif case == "wrong_key":
        sig = _channel_delete_sig(NOW, seed=os.urandom(32))
    elif case == "other_blob":
        sig = _channel_delete_sig(NOW, blob_id="ef" * 32)
    elif case == "bad_ts":
        ts, sig = "now", _channel_delete_sig(NOW)
    elif case == "bad_sig":
        sig = {"sig": 5, "sig_suite": "ed25519"}
    else:
        scope, sig = "room", _channel_delete_sig(NOW)
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
    "channel": 1,
    "channel_writer_key": 1,
}


async def _case(env, kind: str, case: str) -> SpaceMediaUploadAuth:
    """Build the upload for (*kind*, *case*), setting up state as needed."""
    _, _, channel_repo = env
    other = "1e" * 16
    if kind == "channel":
        return {
            "ok": lambda: _channel_key(_ch_claim()),
            "wrong_key": lambda: _channel_key(_ch_claim(), seed=os.urandom(32)),
            "unknown": lambda: _channel_key(_claim(other, scope=SCOPE_CHANNEL)),
            "suite": lambda: _channel_key(_ch_claim(), suite="pq"),
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


_CASES = [("channel", c) for c in ("ok", "wrong_key", "unknown", "suite")] + [
    ("channel_writer_key", c)
    for c in ("ok", "no_pin", "wrong_key", "wrong_epoch", "suite", "unknown")
]


@pytest.mark.parametrize(("kind", "case"), _CASES, ids=[f"{k}-{c}" for k, c in _CASES])
async def test_every_outcome_of_a_kind_costs_the_same_verifications(env, kind, case):
    """Timing must not tell which spaces are listed or banned, which channels
    exist or which keys are pinned: a refusal at
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
        # v_96 — the retired scope is refused, at the cost of a success.
        scope_id = "sp-nope" if case == "unlisted" else SPACE
        sig = _channel_delete_sig(ts)
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


@pytest.mark.parametrize("seat", ["none", "closed_epoch"])
async def test_a_channel_nobody_holds_a_seat_in_gets_no_media(env, seat):
    """Security review F1: registration is anonymous, so only a channel IN
    USE — a member household's seat with a pass for an open epoch — takes
    uploads, and the refusal costs what a success of the kind does."""
    auth, _, channel_repo = env
    await channel_repo.remove_subscriber(CHANNEL, "owner")
    await channel_repo.remove_subscriber(CHANNEL, "member2")
    if seat == "closed_epoch":
        await channel_repo.add_subscriber(CHANNEL, "owner", pass_epoch=1, now=NOW)
        await channel_repo.add_subscriber(CHANNEL, "member2", pass_epoch=1, now=NOW)
    a = _channel_key(_claim(scope=SCOPE_CHANNEL, scope_id=CHANNEL))
    with counting_ed25519_verifies() as done, pytest.raises(PermissionError):
        await _upload(auth, a)
    assert done[0] == 1


async def test_one_seated_member_household_is_enough_for_media(env):
    """Third review A4: the owner household never subscribes to its own
    channel, so a private space with one link-joined member has ONE seat —
    and its media must work."""
    auth, _, channel_repo = env
    await channel_repo.remove_subscriber(CHANNEL, "member2")
    await _upload(auth, _channel_key(_claim(scope=SCOPE_CHANNEL, scope_id=CHANNEL)))
