"""Tests for socialhome.domain.space."""

from __future__ import annotations

import pytest

from socialhome.domain.space import (
    ACCESS_FEATURES,
    owner_seat_from_roster,
    parse_have_seq,
    CONTENT_AUTHORITY_ROLES,
    MIRRORABLE_REMOTE_ROLES,
    SETTINGS_AUTHORITY_ROLES,
    WRITER_ROLES,
    AccessAdminOnlyError,
    AccessDecision,
    ContentAction,
    HouseholdFeatures,
    JoinMode,
    RemoteAdminOutcome,
    SpaceConfigGapError,
    SpaceFeatureAccess,
    SpaceFeatures,
    SpacePermissionError,
    SpaceRole,
    mirrorable_remote_role,
    normalize_join_mode,
    role_change_allowed,
    normalize_min_age,
    normalize_retention_exempt_types,
    restricted_access_changes,
    MAX_ZONE_NAME_LENGTH,
    validate_zone_color,
    validate_zone_coord,
    validate_zone_name,
    validate_zone_radius,
    InvalidSpaceLinkError,
    validate_space_link_url,
)
from socialhome.domain.post import PostType
from socialhome.domain.errors import CodedError


def test_remote_admin_outcome_values():
    """The host's gate signals three outcomes by stable string value."""
    assert RemoteAdminOutcome.EXECUTED == "executed"
    assert RemoteAdminOutcome.NEEDS_OWNER_APPROVAL == "needs_owner_approval"
    assert RemoteAdminOutcome.DROPPED == "dropped"


def test_space_features_roundtrip():
    """SpaceFeatures survives a to_columns / from_row round-trip."""
    f = SpaceFeatures(calendar=True, tasks_access=SpaceFeatureAccess.MODERATED)
    f2 = SpaceFeatures.from_row(f.to_columns())
    assert f == f2


def test_space_features_wire_roundtrip():
    """SpaceFeatures survives a to_wire_dict / from_wire_dict round-trip —
    the path the cross-household admin config edit uses."""
    f = SpaceFeatures(
        calendar=False,
        bazaar=False,
        location=True,
        location_mode="zone_only",
        tasks_access=SpaceFeatureAccess.MODERATED,
        allow_subscriber_comment=True,
        allowed_post_types=("image", "text"),
    )
    assert SpaceFeatures.from_wire_dict(f.to_wire_dict()) == f


def test_space_features_wire_roundtrip_every_field_non_default():
    """CI guard: every SpaceFeatures field flipped to a NON-default value
    survives ``from_wire_dict(to_wire_dict())``.

    This fails the moment a field is dropped from either ``to_wire_dict``
    or ``from_wire_dict`` — the convergence point that previously let
    ``delegated_admin_authority`` (and the access / subscriber fields) be
    silently lost on the wire. Construct with EVERY field non-default so a
    missing field round-trips to its default and the equality breaks.
    """
    f = SpaceFeatures(
        calendar=False,  # default True
        todo=False,  # default True
        location=True,  # default False
        location_mode="zone_only",  # default "gps"
        stickies=False,  # default True
        pages=False,  # default True
        gallery=False,  # default True
        bazaar=False,  # default True
        timetable=True,  # default False
        chat=False,  # default True
        posts_access=SpaceFeatureAccess.MODERATED,  # default OPEN
        pages_access=SpaceFeatureAccess.ADMIN_ONLY,  # default OPEN
        stickies_access=SpaceFeatureAccess.MODERATED,  # default OPEN
        calendar_access=SpaceFeatureAccess.ADMIN_ONLY,  # default OPEN
        tasks_access=SpaceFeatureAccess.MODERATED,  # default OPEN
        allow_subscriber_comment=True,  # default False
        allow_subscriber_react=True,  # default False
        delegated_admin_authority=True,  # default False
        allowed_post_types=("image", "text"),  # default _ALL_POST_TYPES
    )
    assert SpaceFeatures.from_wire_dict(f.to_wire_dict()) == f


def test_space_features_from_wire_dict_defaults_on_partial():
    """A partial / older-shaped wire dict falls back to class defaults
    rather than raising, and an unknown access level degrades to OPEN."""
    f = SpaceFeatures.from_wire_dict({"name_only": 1, "posts_access": "bogus"})
    assert f == SpaceFeatures()  # every field defaulted


def test_space_features_chat_defaults_on_and_round_trips():
    """The space chat is on everywhere (owner decision): on by default, on
    for a row or a wire dict that predates it, and an admin's off survives
    both round-trips and a partial edit."""
    assert SpaceFeatures().chat is True
    assert SpaceFeatures.from_row({}).chat is True
    assert SpaceFeatures.from_wire_dict({}).chat is True
    off = SpaceFeatures(chat=False)
    assert off.to_columns()["feature_chat"] == 0
    assert SpaceFeatures.from_row(off.to_columns()).chat is False
    assert off.to_wire_dict()["chat"] is False
    assert SpaceFeatures.from_wire_dict(off.to_wire_dict()).chat is False
    # A partial edit leaves the current value alone.
    assert not SpaceFeatures.from_wire_dict({"bazaar": False}, defaults=off).chat


def test_space_features_timetable_defaults_off_and_round_trips():
    """The timetable tab is opt-in: off by default, off for a row or a
    wire dict that predates it, and it survives both round-trips."""
    assert SpaceFeatures().timetable is False
    assert SpaceFeatures.from_row({}).timetable is False
    assert SpaceFeatures.from_wire_dict({}).timetable is False
    on = SpaceFeatures(timetable=True)
    assert on.to_columns()["feature_timetable"] == 1
    assert SpaceFeatures.from_row(on.to_columns()).timetable is True
    assert on.to_wire_dict()["timetable"] is True
    assert SpaceFeatures.from_wire_dict(on.to_wire_dict()).timetable is True
    # A partial edit leaves the current value alone.
    assert SpaceFeatures.from_wire_dict({"bazaar": False}, defaults=on).timetable


def test_space_features_gallery_roundtrip():
    """gallery flag survives to_columns / from_row + appears on the wire."""
    on = SpaceFeatures(gallery=True)
    off = SpaceFeatures(gallery=False)
    assert SpaceFeatures.from_row(on.to_columns()).gallery is True
    assert SpaceFeatures.from_row(off.to_columns()).gallery is False
    assert on.to_wire_dict()["gallery"] is True
    assert off.to_wire_dict()["gallery"] is False


def test_space_features_gallery_default_on_missing_column():
    """A row missing ``feature_gallery`` (pre-0008 spaces from a peer
    that hasn't migrated yet) defaults to gallery=True so the tab
    stays visible — matches the dataclass default."""
    f = SpaceFeatures.from_row({})  # no feature_gallery column at all
    assert f.gallery is True


def test_space_features_delegated_admin_authority_default_false():
    """The delegated-admin-authority opt-in defaults OFF (least-privilege)."""
    assert SpaceFeatures().delegated_admin_authority is False


def test_space_features_delegated_admin_authority_roundtrip():
    """delegated_admin_authority survives to_columns/from_row + the wire."""
    on = SpaceFeatures(delegated_admin_authority=True)
    off = SpaceFeatures(delegated_admin_authority=False)
    assert SpaceFeatures.from_row(on.to_columns()).delegated_admin_authority is True
    assert SpaceFeatures.from_row(off.to_columns()).delegated_admin_authority is False
    assert on.to_wire_dict()["delegated_admin_authority"] is True
    assert off.to_wire_dict()["delegated_admin_authority"] is False
    assert (
        SpaceFeatures.from_wire_dict(on.to_wire_dict()).delegated_admin_authority
        is True
    )


def test_space_features_delegated_admin_authority_back_compat():
    """A row / wire dict missing the flag (older peer) defaults to False."""
    assert SpaceFeatures.from_row({}).delegated_admin_authority is False
    assert SpaceFeatures.from_wire_dict({}).delegated_admin_authority is False


_ACCESS_FEATURES = ("posts", "pages", "tasks", "stickies", "calendar")


def _expected_access(
    level: SpaceFeatureAccess,
    role: SpaceRole,
    action: ContentAction,
    owns: bool,
) -> AccessDecision:
    """The owner decision table, spelled out independently of the code."""
    if level is SpaceFeatureAccess.OPEN:
        return AccessDecision.PROCEED
    if level is SpaceFeatureAccess.ADMIN_ONLY:
        if role in (SpaceRole.OWNER, SpaceRole.ADMIN):
            return AccessDecision.PROCEED
        return AccessDecision.DENY
    # MODERATED
    if role in (SpaceRole.OWNER, SpaceRole.ADMIN, SpaceRole.MODERATOR):
        return AccessDecision.PROCEED
    if action is ContentAction.CREATE:
        return AccessDecision.QUEUE
    if action is ContentAction.LAYOUT:
        return AccessDecision.PROCEED
    return AccessDecision.PROCEED if owns else AccessDecision.QUEUE


@pytest.mark.parametrize("feature", _ACCESS_FEATURES)
@pytest.mark.parametrize("level", list(SpaceFeatureAccess))
@pytest.mark.parametrize(
    "role",
    [SpaceRole.OWNER, SpaceRole.ADMIN, SpaceRole.MODERATOR, SpaceRole.MEMBER],
)
@pytest.mark.parametrize("action", list(ContentAction))
@pytest.mark.parametrize("owns", [True, False])
def test_access_decision_matrix(feature, level, role, action, owns):
    """role × level × action × owns — every cell of the owner decisions."""
    features = SpaceFeatures(**{f"{feature}_access": level})
    got = features.access_decision(feature, role=role, action=action, owns_target=owns)
    assert got is _expected_access(level, role, action, owns)


def test_access_decision_reads_only_the_named_feature():
    """One feature's level never leaks into another's decision."""
    f = SpaceFeatures(tasks_access=SpaceFeatureAccess.ADMIN_ONLY)
    assert (
        f.access_decision(
            "pages",
            role=SpaceRole.MEMBER,
            action=ContentAction.CREATE,
            owns_target=True,
        )
        is AccessDecision.PROCEED
    )
    assert (
        f.access_decision(
            "tasks",
            role=SpaceRole.MEMBER,
            action=ContentAction.CREATE,
            owns_target=True,
        )
        is AccessDecision.DENY
    )


def test_access_decision_accepts_plain_role_strings():
    """A ``space_members.role`` column value (a plain str) works too."""
    f = SpaceFeatures(posts_access=SpaceFeatureAccess.ADMIN_ONLY)
    assert (
        f.access_decision(
            "posts", role="admin", action=ContentAction.EDIT, owns_target=True
        )
        is AccessDecision.PROCEED
    )
    assert (
        f.access_decision(
            "posts", role="moderator", action=ContentAction.EDIT, owns_target=True
        )
        is AccessDecision.DENY
    )


def test_access_decision_fails_closed_for_unknown_roles():
    """No seat / a subscriber / junk never passes a restricted feature."""
    for level in (SpaceFeatureAccess.MODERATED, SpaceFeatureAccess.ADMIN_ONLY):
        f = SpaceFeatures(calendar_access=level)
        for role in (None, "subscriber", "root", ""):
            got = f.access_decision(
                "calendar",
                role=role,
                action=ContentAction.CREATE,
                owns_target=True,
            )
            assert got is AccessDecision.DENY, (level, role)
    # OPEN stays open — the writer gates decide membership, not this.
    assert (
        SpaceFeatures().access_decision(
            "calendar", role=None, action=ContentAction.CREATE, owns_target=True
        )
        is AccessDecision.PROCEED
    )


def test_access_decision_rejects_an_unknown_feature():
    with pytest.raises(ValueError):
        SpaceFeatures().access_decision(
            "gallery",
            role=SpaceRole.OWNER,
            action=ContentAction.CREATE,
            owns_target=True,
        )


def test_access_level_names_every_gated_feature():
    f = SpaceFeatures(stickies_access=SpaceFeatureAccess.MODERATED)
    assert f.access_level("stickies") is SpaceFeatureAccess.MODERATED
    assert f.access_level("posts") is SpaceFeatureAccess.OPEN
    assert set(ACCESS_FEATURES) == set(_ACCESS_FEATURES)


def test_restricted_access_changes_lists_only_raised_levels():
    """Which features a features edit moves OFF ``OPEN`` (or between
    restricted levels) — the PEERS_TOO_OLD guard's trigger."""
    before = SpaceFeatures(pages_access=SpaceFeatureAccess.ADMIN_ONLY)
    after = SpaceFeatures(
        pages_access=SpaceFeatureAccess.OPEN,
        tasks_access=SpaceFeatureAccess.ADMIN_ONLY,
        posts_access=SpaceFeatureAccess.MODERATED,
    )
    assert restricted_access_changes(before, after) == ("posts", "tasks")
    assert restricted_access_changes(after, after) == ()


def test_access_admin_only_error_is_a_permission_error():
    exc = AccessAdminOnlyError("tasks")
    assert isinstance(exc, SpacePermissionError)
    assert exc.feature == "tasks"


def test_space_features_with_allowed_post_types():
    """with_allowed_post_types normalises and stores the set; empty set raises ValueError."""
    f = SpaceFeatures()
    f2 = f.with_allowed_post_types({"text", "image"})
    assert f2.allowed_post_types == ("image", "text")
    with pytest.raises(ValueError):
        f.with_allowed_post_types(set())


def test_household_features_roundtrip():
    """HouseholdFeatures survives a to_columns / from_row round-trip."""
    h = HouseholdFeatures(bazaar=False, household_name="Casa")
    h2 = HouseholdFeatures.from_row(h.to_columns())
    assert h == h2


def test_permission_error_banned():
    """SpacePermissionError with banned=True exposes the flag and a useful message."""
    e = SpacePermissionError("banned", banned=True)
    assert e.banned and "banned" in str(e)


def test_config_gap_error():
    """SpaceConfigGapError includes space_id, have, and need in its string form."""
    e = SpaceConfigGapError(space_id="s1", have=3, need=7)
    assert "s1" in str(e) and "3" in str(e)


# ─── normalize_min_age ───────────────────────────────────────────────────


@pytest.mark.parametrize("value", [0, 13, 16, 18, "13", "18"])
def test_normalize_min_age_passes_allowed_values(value):
    """Every value in the allowed set (and its string form) round-trips."""
    assert normalize_min_age(value) == int(value)


@pytest.mark.parametrize(
    "value",
    [15, -1, 99, None, "nope", 1.5, object(), [13]],
)
def test_normalize_min_age_clamps_everything_else(value):
    """Anything outside {0,13,16,18} falls back to 0 (fail-soft default)."""
    assert normalize_min_age(value) == 0


def test_allow_subscribers_defaults_off_and_is_independent_of_join_mode():
    """Readability is its own switch, not a property of ``JoinMode``.

    ``allow_subscribers`` defaults OFF, exactly like the two sibling
    subscriber flags it sits beside — a space is private until its owner says
    otherwise. And because it is a separate dial, BOTH cross-combinations are
    expressible: ``invite_only`` + subscribers-on (a broadcast space: invited
    people post, anyone may follow) and ``open`` + subscribers-off (joinable,
    but not publicly readable).
    """
    assert SpaceFeatures().allow_subscribers is False
    assert SpaceFeatures().allow_subscriber_comment is False
    assert SpaceFeatures().allow_subscriber_react is False
    # No join mode implies anything about readability any more — the enum
    # carries exactly three membership gates and nothing else.
    assert set(JoinMode) == {
        JoinMode.INVITE_ONLY,
        JoinMode.OPEN,
        JoinMode.REQUEST,
    }
    broadcast = SpaceFeatures(allow_subscribers=True)
    assert broadcast.allow_subscribers is True
    assert broadcast.to_wire_dict()["allow_subscribers"] is True


def test_allow_subscribers_round_trips_through_every_boundary():
    """Row → dataclass → columns → wire → dataclass, all four directions."""
    on = SpaceFeatures(allow_subscribers=True)
    assert on.to_columns()["allow_subscribers"] == 1
    assert SpaceFeatures().to_columns()["allow_subscribers"] == 0
    assert SpaceFeatures.from_row({"allow_subscribers": 1}).allow_subscribers is True
    assert SpaceFeatures.from_row({"allow_subscribers": 0}).allow_subscribers is False
    # A row written before migration 0051 has no such column — fail closed.
    assert SpaceFeatures.from_row({}).allow_subscribers is False
    assert SpaceFeatures.from_wire_dict(on.to_wire_dict()).allow_subscribers is True
    # An older peer's SPACE_SYNC_BEGIN omits the key ⇒ not readable.
    assert SpaceFeatures.from_wire_dict({}).allow_subscribers is False


# ─── normalize_join_mode ─────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["invite_only", "open", "request"])
def test_normalize_join_mode_passes_known_values(value):
    """Every known join mode round-trips as a plain ``str``."""
    got = normalize_join_mode(value)
    assert got == value
    assert type(got) is str


@pytest.mark.parametrize(
    "value",
    [None, "", "Open", "public", 7, 1.5, ["open"], {"open": 1}, object()],
)
def test_normalize_join_mode_fails_closed(value):
    """Anything unknown — missing, misspelled, hostile, non-string — becomes
    ``invite_only``, the mode that grants no public readership."""
    assert normalize_join_mode(value) == "invite_only"


def test_normalize_join_mode_accepts_the_enum_member():
    """A :class:`JoinMode` member (a ``StrEnum``) normalises to its value, so
    callers can pass the domain object straight through."""
    assert normalize_join_mode(JoinMode.OPEN) == "open"


# ─── mirrorable_remote_role ──────────────────────────────────────────────


def test_mirrorable_remote_roles_are_the_four_remote_seats():
    """The ``space_remote_members.role`` CHECK, in code. ``owner`` is
    absent: ownership is local-only and has no remote row shape."""
    assert MIRRORABLE_REMOTE_ROLES == {"member", "admin", "moderator", "subscriber"}


def test_a_real_remote_role_passes_through():
    for role in MIRRORABLE_REMOTE_ROLES:
        assert mirrorable_remote_role(role) == role


def test_an_owner_on_the_wire_coerces_down_to_member():
    """The host ships its own ``space_members.role`` on the roster wire,
    ``owner`` included, and a receiver's CHECK rejects it — which used to
    raise out of the whole apply and lose the mutation, tombstone and
    all."""
    assert mirrorable_remote_role(SpaceRole.OWNER.value) == SpaceRole.MEMBER.value


def test_an_unknown_future_role_coerces_down_rather_than_up():
    assert mirrorable_remote_role("overlord") == SpaceRole.MEMBER.value


def test_a_missing_role_reads_as_member():
    """A first-revision payload from a sender that knew only
    member/admin — the same answer."""
    assert mirrorable_remote_role(None) == SpaceRole.MEMBER.value
    assert mirrorable_remote_role("") == SpaceRole.MEMBER.value


# ─── normalize_retention_exempt_types ─────────────────────────────────────


def test_retention_exempt_types_cover_every_post_type():
    """The valid set is exactly the ``PostType`` enum — a new post type
    becomes exemptable without a second list to keep in sync."""
    every = [p.value for p in PostType]
    assert normalize_retention_exempt_types(every, strict=True) == tuple(sorted(every))


def test_retention_exempt_types_strict_rejects_unknown():
    with pytest.raises(ValueError, match="pages"):
        normalize_retention_exempt_types(["poll", "pages"], strict=True)


def test_retention_exempt_types_strict_rejects_non_list():
    # A bare string would otherwise iterate into single characters.
    with pytest.raises(ValueError):
        normalize_retention_exempt_types("poll", strict=True)


def test_retention_exempt_types_lenient_drops_unknown():
    assert normalize_retention_exempt_types(["pages", "poll", "gallery", 7, None]) == (
        "poll",
    )
    assert normalize_retention_exempt_types("poll") == ()
    assert normalize_retention_exempt_types({"poll": 1}) == ()


def test_retention_exempt_types_dedupes_strips_and_sorts():
    assert normalize_retention_exempt_types(
        [" schedule", "poll", "poll", "", "  "], strict=True
    ) == ("poll", "schedule")


def test_retention_exempt_types_none_is_empty():
    assert normalize_retention_exempt_types(None, strict=True) == ()


# ─── Role authority sets + the promotion matrix ───────────────────────────

_O, _A, _MOD, _M, _S = (
    SpaceRole.OWNER,
    SpaceRole.ADMIN,
    SpaceRole.MODERATOR,
    SpaceRole.MEMBER,
    SpaceRole.SUBSCRIBER,
)


def test_moderator_role_value():
    assert SpaceRole.MODERATOR.value == "moderator"
    assert [r.value for r in SpaceRole] == [
        "owner",
        "admin",
        "moderator",
        "member",
        "subscriber",
    ]


@pytest.mark.parametrize(
    ("role", "settings", "content", "writer"),
    [
        (_O, True, True, True),
        (_A, True, True, True),
        (_MOD, False, True, True),
        (_M, False, False, True),
        (_S, False, False, False),
    ],
)
def test_role_authority_matrix(role, settings, content, writer):
    """The three sets are the only in-code authority: a moderator has
    content authority and writes, but holds no settings power."""
    assert (role in SETTINGS_AUTHORITY_ROLES) is settings
    assert (role in CONTENT_AUTHORITY_ROLES) is content
    assert (role in WRITER_ROLES) is writer
    # The sets hold plain strings too (wire / row values compare equal).
    assert (role.value in CONTENT_AUTHORITY_ROLES) is content


def test_authority_sets_nest():
    assert SETTINGS_AUTHORITY_ROLES < CONTENT_AUTHORITY_ROLES < WRITER_ROLES


def test_moderator_is_mirrorable_but_owner_is_not():
    assert mirrorable_remote_role("moderator") == "moderator"
    assert mirrorable_remote_role("owner") == "member"


_ALLOWED: set[tuple[SpaceRole, SpaceRole, SpaceRole]] = {
    # The owner sets admin / moderator / member on any non-owner seat.
    *((_O, cur, new) for cur in (_A, _MOD, _M, _S) for new in (_A, _MOD, _M)),
    # An admin moves a seat only between member and moderator.
    *((_A, cur, new) for cur in (_MOD, _M) for new in (_MOD, _M)),
}


@pytest.mark.parametrize("actor", list(SpaceRole))
@pytest.mark.parametrize("current", list(SpaceRole))
@pytest.mark.parametrize("new", list(SpaceRole))
def test_role_change_matrix(actor, current, new):
    """Exhaustive 5x5x5 matrix. Nobody assigns ``owner`` (transfer does);
    nobody demotes the owner; an admin can't touch another admin or
    make one; moderators, members and subscribers change no roles."""
    expected = (actor, current, new) in _ALLOWED
    assert role_change_allowed(actor, current, new) is expected
    # Plain strings (route / row values) give the same answer.
    assert role_change_allowed(actor.value, current.value, new.value) is expected


def test_role_change_rejects_unknown_roles():
    assert role_change_allowed("owner", "member", "overlord") is False
    assert role_change_allowed("overlord", "member", "moderator") is False
    assert role_change_allowed("owner", "overlord", "member") is False


def test_moderated_keeps_an_own_edit_or_delete_for_a_read_only_seat():
    """A demoted (subscriber) author could always edit / delete their own
    post — MODERATED keeps that; ADMIN_ONLY does not."""
    mod = SpaceFeatures(posts_access=SpaceFeatureAccess.MODERATED)
    for action in (ContentAction.EDIT, ContentAction.DELETE):
        assert (
            mod.access_decision(
                "posts", role="subscriber", action=action, owns_target=True
            )
            is AccessDecision.PROCEED
        )
        assert (
            mod.access_decision(
                "posts", role="subscriber", action=action, owns_target=False
            )
            is AccessDecision.DENY
        )
        assert (
            mod.access_decision("posts", role=None, action=action, owns_target=True)
            is AccessDecision.DENY
        )
    ao = SpaceFeatures(posts_access=SpaceFeatureAccess.ADMIN_ONLY)
    assert (
        ao.access_decision(
            "posts", role="subscriber", action=ContentAction.DELETE, owns_target=True
        )
        is AccessDecision.DENY
    )


def test_cap_remote_role_never_raises_a_seat():
    from socialhome.domain.space import cap_remote_role

    assert cap_remote_role("admin", "member") == "member"
    assert cap_remote_role("moderator", "member") == "member"
    assert cap_remote_role("admin", "moderator") == "moderator"
    assert cap_remote_role("member", "admin") == "member"
    assert cap_remote_role("subscriber", "member") == "subscriber"
    assert cap_remote_role("admin", "admin") == "admin"
    # Unknown values coerce down to member before comparing.
    assert cap_remote_role("owner", "admin") == "member"
    assert cap_remote_role("admin", "bogus") == "member"


def test_owner_seat_from_roster_takes_only_the_hosts_own_owner_entry():
    roster = [
        {"user_id": "u-fake", "instance_id": "house-c", "role": "owner"},
        {"user_id": "u-bob", "instance_id": "host", "role": "member"},
        {"user_id": "u-anna", "instance_id": "host", "role": "owner"},
    ]
    assert owner_seat_from_roster(roster, "host") == "u-anna"
    # An "owner" on another household names nobody.
    assert owner_seat_from_roster(roster[:2], "host") is None
    assert owner_seat_from_roster(roster, "") is None
    assert owner_seat_from_roster(None, "host") is None
    assert owner_seat_from_roster(["junk", {"role": "owner"}], "host") is None


# ─── Zone display-data validation (§23.8.7) ──────────────────────────────


def test_validate_zone_name_strips_and_accepts_unicode():
    assert validate_zone_name("  Grandma's 🏡 Zürich  ") == "Grandma's 🏡 Zürich"
    # ZWJ emoji sequences are format characters, not control characters.
    assert validate_zone_name("👨\u200d👩\u200d👧") == "👨\u200d👩\u200d👧"


def test_validate_zone_name_accepts_exactly_the_cap():
    assert validate_zone_name("x" * MAX_ZONE_NAME_LENGTH) == "x" * 64


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        None,
        42,
        "x" * (MAX_ZONE_NAME_LENGTH + 1),
        "x" * 10_240,
        "Home\x00",
        "Ho\nme",
        "Ho\tme",
        "Home\x1b[31m",
        "Home\x7f",
        "Ho\x85me",
        "Ho\u2028me",
        "Ho\u2029me",
    ],
)
def test_validate_zone_name_rejects(bad):
    with pytest.raises(ValueError, match="zone name"):
        validate_zone_name(bad)


def test_validate_zone_name_error_never_echoes_the_name():
    with pytest.raises(ValueError) as exc:
        validate_zone_name("secret\x00<img src=x>")
    assert "secret" not in str(exc.value)


def test_validate_zone_color_normalises_hex_and_passes_none():
    assert validate_zone_color("#3B82F6") == "#3b82f6"
    assert validate_zone_color(None) is None


@pytest.mark.parametrize(
    "bad",
    [
        "red;background:url(x)",
        "#3b82f6;background:url(x)",
        "#3b82f6\n",
        "#fff",
        "3b82f6",
        "pink",
        "",
        123,
        ["#3b82f6"],
    ],
)
def test_validate_zone_color_rejects(bad):
    with pytest.raises(ValueError, match="color"):
        validate_zone_color(bad)


# ─── Space quick links ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("https://wiki.example/a?b=1#c", "https://wiki.example/a?b=1#c"),
        ("  http://wiki  ", "http://wiki"),
        ("HTTPS://Wiki.Example/", "HTTPS://Wiki.Example/"),
    ],
)
def test_validate_space_link_url_keeps_http(raw, want):
    assert validate_space_link_url(raw) == want


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "javascript:alert(1)",
        "java\tscript:alert(1)",
        "data:text/html,x",
        "ftp://x.example/",
        "mailto:a@b.example",
        "/spaces/x",
        "//wiki.example/",
        "wiki.example",
        "https://",
        "https:///path",
        "https://user:pw@wiki.example/",
        "https://wiki.example/a b",
        "https://wiki.example/\x00",
        "https://wiki.example/" + "a" * 2048,
        None,
        7,
    ],
)
def test_validate_space_link_url_rejects(bad):
    with pytest.raises(InvalidSpaceLinkError):
        validate_space_link_url(bad)


def test_invalid_space_link_error_is_a_coded_value_error():
    """Coded (422 ``INVALID_LINK``) for the SPA's translated line, and
    still a ``ValueError`` for existing callers."""
    assert issubclass(InvalidSpaceLinkError, ValueError)
    assert issubclass(InvalidSpaceLinkError, CodedError)
    exc = InvalidSpaceLinkError("url must be an http(s) web address")
    assert (exc.status, exc.code) == (422, "INVALID_LINK")
    assert exc.detail == "url must be an http(s) web address"
    assert exc.params == {}
    assert InvalidSpaceLinkError().detail.startswith("A link needs")


@pytest.mark.parametrize(
    ("value", "limit", "expected"),
    [
        (47.376912345, 90, 47.3769),
        (-8.541789, 180, -8.5418),
        ("12.5", 90, 12.5),
        (90, 90, 90.0),
        (-180, 180, -180.0),
        (-0.00001, 90, 0.0),
    ],
)
def test_validate_zone_coord_truncates_to_4dp(value, limit, expected):
    assert validate_zone_coord(value, name="latitude", limit=limit) == expected


@pytest.mark.parametrize(
    ("value", "limit"),
    [
        (float("nan"), 90),
        ("nan", 90),
        (float("inf"), 180),
        ("-inf", 180),
        (90.0001, 90),
        (-180.5, 180),
        (True, 90),
        (None, 90),
        ("north", 90),
        (10**400, 90),
    ],
)
def test_validate_zone_coord_refuses(value, limit):
    with pytest.raises(ValueError):
        validate_zone_coord(value, name="latitude", limit=limit)


@pytest.mark.parametrize(
    ("value", "expected"), [(25, 25), (50_000, 50_000), ("100", 100), (30.7, 30)]
)
def test_validate_zone_radius_accepts(value, expected):
    assert validate_zone_radius(value) == expected


@pytest.mark.parametrize(
    "value", [-1, 0, 24, 50_001, 10**12, float("nan"), float("inf"), True, None, "x"]
)
def test_validate_zone_radius_refuses(value):
    with pytest.raises(ValueError):
        validate_zone_radius(value)


@pytest.mark.parametrize(
    ("wire", "parsed"),
    [
        (0, 0),
        (9, 9),
        (2**63 - 1, 2**63 - 1),
        (2**63, None),
        (10**30, None),
        (None, None),
        (-1, None),
        (True, None),
        ("9", None),
        (9.0, None),
    ],
)
def test_parse_have_seq_keeps_only_a_non_negative_int(wire, parsed):
    """…that fits SQLite's signed 64-bit INTEGER: a larger value would raise
    ``OverflowError`` in the database writer and fail its whole batch."""
    """§25.6 (migration 0087): anything but a non-negative int is an absent
    echo — the provider then streams in full."""
    assert parse_have_seq(wire) == parsed
