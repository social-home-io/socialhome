"""Calls and small decisions reach you (notifications redesign, PR 16).

* ``call_incoming`` — a push, no row, when a call rings for you: "Anna is
  calling" / "Anna is calling in Team" (D11 who + where). Its tag is the
  missed call's, so a missed call replaces the ring on the device.
* ``call_missed`` — a row (one per chat, D7) + a push: "Missed call from
  Anna". Both honour the chat mute (D9) and Messages and calls (D4).
* ``chat_added`` — someone put you in a group chat → a row.
* ``poll_result`` — a poll you voted in was closed, or reached its deadline
  → a row (Plans and tasks).
* ``poll_scheduled`` — a date poll you answered got its time → a row.
* ``report_answer`` — a report you filed was reviewed → a row (About you);
  never names the moderator or whom the report was about.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.conversation import (
    Conversation,
    ConversationMember,
    ConversationType,
)
from socialhome.domain.events import (
    CallMissed,
    CallRinging,
    DmConversationCreated,
    PollClosed,
    RemoteSchedulePollFinalized,
    ReportResolved,
    SchedulePollFinalized,
)
from socialhome.domain.notification_kinds import KNOWN_TYPES, NotifKind, notif_kind
from socialhome.i18n import Catalog
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.conversation_repo import SqliteConversationRepo
from socialhome.repositories.notification_repo import SqliteNotificationRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.notification_service import NotificationService
from socialhome.services.user_service import UserService

_CATALOG = Path(__file__).resolve().parents[2] / "socialhome" / "i18n" / "messages"


class _Push:
    def __init__(self) -> None:
        self.sent: list[tuple[str, object]] = []

    async def push_to_user(self, user_id, payload) -> None:
        self.sent.append((user_id, payload))

    def to(self, user) -> list:
        return [p for uid, p in self.sent if uid == user.user_id]

    def titles(self, user) -> list[str]:
        return [p.title for p in self.to(user)]


@dataclass
class _Report:
    id: str
    reporter_user_id: str
    space_id: str | None = None


class _Reports:
    def __init__(self) -> None:
        self.rows: dict[str, _Report] = {}

    async def get(self, report_id: str):
        return self.rows.get(report_id)

    async def count_pending_in_space(self, space_id: str, **_kw) -> int:
        return 0


class _Polls:
    def __init__(self, author: str | None = None) -> None:
        self.voters: dict[str, list[str]] = {}
        self.answers: dict[str, list[dict]] = {}
        self.author = author

    async def list_voter_ids(self, post_id: str) -> list[str]:
        return self.voters.get(post_id, [])

    async def list_schedule_responses(self, post_id: str) -> list[dict]:
        return self.answers.get(post_id, [])

    async def get_post_author(self, post_id: str) -> str | None:
        return self.author


@pytest.fixture
async def s(tmp_dir):
    kp = generate_identity_keypair()
    db = AsyncDatabase(tmp_dir / "calls.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (
            derive_instance_id(kp.public_key),
            kp.private_key.hex(),
            kp.public_key.hex(),
            "aa" * 32,
        ),
    )
    bus = EventBus()
    users = SqliteUserRepo(db)
    spaces = SqliteSpaceRepo(db, key_manager=KeyManager(b"\x0c" * 32))
    notifs = SqliteNotificationRepo(db, max_per_user=200)
    convos = SqliteConversationRepo(db)
    svc = NotificationService(
        notifs,
        users,
        spaces,
        bus,
        conversation_repo=convos,
        i18n=Catalog.from_directory(_CATALOG),
    )
    push = _Push()
    svc.attach_push_service(push)
    reports = _Reports()
    svc.attach_report_repo(reports)
    polls, space_polls = _Polls(), _Polls()
    svc.attach_poll_repos(polls, space_polls)
    svc.wire()
    user_svc = UserService(users, bus, own_instance_public_key=kp.public_key)

    class S:
        pass

    st = S()
    st.db, st.bus, st.notifs, st.push, st.convos, st.svc = (
        db,
        bus,
        notifs,
        push,
        convos,
        svc,
    )
    st.users, st.reports, st.polls, st.space_polls = users, reports, polls, space_polls

    async def user(name: str, locale: str = "en", **prefs):
        await user_svc.provision(username=name, display_name=name.title())
        await db.enqueue("UPDATE users SET locale=? WHERE username=?", (locale, name))
        if prefs:
            await user_svc.patch_preferences(name, prefs)
        return await users.get(name)

    st.user = user
    yield st
    await db.shutdown()


async def _rows(s, user):
    return await s.notifs.list(user.user_id, limit=50)


async def _chat(s, *people, cid="c1", group=False, name=None) -> None:
    await s.convos.create(
        Conversation(
            id=cid,
            type=ConversationType.GROUP_DM if group else ConversationType.DM,
            created_at=datetime.now(UTC) - timedelta(hours=1),
            name=name,
        )
    )
    for u in people:
        await s.convos.add_member(
            ConversationMember(
                conversation_id=cid,
                username=u.username,
                joined_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
            )
        )


def _ring(caller, *callees, cid="c1", call_id="call-1") -> CallRinging:
    return CallRinging(
        call_id=call_id,
        conversation_id=cid,
        caller_user_id=caller.user_id,
        callee_user_ids=tuple(u.user_id for u in callees),
    )


def _missed(caller, *callees, cid="c1", call_id="call-1") -> CallMissed:
    return CallMissed(
        call_id=call_id,
        conversation_id=cid,
        caller_user_id=caller.user_id,
        callee_user_ids=tuple(u.user_id for u in callees),
    )


def test_every_new_type_is_filed_under_its_kind():
    assert notif_kind("call_incoming") is NotifKind.MESSAGES
    assert notif_kind("call_missed") is NotifKind.MESSAGES
    assert notif_kind("chat_added") is NotifKind.REPLIES
    assert notif_kind("poll_result") is NotifKind.PLANS
    assert notif_kind("report_answer") is NotifKind.ABOUT_YOU
    # No flow decides on a page someone proposed: nothing files it.
    assert "page_decision" not in KNOWN_TYPES


# ── call_incoming ─────────────────────────────────────────────────────


async def test_an_incoming_call_pushes_who_is_calling_and_makes_no_row(s):
    anna, bob = await s.user("anna"), await s.user("bob")
    await _chat(s, anna, bob)
    await s.bus.publish(_ring(anna, bob))
    (push,) = s.push.to(bob)
    assert push.title == "Anna is calling"
    assert push.click_url == "/dms/c1"
    # The missed call reuses the tag, so it replaces the ring.
    assert push.tag == "call_missed:/dms/c1"
    assert await _rows(s, bob) == []
    assert s.push.to(anna) == []


async def test_a_group_call_says_where(s):
    anna, bob, cid = await s.user("anna"), await s.user("bob"), await s.user("cid")
    await _chat(s, anna, bob, cid, group=True, name="Team")
    await s.bus.publish(_ring(anna, bob, cid))
    assert s.push.titles(bob) == ["Anna is calling in Team"]
    assert s.push.titles(cid) == ["Anna is calling in Team"]


async def test_an_incoming_call_in_the_recipients_language(s):
    anna = await s.user("anna")
    bob = await s.user("bob", locale="de")
    await _chat(s, anna, bob)
    await s.bus.publish(_ring(anna, bob))
    assert s.push.titles(bob) == ["Anna ruft an"]


async def test_a_muted_chat_does_not_ring_the_phone(s):
    anna, bob = await s.user("anna"), await s.user("bob")
    await _chat(s, anna, bob)
    soon = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    await s.convos.set_muted_until("c1", bob.username, soon)
    await s.bus.publish(_ring(anna, bob))
    await s.bus.publish(_missed(anna, bob))
    assert s.push.to(bob) == []
    assert await _rows(s, bob) == []


async def test_messages_and_calls_off_means_no_call_push_and_no_row(s):
    anna = await s.user("anna")
    bob = await s.user("bob", notif_kinds={"messages": "off"})
    await _chat(s, anna, bob)
    await s.bus.publish(_ring(anna, bob))
    await s.bus.publish(_missed(anna, bob))
    assert s.push.to(bob) == []
    assert await _rows(s, bob) == []


async def test_a_blocked_caller_rings_nobody(s):
    anna, bob = await s.user("anna"), await s.user("bob")
    await _chat(s, anna, bob)
    await s.users.block(bob.user_id, anna.user_id)
    await s.bus.publish(_ring(anna, bob))
    await s.bus.publish(_missed(anna, bob))
    assert s.push.to(bob) == []
    assert await _rows(s, bob) == []


async def test_a_remote_callee_is_left_to_their_own_household(s):
    anna = await s.user("anna")
    await _chat(s, anna)
    await s.bus.publish(
        CallRinging(
            call_id="x",
            conversation_id="c1",
            caller_user_id=anna.user_id,
            callee_user_ids=("u-remote",),
        )
    )
    assert s.push.sent == []


# ── call_missed ───────────────────────────────────────────────────────


async def test_a_missed_call_is_a_row_and_a_push_that_replaces_the_ring(s):
    anna, bob = await s.user("anna"), await s.user("bob")
    await _chat(s, anna, bob)
    await s.bus.publish(_ring(anna, bob))
    await s.bus.publish(_missed(anna, bob))
    (row,) = await _rows(s, bob)
    assert row.type == "call_missed"
    assert row.title == "Missed call from Anna"
    assert row.link_url == "/dms/c1"
    assert row.object_key == "dm:c1"
    assert [(a.id, a.name) for a in row.params.actors] == [(anna.user_id, "Anna")]
    ring, missed = s.push.to(bob)
    assert missed.title == "Missed call from Anna"
    assert missed.tag == ring.tag


async def test_missed_calls_group_on_the_chat_and_each_replaces_its_ring(s):
    """D7: one row per chat. Every missed call still pushes — the device
    shows "is calling" from its ring until the missed call replaces it."""
    anna, bob = await s.user("anna"), await s.user("bob")
    await _chat(s, anna, bob)
    for n in (1, 2):
        await s.bus.publish(_ring(anna, bob, call_id=f"call-{n}"))
        await s.bus.publish(_missed(anna, bob, call_id=f"call-{n}"))
    (row,) = await _rows(s, bob)
    assert row.params.new == 2
    assert s.push.titles(bob) == [
        "Anna is calling",
        "Missed call from Anna",
        "Anna is calling",
        "Missed call from Anna",
    ]


async def test_a_missed_call_in_a_named_group_names_the_chat(s):
    anna, bob, cid = await s.user("anna"), await s.user("bob"), await s.user("cid")
    await _chat(s, anna, bob, cid, group=True, name="Team")
    await s.bus.publish(_missed(anna, bob))
    (row,) = await _rows(s, bob)
    assert row.title == "Missed call from Anna in Team"
    # The chat is the place the client's sentence names.
    assert row.params.space_name == "Team"
    assert row.space_id is None


async def test_a_missed_call_in_german(s):
    anna = await s.user("anna")
    bob = await s.user("bob", locale="de")
    await _chat(s, anna, bob)
    await s.bus.publish(_missed(anna, bob))
    assert s.push.titles(bob) == ["Verpasster Anruf von Anna"]


async def test_opening_the_chat_reads_the_missed_call(s):
    anna, bob = await s.user("anna"), await s.user("bob")
    await _chat(s, anna, bob)
    await s.bus.publish(_missed(anna, bob))
    assert await s.svc.mark_read_for_dm(bob.user_id, "c1") == 1
    (row,) = await _rows(s, bob)
    assert row.is_read


# ── chat_added ────────────────────────────────────────────────────────


def _added(cid, adder, *people, kind="group_dm", name="Team"):
    return DmConversationCreated(
        conversation_id=cid,
        conversation_type=kind,
        name=name,
        creator_user_id=adder.user_id if adder else "",
        member_user_ids=tuple(u.user_id for u in people),
    )


async def test_being_added_to_a_group_is_a_row_naming_who_and_where(s):
    anna, bob = await s.user("anna"), await s.user("bob")
    await _chat(s, anna, bob, group=True, name="Team")
    await s.bus.publish(_added("c1", anna, anna, bob))
    (row,) = await _rows(s, bob)
    assert (row.type, row.title, row.link_url) == (
        "chat_added",
        "Anna added you to Team",
        "/dms/c1",
    )
    assert s.push.titles(bob) == ["Anna added you to Team"]
    assert await _rows(s, anna) == []  # the one who added nobody


async def test_added_by_another_household_says_you_were_added(s):
    bob = await s.user("bob", locale="nl")
    await _chat(s, bob, group=True, name=None)
    await s.bus.publish(_added("c1", None, bob, name=None))
    (row,) = await _rows(s, bob)
    assert row.title == "Je bent toegevoegd aan een groepschat"
    assert row.params.actors == ()


async def test_a_new_one_to_one_chat_is_not_being_added(s):
    anna, bob = await s.user("anna"), await s.user("bob")
    await _chat(s, anna, bob)
    await s.bus.publish(_added("c1", anna, anna, bob, kind="dm", name=None))
    assert await _rows(s, bob) == []


async def test_replies_and_mentions_off_means_no_added_row(s):
    anna = await s.user("anna")
    bob = await s.user("bob", notif_kinds={"replies": "off"})
    await _chat(s, anna, bob, group=True, name="Team")
    await s.bus.publish(_added("c1", anna, bob))
    assert await _rows(s, bob) == []


# ── poll_result ───────────────────────────────────────────────────────


async def test_a_closed_poll_tells_who_voted_never_the_author(s):
    anna, bob, cid = await s.user("anna"), await s.user("bob"), await s.user("cid")
    s.polls.voters["p1"] = [anna.user_id, bob.user_id]
    await s.bus.publish(PollClosed(post_id="p1", actor_user_id=anna.user_id))
    (row,) = await _rows(s, bob)
    assert (row.type, row.title, row.link_url, row.object_key) == (
        "poll_result",
        "Anna closed a poll you voted in",
        "/post/p1",
        "post:p1",
    )
    assert s.push.titles(bob) == ["Anna closed a poll you voted in"]
    assert await _rows(s, anna) == []
    assert await _rows(s, cid) == []  # didn't vote


async def test_a_closed_space_poll_says_where(s):
    anna, bob = await s.user("anna"), await s.user("bob", locale="fr")
    await s.db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key, config_sequence) VALUES(?,?,?,?,?,?)",
        ("sp1", "Crew", "iid", "anna", "00" * 32, 0),
    )
    s.space_polls.voters["p1"] = [bob.user_id]
    s.space_polls.author = anna.user_id
    # A close from a peer carries no actor here: the repo names the author.
    await s.bus.publish(
        PollClosed(post_id="p1", space_id="sp1", origin_instance_id="peer")
    )
    (row,) = await _rows(s, bob)
    assert row.title == "Anna a clos un sondage dans Crew"
    assert row.link_url == "/spaces/sp1?post=p1"
    assert row.space_id == "sp1"


async def test_plans_off_means_no_poll_row(s):
    anna = await s.user("anna")
    bob = await s.user("bob", notif_kinds={"plans": "off"})
    s.polls.voters["p1"] = [bob.user_id]
    await s.bus.publish(PollClosed(post_id="p1", actor_user_id=anna.user_id))
    assert await _rows(s, bob) == []


async def test_a_poll_nobody_voted_in_tells_nobody(s):
    anna = await s.user("anna")
    await s.bus.publish(PollClosed(post_id="p9", actor_user_id=anna.user_id))
    assert s.push.sent == []


async def test_a_poll_at_its_deadline_has_ended_for_every_voter(s):
    """Nobody closed it: no actor, and the author hears too if they voted."""
    anna, bob = await s.user("anna"), await s.user("bob", locale="de")
    s.polls.voters["p1"] = [anna.user_id, bob.user_id]
    await s.bus.publish(
        PollClosed(post_id="p1", actor_user_id=anna.user_id, at_deadline=True)
    )
    (row,) = await _rows(s, anna)
    assert (row.type, row.title, row.object_key) == (
        "poll_result",
        "A poll you voted in has ended",
        "post:p1",
    )
    assert row.params.actors == ()
    (row,) = await _rows(s, bob)
    assert row.title == "Eine Umfrage, bei der du abgestimmt hast, ist beendet"


async def test_a_space_poll_at_its_deadline_names_the_space(s):
    bob = await s.user("bob")
    await s.db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key, config_sequence) VALUES(?,?,?,?,?,?)",
        ("sp1", "Crew", "iid", "anna", "00" * 32, 0),
    )
    s.space_polls.voters["p1"] = [bob.user_id, "u-remote"]
    await s.bus.publish(
        PollClosed(
            post_id="p1", space_id="sp1", origin_instance_id="peer", at_deadline=True
        )
    )
    (row,) = await _rows(s, bob)
    assert row.title == "A poll in Crew has ended"
    assert row.link_url == "/spaces/sp1?post=p1"


# ── poll_scheduled ────────────────────────────────────────────────────


async def test_a_picked_time_tells_who_answered_never_the_organiser(s):
    anna, bob, cid = await s.user("anna"), await s.user("bob"), await s.user("cid")
    s.polls.answers["p1"] = [
        {"slot_id": "s1", "user_id": bob.user_id, "availability": "yes"},
        {"slot_id": "s2", "user_id": bob.user_id, "availability": "no"},
        {"slot_id": "s1", "user_id": anna.user_id, "availability": "yes"},
    ]
    await s.bus.publish(
        SchedulePollFinalized(
            post_id="p1",
            slot_id="s1",
            slot_date="2026-11-02",
            start_time=None,
            end_time=None,
            title="Dinner",
            finalized_by=anna.user_id,
        )
    )
    (row,) = await _rows(s, bob)  # one row, however many slots they answered
    assert (row.type, row.title, row.link_url, row.object_key) == (
        "poll_scheduled",
        "Anna picked a time for a date poll you answered",
        "/post/p1",
        "post:p1",
    )
    assert "Dinner" not in row.title
    assert s.push.titles(bob) == ["Anna picked a time for a date poll you answered"]
    assert await _rows(s, anna) == []
    assert await _rows(s, cid) == []


async def test_a_peers_picked_time_tells_who_answered_here(s):
    anna, bob = await s.user("anna"), await s.user("bob", locale="nl")
    await s.db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key, config_sequence) VALUES(?,?,?,?,?,?)",
        ("sp1", "Crew", "iid", "anna", "00" * 32, 0),
    )
    s.space_polls.answers["p1"] = [
        {"slot_id": "s1", "user_id": bob.user_id, "availability": "maybe"},
        {"slot_id": "s1", "user_id": "u-remote", "availability": "yes"},
    ]
    await s.bus.publish(
        RemoteSchedulePollFinalized(
            post_id="p1",
            space_id="sp1",
            finalized_by=anna.user_id,
            origin_instance_id="peer",
        )
    )
    (row,) = await _rows(s, bob)
    assert row.title == "Anna heeft in Crew een tijd gekozen"
    assert row.link_url == "/spaces/sp1?post=p1"
    assert notif_kind("poll_scheduled") is NotifKind.PLANS


# ── report_answer ─────────────────────────────────────────────────────


async def test_the_reporter_hears_their_report_was_reviewed_and_nothing_more(s):
    mod, bob = await s.user("mod"), await s.user("bob", locale="es")
    s.reports.rows["r1"] = _Report(id="r1", reporter_user_id=bob.user_id)
    await s.bus.publish(ReportResolved(report_id="r1", resolved_by=mod.user_id))
    (row,) = await _rows(s, bob)
    assert (row.type, row.title, row.object_key) == (
        "report_answer",
        "Se revisó tu denuncia",
        "report:r1",
    )
    # Never who reviewed it, never whom it was about.
    assert row.params.actors == ()
    assert s.push.titles(bob) == ["Se revisó tu denuncia"]


async def test_a_space_report_answer_names_the_space(s):
    mod, bob = await s.user("mod"), await s.user("bob")
    await s.db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key, config_sequence) VALUES(?,?,?,?,?,?)",
        ("sp1", "Crew", "iid", "mod", "00" * 32, 0),
    )
    s.reports.rows["r1"] = _Report(
        id="r1", reporter_user_id=bob.user_id, space_id="sp1"
    )
    await s.bus.publish(
        ReportResolved(report_id="r1", resolved_by=mod.user_id, space_id="sp1")
    )
    (row,) = await _rows(s, bob)
    assert row.title == "Your report in Crew was reviewed"
    assert row.link_url == "/spaces/sp1"


async def test_resolving_your_own_report_or_a_remote_ones_tells_nobody(s):
    bob = await s.user("bob")
    s.reports.rows["r1"] = _Report(id="r1", reporter_user_id=bob.user_id)
    s.reports.rows["r2"] = _Report(id="r2", reporter_user_id="u-remote")
    await s.bus.publish(ReportResolved(report_id="r1", resolved_by=bob.user_id))
    await s.bus.publish(ReportResolved(report_id="r2", resolved_by=bob.user_id))
    await s.bus.publish(ReportResolved(report_id="gone", resolved_by=bob.user_id))
    assert await _rows(s, bob) == []
