"""Tests for socialhome.domain.notification."""

from __future__ import annotations

from socialhome.domain.notification import (
    ACTION_ITEM_TYPES,
    GROUP_QUIET_DAYS,
    MAX_ACTORS,
    Notification,
    NotificationActor,
    NotificationObject,
    NotificationParams,
    NotificationSubject,
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


def test_action_items_are_the_review_queue_rows():
    """Join requests, posts waiting for review and reports — the rows the
    prune keeps while unread and a handling admin resolves for all."""
    assert ACTION_ITEM_TYPES == {
        "space_join_requested",
        "moderation_pending",
        "space_report",
    }


# ── Subject, params and regrouping (0129, D7) ────────────────────────────


def test_subject_builds_kind_colon_id():
    s = NotificationSubject.of(
        NotificationObject.SPACE_POST, "p1", space_id="s1", space_name="Salon"
    )
    assert s == NotificationSubject("space_post:p1", "s1", "Salon")
    assert object_key(NotificationObject.CONVERSATION, "c1") == "dm:c1"


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
