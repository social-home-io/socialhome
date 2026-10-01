"""Owner-bound row ids: minting, the legacy shape, and every refusal."""

from __future__ import annotations

import uuid

import pytest

from socialhome.federation.owner_bound_id import (
    GALLERY_ALBUM_KIND,
    GROUP_CONVERSATION_KIND,
    GALLERY_ITEM_KIND,
    MOMENT_KIND,
    OWNER_BOUND_ID_SUITE_SHA256,
    SPACE_CALENDAR_EVENT_KIND,
    SPACE_COMMENT_KIND,
    SPACE_PAGE_KIND,
    SPACE_POST_KIND,
    SPACE_STICKY_KIND,
    SPACE_TASK_KIND,
    SPACE_TASK_LIST_KIND,
    UNSCOPED_KINDS,
    SUPPORTED_OWNER_BOUND_ID_SUITES,
    OwnerBinding,
    UnsupportedOwnerBoundIdSuite,
    check_owner_bound_id,
    is_owner_bound,
    mint_owner_bound_id,
    owner_bound_id_refused,
    suite_of,
)

SP = "sp-1"
OWNER = "u-owner"


def _mint(**kw) -> str:
    args = {"space_id": SP, "owner_user_id": OWNER, **kw}
    return mint_owner_bound_id(GALLERY_ALBUM_KIND, **args)


def _check(row_id: str, *, kind=GALLERY_ALBUM_KIND, space=SP, owner=OWNER):
    return check_owner_bound_id(kind, row_id, space_id=space, owner_user_id=owner)


def test_a_minted_id_is_a_uuidv8_hex_that_verifies_for_its_creator():
    row_id = _mint()
    assert len(row_id) == 32
    assert uuid.UUID(hex=row_id).version == 8
    assert is_owner_bound(row_id)
    assert suite_of(row_id) == OWNER_BOUND_ID_SUITE_SHA256
    assert _check(row_id) is OwnerBinding.VALID


def test_every_minted_id_is_fresh():
    assert len({_mint() for _ in range(200)}) == 200


@pytest.mark.parametrize(
    "overrides",
    [
        {"owner": "u-squatter"},
        {"space": "sp-other"},
        {"kind": "space-post"},
        {"owner": None},
        {"owner": ""},
        {"space": ""},
    ],
)
def test_a_claim_for_anything_else_is_a_mismatch(overrides):
    assert _check(_mint(), **overrides) is OwnerBinding.MISMATCH


def test_a_tampered_commitment_or_nonce_is_a_mismatch():
    row_id = _mint()
    flipped_tag = row_id[:-1] + ("0" if row_id[-1] != "0" else "1")
    flipped_nonce = ("0" if row_id[0] != "0" else "1") + row_id[1:]
    assert _check(flipped_tag) is OwnerBinding.MISMATCH
    assert _check(flipped_nonce) is OwnerBinding.MISMATCH


@pytest.mark.parametrize("nibble", ["9", "a", "b"])
def test_an_unknown_suite_nibble_is_refused_never_defaulted(nibble):
    row_id = _mint()
    unknown = row_id[:16] + nibble + row_id[17:]
    assert is_owner_bound(unknown)
    with pytest.raises(UnsupportedOwnerBoundIdSuite):
        suite_of(unknown)
    assert _check(unknown) is OwnerBinding.MISMATCH


@pytest.mark.parametrize(
    "legacy",
    [
        "5f0c1b2e9d7a4c3e8b1f2a3d4c5e6f70",  # a uuid4 hex — every album id before
        "5f0c1b2e-9d7a-4c3e-8b1f-2a3d4c5e6f70",  # dashed form
        "album-a",
        "",
        "0123456789ab8def0123456789abcdeZ",  # right length, not hex
        "0123456789ab8def7123456789abcdef",  # v8 nibble, non-RFC variant
        "0123456789AB8DEF8123456789ABCDEF",  # upper case
    ],
)
def test_anything_else_is_legacy(legacy):
    assert not is_owner_bound(legacy)
    assert _check(legacy) is OwnerBinding.LEGACY


def test_suite_of_rejects_a_legacy_id():
    with pytest.raises(ValueError):
        suite_of(uuid.uuid4().hex)


def test_minting_refuses_an_unknown_suite_or_missing_inputs():
    assert OWNER_BOUND_ID_SUITE_SHA256 in SUPPORTED_OWNER_BOUND_ID_SUITES
    with pytest.raises(UnsupportedOwnerBoundIdSuite):
        _mint(suite="md5")
    with pytest.raises(ValueError):
        _mint(owner_user_id="")
    with pytest.raises(ValueError):
        _mint(space_id="")
    with pytest.raises(ValueError):
        mint_owner_bound_id("", space_id=SP, owner_user_id=OWNER)


_SCOPED_KINDS = [
    GALLERY_ALBUM_KIND,
    GALLERY_ITEM_KIND,
    SPACE_POST_KIND,
    SPACE_COMMENT_KIND,
    SPACE_CALENDAR_EVENT_KIND,
    SPACE_TASK_KIND,
    SPACE_TASK_LIST_KIND,
    SPACE_PAGE_KIND,
    SPACE_STICKY_KIND,
]


def test_every_kind_is_distinct():
    kinds = [*_SCOPED_KINDS, MOMENT_KIND, GROUP_CONVERSATION_KIND]
    assert len(set(kinds)) == len(kinds)
    assert UNSCOPED_KINDS == {MOMENT_KIND, GROUP_CONVERSATION_KIND}


def test_a_group_conversation_id_binds_its_authority_household():
    row_id = mint_owner_bound_id(
        GROUP_CONVERSATION_KIND, space_id="", owner_user_id="inst-a"
    )
    assert is_owner_bound(row_id)
    check = check_owner_bound_id
    assert (
        check(GROUP_CONVERSATION_KIND, row_id, space_id="", owner_user_id="inst-a")
        is OwnerBinding.VALID
    )
    assert (
        check(GROUP_CONVERSATION_KIND, row_id, space_id="", owner_user_id="inst-b")
        is OwnerBinding.MISMATCH
    )
    assert (
        check(MOMENT_KIND, row_id, space_id="", owner_user_id="inst-a")
        is OwnerBinding.MISMATCH
    )


@pytest.mark.parametrize("kind", _SCOPED_KINDS)
def test_a_space_kind_binds_owner_and_space_and_kind(kind):
    row_id = mint_owner_bound_id(kind, space_id=SP, owner_user_id=OWNER)
    assert _check(row_id, kind=kind) is OwnerBinding.VALID
    assert _check(row_id, kind=kind, owner="u-x") is OwnerBinding.MISMATCH
    assert _check(row_id, kind=kind, space="sp-x") is OwnerBinding.MISMATCH
    other = GALLERY_ITEM_KIND if kind != GALLERY_ITEM_KIND else SPACE_POST_KIND
    assert _check(row_id, kind=other) is OwnerBinding.MISMATCH
    with pytest.raises(ValueError):
        mint_owner_bound_id(kind, space_id="", owner_user_id=OWNER)


def test_a_moment_id_binds_its_author_without_a_space():
    row_id = mint_owner_bound_id(MOMENT_KIND, space_id="", owner_user_id=OWNER)
    assert is_owner_bound(row_id)
    assert _check(row_id, kind=MOMENT_KIND, space="") is OwnerBinding.VALID
    assert _check(row_id, kind=MOMENT_KIND, space="", owner="u-x") is (
        OwnerBinding.MISMATCH
    )
    # An unscoped kind never names a space — on either side.
    assert _check(row_id, kind=MOMENT_KIND, space=SP) is OwnerBinding.MISMATCH
    with pytest.raises(ValueError):
        mint_owner_bound_id(MOMENT_KIND, space_id=SP, owner_user_id=OWNER)


def test_refused_warns_only_on_a_mismatch(caplog):
    row_id = _mint()
    with caplog.at_level("WARNING"):
        assert not owner_bound_id_refused(
            GALLERY_ALBUM_KIND, row_id, space_id=SP, owner_user_id=OWNER, context="t"
        )
        assert not owner_bound_id_refused(
            GALLERY_ALBUM_KIND,
            uuid.uuid4().hex,
            space_id=SP,
            owner_user_id="u-x",
            context="t",
        )
        assert not caplog.records
        assert owner_bound_id_refused(
            GALLERY_ALBUM_KIND, row_id, space_id=SP, owner_user_id="u-x", context="t"
        )
    assert "not bound to 'u-x' in space sp-1" in caplog.text
    caplog.clear()
    moment = mint_owner_bound_id(MOMENT_KIND, space_id="", owner_user_id=OWNER)
    with caplog.at_level("WARNING"):
        assert owner_bound_id_refused(
            MOMENT_KIND, moment, space_id="", owner_user_id="u-x", context="t"
        )
    assert "in space" not in caplog.text
