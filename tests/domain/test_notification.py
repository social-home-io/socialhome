"""Tests for socialhome.domain.notification."""

from __future__ import annotations

from socialhome.domain.notification import (
    ACTION_ITEM_TYPES,
    GROUP_QUIET_DAYS,
    HOUSEHOLD_FEED_ID,
    MAX_ACTORS,
    Notification,
    NotificationActor,
    NotificationHandled,
    NotificationObject,
    NotificationParams,
    NotificationSubject,
    SpaceNotifLevel,
    default_space_notif_level,
    first_params,
    object_key,
    regroup,
)


def test_is_read_follows_read_at():
    note = Notification(id="n", user_id="u", type="t", title="t", created_at="x")
    assert not note.is_read
    assert Notification(
        id="n", user_id="u", type="t", title="t", created_at="x", read_at="y"
    ).is_read


def test_action_items_are_the_needs_you_rows():
    """Join requests, the review queue (posts, reports), invites and
    pairing requests (D12) — the rows the prune keeps while unread and that
    stay pinned until handled."""
    assert ACTION_ITEM_TYPES == {
        "space_join_requested",
        "moderation_pending",
        "space_report",
        "space_invite",
        "pairing_request",
    }


def _note(type: str, params: NotificationParams | None = None) -> Notification:
    return Notification(
        id="n", user_id="u", type=type, title="t", created_at="x", params=params
    )


def test_a_needs_you_row_waits_until_handled_not_until_read():
    assert _note("space_join_requested").is_pending_action
    read = Notification(
        id="n",
        user_id="u",
        type="space_invite",
        title="t",
        created_at="x",
        read_at="y",
    )
    assert read.is_pending_action  # reading it never un-pins it
    handled = NotificationParams(handled=NotificationHandled("approved"))
    assert not _note("space_join_requested", handled).is_pending_action
    assert not _note("comment_added").is_pending_action


def test_flags_and_handled_round_trip_through_a_dict():
    params = NotificationParams(
        flags=("edge", "unpaired"),
        handled=NotificationHandled("declined", NotificationActor("u1", "Søren")),
    )
    raw = params.to_dict()
    assert raw["flags"] == ["edge", "unpaired"]
    assert raw["handled"] == {
        "outcome": "declined",
        "by": {"id": "u1", "name": "Søren"},
    }
    assert NotificationParams.from_dict(raw) == params
    # Nothing to say: neither key is written.
    assert "flags" not in NotificationParams().to_dict()
    assert "handled" not in NotificationParams().to_dict()


def test_unknown_flags_and_outcomes_never_break_a_row():
    params = NotificationParams.from_dict(
        {
            "flags": ["edge", "nonsense", 3, {"x": 1}, "edge"],
            "handled": {"outcome": "teleported", "by": {"name": "no id"}},
        }
    )
    assert params is not None
    assert params.flags == ("edge",)
    assert params.handled == NotificationHandled("done", None)
    assert NotificationParams.from_dict({"flags": "edge"}).flags == ()
    assert NotificationHandled.from_dict("approved") is None


def test_new_activity_on_a_handled_queue_row_is_pending_again():
    old = NotificationParams(handled=NotificationHandled("done"), total=2)
    fresh = regroup(old, None, reopen=True, flags=("age",))
    assert fresh.handled is None
    assert fresh.flags == ("age",)
    assert fresh.total == 3
    assert regroup(old, None, reopen=False).handled is None


# ── Subject, params and regrouping (0129, D7) ────────────────────────────


def test_subject_builds_kind_colon_id():
    s = NotificationSubject.of(
        NotificationObject.SPACE_POST, "p1", space_id="s1", space_name="Salon"
    )
    assert s == NotificationSubject("space_post:p1", "s1", "Salon")
    assert object_key(NotificationObject.CONVERSATION, "c1") == "dm:c1"


def test_feed_keys_group_new_posts_per_feed():
    """D5: one key for the household feed, one per space feed."""
    assert object_key(NotificationObject.FEED, HOUSEHOLD_FEED_ID) == "feed:household"
    assert object_key(NotificationObject.SPACE_FEED, "s1") == "space_feed:s1"


def test_params_round_trip_through_a_dict():
    p = NotificationParams(
        actors=(NotificationActor("u1", "Ben"), NotificationActor("u2", None)),
        actor_count=2,
        new=3,
        total=5,
        space_name="Salon",
    )
    d = p.to_dict()
    assert d == {
        "actors": [{"id": "u1", "name": "Ben"}, {"id": "u2", "name": None}],
        "actor_count": 2,
        "new": 3,
        "total": 5,
        "space_name": "Salon",
    }
    assert NotificationParams.from_dict(d) == p
    assert "space_name" not in NotificationParams().to_dict()


def test_params_from_a_bad_dict_fall_back():
    assert NotificationParams.from_dict(None) is None
    assert NotificationParams.from_dict("x") is None
    p = NotificationParams.from_dict(
        {"actors": [{"id": 1}, "x", {"id": "u1", "name": 3}], "new": -1, "total": True}
    )
    assert p == NotificationParams(
        actors=(NotificationActor("u1", None),), actor_count=1, new=1, total=1
    )


def test_first_params_name_the_actor_or_nobody():
    ben = NotificationActor("u1", "Ben")
    assert first_params(ben, space_name="S") == NotificationParams(
        actors=(ben,), actor_count=1, new=1, total=1, space_name="S"
    )
    assert first_params(None) == NotificationParams(actor_count=0)


def test_an_unread_bump_adds_to_new_and_moves_the_actor_up():
    ben, anna = NotificationActor("u1", "Ben"), NotificationActor("u2", "Anna")
    p = regroup(first_params(ben), anna, reopen=False)
    assert p.actors == (anna, ben)
    assert (p.actor_count, p.new, p.total) == (2, 2, 2)
    p = regroup(p, ben, reopen=False)  # Ben again: counted once
    assert p.actors == (ben, anna)
    assert (p.actor_count, p.new, p.total) == (2, 3, 3)
    p = regroup(p, None, reopen=False)  # nobody acted: counts only
    assert (p.actors, p.actor_count, p.new) == ((ben, anna), 2, 4)


def test_a_reopen_names_only_what_is_new():
    ben, anna = NotificationActor("u1", "Ben"), NotificationActor("u2", "Anna")
    old = regroup(first_params(ben, space_name="S"), anna, reopen=False)
    p = regroup(old, ben, reopen=True)
    assert p == NotificationParams(
        actors=(ben,), actor_count=1, new=1, total=3, space_name="S"
    )


def test_a_row_from_before_0129_regroups_as_one_earlier_event():
    ben = NotificationActor("u1", "Ben")
    assert regroup(None, ben, reopen=False) == NotificationParams(
        actors=(ben,), actor_count=1, new=2, total=2
    )


def test_the_actor_list_is_bounded_but_still_counts():
    p = first_params(NotificationActor("u0"))
    for i in range(1, MAX_ACTORS + 5):
        p = regroup(p, NotificationActor(f"u{i}"), reopen=False)
    assert len(p.actors) == MAX_ACTORS
    assert p.actors[0].id == f"u{MAX_ACTORS + 4}"
    assert p.actor_count == MAX_ACTORS + 5


def test_rows_regroup_for_a_week():
    assert GROUP_QUIET_DAYS == 7


# ── D5: the default level by space type ───────────────────────────────


def test_d5_household_and_private_spaces_default_to_everything():
    for space_type in ("household", "private"):
        assert default_space_notif_level(space_type, "member") is SpaceNotifLevel.ALL
        assert default_space_notif_level(space_type, "owner") is SpaceNotifLevel.ALL


def test_d5_public_and_global_spaces_default_to_replies_and_mentions():
    for space_type in ("public", "global"):
        assert (
            default_space_notif_level(space_type, "admin") is SpaceNotifLevel.MENTIONS
        )


def test_d5_a_followed_space_defaults_to_replies_and_mentions():
    for space_type in ("household", "private", "public", "global"):
        assert (
            default_space_notif_level(space_type, "subscriber")
            is SpaceNotifLevel.MENTIONS
        )


def test_d5_an_unknown_space_type_errs_quiet():
    assert default_space_notif_level("galaxy", "member") is SpaceNotifLevel.MENTIONS
