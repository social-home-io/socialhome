"""A received space file's name here is bound to the household that sent it."""

from __future__ import annotations

from socialhome.domain.file_hashes import FileHash, space_content_media_name
from socialhome.domain.space_media_ref import SpaceMediaLocalization
from socialhome.services.inbound_media_store import (
    SPACE_DIRECT_MEDIA_PREFIX,
    is_reserved_media_name,
    is_safe_media_name,
    is_space_direct_media_name,
    space_direct_media_name,
)
from socialhome.services.space_media_names import (
    SpaceMediaNamer,
    feature_media_urls,
    renamed_media,
    same_file,
    space_own_files,
)

SP = "sp-1"


def sd(origin: str, name: str) -> str:
    return space_direct_media_name(SP, origin, name)


def test_the_name_binds_space_household_and_file_never_the_receiver():
    a = sd("hh-a", "f.webp")
    assert a.startswith(SPACE_DIRECT_MEDIA_PREFIX) and a.endswith(".webp")
    assert is_safe_media_name(a) and not is_reserved_media_name(a)
    assert is_space_direct_media_name(a) and not is_space_direct_media_name("f.webp")
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
    assert feature_media_urls("stickies", post) == []
    assert renamed_media("stickies", post, str.upper) == post


def test_a_pages_cover_and_body_pictures_are_listed_and_renamed():
    """A page's files are its cover and every ``api/media/`` picture in its
    body: a create, an edit's patch, a merged conflict resolution and the
    wire event of a release all list them, and a rename rewrites the body
    references in place (prefix and query kept, other text untouched)."""
    body = (
        "Intro ![a](api/media/a.webp) and ![b](/api/media/b.webp?exp=1&sig=x)\n"
        "a [link](https://example.org/api/media/x) stays text"
    )
    create = {"title": "T", "content": body, "cover_image_url": "api/media/c.webp"}
    assert feature_media_urls("pages", create) == [
        "api/media/c.webp",
        "api/media/a.webp",
        "/api/media/b.webp",
    ]
    out = renamed_media("pages", create, lambda u: (u or "").replace("media/", "M/"))
    assert out["cover_image_url"] == "api/M/c.webp"
    assert out["content"] == (
        "Intro ![a](api/M/a.webp) and ![b](/api/M/b.webp?exp=1&sig=x)\n"
        "a [link](https://example.org/api/media/x) stays text"
    )
    assert create["content"] == body  # a new dict
    edit = {"patch": {"content": "![n](api/media/n.webp)", "title": "T2"}}
    assert feature_media_urls("pages", edit) == ["api/media/n.webp"]
    assert renamed_media("pages", edit, str.upper)["patch"] == {
        "content": "![n](API/MEDIA/N.WEBP)",
        "title": "T2",
    }
    cover_only = {"patch": {"cover_image_url": "api/media/e.webp"}}
    assert feature_media_urls("pages", cover_only) == ["api/media/e.webp"]
    resolve = {"op": "resolve_conflict", "merged_content": "![m](api/media/m.webp)"}
    assert feature_media_urls("pages", resolve) == ["api/media/m.webp"]
    assert renamed_media("pages", resolve, str.upper)["merged_content"] == (
        "![m](API/MEDIA/M.WEBP)"
    )
    assert feature_media_urls("pages", {"title": "T", "content": None}) == []
    # A rename to nothing empties the reference (the sanitizer's drop).
    gone = renamed_media("pages", create, lambda u: None)
    assert gone["cover_image_url"] is None
    assert gone["content"].startswith("Intro ![a]() and ![b]()")


def test_a_page_namer_keeps_the_names_its_stored_page_shows():
    """An edit of a page carries the pictures the page already shows under
    the names it shows them by (the host's): those stay, only a new file
    takes the submitting household's name."""
    namer = SpaceMediaNamer("us")
    shown = f"api/media/{sd('hh-a', 'a.webp')}"
    patch = {"patch": {"content": f"![a]({shown}) ![n](api/media/n.webp)"}}
    names = namer.localize(SP, "hh-a", feature_media_urls("pages", patch), held=[shown])
    out = renamed_media("pages", patch, names.url)
    assert out["patch"]["content"] == (
        f"![a]({shown}) ![n](api/media/{sd('hh-a', 'n.webp')})"
    )


def test_same_file_matches_a_sent_name_to_the_one_held_here():
    held = f"api/media/{sd('hh-a', 'f.webp')}"
    assert same_file(held, "api/media/f.webp", space_id=SP, origin="hh-a")
    assert same_file(held, held, space_id=SP, origin="hh-x")
    assert same_file(None, None, space_id=SP, origin="hh-a")
    assert not same_file(held, "api/media/f.webp", space_id=SP, origin="hh-m")
    assert not same_file(held, "api/media/g.webp", space_id=SP, origin="hh-a")
    assert not same_file(held, None, space_id=SP, origin="hh-a")
    assert not same_file(None, "api/media/f.webp", space_id=SP, origin="hh-a")
    assert not same_file(held, 7, space_id=SP, origin="hh-a")


async def test_only_the_spaces_own_files_ship():
    """What a host ships for a page: files no row outside the space names
    here; without the repo, nothing (fail closed)."""

    class _Refs:
        async def confined_to_space(self, name, space_id):
            return space_id == SP and name != "home.webp"

    urls = ["api/media/a.webp", "/api/media/home.webp?x=1", "api/media/b.webp"]
    assert await space_own_files(_Refs(), SP, urls) == [
        "api/media/a.webp",
        "api/media/b.webp",
    ]
    assert await space_own_files(None, SP, urls) == []


# ── v_85: content-addressed names for hashed files ─────────────────────────

_HA = "a" * 64
_HM = "b" * 64


def _sc(h: str, name: str = "f.webp", space: str = SP) -> str:
    return space_content_media_name(space, h, name)


def _fh(h: str) -> FileHash:
    return FileHash(h, 3)


def test_a_hashed_file_takes_its_content_name_whoever_delivers_it():
    namer = SpaceMediaNamer("us")
    url = "api/media/f.webp"
    by_a = namer.localize(SP, "hh-a", [url], hashes={"f.webp": _fh(_HA)})
    by_host = namer.localize(SP, "host", [url], hashes={"f.webp": _fh(_HA)})
    assert by_a.names == {"f.webp": _sc(_HA)} == by_host.names
    # Unhashed (legacy) files keep the sender-bound sd- names.
    legacy = namer.localize(SP, "hh-a", [url, "api/media/g.webp"], hashes={})
    assert legacy.names == {
        "f.webp": sd("hh-a", "f.webp"),
        "g.webp": sd("hh-a", "g.webp"),
    }


def test_a_decoy_item_naming_anothers_file_resolves_to_its_own_bytes():
    """Member M posts an item naming A's file name with M's own bytes: M's
    item points at M's content name, A's at A's — never each other's."""
    namer = SpaceMediaNamer("us")
    url = "api/media/f.webp"
    a_item = namer.localize(SP, "hh-a", [url], hashes={"f.webp": _fh(_HA)})
    m_item = namer.localize(SP, "hh-m", [url], hashes={"f.webp": _fh(_HM)})
    assert a_item.url(url) == f"api/media/{_sc(_HA)}"
    assert m_item.url(url) == f"api/media/{_sc(_HM)}"


def test_a_cross_space_hash_never_matches():
    namer = SpaceMediaNamer("us")
    url = "api/media/f.webp"
    one = namer.localize("sp-1", "hh-a", [url], hashes={"f.webp": _fh(_HA)})
    two = namer.localize("sp-2", "hh-a", [url], hashes={"f.webp": _fh(_HA)})
    assert one.names["f.webp"] != two.names["f.webp"]
    assert two.names["f.webp"] == _sc(_HA, space="sp-2")


def test_a_raw_content_name_is_never_kept_as_is():
    namer = SpaceMediaNamer("us")
    raw = _sc(_HA)
    url = f"api/media/{raw}"
    # Hashed: re-derived from the hash the item claims — never the raw name.
    hashed = namer.localize(SP, "host", [url], hashes={raw: _fh(_HM)})
    assert hashed.names == {raw: _sc(_HM, raw)}
    # Unhashed: rebound to its sender like any legacy name.
    legacy = namer.localize(SP, "hh-m", [url])
    assert legacy.names == {raw: sd("hh-m", raw)}


def test_held_and_own_files_keep_their_names_over_a_hash():
    namer = SpaceMediaNamer("us")
    got = namer.localize(
        SP,
        "hh-a",
        ["api/media/sd-held.webp", f"api/media/{sd('us', 'mine.webp')}"],
        held=["api/media/sd-held.webp", "api/media/mine.webp"],
        hashes={"sd-held.webp": _fh(_HA), sd("us", "mine.webp"): _fh(_HM)},
    )
    # The held file stays; our own file keeps the name it has here.
    assert got.names == {sd("us", "mine.webp"): "mine.webp"}


def test_same_file_knows_the_content_name():
    assert same_file(
        f"api/media/{_sc(_HA)}",
        "api/media/f.webp",
        space_id=SP,
        origin="hh-a",
        hashes={"f.webp": _fh(_HA)},
    )
    assert not same_file(
        f"api/media/{_sc(_HA)}",
        "api/media/f.webp",
        space_id=SP,
        origin="hh-a",
        hashes={"f.webp": _fh(_HM)},
    )


def test_a_legacy_row_keeps_its_senders_file_when_an_edit_carries_hashes():
    namer = SpaceMediaNamer("us")
    got = namer.localize(
        SP,
        "hh-a",
        ["api/media/f.webp"],
        held=[f"api/media/{sd('hh-a', 'f.webp')}"],
        hashes={"f.webp": _fh(_HA)},
    )
    assert got.names == {"f.webp": sd("hh-a", "f.webp")}
