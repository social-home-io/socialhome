"""Tests for socialhome.domain.notification_kinds (D4, D10)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from socialhome.domain.notification_kinds import (
    KIND_CHOICES,
    KIND_DEFAULTS,
    MUTED_POST_QUIET_DAYS,
    MUTED_POSTS_MAX,
    NotifDelivery,
    NotifKind,
    NotifSettings,
    SpaceNotifPref,
    kind_of,
    mutable_post_key,
    notif_kind,
)

# ── D4: the seven kinds and their one choice ──────────────────────────


def test_d4_every_kind_has_a_default_among_its_choices():
    assert set(KIND_CHOICES) == set(NotifKind)
    assert len(NotifKind) == 7
    for kind, choices in KIND_CHOICES.items():
        assert KIND_DEFAULTS[kind] in choices


def test_d4_needs_you_cannot_be_off_and_messages_have_no_in_app():
    assert KIND_CHOICES[NotifKind.NEEDS_YOU] == (
        NotifDelivery.PUSH,
        NotifDelivery.IN_APP,
    )
    assert KIND_CHOICES[NotifKind.MESSAGES] == (NotifDelivery.PUSH, NotifDelivery.OFF)


def test_d4_reactions_and_activity_default_to_in_app_the_rest_push():
    in_app = {k for k, d in KIND_DEFAULTS.items() if d is NotifDelivery.IN_APP}
    assert in_app == {NotifKind.REACTIONS, NotifKind.ACTIVITY}
    pushed = {k for k, d in KIND_DEFAULTS.items() if d is NotifDelivery.PUSH}
    assert pushed == set(NotifKind) - in_app


@pytest.mark.parametrize(
    ("ntype", "kind"),
    [
        ("dm_message", NotifKind.MESSAGES),
        ("post_replied", NotifKind.REPLIES),
        ("comment_replied", NotifKind.REPLIES),
        ("space_mention", NotifKind.REPLIES),
        ("space_here", NotifKind.REPLIES),
        ("dm_mention", NotifKind.REPLIES),
        ("moment_replied", NotifKind.REPLIES),
        ("space_join_requested", NotifKind.NEEDS_YOU),
        ("moderation_pending", NotifKind.NEEDS_YOU),
        ("space_report", NotifKind.NEEDS_YOU),
        ("dm_contact_requested", NotifKind.NEEDS_YOU),
        ("app_challenge", NotifKind.NEEDS_YOU),
        ("space_invite", NotifKind.NEEDS_YOU),
        ("pairing_request", NotifKind.NEEDS_YOU),
        ("task_assigned", NotifKind.PLANS),
        ("task_deadline", NotifKind.PLANS),
        ("calendar_event_updated", NotifKind.PLANS),
        ("calendar_reminder", NotifKind.PLANS),
        ("space_join_approved", NotifKind.ABOUT_YOU),
        ("join_answer", NotifKind.ABOUT_YOU),
        ("bazaar_bid_placed", NotifKind.ABOUT_YOU),
        ("post_moderated", NotifKind.ABOUT_YOU),
        ("space_dissolved", NotifKind.ABOUT_YOU),
        ("post_reacted", NotifKind.REACTIONS),
        ("moment_reacted", NotifKind.REACTIONS),
        ("user_followed", NotifKind.REACTIONS),
        ("post_created", NotifKind.ACTIVITY),
        ("comment_added", NotifKind.ACTIVITY),
        ("space_post_created", NotifKind.ACTIVITY),
        ("space_comment_added", NotifKind.ACTIVITY),
        ("space_member_joined", NotifKind.ACTIVITY),
    ],
)
def test_d4_types_fall_into_their_kind(ntype, kind):
    assert notif_kind(ntype) is kind


def test_d4_an_event_added_to_your_calendar_is_a_plan_one_in_a_space_activity():
    assert notif_kind("calendar_event_created") is NotifKind.PLANS
    assert notif_kind("calendar_event_created", space_id="s1") is NotifKind.ACTIVITY


def test_d4_an_unknown_type_has_no_kind():
    assert notif_kind("something_new") is None
    assert kind_of("something_new") is None
    assert kind_of("post_reacted") == "reactions"


def test_settings_read_the_choice_else_the_default():
    s = NotifSettings.from_dict(
        {"notif_kinds": {"reactions": "push", "plans": "off", "bogus": "push"}}
    )
    assert s.delivery(NotifKind.REACTIONS) is NotifDelivery.PUSH
    assert s.delivery(NotifKind.PLANS) is NotifDelivery.OFF
    assert s.delivery(NotifKind.REPLIES) is NotifDelivery.PUSH
    assert s.delivery(NotifKind.ACTIVITY) is NotifDelivery.IN_APP
    assert s.chosen == {
        NotifKind.REACTIONS: NotifDelivery.PUSH,
        NotifKind.PLANS: NotifDelivery.OFF,
    }


def test_settings_never_take_a_choice_the_kind_does_not_offer():
    s = NotifSettings.from_dict(
        {"notif_kinds": {"needs_you": "off", "messages": "in_app", "replies": 3}}
    )
    assert s.delivery(NotifKind.NEEDS_YOU) is NotifDelivery.PUSH
    assert s.delivery(NotifKind.MESSAGES) is NotifDelivery.PUSH
    assert s.delivery(NotifKind.REPLIES) is NotifDelivery.PUSH
    assert s.chosen == {}


@pytest.mark.parametrize(
    "raw", [None, [], "x", {"notif_kinds": "x", "notif_muted_posts": 5}]
)
def test_settings_from_garbage_are_the_defaults(raw):
    s = NotifSettings.from_dict(raw)
    assert s.chosen == {}
    assert s.muted_posts == {}


def test_valid_choice():
    assert KIND_CHOICES[NotifKind.REPLIES] == (
        NotifDelivery.PUSH,
        NotifDelivery.IN_APP,
        NotifDelivery.OFF,
    )


# ── D10: mute a post ──────────────────────────────────────────────────


_NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


def test_d10_a_muted_post_is_muted_until_90_quiet_days():
    fresh = (_NOW - timedelta(days=89)).isoformat()
    stale = (_NOW - timedelta(days=91)).isoformat()
    s = NotifSettings.from_dict(
        {"notif_muted_posts": {"post:a": fresh, "space_post:b": stale}}
    )
    assert s.post_muted("post:a", now=_NOW)
    assert not s.post_muted("space_post:b", now=_NOW)
    assert not s.post_muted("post:zzz", now=_NOW)
    assert not s.post_muted(None, now=_NOW)
    assert s.active_muted_posts(now=_NOW) == {"post:a": fresh}


def test_d10_only_posts_can_be_muted():
    assert mutable_post_key("post:abc")
    assert mutable_post_key("space_post:abc-1_2")
    assert not mutable_post_key("dm:abc")
    assert not mutable_post_key("post:")
    assert not mutable_post_key("post:a b")
    assert not mutable_post_key("post:" + "a" * 200)
    assert not mutable_post_key(5)


def test_d10_muted_posts_drop_bad_entries():
    s = NotifSettings.from_dict(
        {
            "notif_muted_posts": {
                "post:a": "not a time",
                "dm:x": _NOW.isoformat(),
                "post:b": 1,
            }
        }
    )
    assert s.muted_posts == {}


def test_d10_a_naive_stamp_reads_as_utc():
    s = NotifSettings.from_dict(
        {"notif_muted_posts": {"post:a": "2026-10-27 12:00:00"}}
    )
    assert s.post_muted("post:a", now=_NOW)


def test_d10_a_mute_stays_fresh_while_the_post_is_active():
    old = (_NOW - timedelta(days=2)).isoformat()
    new = (_NOW - timedelta(hours=2)).isoformat()
    s = NotifSettings.from_dict({"notif_muted_posts": {"post:a": old, "post:b": new}})
    assert s.needs_refresh("post:a", now=_NOW)
    assert not s.needs_refresh("post:b", now=_NOW)
    assert not s.needs_refresh("post:c", now=_NOW)


def test_d10_the_bounds():
    assert MUTED_POSTS_MAX == 200
    assert MUTED_POST_QUIET_DAYS == 90


def test_d10_with_muted_post_caps_at_200_dropping_the_quietest():
    posts = {
        f"post:{i}": (_NOW - timedelta(minutes=i)).isoformat()
        for i in range(MUTED_POSTS_MAX)
    }
    s = NotifSettings.from_dict({"notif_muted_posts": posts})
    out = s.with_muted_post("post:new", now=_NOW)
    assert len(out) == MUTED_POSTS_MAX
    assert "post:new" in out
    assert f"post:{MUTED_POSTS_MAX - 1}" not in out  # the oldest went
    assert "post:0" in out


def test_d10_with_muted_post_drops_expired_ones():
    stale = (_NOW - timedelta(days=100)).isoformat()
    s = NotifSettings.from_dict({"notif_muted_posts": {"post:old": stale}})
    assert s.with_muted_post("post:new", now=_NOW) == {"post:new": _NOW.isoformat()}


# ── D9: a space muted for a while ─────────────────────────────────────


def test_d9_a_timed_mute_reads_as_nothing_then_the_level_before():
    pref = SpaceNotifPref("mentions", (_NOW + timedelta(hours=1)).isoformat())
    assert pref.muted_now(_NOW)
    assert pref.effective(_NOW) == "muted"
    later = _NOW + timedelta(hours=2)
    assert not pref.muted_now(later)
    assert pref.effective(later) == "mentions"


def test_d9_without_a_time_the_level_is_the_level():
    assert SpaceNotifPref("muted").effective(_NOW) == "muted"
    assert SpaceNotifPref("all", "garbage").effective(_NOW) == "all"
