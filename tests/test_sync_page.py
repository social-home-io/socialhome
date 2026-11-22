"""``socialhome/sync_page.py`` — commit form, hashing, page sign / verify.

``docs/protocol/sync-pages.md`` §1–§3. The cross-household test (the host's
record and a member's re-export hash the same) is
``tests/federation/sync/space/test_page_state.py``.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import replace

import pytest

from socialhome.authority_sig import AUTHORITY_EVENT_SPACE_SYNC_PAGE
from socialhome.canonical_json import canonical_bytes
from socialhome.crypto import b64url_encode, ed25519_public_key
from socialhome.sync_page import (
    BUCKETS,
    COMMIT_SPECS,
    COMMITTED_RESOURCES,
    HINT_PREFIX_BYTES,
    MAX_HINT_LEAVES,
    PAGE_SUITE_SHA256_B256,
    SUPPORTED_PAGE_SUITES,
    VECTOR_BYTES,
    ListingRow,
    PageRefused,
    UnsupportedPageSuite,
    VectorBuilder,
    apply_delta,
    bucket_hash,
    change_time,
    commit_form,
    delta_from_bytes,
    delta_from_wire,
    delta_to_bytes,
    delta_to_wire,
    differing_buckets,
    fetched_record,
    fetched_row_to_wire,
    file_refs,
    hint_from_wire,
    hint_to_wire,
    is_conflict,
    is_superseded,
    leaf_of,
    listing_to_wire,
    page_time,
    parse_bucket_request,
    parse_fetch_request,
    parse_fetched_row,
    parse_listing,
    parse_page_ref,
    record_leaf,
    root_of,
    row_key,
    sign_page,
    superseded_key,
    vector_delta,
    vector_from_wire,
    vector_to_wire,
    verify_listing,
    verify_page,
)

SPACE = "sp-1"
SEED = bytes(range(32))
PK = ed25519_public_key(SEED)
NOW = 1_800_000_000_000


def _no_files(_url: str) -> str | None:
    return None


def _hashes(url: str) -> str | None:
    return hashlib.sha256(url.rsplit("/", 1)[-1].encode()).hexdigest()


def _vector(fill: int = 0) -> bytes:
    return bytes([fill]) * VECTOR_BYTES


def _page(**over):
    fields = {
        "space_id": SPACE,
        "epoch": 2,
        "signer": "host-1",
        "seq": 40,
        "issued_at": NOW,
        "cutoff": None,
        "rows": 3,
        "vector": _vector(),
        "prev_root": None,
        "space_seed": SEED,
    }
    fields.update(over)
    return sign_page(**fields)


def _verify(raw, **over):
    kw = {"space_id": SPACE, "space_public_key": PK, "pinned_epoch": 2, "now_ms": NOW}
    kw.update(over)
    return verify_page(raw, **kw)


# ── Suite, shape constants ───────────────────────────────────────────────


def test_suite_trio():
    assert PAGE_SUITE_SHA256_B256 == "sha256-b256-c1"
    assert SUPPORTED_PAGE_SUITES == frozenset({PAGE_SUITE_SHA256_B256})
    assert issubclass(UnsupportedPageSuite, ValueError)
    assert BUCKETS == 256 and VECTOR_BYTES == 8192
    assert AUTHORITY_EVENT_SPACE_SYNC_PAGE == "space_sync_page"


def test_members_bans_pictures_and_chat_are_host_only():
    """Owner decision 6 + §2: never committed in Phase 2."""
    for resource in (
        "bans",
        "members",
        "member_pictures",
        "chat_messages",
        "chat_messages_deleted",
    ):
        assert resource not in COMMITTED_RESOURCES
        assert commit_form(resource, {"id": "x"}, _no_files) is None


# ── Golden vectors: the bytes are pinned ─────────────────────────────────


def test_shape_golden_vectors():
    rkey = row_key("posts", "p-1")
    assert rkey.hex() == hashlib.sha256(b"sh-sync-rkey:v1:posts\x00p-1").hexdigest()
    form = {"r": "posts", "v": {"id": "p-1"}}
    assert (
        leaf_of(form)
        == hashlib.sha256(b'sh-sync-leaf:v1:{"r":"posts","v":{"id":"p-1"}}').digest()
    )
    a, b = b"\x02" * 32, b"\x01" * 32
    assert (
        bucket_hash(7, [a, b])
        == hashlib.sha256(b"sh-sync-bucket:v1:\x07" + b + a).digest()
    )
    vec = _vector(3)
    assert root_of(vec) == hashlib.sha256(b"sh-sync-root:v1:" + vec).digest()


def test_a_calendar_feed_card_is_never_committed():
    """Every household mints its own feed card for an announced event (own
    id, own text), so a page never commits to one — on any side."""
    card = {"id": "c1", "author": "u", "type": "text", "linked_event_id": "ev-1"}
    assert commit_form("posts", card, _no_files) is None
    assert commit_form("posts", {**card, "linked_event_id": None}, _no_files)


def test_post_commit_form_golden():
    record = {
        "id": "post-1",
        "author": "u-anna",
        "type": "text",
        "created_at": "2026-11-01T09:00:00+00:00",
        "content": "héllo",
        "media_url": None,
        "image_urls": ["api/media/b.webp", "api/media/a.webp"],
        "reactions": {"🎉": ["u-carl"]},
        "reaction_stamps": {"u-carl\x00🎉": ["2026-11-21 09:41:30", True]},
        "comment_count": 4,
        "pinned": True,
        "moderated": False,
        "location": {"lat": 52.1, "lon": 4.1},
        "bot_id": "bot-1",
        "linked_event_id": None,  # a feed card is never committed
        "linked_highlight_id": "h-1",
        "no_link_preview": True,
        "deleted": False,
        "edited_at": "2026-11-02 10:00:00",
        "file_meta": None,
        "hidden_from_feed": False,
        "link_preview": None,
    }
    form = commit_form("posts", record, _hashes)
    assert form == {
        "r": "posts",
        "v": {
            "id": "s:post-1",
            "space_id": None,
            "author": "s:u-anna",
            "type": "s:text",
            "content": "s:héllo",
            "created_at": "s:2026-11-01T09:00:00.000000+00:00",
            "edited_at": "s:2026-11-02T10:00:00.000000+00:00",
            "hidden_from_feed": False,
            "media_url": None,
            # Pictures in their order (review N4): order means something.
            "image_urls": ["s:" + _hashes("b.webp"), "s:" + _hashes("a.webp")],
            "file_meta": None,
            "link_preview": None,
        },
    }
    # The bytes: canonical JSON (sorted keys, ASCII escapes).
    assert b"h\\u00e9llo" in canonical_bytes(form)
    assert (
        leaf_of(form).hex()
        == hashlib.sha256(b"sh-sync-leaf:v1:" + canonical_bytes(form)).hexdigest()
    )


def test_leaf_golden_with_string_lists():
    """Pins the leaf bytes of a commit form with a string list (plain
    code-point order) — a change here is a new page suite."""
    task = {
        "id": "t-1",
        "title": "Dishes",
        "assignees": ["b", 'a"', "a#", "é"],
        "created_at": "2026-11-01 09:00:00",
    }
    form = commit_form("tasks", task, _no_files)
    assert form is not None
    assert form["v"]["assignees"] == ['s:a"', "s:a#", "s:b", "s:é"]
    assert (
        leaf_of(form).hex()
        == "930a53470de538e68dcf0d32a5efb0a3e41c9900b7df90bf0d65571e85a89ae1"
    )


def test_item_state_and_counts_never_change_a_leaf():
    """Rule 1 / 2: reactions, counts, receipt-local fields are uncommitted."""
    base = {"id": "p", "author": "u", "type": "text", "content": "x"}
    leaf = record_leaf("posts", base, _no_files)
    noisy = {
        **base,
        "reactions": {"x": ["a"]},
        "comment_count": 9,
        "pinned": True,
        "moderated": True,
        "location": {"lat": 1.0},
    }
    assert record_leaf("posts", noisy, _no_files) == leaf
    assert record_leaf("posts", {**base, "content": "y"}, _no_files) != leaf
    poll = {
        "post_id": "p",
        "meta": {"question": "Q", "closed": False},
        "options": [{"id": "a", "text": "A", "count": 0}],
    }
    voted = {
        **poll,
        "options": [{"id": "a", "text": "A", "count": 5}],
        "vote_stamps": {"u": ["t", ["a"]]},
    }
    assert record_leaf("polls", poll, _no_files) == record_leaf(
        "polls", voted, _no_files
    )
    ev = {"id": "e", "summary": "S", "start": "2026-11-04T09:00:00+00:00"}
    rsvped = {**ev, "rsvps": [{"user_id": "u"}], "rsvps_at": "now"}
    assert record_leaf("calendar", ev, _no_files) == record_leaf(
        "calendar", rsvped, _no_files
    )
    listing = {"post_id": "p", "title": "Bike", "price": 4}
    assert record_leaf("bazaar", listing, _no_files) == record_leaf(
        "bazaar", {**listing, "bids": [{"id": "b"}]}, _no_files
    )
    task = {"id": "t", "title": "T", "updated_at": "2026-01-01T00:00:00+00:00"}
    assert record_leaf("tasks", task, _no_files) == record_leaf(
        "tasks", {**task, "updated_at": "2027-01-01T00:00:00+00:00"}, _no_files
    )


def test_tombstones_drop_the_actor():
    t = {"id": "s", "author": "u", "created_at": "2026-01-01T00:00:00+00:00"}
    assert record_leaf("stickies_deleted", t, _no_files) == record_leaf(
        "stickies_deleted", {**t, "actor_user_id": "admin"}, _no_files
    )


def test_tombstones_commit_their_created_at():
    t = {"id": "s", "author": "u", "created_at": "2026-01-01T00:00:00+00:00"}
    assert record_leaf("stickies_deleted", t, _no_files) != record_leaf(
        "stickies_deleted", {**t, "created_at": "2026-02-01T00:00:00+00:00"}, _no_files
    )


def test_nested_file_meta_and_link_card_are_allow_listed():
    post = {
        "id": "p",
        "file_meta": {"url": "api/media/f.pdf", "mime_type": "x", "size_bytes": 3},
        "link_preview": {"url": "https://x", "thumbnail_url": "api/media/t.webp"},
    }
    later = {
        **post,
        "file_meta": {**post["file_meta"], "new_field": 1},
        "link_preview": {**post["link_preview"], "new_field": 2},
    }
    assert record_leaf("posts", post, _hashes) == record_leaf("posts", later, _hashes)


def test_calendar_tombstone_drops_its_local_created_at():
    t = {"id": "e", "created_by": "u", "created_at": "2026-01-01 00:00:00"}
    assert record_leaf("calendar_deleted", t, _no_files) == record_leaf(
        "calendar_deleted", {**t, "created_at": "2026-05-05 00:00:00"}, _no_files
    )


def test_absent_and_null_fields_hash_the_same():
    a = {"id": "z", "name": "School", "color": None}
    b = {"id": "z", "name": "School"}
    assert record_leaf("space_zones", a, _no_files) == record_leaf(
        "space_zones", b, _no_files
    )


def test_floats_hash_as_decimal_strings_and_ints_match():
    a = {"id": "s", "position_x": 0.0, "position_y": 10.5}
    form = commit_form("stickies", a, _no_files)
    assert form is not None
    assert form["v"]["position_x"] == "f:0.0"
    assert b"10.5" in canonical_bytes(form)
    # Review N4: the float 10.5 and the string "10.5" commit differently.
    assert record_leaf("stickies", a, _no_files) != record_leaf(
        "stickies", {**a, "position_y": "10.5"}, _no_files
    )


def test_timestamp_forms_normalise():
    a = {"id": "t", "title": "T", "created_at": "2026-11-01 09:00:00"}
    b = {"id": "t", "title": "T", "created_at": "2026-11-01T09:00:00+00:00"}
    c = {"id": "t", "title": "T", "created_at": "2026-11-01T10:00:00+01:00"}
    leaves = {record_leaf("tasks", r, _no_files) for r in (a, b, c)}
    assert len(leaves) == 1


def test_lists_sort():
    a = {"id": "t", "title": "T", "assignees": ["b", "a"], "labels": ["y", "x"]}
    b = {"id": "t", "title": "T", "assignees": ["a", "b"], "labels": ["x", "y"]}
    assert record_leaf("tasks", a, _no_files) == record_leaf("tasks", b, _no_files)


def test_files_hash_by_content_and_unhashable_rows_are_host_only():
    """§2 files: local names never enter a hash; a file without a known hash
    keeps the row out of the commitment."""
    host = {"id": "c", "author": "u", "content": "x", "media_url": "api/media/a.webp"}
    member = {**host, "media_url": "api/media/sc-7f0e.webp"}
    same = {"api/media/a.webp": "ab" * 32, "api/media/sc-7f0e.webp": "ab" * 32}
    assert record_leaf("comments", host, same.get) == record_leaf(
        "comments", member, same.get
    )
    assert record_leaf("comments", host, _no_files) is None
    assert file_refs("comments", host) == ["api/media/a.webp"]
    post = {
        "id": "p",
        "file_meta": {"url": "api/media/f.pdf", "name": "f.pdf", "size": 3},
        "link_preview": {"url": "https://x", "thumbnail_url": "api/media/t.webp"},
    }
    assert file_refs("posts", post) == ["api/media/f.pdf", "api/media/t.webp"]
    form = commit_form("posts", post, _hashes)
    assert form is not None
    assert form["v"]["file_meta"]["url"] == "s:" + _hashes("f.pdf")
    assert form["v"]["link_preview"]["thumbnail_url"] == "s:" + _hashes("t.webp")


def test_gallery_system_albums_are_left_out():
    album = {"kind": "album", "id": "a", "is_system": True}
    assert record_leaf("gallery", album, _no_files) is None
    assert record_leaf("gallery", {**album, "is_system": False}, _no_files)


def test_every_committed_resource_has_an_id_key():
    for resource, spec in COMMIT_SPECS.items():
        assert spec.id_key in spec.fields, resource


# ── Vector, root, delta ──────────────────────────────────────────────────


def test_vector_builder_is_order_independent():
    recs = [{"id": f"t{i}", "title": str(i)} for i in range(50)]
    a, b = VectorBuilder(), VectorBuilder()
    for r in recs:
        a.add("tasks", r, _no_files)
    for r in reversed(recs):
        b.add("tasks", r, _no_files)
    assert a.finish() == b.finish() and a.rows == b.rows == 50
    assert len(a.finish()) == VECTOR_BYTES


def test_a_changed_row_changes_exactly_its_bucket():
    recs = [{"id": f"t{i}", "title": str(i)} for i in range(50)]
    a, b = VectorBuilder(), VectorBuilder()
    for r in recs:
        a.add("tasks", r, _no_files)
        b.add("tasks", {**r, "title": "new"} if r["id"] == "t7" else r, _no_files)
    va, vb = a.finish(), b.finish()
    assert differing_buckets(va, vb) == [row_key("tasks", "t7")[0]]


def test_delta_round_trip():
    recs = [{"id": f"t{i}", "title": str(i)} for i in range(300)]
    old, new = VectorBuilder(), VectorBuilder()
    for r in recs:
        old.add("tasks", r, _no_files)
    for r in recs[:290]:
        new.add(
            "tasks",
            {**r, "title": r["title"] + "!"} if r["id"] < "t2" else r,
            _no_files,
        )
    vo, vn = old.finish(), new.finish()
    delta = vector_delta(vo, vn)
    assert apply_delta(vo, delta) == vn
    assert delta_from_wire(delta_to_wire(delta)) == delta
    assert delta_from_bytes(delta_to_bytes(delta)) == delta
    assert len(delta_to_bytes(delta)) == 33 * len(delta)
    assert vector_from_wire(vector_to_wire(vn)) == vn


@pytest.mark.parametrize(
    "bad",
    [
        "x",
        [[0, "AA"]],
        [[256, b64url_encode(b"\0" * 32)]],
        [[-1, b64url_encode(b"\0" * 32)]],
        [[True, b64url_encode(b"\0" * 32)]],
        [[1, b64url_encode(b"\0" * 32)], [1, b64url_encode(b"\0" * 32)]],
        [[i % 256, b64url_encode(b"\0" * 32)] for i in range(257)],
    ],
)
def test_malformed_deltas_are_refused(bad):
    with pytest.raises(PageRefused):
        delta_from_wire(bad)


def test_malformed_vector_refused():
    with pytest.raises(PageRefused):
        vector_from_wire(b64url_encode(b"\0" * 100))
    with pytest.raises(PageRefused):
        vector_from_wire(None)


# ── Sign / verify ────────────────────────────────────────────────────────


def test_seal_sign_verify_round_trip():
    page = _page(vector=_vector(5), prev_root=b"\x09" * 32)
    got = _verify(page.to_wire())
    assert got == page
    assert got.root == root_of(_vector(5))
    assert len(canonical_bytes(page.to_wire())) <= 1024


def test_unknown_suite_is_refused_never_defaulted():
    wire = _page().to_wire()
    wire["page_suite"] = "sha256-b512-c1"
    with pytest.raises(UnsupportedPageSuite):
        _verify(wire)
    del wire["page_suite"]
    with pytest.raises(UnsupportedPageSuite):
        _verify(wire)


def test_forged_page_refused():
    wire = _page().to_wire()
    wire["rows"] = 999
    with pytest.raises(PageRefused):
        _verify(wire)
    other = sign_page(
        space_id=SPACE,
        epoch=2,
        signer="host-1",
        seq=40,
        issued_at=NOW,
        cutoff=None,
        rows=3,
        vector=_vector(),
        prev_root=None,
        space_seed=os.urandom(32),
    )
    with pytest.raises(PageRefused):
        _verify(other.to_wire())


def test_a_page_signed_for_another_space_is_refused():
    wire = _page(space_id="sp-other").to_wire()
    with pytest.raises(PageRefused):
        _verify(wire)
    wire["space_id"] = SPACE
    with pytest.raises(PageRefused):
        _verify(wire)


def test_old_and_future_epochs_refused():
    with pytest.raises(PageRefused):
        _verify(_page(epoch=1).to_wire())
    with pytest.raises(PageRefused):
        _verify(_page(epoch=3).to_wire())


def test_future_skew_refused():
    _verify(_page(issued_at=NOW + 300_000).to_wire())
    with pytest.raises(PageRefused):
        _verify(_page(issued_at=NOW + 300_001).to_wire())


def test_unknown_authority_sig_suite_refused():
    wire = _page().to_wire()
    wire["authority_sig_suite"] = "ed25519+mldsa65"
    with pytest.raises(PageRefused):
        _verify(wire)


@pytest.mark.parametrize(
    "field,value",
    [
        ("epoch", "2"),
        ("seq", -1),
        ("rows", True),
        ("root", "AAAA"),
        ("prev_root", 5),
        ("signer", ""),
        ("cutoff", 7),
    ],
)
def test_malformed_fields_refused(field, value):
    wire = _page().to_wire()
    wire[field] = value
    with pytest.raises(PageRefused):
        _verify(wire)


def test_oversized_page_refused():
    wire = _page().to_wire()
    wire["padding"] = "x" * 2000
    with pytest.raises(PageRefused):
        _verify(wire)


def test_ordering():
    a = _page(issued_at=NOW, seq=5)
    assert _page(issued_at=NOW + 1, seq=1).is_newer_than(a)
    assert _page(issued_at=NOW, seq=6).is_newer_than(a)
    assert not _page(issued_at=NOW - 1, seq=99).is_newer_than(a)
    assert a.is_newer_than(None)


# ── Equivocation ─────────────────────────────────────────────────────────


def test_same_seq_different_root_is_a_conflict():
    a = _page(vector=_vector(1))
    b = _page(vector=_vector(2), issued_at=NOW + 5)
    assert is_conflict(a, b) and is_conflict(b, a)


def test_two_successors_of_one_page_are_a_conflict():
    p0 = _page(vector=_vector(1), seq=10)
    a = _page(vector=_vector(2), seq=20, prev_root=p0.root)
    b = _page(vector=_vector(3), seq=21, prev_root=p0.root)
    assert is_conflict(a, b)


def test_honest_chain_is_no_conflict():
    p0 = _page(vector=_vector(1), seq=10)
    p1 = _page(vector=_vector(2), seq=20, prev_root=p0.root)
    p2 = _page(vector=_vector(3), seq=30, prev_root=p1.root)
    assert not is_conflict(p0, p1)
    assert not is_conflict(p0, p2)  # a member that missed p1
    assert not is_conflict(p1, replace(p1))  # the same page again
    other_signer = _page(vector=_vector(4), seq=20, signer="admin-1")
    assert not is_conflict(p1, other_signer)


def test_page_ref():
    page = _page()
    ref = parse_page_ref(page.ref())
    assert ref == {
        "signer": "host-1",
        "epoch": 2,
        "seq": 40,
        "issued_at": NOW,
        "root": page.root,
    }
    assert parse_page_ref({"signer": "x"}) is None
    assert parse_page_ref("nope") is None


# ── Bucket listings ──────────────────────────────────────────────────────


def _listing_rows(n: int, index: int | None = None) -> tuple[int, list[ListingRow]]:
    rows: list[ListingRow] = []
    k = 0
    target = index
    while len(rows) < n:
        rid = f"r{k}"
        k += 1
        i = row_key("posts", rid)[0]
        if target is None:
            target = i
        if i == target:
            rows.append(
                ListingRow("posts", rid, hashlib.sha256(rid.encode()).digest(), None)
            )
    assert target is not None
    return target, rows


def test_change_time_is_the_newest_known_stamp():
    assert change_time({"created_at": "2026-01-01 00:00:00"}) == (
        "2026-01-01T00:00:00.000000+00:00"
    )
    assert change_time(
        {"created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-03-01 10:00:00"}
    ) == ("2026-03-01T10:00:00.000000+00:00")
    assert change_time({"title": "x"}) is None
    page = _page(issued_at=NOW)
    assert page_time(page) < change_time({"edited_at": "2030-01-01 00:00:00"})


def test_a_listing_round_trips_and_verifies():
    index, rows = _listing_rows(3)
    vector = bytearray(VECTOR_BYTES)
    vector[index * 32 : (index + 1) * 32] = bucket_hash(index, (r.leaf for r in rows))
    wire = listing_to_wire(index, rows)
    got_index, got_rows, current = parse_listing(wire)
    assert got_index == index and got_rows == rows and current is False
    verify_listing(index, got_rows, bytes(vector))
    with pytest.raises(PageRefused):
        verify_listing(index, got_rows[:-1], bytes(vector))  # truncated
    assert parse_listing(listing_to_wire(index, None)) == (index, None, False)
    assert parse_listing(listing_to_wire(index, rows, current=True)) == (
        index,
        rows,
        True,
    )


@pytest.mark.parametrize(
    "bad",
    [
        "x",
        {"i": 256, "held": True, "rows": []},
        {"i": True, "held": True, "rows": []},
        {"i": 0, "held": True, "rows": "x"},
        {"i": 0, "held": True, "rows": [["members", "u", "A" * 43, None]]},
        {"i": 0, "held": True, "rows": [["posts", "", "A" * 43, None]]},
        {"i": 0, "held": True, "rows": [["posts", "p", "AA", None]]},
        {"i": 0, "held": True, "rows": [["posts", "p", "A" * 43, 5]]},
    ],
)
def test_malformed_listings_are_refused(bad):
    with pytest.raises(PageRefused):
        parse_listing(bad)


def test_a_listing_row_of_another_bucket_is_refused():
    index, rows = _listing_rows(1)
    wire = listing_to_wire((index + 1) % 256, rows)
    with pytest.raises(PageRefused):
        parse_listing(wire)


def test_a_listing_naming_a_row_twice_is_refused():
    index, rows = _listing_rows(2)
    for wire in (
        listing_to_wire(index, [rows[0], rows[0]]),
        listing_to_wire(index, [rows[0], rows[0]], current=True),
    ):
        with pytest.raises(PageRefused, match="twice"):
            parse_listing(wire)


def test_an_oversized_listing_is_refused():
    wire = {"i": 0, "held": True, "rows": [["posts", "p", "A" * 43, None]] * 4097}
    with pytest.raises(PageRefused):
        parse_listing(wire)


def test_bucket_request_is_bounded():
    assert parse_bucket_request([3, 1, 3]) == [3, 1]
    assert parse_bucket_request(list(range(8))) == list(range(8))
    for bad in ([], list(range(9)), [256], [True], "x", None):
        assert parse_bucket_request(bad) is None


def test_superseded_rows_are_not_missing():
    gone = {("posts", "p1"), ("tasks", "t1")}
    assert is_superseded("posts", "p1", gone)
    assert is_superseded("polls", "p1", gone)  # its post is gone
    assert is_superseded("tasks", "t1", gone)
    assert not is_superseded("posts", "p2", gone)
    assert superseded_key("posts_deleted", "p1") == ("posts", "p1")
    assert superseded_key("tasks_archived", "t1") == ("tasks", "t1")
    assert superseded_key("posts", "p1") is None


# ── Verified fetch (Phase 3) ─────────────────────────────────────────────


def test_a_fetch_hint_round_trips_as_8_byte_leaf_prefixes():
    leaves = [hashlib.sha256(bytes([i])).digest() for i in range(5)]
    wire = hint_to_wire(leaves)
    assert hint_from_wire(wire) == frozenset(
        leaf[:HINT_PREFIX_BYTES] for leaf in leaves
    )
    assert hint_from_wire(hint_to_wire([])) == frozenset()
    assert hint_from_wire(None) is None


@pytest.mark.parametrize(
    "bad",
    [
        5,
        b64url_encode(b"\0" * 7),
        b64url_encode(b"\0" * 8 * (MAX_HINT_LEAVES + 1)),
    ],
)
def test_a_malformed_or_oversized_hint_is_refused(bad):
    with pytest.raises(PageRefused):
        hint_from_wire(bad)


def test_a_fetch_request_wants_rows_only_when_asked_and_well_formed():
    leaf = hashlib.sha256(b"x").digest()
    assert parse_fetch_request({}) == (False, None)
    assert parse_fetch_request({"rows": "yes"}) == (False, None)
    assert parse_fetch_request({"rows": True}) == (True, None)
    assert parse_fetch_request({"rows": True, "have": hint_to_wire([leaf])}) == (
        True,
        frozenset({leaf[:HINT_PREFIX_BYTES]}),
    )
    # A malformed hint: listings only, never every row.
    assert parse_fetch_request({"rows": True, "have": 5}) == (False, None)


def test_a_fetched_row_round_trips():
    record = {"id": "p1", "content": "hi"}
    index = row_key("posts", "p1")[0]
    assert parse_fetched_row(fetched_row_to_wire(index, "posts", record)) == (
        index,
        "posts",
        record,
    )


@pytest.mark.parametrize(
    "bad",
    [
        "x",
        {"i": 0, "r": "posts", "row": "x"},
        {"i": 300, "r": "posts", "row": {"id": "p"}},
        {"i": 0, "r": "members", "row": {"id": "p"}},  # host-only: never fetched
        {"i": 0, "r": "chat_messages", "row": {"id": "p"}},
        {"i": True, "r": "posts", "row": {"id": "p"}},
        {"i": 0, "r": "posts", "row": {"content": "no id"}},
    ],
)
def test_a_malformed_fetched_row_is_refused(bad):
    with pytest.raises(PageRefused):
        parse_fetched_row(bad)


def test_a_fetched_row_of_another_bucket_is_refused():
    index = row_key("posts", "p1")[0]
    with pytest.raises(PageRefused):
        parse_fetched_row(fetched_row_to_wire((index + 1) % 256, "posts", {"id": "p1"}))


def test_a_fetched_record_keeps_only_what_the_page_commits():
    """Uncommitted fields of a delivered row are never applied: the leaf
    does not cover them, so a deliverer could set them at will (reactions,
    a pin, a far-future ``updated_at``, a GFS descriptor, a feed-card link)."""
    record = {
        "id": "p1",
        "space_id": SPACE,
        "author": "anna",
        "type": "text",
        "content": "hi",
        "created_at": "2026-01-01 00:00:00",
        "reactions": {"x": ["mallory"]},
        "reaction_stamps": {"x": {}},
        "pinned": True,
        "moderated": True,
        "media_refs": {"x": 1},
        "updated_at": "2099-01-01 00:00:00",
        "comment_count": 99,
        "file_meta": {"url": "api/media/a.webp", "mime_type": "x", "evil": 1},
    }
    got = fetched_record("posts", record)
    assert set(got) <= set(COMMIT_SPECS["posts"].fields)
    assert got["file_meta"] == {"url": "api/media/a.webp", "mime_type": "x"}
    assert record_leaf("posts", got, lambda u: "a" * 64) == record_leaf(
        "posts", record, lambda u: "a" * 64
    )


def test_fetched_poll_and_schedule_shapes_keep_their_order_and_committed_keys():
    poll = {
        "post_id": "p1",
        "meta": {"question": "Q", "closed": False, "votes_total": 9},
        "options": [{"id": "b", "text": "B", "votes": 3}, {"id": "a", "text": "A"}],
        "votes": [{"user_id": "u", "option_id": "a"}],
    }
    got = fetched_record("polls", poll)
    assert got["options"] == [{"id": "b", "text": "B"}, {"id": "a", "text": "A"}]
    assert "votes" not in got and "votes_total" not in got["meta"]
    assert record_leaf("polls", got, _no_files) == record_leaf("polls", poll, _no_files)
    schedule = {
        "post_id": "p2",
        "title": "T",
        "slots": [{"id": "s2", "slot_date": "2027-01-01", "answers": {"u": "yes"}}],
        "responses": [{"user_id": "u"}],
    }
    got = fetched_record("schedules", schedule)
    assert got["slots"] == [{"id": "s2", "slot_date": "2027-01-01"}]
    assert "responses" not in got
    assert record_leaf("schedules", got, _no_files) == record_leaf(
        "schedules", schedule, _no_files
    )


# ── Review N4: one leaf, one applied value ───────────────────────────────


def test_order_meaningful_lists_commit_in_order():
    """Pictures, poll options, schedule slots and timetable entries keep
    their order: a deliverer that reorders them changes the leaf."""
    post = {"id": "p", "image_urls": ["api/media/a.webp", "api/media/b.webp"]}
    swapped = {**post, "image_urls": list(reversed(post["image_urls"]))}
    assert record_leaf("posts", post, _hashes) != record_leaf("posts", swapped, _hashes)
    poll = {
        "post_id": "p",
        "meta": {"question": "Q"},
        "options": [{"id": "a", "text": "A"}, {"id": "b", "text": "B"}],
    }
    assert record_leaf("polls", poll, _no_files) != record_leaf(
        "polls", {**poll, "options": list(reversed(poll["options"]))}, _no_files
    )
    schedule = {
        "post_id": "p",
        "title": "T",
        "slots": [{"id": "s1", "slot_date": "2027-01-01"}, {"id": "s2"}],
    }
    assert record_leaf("schedules", schedule, _no_files) != record_leaf(
        "schedules", {**schedule, "slots": list(reversed(schedule["slots"]))}, _no_files
    )
    for name, spec in COMMIT_SPECS.items():
        for field_name in spec.ordered:
            assert field_name in spec.fields, (name, field_name)


def test_set_like_lists_still_sort():
    a = {"id": "t", "title": "T", "assignees": ["b", "a"]}
    b = {**a, "assignees": ["a", "b"]}
    assert record_leaf("tasks", a, _no_files) == record_leaf("tasks", b, _no_files)


def test_a_fetched_record_applies_the_committed_spelling():
    """The applied value is the one the leaf commits: one stamp spelling,
    set-like lists sorted, ordered lists as delivered."""
    task = {
        "id": "t",
        "title": "T",
        "created_at": "2026-11-01T10:00:00+01:00",
        "assignees": ["b", "a"],
    }
    got = fetched_record("tasks", task)
    assert got["created_at"] == "2026-11-01T09:00:00.000000+00:00"
    assert got["assignees"] == ["a", "b"]
    assert record_leaf("tasks", got, _no_files) == record_leaf("tasks", task, _no_files)
    post = {"id": "p", "image_urls": ["api/media/b.webp", "api/media/a.webp"]}
    assert fetched_record("posts", post)["image_urls"] == post["image_urls"]


def test_a_string_never_commits_like_a_number_or_flag():
    base = {"id": "s", "position_x": 1}
    for other in ("1", 1.0, True):
        assert record_leaf("stickies", base, _no_files) != record_leaf(
            "stickies", {**base, "position_x": other}, _no_files
        ), other
