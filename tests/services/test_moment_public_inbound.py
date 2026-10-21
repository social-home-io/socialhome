"""MomentPublicInbound — a verified envelope refreshes the author's public
profile (name, bio, picture digest) on the local follows (§Momentum-public),
and only an encrypted envelope that opens with the author's key lands (v_62).

The rest of the inbound path (signature, binding, deletes) is covered in
``test_moment_public_service.py``.
"""

from __future__ import annotations

import json
import os
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from socialhome.crypto import b64url_encode, sign_ed25519
from socialhome.domain.events import MomentCreated
from socialhome.global_server.envelope_relay import ENVELOPE_QUEUE_TTL_SECONDS
from socialhome.infrastructure.event_bus import EventBus
from socialhome.moment_public_seal import item_key, seal_body, sign_wire
from socialhome.repositories.moment_public_repo import SqliteMomentPublicFollowRepo
from socialhome.repositories.moment_repo import SqliteMomentRepo
from socialhome.services import moment_public_inbound
from socialhome.services.moment_public_inbound import (
    HOLD_FOR_S,
    MAX_HELD_PER_EPOCH,
    SENT_AT_MAX_AGE_S,
    SENT_AT_MAX_SKEW_S,
    MomentPublicInbound,
)
from tests.moment_public_wire import (
    TEST_GEN,
    TEST_KEY,
    TEST_STEP,
    StubMomentKeys,
    encrypted_envelope,
    now_iso,
)

_SEED = b"\x22" * 32
_OTHER_SEED = b"\x33" * 32


def _pk(seed: bytes) -> str:
    sk = Ed25519PrivateKey.from_private_bytes(seed)
    return sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


def _signed(payload: dict, seed: bytes = _SEED) -> dict:
    """The author household's envelope: sealed, then signed (v_62)."""
    return encrypted_envelope(payload, seed)


#: The flat profile keys tests pass, and where they go in the sealed body.
_PROFILE_KEYS = {
    "author_display_name": "display_name",
    "author_username": "username",
    "author_bio": "bio",
    "author_picture_digest": "picture_digest",
}


def _moment(moment_id: str = "m-1", *, with_profile: bool = True, **extra) -> dict:
    """A sealed create body. The ``author_*`` keys become the moment's
    ``profile`` (the shape a profile change rides in)."""
    profile = {"username": "noor", "display_name": "Noor Haddad"}
    for flat, key in _PROFILE_KEYS.items():
        if flat in extra:
            profile[key] = extra.pop(flat)
    body = {
        "moment_id": moment_id,
        "author_user_id": "u-noor",
        "content": "bees",
        "media_url": None,
        "media_type": None,
        "duration_ms": None,
        "parent_moment_id": None,
        "origin_instance_id": "inst-noor",
        "created_at": now_iso(hours=-1),
        "expires_at": now_iso(hours=23),
        "profile_rev": "0123456789abcdef",
        **extra,
    }
    if with_profile:
        body["profile"] = profile
    return body


async def _env(db, *, followers: tuple[str, ...] = ("u1",), keys=None):
    for uid in followers:
        await db.enqueue(
            "INSERT INTO users(user_id, username, display_name, state) "
            "VALUES(?, ?, ?, 'active')",
            (uid, uid, uid.upper()),
        )
    await db.enqueue(
        "INSERT INTO gfs_connections(id, gfs_instance_id, display_name, "
        "public_key, inbox_url, status, paired_at) VALUES('g1', 'gfs-1', 'G', "
        "?, 'https://g.example', 'active', datetime('now'))",
        ("aa" * 32,),
    )
    follows = SqliteMomentPublicFollowRepo(db)
    for uid in followers:
        await follows.upsert(
            follower_user_id=uid,
            followed_user_id="u-noor",
            gfs_id="g1",
            followed_instance_pk=_pk(_SEED),
            followed_username="noor",
            followed_display_name="Old Name",
            followed_bio="old bio",
            followed_picture_digest="aaaaaaaaaaaaaaaa",
        )
    inbound = MomentPublicInbound(
        bus=EventBus(),
        moment_repo=SqliteMomentRepo(db),
        follow_repo=follows,
        key_inbound=keys or StubMomentKeys(),
    )
    return inbound, follows


async def _create(inbound, payload: dict, seed: bytes = _SEED) -> None:
    await inbound.handle(
        {"type": "incoming_public_moment", "payload": _signed(payload, seed)},
        gfs_id="g1",
    )


async def _follow(follows, follower: str = "u1"):
    got = await follows.get(
        follower_user_id=follower, followed_user_id="u-noor", gfs_id="g1"
    )
    assert got is not None
    return got


async def test_verified_envelope_refreshes_every_local_follow(db):
    inbound, follows = await _env(db, followers=("u1", "u2"))
    await _create(
        inbound,
        _moment(author_bio="Beekeeper.", author_picture_digest="0123456789abcdef"),
    )
    for uid in ("u1", "u2"):
        f = await _follow(follows, uid)
        assert f.followed_display_name == "Noor Haddad"
        assert f.followed_bio == "Beekeeper."
        assert f.followed_picture_digest == "0123456789abcdef"


async def test_a_bad_signature_changes_nothing(db):
    inbound, follows = await _env(db)
    await _create(
        inbound,
        _moment(author_display_name="Mallory", author_bio="evil"),
        seed=_OTHER_SEED,
    )
    f = await _follow(follows)
    assert (f.followed_display_name, f.followed_bio) == ("Old Name", "old bio")


async def test_a_moment_without_a_profile_keeps_the_held_one(db):
    """The profile rides only when it changed (or once a day): a moment
    without one changes nothing that is held."""
    inbound, follows = await _env(db)
    await _create(inbound, _moment(with_profile=False))
    f = await _follow(follows)
    assert f.followed_display_name == "Old Name"
    assert f.followed_bio == "old bio"
    assert f.followed_picture_digest == "aaaaaaaaaaaaaaaa"
    assert await SqliteMomentRepo(db).get("m-1") is not None


async def test_a_replayed_expired_moment_is_neither_stored_nor_a_profile(db):
    """Review finding M7: a validly signed old envelope replayed by the GFS
    must not re-store an expired moment, nor roll the profile back."""
    inbound, follows = await _env(db)
    await _create(
        inbound,
        _moment(
            "m-old",
            created_at=now_iso(hours=-30),
            expires_at=now_iso(hours=-6),
            author_display_name="Noor Old",
        ),
    )
    await _create(
        inbound,
        _moment(
            "m-ancient",
            created_at=now_iso(hours=-24 * 9),
            expires_at=now_iso(hours=24),
            author_display_name="Noor Ancient",
        ),
    )
    await _create(inbound, _moment("m-junk", created_at="not a date"))
    assert (await _follow(follows)).followed_display_name == "Old Name"
    for mid in ("m-old", "m-ancient", "m-junk"):
        assert await SqliteMomentRepo(db).get(mid) is None


async def test_a_cleared_bio_and_picture_are_cleared(db):
    inbound, follows = await _env(db)
    await _create(inbound, _moment(author_bio=None, author_picture_digest=None))
    f = await _follow(follows)
    assert f.followed_bio is None and f.followed_picture_digest is None


async def test_hostile_values_are_dropped_and_an_id_is_not_a_name(db):
    inbound, follows = await _env(db)
    await _create(
        inbound,
        _moment(
            author_display_name="u-noor",
            author_username="",
            author_bio="x" * 281,
            author_picture_digest="ab&sig=evil",
        ),
    )
    f = await _follow(follows)
    assert f.followed_display_name == "Old Name"
    assert f.followed_bio is None
    assert f.followed_picture_digest is None


async def test_the_username_names_an_author_without_a_display_name(db):
    inbound, follows = await _env(db)
    await _create(inbound, _moment(author_display_name="  "))
    assert (await _follow(follows)).followed_display_name == "noor"


async def test_a_replayed_moment_does_not_roll_the_profile_back(db):
    inbound, follows = await _env(db)
    old = _moment("m-old", author_bio="first bio")
    await _create(inbound, old)
    await _create(inbound, _moment("m-new", author_bio="second bio"))
    await _create(inbound, old)  # the GFS replays the older envelope
    assert (await _follow(follows)).followed_bio == "second bio"


async def test_a_reply_to_a_moment_not_held_here_is_dropped_not_crashed(db):
    """Regression (federation demo): a public reply whose parent this
    household never received hit the ``parent_moment_id`` foreign key and
    raised out of the GFS socket handler."""
    inbound, _follows = await _env(db)
    await _create(inbound, _moment("m-reply", parent_moment_id="m-unknown"))
    assert await SqliteMomentRepo(db).get("m-reply") is None


async def test_a_reply_to_a_held_moment_lands(db):
    inbound, _follows = await _env(db)
    await _create(inbound, _moment("m-root"))
    await _create(inbound, _moment("m-reply", parent_moment_id="m-root"))
    got = await SqliteMomentRepo(db).get("m-reply")
    assert got is not None and got.parent_moment_id == "m-root"


async def test_a_gfs_moment_is_published_as_gfs_received(db):
    """The bus event says where it came from, so realtime can keep it off
    protected accounts' sockets."""
    bus = EventBus()
    seen: list = []
    bus.subscribe(MomentCreated, lambda e: seen.append(e))
    inbound, _follows = await _env(db)
    inbound._bus = bus  # type: ignore[attr-defined]
    await _create(inbound, _moment("m-ev"))
    assert [e.received_via for e in seen] == ["gfs"]


# ── Encrypted envelopes (v_62) ────────────────────────────────────────────


async def _stored(db, moment_id: str):
    return await SqliteMomentRepo(db).get(moment_id)


async def test_a_plaintext_envelope_from_an_older_author_is_refused(db):
    """A public moment that crossed the GFS in the clear never lands, even
    validly signed."""
    inbound, _ = await _env(db)
    payload = _moment("m-plain")
    canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    plain = {**payload, "signature": b64url_encode(sign_ed25519(_SEED, canonical))}
    await inbound.handle(
        {"type": "incoming_public_moment", "payload": plain}, gfs_id="g1"
    )
    assert await _stored(db, "m-plain") is None


async def test_an_envelope_sealed_under_another_key_is_dropped(db):
    inbound, _ = await _env(db)
    env = encrypted_envelope(_moment("m-k"), _SEED, key=os.urandom(32))
    await inbound.handle(
        {"type": "incoming_public_moment", "payload": env}, gfs_id="g1"
    )
    assert await _stored(db, "m-k") is None


async def test_a_sealed_create_sent_as_a_delete_is_not_applied(db):
    """The sealed ``op`` must match the frame — a GFS can't turn one into
    the other."""
    inbound, _ = await _env(db)
    await _create(inbound, _moment("m-op"))
    env = _signed(_moment("m-op"))
    await inbound.handle(
        {"type": "incoming_public_moment_delete", "payload": env}, gfs_id="g1"
    )
    got = await _stored(db, "m-op")
    assert got is not None and got.deleted_at is None


async def test_a_sealed_body_naming_another_author_is_dropped(db):
    inbound, _ = await _env(db)
    # Sealed as u-sam's (the AAD binds that author), then relabelled as
    # u-noor's and signed: it can't open as u-noor's.
    env = encrypted_envelope({**_moment("m-a"), "author_user_id": "u-sam"}, _SEED)
    env = sign_wire(_SEED, {**env, "author_user_id": "u-noor"})
    await inbound.handle(
        {"type": "incoming_public_moment", "payload": env}, gfs_id="g1"
    )
    # Sealed for u-noor (opens), but the body inside names u-sam.
    inner = {**_moment("m-a"), "author_user_id": "u-sam", "op": "create"}
    env = sign_wire(
        _SEED,
        {
            "author_user_id": "u-noor",
            "instance_id": "inst-noor",
            **seal_body(
                step_key=TEST_KEY,
                author_user_id="u-noor",
                key_gen=TEST_GEN,
                key_step=TEST_STEP,
                moment_id="m-a",
                body=inner,
            ),
        },
    )
    await inbound.handle(
        {"type": "incoming_public_moment", "payload": env}, gfs_id="g1"
    )
    assert await _stored(db, "m-a") is None


async def test_a_malformed_sealed_create_is_dropped(db):
    inbound, _ = await _env(db)
    payload = _moment("m-bad")
    del payload["expires_at"]
    await _create(inbound, payload)
    assert await _stored(db, "m-bad") is None


async def test_a_bool_epoch_is_dropped(db):
    inbound, _ = await _env(db)
    env = {k: v for k, v in _signed(_moment("m-b")).items() if k != "signature"}
    env["key_epoch"] = True
    await inbound.handle(
        {"type": "incoming_public_moment", "payload": sign_wire(_SEED, env)},
        gfs_id="g1",
    )
    assert await _stored(db, "m-b") is None


async def test_without_the_key_the_moment_waits_and_the_key_is_asked_for(db):
    keys = StubMomentKeys(missing={"u-noor"})
    inbound, _ = await _env(db, keys=keys)
    await _create(inbound, _moment("m-wait"))
    assert await _stored(db, "m-wait") is None
    assert keys.requests == [("u-noor", "g1", TEST_GEN)]
    # The key arrives: what waited is opened and stored.
    keys.import_gen = TEST_GEN
    await inbound.handle(
        {"type": "moment_key", "payload": {"author_user_id": "u-noor"}}, gfs_id="g1"
    )
    assert await _stored(db, "m-wait") is not None


async def test_a_refused_key_releases_nothing(db):
    keys = StubMomentKeys(missing={"u-noor"})
    inbound, _ = await _env(db, keys=keys)
    await _create(inbound, _moment("m-wait"))
    await inbound.handle(
        {"type": "moment_key", "payload": {"author_user_id": "u-noor"}}, gfs_id="g1"
    )
    assert await _stored(db, "m-wait") is None


async def test_held_moments_are_bounded_per_epoch_and_in_all(db, monkeypatch):
    keys = StubMomentKeys(missing={"u-noor"})
    inbound, _ = await _env(db, keys=keys)
    for i in range(MAX_HELD_PER_EPOCH + 3):
        await _create(inbound, _moment(f"m-{i}"))
    assert sum(len(v) for v in inbound._held.values()) == MAX_HELD_PER_EPOCH
    monkeypatch.setattr(moment_public_inbound, "MAX_HELD", 4)
    for epoch in range(3):
        env = encrypted_envelope(_moment(f"e-{epoch}"), _SEED, gen=epoch)
        await inbound.handle(
            {"type": "incoming_public_moment", "payload": env}, gfs_id="g1"
        )
    assert sum(len(v) for v in inbound._held.values()) == 4


async def test_a_held_moment_past_its_wait_is_not_opened(db, monkeypatch):
    keys = StubMomentKeys(missing={"u-noor"})
    inbound, _ = await _env(db, keys=keys)
    clock = [1000.0]
    monkeypatch.setattr(moment_public_inbound.time, "monotonic", lambda: clock[0])
    await _create(inbound, _moment("m-old"))
    clock[0] += HOLD_FOR_S + 1
    keys.import_gen = TEST_GEN
    await inbound.handle(
        {"type": "moment_key", "payload": {"author_user_id": "u-noor"}}, gfs_id="g1"
    )
    assert await _stored(db, "m-old") is None


# ── Encrypted media (v_63) ────────────────────────────────────────────────


class _MediaIn:
    def __init__(self, *, accept: bool = True) -> None:
        self.accept = accept
        self.jobs: list = []

    def submit(self, job) -> bool:
        self.jobs.append(job)
        return self.accept


_MEDIA = {
    "blob_id": "c" * 64,
    "media_suite": "aesgcm-256-pad",
    "content_type": "image/webp",
}


async def test_a_moment_with_media_is_stored_once_its_file_is_kept(db):
    inbound, _ = await _env(db)
    media = _MediaIn()
    inbound.attach_media(media)
    await _create(inbound, _moment("m-pic", media=_MEDIA, media_type="image"))
    # Nothing stored until the file is here.
    assert await _stored(db, "m-pic") is None
    [job] = media.jobs
    assert (job.moment_id, job.author_user_id, job.gfs_id) == ("m-pic", "u-noor", "g1")
    assert job.item_key == item_key(TEST_KEY, "m-pic")
    assert job.media == _MEDIA
    await job.then("api/media/gm-abc.webp", "image")
    got = await _stored(db, "m-pic")
    assert (got.media_url, got.media_type) == ("api/media/gm-abc.webp", "image")


async def test_a_moment_whose_file_couldnt_be_kept_is_stored_without_it(db):
    inbound, _ = await _env(db)
    media = _MediaIn()
    inbound.attach_media(media)
    await _create(inbound, _moment("m-pic", media=_MEDIA, media_type="image"))
    await media.jobs[0].then(None, None)
    got = await _stored(db, "m-pic")
    assert got is not None and got.media_url is None and got.media_type is None
    assert got.content == "bees"


async def test_a_full_fetch_queue_stores_the_moment_without_its_file(db):
    inbound, _ = await _env(db)
    inbound.attach_media(_MediaIn(accept=False))
    await _create(inbound, _moment("m-pic", media=_MEDIA, media_type="image"))
    got = await _stored(db, "m-pic")
    assert got is not None and got.media_url is None


async def test_an_authors_local_reference_is_never_stored(db):
    """A body naming ``media_url`` (the author's own file) points at
    nothing here — it is not kept."""
    inbound, _ = await _env(db)
    await _create(
        inbound, _moment("m-ref", media_url="api/media/a.webp", media_type="image")
    )
    got = await _stored(db, "m-ref")
    assert got is not None and got.media_url is None and got.media_type is None


async def test_deleting_a_moment_forgets_its_fetched_file(db):
    inbound, _ = await _env(db)
    media = _MediaIn()
    forgotten: list = []

    async def _forget(url):
        forgotten.append(url)

    media.forget = _forget  # type: ignore[attr-defined]
    inbound.attach_media(media)
    await _create(inbound, _moment("m-pic", media=_MEDIA, media_type="image"))
    await media.jobs[0].then("api/media/gm-abc.webp", "image")
    await inbound.handle(
        {
            "type": "incoming_public_moment_delete",
            "payload": _signed(
                {
                    "moment_id": "m-pic",
                    "author_user_id": "u-noor",
                    "instance_id": "inst-noor",
                    "origin_instance_id": "inst-noor",
                }
            ),
        },
        gfs_id="g1",
    )
    assert forgotten == ["api/media/gm-abc.webp"]


# ── Signed send time (v_64) ───────────────────────────────────────────────


def _created_events(inbound) -> list:
    seen: list = []
    bus = EventBus()
    bus.subscribe(MomentCreated, lambda e: seen.append(e))
    inbound._bus = bus  # type: ignore[attr-defined]
    return seen


def _delete(moment_id: str, **extra) -> dict:
    return {
        "moment_id": moment_id,
        "author_user_id": "u-noor",
        "instance_id": "inst-noor",
        "origin_instance_id": "inst-noor",
        **extra,
    }


async def _deliver_delete(inbound, payload: dict) -> None:
    await inbound.handle(
        {"type": "incoming_public_moment_delete", "payload": _signed(payload)},
        gfs_id="g1",
    )


async def test_a_moment_with_a_fresh_send_time_lands(db):
    inbound, _ = await _env(db)
    await _create(inbound, _moment("m-fresh", sent_at=int(time.time())))
    assert await _stored(db, "m-fresh") is not None


async def test_a_moment_sent_longer_ago_than_any_delivery_takes_is_refused(db):
    """A captured envelope re-posted a day later is a replay: no GFS queue
    or key wait holds a real one that long."""
    inbound, _ = await _env(db)
    stale = int(time.time()) - SENT_AT_MAX_AGE_S - 60
    await _create(inbound, _moment("m-stale", sent_at=stale))
    assert await _stored(db, "m-stale") is None


async def test_a_moment_sent_in_the_future_is_refused(db):
    inbound, _ = await _env(db)
    ahead = int(time.time()) + SENT_AT_MAX_SKEW_S + 60
    await _create(inbound, _moment("m-ahead", sent_at=ahead))
    assert await _stored(db, "m-ahead") is None


async def test_a_moment_created_after_it_was_sent_is_refused(db):
    inbound, _ = await _env(db)
    sent = int(time.time()) - 2 * 3600
    await _create(
        inbound, _moment("m-later", sent_at=sent, created_at=now_iso(hours=-1))
    )
    assert await _stored(db, "m-later") is None


async def test_a_send_time_that_is_not_an_integer_is_refused(db):
    inbound, _ = await _env(db)
    for i, bad in enumerate((True, "1700000000", 1.5, -1, None)):
        await _create(inbound, _moment(f"m-bad-{i}", sent_at=bad))
        assert await _stored(db, f"m-bad-{i}") is None


async def test_an_older_authors_moment_without_a_send_time_still_lands(db):
    """Fallback for a v_63 author household: no ``sent_at``, the older
    rules (``created_at`` / ``expires_at``) apply."""
    inbound, _ = await _env(db)
    await _create(inbound, _moment("m-legacy"))
    assert await _stored(db, "m-legacy") is not None


async def test_once_an_author_sent_a_send_time_its_absence_is_refused(db):
    """Monotonic per author: after a v_64 envelope, one without ``sent_at``
    can only be an older envelope replayed."""
    inbound, _ = await _env(db)
    await _create(inbound, _moment("m-new", sent_at=int(time.time())))
    await _create(inbound, _moment("m-old-replay"))
    assert await _stored(db, "m-old-replay") is None


async def test_a_replayed_create_of_a_held_moment_is_announced_once(db):
    inbound, _ = await _env(db)
    seen = _created_events(inbound)
    payload = _moment("m-once", sent_at=int(time.time()))
    envelope = _signed(payload)
    for _ in range(3):
        await inbound.handle(
            {"type": "incoming_public_moment", "payload": envelope}, gfs_id="g1"
        )
    assert [e.moment_id for e in seen] == ["m-once"]


async def test_a_stale_delete_is_refused(db):
    inbound, _ = await _env(db)
    await _create(inbound, _moment("m-keep", sent_at=int(time.time())))
    stale = int(time.time()) - SENT_AT_MAX_AGE_S - 60
    await _deliver_delete(inbound, _delete("m-keep", sent_at=stale))
    got = await SqliteMomentRepo(db).get("m-keep", include_deleted=True)
    assert got is not None and got.deleted_at is None


async def test_a_fresh_delete_applies(db):
    inbound, _ = await _env(db)
    await _create(inbound, _moment("m-gone", sent_at=int(time.time())))
    await _deliver_delete(inbound, _delete("m-gone", sent_at=int(time.time())))
    got = await SqliteMomentRepo(db).get("m-gone", include_deleted=True)
    assert got is not None and got.deleted_at is not None


def test_the_age_window_covers_every_honest_delay():
    """A real envelope can wait in a GFS's offline queue for its TTL and
    then for its key here; the window must not refuse it."""
    honest = ENVELOPE_QUEUE_TTL_SECONDS + HOLD_FOR_S + SENT_AT_MAX_SKEW_S
    assert SENT_AT_MAX_AGE_S >= honest
