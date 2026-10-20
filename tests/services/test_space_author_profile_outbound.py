"""SpaceAuthorProfileOutbound — our authors' profiles for a space's GFS
followers (v_60)."""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from PIL import Image

from socialhome.crypto import (
    derive_instance_id,
    derive_user_id,
    generate_identity_keypair,
)
from socialhome.domain.events import (
    SpaceMemberProfileUpdated,
    SpacePostCreated,
    UserProfileUpdated,
)
from socialhome.domain.post import Post, PostType
from socialhome.domain.space import SpaceType
from socialhome.domain.space_author_profile import (
    PROFILE_PICTURE_MAX_BYTES,
    decode_profile_content,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services import space_author_profile_outbound as mod
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
    member: object | None = None
    instances: list[str] = field(default_factory=list)

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
        return (self.member_pic, "m") if self.member_pic else None

    async def get_user_picture(self, user_id):
        return (self.user_pic, "u") if self.user_pic else None


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
    def __init__(self) -> None:
        self.items: list[tuple] = []

    def schedule_item(self, space_id, author_user_id, item_type, inner):
        self.items.append((space_id, author_user_id, item_type, inner))
        return True


class _Protected:
    def __init__(self, ids) -> None:
        self.ids = ids

    def register_gate(self, gate):
        pass

    async def is_protected(self, user_id):
        return user_id in self.ids


def _svc(**kw):
    gfs = _MemberGfs()
    svc = SpaceAuthorProfileOutbound(
        bus=kw.pop("bus", EventBus()),
        space_repo=kw.pop("spaces", _Spaces()),  # type: ignore[arg-type]
        user_repo=_Users(),  # type: ignore[arg-type]
        picture_repo=kw.pop("pictures", _Pictures(user_pic=_webp())),  # type: ignore[arg-type]
        federation_repo=kw.pop("federation", _Federation()),  # type: ignore[arg-type]
        space_crypto=kw.pop("crypto", _Crypto()),  # type: ignore[arg-type]
        member_gfs=gfs,  # type: ignore[arg-type]
        own_instance_id=OWN,
        own_instance_pk=KP.public_key,
        own_identity_seed=KP.private_key,
    )
    return svc, gfs


def _profile(gfs, i: int = -1):
    space_id, author, item_type, inner = gfs.items[i]
    assert (space_id, author, item_type) == ("sp", NOOR, "author_profile")
    assert verify_signed_item_inner(inner)
    assert inner["item_target"] == inner["post_id"] == NOOR
    return decode_profile_content(inner["content"])


async def test_publishes_a_signed_profile_with_name_bio_and_a_small_picture():
    svc, gfs = _svc()
    assert await svc.publish("sp", NOOR)
    p = _profile(gfs)
    assert (p.display_name, p.bio) == ("Noor Haddad", "Beekeeper.")
    assert p.picture_webp is not None
    assert len(p.picture_webp) <= PROFILE_PICTURE_MAX_BYTES
    with Image.open(io.BytesIO(p.picture_webp)) as im:
        assert max(im.size) <= 256


async def test_the_per_space_name_and_picture_win():
    spaces = _Spaces(member=SimpleNamespace(space_display_name="Noor (Imkerei)"))
    member_pic = _webp(100)
    svc, gfs = _svc(
        spaces=spaces, pictures=_Pictures(user_pic=_webp(), member_pic=member_pic)
    )
    await svc.publish("sp", NOOR)
    p = _profile(gfs)
    assert p.display_name == "Noor (Imkerei)"
    with Image.open(io.BytesIO(p.picture_webp)) as im:
        assert im.size == (100, 100)


async def test_a_protected_account_sends_its_name_only():
    svc, gfs = _svc()
    svc.attach_child_protection(_Protected({NOOR}))  # type: ignore[arg-type]
    await svc.publish("sp", NOOR)
    p = _profile(gfs)
    assert (p.display_name, p.bio, p.picture_webp) == ("Noor Haddad", None, None)


async def test_once_per_epoch_then_again_on_a_new_epoch_or_a_day_later(monkeypatch):
    crypto = _Crypto()
    svc, gfs = _svc(crypto=crypto)
    assert await svc.publish("sp", NOOR)
    assert not await svc.publish("sp", NOOR)
    crypto.epoch = 4
    assert await svc.publish("sp", NOOR)
    assert not await svc.publish("sp", NOOR)
    later = datetime.now(UTC) + timedelta(hours=25)

    class _Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return later

    monkeypatch.setattr(mod, "datetime", _Later)
    assert await svc.publish("sp", NOOR)
    assert len(gfs.items) == 3


async def test_a_profile_change_republishes_only_where_sent_and_only_if_changed():
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
    await bus.publish(SpacePostCreated(post=post, space_id="sp"))
    assert len(gfs.items) == 1
    # No change → nothing new.
    await bus.publish(
        UserProfileUpdated(
            user_id=NOOR,
            username="noor",
            display_name="Noor Haddad",
            bio="Beekeeper.",
            picture_hash=None,
        )
    )
    assert len(gfs.items) == 1
    svc._users.user.bio = "Beekeeper and baker."  # type: ignore[attr-defined]
    await bus.publish(
        UserProfileUpdated(
            user_id=NOOR,
            username="noor",
            display_name="Noor Haddad",
            bio="Beekeeper and baker.",
            picture_hash=None,
        )
    )
    assert len(gfs.items) == 2
    assert _profile(gfs).bio == "Beekeeper and baker."
    # A per-space edit in another space where nothing was sent: nothing.
    await bus.publish(
        SpaceMemberProfileUpdated(
            space_id="other", user_id=NOOR, space_display_name="N", picture_hash=None
        )
    )
    assert len(gfs.items) == 2


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
        assert not await svc.publish("sp", NOOR)
        assert gfs.items == []


async def test_gated_on_the_known_member_households_versions():
    spaces = _Spaces(instances=[OWN, "peer-new", "peer-old", "mesh-only"])
    svc, gfs = _svc(
        spaces=spaces, federation=_Federation({"peer-new": 60, "peer-old": 59})
    )
    assert not await svc.publish("sp", NOOR)
    assert gfs.items == []
    svc, gfs = _svc(
        spaces=spaces, federation=_Federation({"peer-new": 60, "peer-old": 60})
    )
    assert await svc.publish("sp", NOOR)


async def test_an_unreadable_picture_is_left_out():
    svc, gfs = _svc(pictures=_Pictures(user_pic=b"not an image"))
    await svc.publish("sp", NOOR)
    assert _profile(gfs).picture_webp is None


async def test_a_noisy_picture_still_fits_the_cap():
    svc, gfs = _svc(pictures=_Pictures(user_pic=_webp(600, noisy=True)))
    await svc.publish("sp", NOOR)
    pic = _profile(gfs).picture_webp
    assert pic is None or len(pic) <= PROFILE_PICTURE_MAX_BYTES


async def test_an_unknown_author_publishes_nothing():
    svc, gfs = _svc()
    assert not await svc.publish("sp", "someone-else")
    assert gfs.items == []
