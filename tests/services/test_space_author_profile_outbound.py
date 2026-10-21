"""SpaceAuthorProfileOutbound: our authors' profiles for a space's GFS
followers (v_60), published only on a real change."""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace

from PIL import Image

from socialhome.crypto import (
    derive_instance_id,
    derive_user_id,
    generate_identity_keypair,
)
from socialhome.domain.events import (
    SpaceMemberLeft,
    SpaceMemberProfileUpdated,
    SpacePostCreated,
    UserPreferencesChanged,
    UserProfileUpdated,
)
from socialhome.domain.picture_hash import compute_picture_hash
from socialhome.domain.post import Post, PostType
from socialhome.domain.preferences import UserPreferences
from socialhome.domain.space import SpaceType
from socialhome.domain.space_author_profile import (
    PROFILE_PICTURE_FIELD,
    PROFILE_PICTURE_MAX_BYTES,
    CachedAuthorProfile,
    decode_picture_sidecar,
    decode_profile_content,
    profile_picture_digest,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.media.profile_picture import PublicPictureCache
from socialhome.services.space_author_profile_outbound import (
    SpaceAuthorProfileOutbound,
)
from socialhome.services.space_item_author import verify_signed_item_inner

KP = generate_identity_keypair()
NOOR = derive_user_id(KP.public_key, "noor")
OWN = derive_instance_id(KP.public_key)


def _webp(size: int = 600, *, noisy: bool = False) -> bytes:
    im = Image.new("RGB", (size, size), (200, 120, 60))
    if noisy:
        im = Image.frombytes(
            "RGB", (size, size), bytes(range(256)) * (size * size * 3 // 256 + 1)
        )
    buf = io.BytesIO()
    im.save(buf, "WEBP")
    return buf.getvalue()


@dataclass
class _Spaces:
    space_type: SpaceType = SpaceType.GLOBAL
    allow_subscribers: bool = True
    member: object | None = field(
        default_factory=lambda: SimpleNamespace(space_display_name=None)
    )
    instances: list[str] = field(default_factory=list)
    seed: bytes | None = None

    async def get(self, space_id):
        return SimpleNamespace(
            id=space_id,
            space_type=self.space_type,
            features=SimpleNamespace(allow_subscribers=self.allow_subscribers),
        )

    async def get_member(self, space_id, user_id):
        return self.member

    async def list_member_instances(self, space_id):
        return self.instances

    async def get_space_seed(self, space_id):
        return self.seed


class _Users:
    def __init__(self) -> None:
        self.user = SimpleNamespace(
            user_id=NOOR,
            username="noor",
            display_name="Noor Haddad",
            bio="Beekeeper.",
            identity_anchor="noor",
        )

    async def get_by_user_id(self, user_id):
        return self.user if user_id == NOOR else None


class _Pictures:
    def __init__(self, user_pic=None, member_pic=None) -> None:
        self.user_pic, self.member_pic = user_pic, member_pic

    async def get_member_picture(self, space_id, user_id):
        return (
            (self.member_pic, compute_picture_hash(self.member_pic))
            if self.member_pic
            else None
        )

    async def get_user_picture(self, user_id):
        return (
            (self.user_pic, compute_picture_hash(self.user_pic))
            if self.user_pic
            else None
        )


class _Federation:
    def __init__(self, versions: dict[str, int] | None = None) -> None:
        self.versions = versions or {}

    async def get_instance(self, instance_id):
        v = self.versions.get(instance_id)
        return None if v is None else SimpleNamespace(proto_version=v)


class _Crypto:
    epoch = 3

    async def get_current_epoch(self, space_id):
        return self.epoch


class _MemberGfs:
    """The member-publish path: ``on_published`` runs only once a GFS took
    the item (:meth:`flush` stands in for that; ``fail`` for a refusal)."""

    def __init__(self) -> None:
        self.items: list[tuple] = []
        self.pending: list = []
        self.fail = False

    def schedule_item(
        self, space_id, author_user_id, item_type, inner, *, on_published=None
    ):
        self.items.append((space_id, author_user_id, item_type, inner))
        if on_published is not None:
            self.pending.append(on_published)
        return True

    async def flush(self) -> None:
        pending, self.pending = self.pending, []
        if not self.fail:
            for done in pending:
                await done()


class _Protected:
    def __init__(self, ids) -> None:
        self.ids = ids

    def register_gate(self, gate):
        pass

    async def is_protected(self, user_id):
        return user_id in self.ids


class _Cache:
    """The persisted author-profile cache, in memory: one row per
    ``(space, author)``, as the SQLite repo keeps it."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], tuple] = {}
        self.writes: list[tuple] = []

    async def upsert_if_newer(self, profile, *, picture_webp, signed_item, item_epoch):
        self.writes.append((profile, picture_webp, signed_item, item_epoch))
        self.rows[(profile.space_id, profile.author_user_id)] = (
            profile,
            picture_webp,
            signed_item,
            item_epoch,
        )
        return True

    async def get(self, space_id, author_user_id):
        row = self.rows.get((space_id, author_user_id))
        if row is None:
            return None
        return CachedAuthorProfile(
            profile=row[0], has_picture=row[1] is not None, picture_sidecar=None
        )

    async def list_spaces_for_author(self, author_user_id):
        return [space for (space, author) in self.rows if author == author_user_id]

    async def delete(self, space_id, author_user_id):
        self.rows.pop((space_id, author_user_id), None)


class _Prefs:
    """The per-user preferences, in memory."""

    def __init__(self, hidden: set[str] | None = None) -> None:
        self.hidden = hidden if hidden is not None else set()

    async def get_user(self, user_id):
        return UserPreferences(
            user_id=user_id, hide_profile_from_followers=user_id in self.hidden
        )


class _Certs:
    def __init__(self, cert=None) -> None:
        self.cert = cert
        self.asked: list[tuple] = []

    async def own_cert(self, space_id, epoch):
        self.asked.append((space_id, epoch))
        return self.cert


class _Cert:
    def to_wire(self):
        return {"cert_suite": "ed25519", "scope": "write"}


def _svc(**kw):
    gfs = _MemberGfs()
    cache = kw.pop("cache", _Cache())
    svc = SpaceAuthorProfileOutbound(
        bus=kw.pop("bus", EventBus()),
        space_repo=kw.pop("spaces", _Spaces()),  # type: ignore[arg-type]
        user_repo=kw.pop("users", _Users()),  # type: ignore[arg-type]
        picture_repo=kw.pop("pictures", _Pictures(user_pic=_webp())),  # type: ignore[arg-type]
        federation_repo=kw.pop("federation", _Federation()),  # type: ignore[arg-type]
        space_crypto=kw.pop("crypto", _Crypto()),  # type: ignore[arg-type]
        member_gfs=gfs,  # type: ignore[arg-type]
        own_instance_id=OWN,
        own_instance_pk=KP.public_key,
        own_identity_seed=KP.private_key,
        picture_cache=kw.pop("picture_cache", None),
        preferences_repo=kw.pop("prefs", _Prefs()),  # type: ignore[arg-type]
    )
    svc.attach_profile_cache(cache, writer_certs=kw.pop("certs", _Certs(_Cert())))  # type: ignore[arg-type]
    return svc, gfs


async def _pub(svc, space_id, user_id) -> bool:
    out = await svc.publish(space_id, user_id)
    await svc._member_gfs.flush()
    return out


def _sent(gfs, i: int = -1) -> dict:
    space_id, author, item_type, inner = gfs.items[i]
    assert (space_id, author, item_type) == ("sp", NOOR, "author_profile")
    assert verify_signed_item_inner(inner)  # the sidecar is outside it
    assert inner["item_target"] == inner["post_id"] == NOOR
    return inner


def _profile(gfs, i: int = -1):
    return decode_profile_content(_sent(gfs, i)["content"])


def _picture(gfs, i: int = -1) -> bytes | None:
    inner = _sent(gfs, i)
    return decode_picture_sidecar(
        inner.get(PROFILE_PICTURE_FIELD), _profile(gfs, i).picture_hash
    )


async def test_publishes_a_signed_profile_with_name_bio_and_a_small_picture():
    svc, gfs = _svc()
    assert await _pub(svc, "sp", NOOR)
    p = _profile(gfs)
    assert (p.display_name, p.bio) == ("Noor Haddad", "Beekeeper.")
    picture = _picture(gfs)
    assert picture is not None and len(picture) <= PROFILE_PICTURE_MAX_BYTES
    assert p.picture_hash == profile_picture_digest(picture)
    with Image.open(io.BytesIO(picture)) as im:
        assert max(im.size) <= 256


async def test_the_per_space_name_and_picture_win():
    spaces = _Spaces(member=SimpleNamespace(space_display_name="Noor (Imkerei)"))
    svc, gfs = _svc(
        spaces=spaces, pictures=_Pictures(user_pic=_webp(), member_pic=_webp(100))
    )
    await _pub(svc, "sp", NOOR)
    assert _profile(gfs).display_name == "Noor (Imkerei)"
    with Image.open(io.BytesIO(_picture(gfs))) as im:
        assert im.size == (100, 100)


async def test_a_protected_account_sends_its_name_only():
    svc, gfs = _svc()
    svc.attach_child_protection(_Protected({NOOR}))  # type: ignore[arg-type]
    await _pub(svc, "sp", NOOR)
    p = _profile(gfs)
    assert (p.display_name, p.bio, p.picture_hash) == ("Noor Haddad", None, None)
    assert PROFILE_PICTURE_FIELD not in _sent(gfs)


async def test_a_person_who_hides_their_profile_sends_their_name_only():
    """M6: "Show my picture and bio to followers of global spaces" off —
    the item carries the name only, like a protected account."""
    svc, gfs = _svc(prefs=_Prefs({NOOR}))
    assert await _pub(svc, "sp", NOOR)
    p = _profile(gfs)
    assert (p.display_name, p.bio, p.picture_hash) == ("Noor Haddad", None, None)
    assert PROFILE_PICTURE_FIELD not in _sent(gfs)


async def test_hiding_the_profile_republishes_name_only_and_back_on_the_full():
    """M6: flipping the switch replaces the published profile in every
    space it went to; flipping it back republishes the full profile (with
    the picture bytes, which the follower no longer holds)."""
    bus = EventBus()
    prefs = _Prefs()
    cache = _Cache()
    svc, gfs = _svc(bus=bus, prefs=prefs, cache=cache)
    svc.wire()
    assert await _pub(svc, "sp", NOOR)
    assert await _pub(svc, "sp2", NOOR)
    assert len(gfs.items) == 2
    prefs.hidden.add(NOOR)
    await bus.publish(
        UserPreferencesChanged(
            user_id=NOOR, changed={"hide_profile_from_followers": True}
        )
    )
    await gfs.flush()
    assert len(gfs.items) == 4
    spaces = set()
    for _space, _author, _type, inner in gfs.items[2:]:
        spaces.add(_space)
        p = decode_profile_content(inner["content"])
        assert (p.bio, p.picture_hash) == (None, None)
        assert PROFILE_PICTURE_FIELD not in inner
    assert spaces == {"sp", "sp2"}
    assert cache.rows[("sp", NOOR)][1] is None  # the bundle forgets the picture
    prefs.hidden.clear()
    await bus.publish(
        UserPreferencesChanged(
            user_id=NOOR, changed={"hide_profile_from_followers": False}
        )
    )
    await gfs.flush()
    assert len(gfs.items) == 6
    for _space, _author, _type, inner in gfs.items[4:]:
        p = decode_profile_content(inner["content"])
        assert p.bio == "Beekeeper."
        assert p.picture_hash is not None
        assert PROFILE_PICTURE_FIELD in inner


async def test_another_preference_change_republishes_nothing():
    bus = EventBus()
    prefs = _Prefs()
    svc, gfs = _svc(bus=bus, prefs=prefs)
    svc.wire()
    assert await _pub(svc, "sp", NOOR)
    await bus.publish(
        UserPreferencesChanged(user_id=NOOR, changed={"hide_bazaar": True})
    )
    await gfs.flush()
    # The same switch reported again with nothing to change: deduped.
    await bus.publish(
        UserPreferencesChanged(
            user_id=NOOR, changed={"hide_profile_from_followers": False}
        )
    )
    await gfs.flush()
    assert len(gfs.items) == 1


async def test_a_content_key_rotation_sends_nothing():
    """REGRESSION (H2): every rotation re-sent each author's full profile
    to all followers (in-memory dedupe keyed by epoch) ≈ 200 × 10 000 ×
    64 KiB. A follower who joins later gets the item, under its original
    epoch, in the host's bundle."""
    crypto = _Crypto()
    svc, gfs = _svc(crypto=crypto)
    assert await _pub(svc, "sp", NOOR)
    crypto.epoch = 4
    assert not await _pub(svc, "sp", NOOR)
    assert len(gfs.items) == 1


async def test_a_restart_sends_nothing():
    """REGRESSION (H2): a restarted household (a fresh service, the same
    persisted cache) re-sent every author's profile on their next post."""
    cache = _Cache()
    svc, _gfs = _svc(cache=cache)
    assert await _pub(svc, "sp", NOOR)
    restarted, gfs2 = _svc(cache=cache)
    assert not await _pub(restarted, "sp", NOOR)
    assert gfs2.items == []


async def test_a_name_change_sends_no_picture_bytes_a_new_picture_does():
    users = _Users()
    pictures = _Pictures(user_pic=_webp())
    svc, gfs = _svc(users=users, pictures=pictures)
    assert await _pub(svc, "sp", NOOR)
    assert PROFILE_PICTURE_FIELD in _sent(gfs)
    users.user.display_name = "Noor H."
    assert await _pub(svc, "sp", NOOR)
    assert _profile(gfs).display_name == "Noor H."
    assert PROFILE_PICTURE_FIELD not in _sent(gfs)  # the 1–4 KiB bucket
    assert _profile(gfs).picture_hash == _profile(gfs, 0).picture_hash
    pictures.user_pic = _webp(300, noisy=True)
    assert await _pub(svc, "sp", NOOR)
    assert _picture(gfs) is not None


async def test_the_public_picture_is_re_encoded_once_per_source(monkeypatch):
    """M4: the profile is checked against what was sent BEFORE anything new
    is encoded, and the public re-encode is cached per source picture."""
    calls: list[bytes] = []

    class _Counting(PublicPictureCache):
        async def get(self, source_hash, raw):
            if source_hash not in self._entries:
                calls.append(raw)
            return await super().get(source_hash, raw)

    svc, gfs = _svc(picture_cache=_Counting())
    for _ in range(4):
        await _pub(svc, "sp", NOOR)
    assert len(calls) == 1 and len(gfs.items) == 1


async def test_a_profile_change_republishes_only_where_sent_and_only_if_changed():
    bus = EventBus()
    users = _Users()
    svc, gfs = _svc(bus=bus, users=users)
    svc.wire()
    post = Post(
        id="p1",
        author=NOOR,
        type=PostType.TEXT,
        created_at=datetime.now(UTC),
        content="x",
    )
    await bus.publish(SpacePostCreated(post=post, space_id="sp"))
    await gfs.flush()
    assert len(gfs.items) == 1
    event = UserProfileUpdated(
        user_id=NOOR,
        username="noor",
        display_name="Noor Haddad",
        bio="Beekeeper.",
        picture_hash=None,
    )
    await bus.publish(event)  # no change → nothing new
    await gfs.flush()
    assert len(gfs.items) == 1
    users.user.bio = "Beekeeper and baker."
    await bus.publish(event)
    await gfs.flush()
    assert len(gfs.items) == 2
    assert _profile(gfs).bio == "Beekeeper and baker."
    # A per-space edit in a space where nothing was sent: nothing.
    await bus.publish(
        SpaceMemberProfileUpdated(
            space_id="other", user_id=NOOR, space_display_name="N", picture_hash=None
        )
    )
    assert len(gfs.items) == 2


async def test_a_profile_change_after_a_restart_still_republishes():
    """The spaces a profile went to are the persisted cache rows, not an
    in-memory map a restart forgets."""
    cache = _Cache()
    users = _Users()
    svc, _gfs = _svc(cache=cache, users=users)
    assert await _pub(svc, "sp", NOOR)
    bus = EventBus()
    restarted, gfs = _svc(cache=cache, users=users, bus=bus)
    restarted.wire()
    users.user.display_name = "Noor H."
    await bus.publish(
        UserProfileUpdated(
            user_id=NOOR,
            username="noor",
            display_name="Noor H.",
            bio="Beekeeper.",
            picture_hash=None,
        )
    )
    assert [_profile(gfs).display_name] == ["Noor H."]


async def test_an_inbound_post_publishes_nothing():
    bus = EventBus()
    svc, gfs = _svc(bus=bus)
    svc.wire()
    post = Post(
        id="p1",
        author=NOOR,
        type=PostType.TEXT,
        created_at=datetime.now(UTC),
        content="x",
    )
    await bus.publish(
        SpacePostCreated(post=post, space_id="sp", origin_instance_id="peer")
    )
    assert gfs.items == []


async def test_only_spaces_that_admit_followers():
    for spaces in (
        _Spaces(space_type=SpaceType.PRIVATE),
        _Spaces(allow_subscribers=False),
    ):
        svc, gfs = _svc(spaces=spaces)
        assert not await _pub(svc, "sp", NOOR)
        assert gfs.items == []


async def test_gated_on_the_known_member_households_versions():
    spaces = _Spaces(instances=[OWN, "peer-new", "peer-old", "mesh-only"])
    svc, gfs = _svc(
        spaces=spaces, federation=_Federation({"peer-new": 60, "peer-old": 59})
    )
    assert not await _pub(svc, "sp", NOOR)
    assert gfs.items == []
    svc, gfs = _svc(
        spaces=spaces, federation=_Federation({"peer-new": 60, "peer-old": 60})
    )
    assert await _pub(svc, "sp", NOOR)


async def test_an_unreadable_picture_is_left_out():
    svc, gfs = _svc(pictures=_Pictures(user_pic=b"not an image"))
    await _pub(svc, "sp", NOOR)
    assert _profile(gfs).picture_hash is None
    assert PROFILE_PICTURE_FIELD not in _sent(gfs)


async def test_a_noisy_picture_still_fits_the_cap():
    svc, gfs = _svc(pictures=_Pictures(user_pic=_webp(600, noisy=True)))
    await _pub(svc, "sp", NOOR)
    pic = _picture(gfs)
    assert pic is None or len(pic) <= PROFILE_PICTURE_MAX_BYTES


async def test_an_unknown_author_publishes_nothing():
    svc, gfs = _svc()
    assert not await _pub(svc, "sp", "someone-else")
    assert gfs.items == []


async def test_without_the_cache_nothing_is_published():
    svc = SpaceAuthorProfileOutbound(
        bus=EventBus(),
        space_repo=_Spaces(),  # type: ignore[arg-type]
        user_repo=_Users(),  # type: ignore[arg-type]
        picture_repo=_Pictures(user_pic=_webp()),  # type: ignore[arg-type]
        federation_repo=_Federation(),  # type: ignore[arg-type]
        space_crypto=_Crypto(),  # type: ignore[arg-type]
        member_gfs=_MemberGfs(),  # type: ignore[arg-type]
        own_instance_id=OWN,
        own_instance_pk=KP.public_key,
        own_identity_seed=KP.private_key,
        preferences_repo=_Prefs(),  # type: ignore[arg-type]
    )
    assert not await _pub(svc, "sp", NOOR)
    svc.wire()
    await svc._on_preferences(
        UserPreferencesChanged(
            user_id=NOOR, changed={"hide_profile_from_followers": True}
        )
    )


# ── The host's copy for a new follower's bundle (v_61) ──────────────


async def test_a_seed_holder_keeps_the_item_signed_for_its_bundle():
    """A household never receives its own items back, so a host keeps what
    it sends: author-signed, with our writer cert and its epoch and the
    original picture, as a follower receives it."""
    cache = _Cache()
    certs = _Certs(_Cert())
    svc, gfs = _svc(cache=cache, certs=certs, spaces=_Spaces(seed=b"s" * 32))
    assert await _pub(svc, "sp", NOOR)
    [(profile, picture, signed, epoch)] = cache.writes
    sent = _sent(gfs)
    assert signed == {**sent, "writer_cert": _Cert().to_wire()}
    assert verify_signed_item_inner(signed)
    assert epoch == 3 and certs.asked == [("sp", 3)]
    assert profile.author_pk == KP.public_key.hex()
    assert picture is not None and profile.picture_hash == profile_picture_digest(
        picture
    )


async def test_a_seed_holder_keeps_the_picture_with_a_name_only_change():
    cache = _Cache()
    users = _Users()
    svc, _gfs = _svc(cache=cache, users=users, spaces=_Spaces(seed=b"s" * 32))
    await _pub(svc, "sp", NOOR)
    users.user.display_name = "Noor H."
    await _pub(svc, "sp", NOOR)
    signed = cache.writes[-1][2]
    assert PROFILE_PICTURE_FIELD in signed  # the bundle still has the bytes


async def test_a_household_without_the_seed_keeps_no_signed_item():
    """M2: only a host builds bundles."""
    cache = _Cache()
    svc, _gfs = _svc(cache=cache)
    assert await _pub(svc, "sp", NOOR)
    [(profile, _picture, signed, epoch)] = cache.writes
    assert profile.display_name == "Noor Haddad"
    assert (signed, epoch) == (None, None)


async def test_without_a_writer_cert_only_the_profile_is_cached():
    cache = _Cache()
    svc, _gfs = _svc(cache=cache, certs=_Certs(None), spaces=_Spaces(seed=b"s" * 32))
    assert await _pub(svc, "sp", NOOR)
    [(_profile_row, _picture, signed, epoch)] = cache.writes
    assert (signed, epoch) == (None, None)


async def test_a_profile_is_recorded_as_sent_only_once_a_gfs_took_it():
    """REGRESSION (review I2): the persisted dedupe used to say "sent" the
    moment a background task started. When no GFS took the item (no cert yet
    after a rotation, a GFS down), the profile was then never sent again."""
    cache = _Cache()
    svc, gfs = _svc(cache=cache)
    gfs.fail = True
    assert await _pub(svc, "sp", NOOR)
    assert cache.rows == {}
    gfs.fail = False
    assert await _pub(svc, "sp", NOOR)  # the next trigger sends it again
    assert len(gfs.items) == 2 and len(cache.rows) == 1


async def test_someone_who_is_not_a_member_publishes_nothing():
    """Review I1: a profile change must not publish into a space the
    person left."""
    svc, gfs = _svc(spaces=_Spaces(member=None))
    assert not await _pub(svc, "sp", NOOR)
    assert gfs.items == []


async def test_leaving_a_space_forgets_the_published_profile():
    """Review I1: a departed member's profile is not forwarded to new
    followers from the host's cache."""
    bus = EventBus()
    cache = _Cache()
    svc, _gfs = _svc(cache=cache, bus=bus)
    svc.wire()
    assert await _pub(svc, "sp", NOOR)
    await bus.publish(SpaceMemberLeft(space_id="sp", user_id=NOOR))
    assert cache.rows == {}
