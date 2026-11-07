"""A received space file's name here is bound to the household that sent it."""

from __future__ import annotations

from socialhome.domain.space_media_ref import SpaceMediaLocalization
from socialhome.services.inbound_media_store import (
    SPACE_DIRECT_MEDIA_PREFIX,
    is_reserved_media_name,
    is_safe_media_name,
    space_direct_media_name,
)
from socialhome.services.space_media_names import (
    SpaceMediaNamer,
    feature_media_urls,
    renamed_media,
)

SP = "sp-1"


def sd(origin: str, name: str) -> str:
    return space_direct_media_name(SP, origin, name)


def test_the_name_binds_space_household_and_file_never_the_receiver():
    a = sd("hh-a", "f.webp")
    assert a.startswith(SPACE_DIRECT_MEDIA_PREFIX) and a.endswith(".webp")
    assert is_safe_media_name(a) and not is_reserved_media_name(a)
    assert a != sd("hh-m", "f.webp")
    assert a != sd("hh-a", "g.webp")
    assert a != space_direct_media_name("sp-2", "hh-a", "f.webp")
    # Relayable: a received sd- name maps again, to another name.
    assert sd("host", a) != a


def test_every_file_a_household_delivers_takes_its_name():
    loc = SpaceMediaNamer().localize(
        SP, "hh-a", ["api/media/f.webp", "/api/media/g.webp?x=1", None, ""]
    )
    assert loc.url("api/media/f.webp") == f"api/media/{sd('hh-a', 'f.webp')}"
    assert loc.url("/api/media/g.webp?x=1") == f"/api/media/{sd('hh-a', 'g.webp')}?x=1"


def test_reserved_names_are_left_to_the_sanitizers():
    loc = SpaceMediaNamer().localize(SP, "hh-a", ["api/media/dm-x.webp"])
    assert loc.names == {}


def test_no_origin_or_space_names_nothing():
    assert SpaceMediaNamer().localize("", "hh-a", ["api/media/f.webp"]).names == {}
    assert SpaceMediaNamer().localize(SP, "", ["api/media/f.webp"]).names == {}


def test_a_held_row_keeps_the_file_it_has():
    """The same file under the name it has here: verbatim (a relay of a file
    from the same origin), the sender's own name mapped, or our own file as
    another household calls it."""
    namer = SpaceMediaNamer(own_instance_id=lambda: "us")
    held = [f"api/media/{sd('hh-a', 'f.webp')}", "api/media/mine.webp"]
    loc = namer.localize(
        SP,
        "hh-w",
        [
            f"api/media/{sd('hh-a', 'f.webp')}",  # W relays A's file
            f"api/media/{sd('us', 'mine.webp')}",  # W names our own file
            "api/media/new.webp",  # a new cover W set
        ],
        held=held,
    )
    assert loc.url(f"api/media/{sd('hh-a', 'f.webp')}") == held[0]
    assert loc.url(f"api/media/{sd('us', 'mine.webp')}") == "api/media/mine.webp"
    assert loc.url("api/media/new.webp") == f"api/media/{sd('hh-w', 'new.webp')}"
    # Unchanged files from the household that delivered the row: mapped to
    # the very name held.
    same = namer.localize(
        SP, "hh-a", ["api/media/f.webp"], held=[f"api/media/{sd('hh-a', 'f.webp')}"]
    )
    assert same.url("api/media/f.webp") == f"api/media/{sd('hh-a', 'f.webp')}"


def test_without_our_own_id_our_file_is_just_another_name():
    loc = SpaceMediaNamer().localize(
        SP, "hh-w", [f"api/media/{sd('us', 'm.webp')}"], held=["api/media/m.webp"]
    )
    assert loc.url(f"api/media/{sd('us', 'm.webp')}") != "api/media/m.webp"


def test_gfs_names_take_precedence_and_keep_their_rows():
    direct = SpaceMediaLocalization(names={"a": "sd-a", "b": "sd-b"})
    gfs = SpaceMediaLocalization(names={"a": "sg-a"}, refs=())
    combined = SpaceMediaNamer.combine(direct, gfs)
    assert combined.names == {"a": "sg-a", "b": "sd-b"}
    assert combined.refs == gfs.refs


def test_moderation_payload_files_are_listed_and_renamed_in_place():
    post = {
        "media_url": "api/media/m.webp",
        "image_urls": ["api/media/i.webp"],
        "file_meta": {"url": "api/media/f.pdf", "name": "x"},
        "link_preview": {"thumbnail_url": "api/media/lp.webp"},
        "attachments": {"bazaar": {"image_urls": ["api/media/bz.webp"], "t": 1}},
        "content": "hi",
    }
    assert feature_media_urls("posts", post) == [
        "api/media/m.webp",
        "api/media/i.webp",
        "api/media/f.pdf",
        "api/media/lp.webp",
        "api/media/bz.webp",
    ]
    out = renamed_media("posts", post, lambda u: (u or "").upper())
    assert out["media_url"] == "API/MEDIA/M.WEBP"
    assert out["image_urls"] == ["API/MEDIA/I.WEBP"]
    assert out["file_meta"] == {"url": "API/MEDIA/F.PDF", "name": "x"}
    assert out["link_preview"] == {"thumbnail_url": "API/MEDIA/LP.WEBP"}
    assert out["attachments"]["bazaar"] == {"image_urls": ["API/MEDIA/BZ.WEBP"], "t": 1}
    assert out["content"] == "hi"
    assert post["media_url"] == "api/media/m.webp"  # a new dict

    create = {"cover_url": "api/media/c.webp"}
    edit = {"patch": {"cover_url": "api/media/e.webp", "summary": "s"}}
    assert feature_media_urls("calendar", create) == ["api/media/c.webp"]
    assert feature_media_urls("calendar", edit) == ["api/media/e.webp"]
    assert renamed_media("calendar", create, str.upper) == {
        "cover_url": "API/MEDIA/C.WEBP"
    }
    assert renamed_media("calendar", edit, str.upper)["patch"] == {
        "cover_url": "API/MEDIA/E.WEBP",
        "summary": "s",
    }
    assert feature_media_urls("pages", post) == []
    assert renamed_media("pages", post, str.upper) == post
