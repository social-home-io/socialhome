"""Tests for socialhome.services.calendar_service."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from socialhome.crypto import generate_identity_keypair, derive_instance_id
from socialhome.db.database import AsyncDatabase
from socialhome.domain.calendar import CalendarEvent, CalendarRSVP, RSVPStatus
from socialhome.domain.events import (
    CalendarEventCreated,
    CalendarEventUpdated,
)
from socialhome.repositories.calendar_repo import (
    SqliteCalendarRepo,
    SqliteSpaceCalendarRepo,
)
from socialhome.services.calendar_service import CalendarService


# Event seeds are anchored in the near future so the service's
# past-occurrence RSVP lock (``cannot RSVP to an occurrence that has
# already ended``) never trips once the wall clock advances — a fixed
# calendar literal silently rots into the past and flakes the suite on
# that date. Each test runs against its own isolated env, so a single
# shared anchor is safe; within-test offsets stay relative to it.
_SEED = (datetime.now(timezone.utc) + timedelta(days=30)).replace(
    hour=9, minute=0, second=0, microsecond=0
)


@pytest.fixture
async def env(tmp_dir):
    """Env with calendar repos and service over a real SQLite database."""
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )

    class Env:
        pass

    e = Env()
    e.db = db
    e.iid = iid
    e.cal_repo = SqliteCalendarRepo(db)
    e.space_cal_repo = SqliteSpaceCalendarRepo(db)
    e.cal_svc = CalendarService(e.cal_repo)
    yield e
    await db.shutdown()


async def test_personal_calendar_crud(env):
    """Create calendar, add event, query by range, delete."""
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("anna", "uid-anna", "Anna"),
    )
    cal = await env.cal_svc.create_calendar(
        name="Personal", owner_username="anna", color="#FF0000"
    )
    assert cal.name == "Personal"

    now = datetime.now(timezone.utc)
    event = await env.cal_svc.create_event(
        calendar_id=cal.id,
        summary="Lunch",
        start=now.isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
        created_by="uid-anna",
    )
    assert event.summary == "Lunch"

    events = await env.cal_svc.list_events_in_range(
        cal.id,
        start=(now - timedelta(minutes=30)).isoformat(),
        end=(now + timedelta(hours=2)).isoformat(),
    )
    assert len(events) == 1

    no_events = await env.cal_svc.list_events_in_range(
        cal.id,
        start=(now - timedelta(hours=3)).isoformat(),
        end=(now - timedelta(hours=2)).isoformat(),
    )
    assert len(no_events) == 0

    await env.cal_svc.delete_event(event.id)
    with pytest.raises(KeyError):
        await env.cal_svc.get_event(event.id)

    await env.cal_svc.delete_calendar(cal.id)
    with pytest.raises(KeyError):
        await env.cal_svc.get_calendar(cal.id)


async def test_space_calendar_with_rsvps(env):
    """Space calendar event with RSVP going/decline/remove flow."""
    now = datetime.now(timezone.utc)

    kp = generate_identity_keypair()
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("owner1", "uid-owner1", "Owner"),
    )
    await env.db.enqueue(
        """INSERT INTO spaces(
            id, name, owner_instance_id, owner_username, identity_public_key,
            config_sequence, space_type, join_mode
        ) VALUES(?,?,?,?,?,0,'private','invite_only')""",
        ("space-1", "TestSpace", env.iid, "owner1", kp.public_key.hex()),
    )

    event = CalendarEvent(
        id=uuid.uuid4().hex,
        calendar_id="space-1",
        summary="Team meeting",
        start=now,
        end=now + timedelta(hours=1),
        created_by="u1",
    )
    await env.space_cal_repo.save_event(event, space_id="space-1")

    rsvp_going = CalendarRSVP(
        event_id=event.id,
        user_id="u1",
        status=RSVPStatus.GOING,
        updated_at=now.isoformat(),
        occurrence_at=now.isoformat(),
    )
    await env.space_cal_repo.upsert_rsvp(rsvp_going)
    rsvps = await env.space_cal_repo.list_rsvps(event.id)
    assert len(rsvps) == 1
    assert rsvps[0].status == RSVPStatus.GOING

    rsvp_declined = CalendarRSVP(
        event_id=event.id,
        user_id="u1",
        status=RSVPStatus.DECLINED,
        updated_at=now.isoformat(),
        occurrence_at=now.isoformat(),
    )
    await env.space_cal_repo.upsert_rsvp(rsvp_declined)
    rsvps2 = await env.space_cal_repo.list_rsvps(event.id)
    assert rsvps2[0].status == RSVPStatus.DECLINED

    await env.space_cal_repo.remove_rsvp(
        event.id,
        "u1",
        occurrence_at=now.isoformat(),
    )
    rsvps3 = await env.space_cal_repo.list_rsvps(event.id)
    assert len(rsvps3) == 0


async def test_list_events_in_range(env):
    """list_events_in_range returns events within the given time window."""
    await env.db.enqueue(
        "INSERT INTO users(username,user_id,display_name) VALUES(?,?,?)",
        ("anna", "u1", "A"),
    )
    cal = await env.cal_svc.create_calendar(
        name="W", owner_username="anna", color="#00F"
    )
    now = datetime.now(timezone.utc)
    await env.cal_svc.create_event(
        calendar_id=cal.id,
        summary="E",
        start=now.isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
        created_by="u1",
    )
    events = await env.cal_svc.list_events_in_range(
        cal.id,
        start=(now - timedelta(hours=1)).isoformat(),
        end=(now + timedelta(hours=2)).isoformat(),
    )
    assert len(events) >= 1


async def test_create_calendar_empty_name_rejected(env):
    """Empty calendar name raises ValueError."""
    with pytest.raises(ValueError, match="empty"):
        await env.cal_svc.create_calendar(name="  ", owner_username="x")


async def test_create_calendar_cycles_default_palette(env):
    """Successive default-coloured calendars walk the SH palette so each
    new household member gets a visually distinct chip on the first
    overlay-on moment."""
    for username in ("anna", "bo", "cleo"):
        await env.db.enqueue(
            "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
            (username, f"uid-{username}", username.title()),
        )
    a = await env.cal_svc.create_calendar(name="Anna", owner_username="anna")
    b = await env.cal_svc.create_calendar(name="Bo", owner_username="bo")
    c = await env.cal_svc.create_calendar(name="Cleo", owner_username="cleo")
    # All three picks are distinct hues from the documented palette.
    assert a.color != b.color
    assert b.color != c.color
    assert a.color != c.color
    # First slot is terracotta (--sh-primary), second moss (--sh-success).
    assert a.color == "#D2542A"
    assert b.color == "#1F4438"


async def test_create_calendar_explicit_color_wins(env):
    """An explicit color from the caller skips the palette cycle."""
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("anna", "uid-anna", "Anna"),
    )
    cal = await env.cal_svc.create_calendar(
        name="Custom",
        owner_username="anna",
        color="#123456",
    )
    assert cal.color == "#123456"


async def test_get_nonexistent_calendar(env):
    """Getting a nonexistent calendar raises KeyError."""
    with pytest.raises(KeyError):
        await env.cal_svc.get_calendar("nonexistent")


async def test_delete_nonexistent_calendar(env):
    """Deleting a nonexistent calendar raises KeyError."""
    with pytest.raises(KeyError):
        await env.cal_svc.delete_calendar("nonexistent")


# ── Personal-calendar invite scope (§23.60) ─────────────────────────────────


@pytest.fixture
async def federated_cal_env(env):
    """Add federation + user repos to ``env`` so ``CalendarService``
    can validate / route attendees. Wires a fake federation service
    that records outbound calls."""
    from socialhome.repositories.federation_repo import SqliteFederationRepo
    from socialhome.repositories.user_repo import SqliteUserRepo

    env.fed_repo = SqliteFederationRepo(env.db)
    env.user_repo = SqliteUserRepo(env.db)

    sent: list[tuple] = []

    class FakeFederation:
        async def send_event(self, *, to_instance_id, event_type, payload):
            sent.append((to_instance_id, event_type, dict(payload)))

            class _R:
                ok = True
                status_code = 200

            return _R()

    env.fed = FakeFederation()
    env.sent = sent
    env.cal_svc.attach_federation(
        env.fed,
        federation_repo=env.fed_repo,
        user_repo=env.user_repo,
    )
    # Calendar owner — local household member.
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("anna", "uid-anna", "Anna"),
    )
    return env


async def _seed_paired(db, *, instance_id, display_name="Smith Home"):
    await db.enqueue(
        """
        INSERT INTO remote_instances(
            id, display_name, remote_identity_pk,
            key_self_to_remote, key_remote_to_self,
            remote_inbox_url, local_inbox_id, status, source
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            instance_id,
            display_name,
            "ab" * 32,
            "00",
            "00",
            f"https://{instance_id}.example/inbox/x",
            instance_id + "_local",
            "confirmed",
            "manual",
        ),
    )


async def _seed_remote(db, *, instance_id, user_id, username, display_name):
    await db.enqueue(
        "INSERT INTO remote_users(user_id, instance_id, remote_username,"
        " display_name) VALUES(?,?,?,?)",
        (user_id, instance_id, username, display_name),
    )


async def test_create_event_rejects_local_attendee(federated_cal_env):
    """Inviting a local household member is the wrong shape — should
    write to their calendar directly. Returns ValueError → 422."""
    e = federated_cal_env
    cal = await e.cal_svc.create_calendar(name="Anna", owner_username="anna")
    await e.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("lina", "uid-lina-local", "Lina"),
    )
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="household member"):
        await e.cal_svc.create_event(
            calendar_id=cal.id,
            summary="Lunch",
            start=now.isoformat(),
            end=(now + timedelta(hours=1)).isoformat(),
            created_by="uid-anna",
            attendees=["uid-lina-local"],
        )


async def test_create_event_rejects_unknown_user(federated_cal_env):
    """user_id that resolves to neither a local nor a remote row is
    rejected — peer drift from a stale invite list shouldn't quietly
    drop the event."""
    e = federated_cal_env
    cal = await e.cal_svc.create_calendar(name="Anna", owner_username="anna")
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="unknown attendee"):
        await e.cal_svc.create_event(
            calendar_id=cal.id,
            summary="Lunch",
            start=now.isoformat(),
            end=(now + timedelta(hours=1)).isoformat(),
            created_by="uid-anna",
            attendees=["uid-stranger"],
        )


async def test_create_event_accepts_paired_remote_attendee(federated_cal_env):
    """Confirmed paired-instance user → event saves AND outbound
    envelope is emitted to that user's home instance."""
    e = federated_cal_env
    cal = await e.cal_svc.create_calendar(name="Anna", owner_username="anna")
    await _seed_paired(e.db, instance_id="i_smith")
    await _seed_remote(
        e.db,
        instance_id="i_smith",
        user_id="u-bob",
        username="bob",
        display_name="Bob",
    )
    now = datetime.now(timezone.utc)
    ev = await e.cal_svc.create_event(
        calendar_id=cal.id,
        summary="BBQ",
        start=now.isoformat(),
        end=(now + timedelta(hours=2)).isoformat(),
        created_by="uid-anna",
        attendees=["u-bob"],
    )
    assert ev.attendees == ("u-bob",)
    # One envelope to one peer — payload includes attendee_user_ids.
    assert len(e.sent) == 1
    inst_id, evt_type, payload = e.sent[0]
    assert inst_id == "i_smith"
    assert "PERSONAL_CALENDAR_EVENT_CREATED" in str(evt_type).upper()
    assert payload["summary"] == "BBQ"
    assert payload["attendee_user_ids"] == ["u-bob"]


async def test_create_event_groups_envelopes_per_instance(federated_cal_env):
    """Two attendees on the same peer get one envelope; two attendees
    on different peers get two."""
    e = federated_cal_env
    cal = await e.cal_svc.create_calendar(name="Anna", owner_username="anna")
    await _seed_paired(e.db, instance_id="i_a", display_name="A Home")
    await _seed_paired(e.db, instance_id="i_b", display_name="B Home")
    await _seed_remote(
        e.db,
        instance_id="i_a",
        user_id="u-a1",
        username="a1",
        display_name="A1",
    )
    await _seed_remote(
        e.db,
        instance_id="i_a",
        user_id="u-a2",
        username="a2",
        display_name="A2",
    )
    await _seed_remote(
        e.db,
        instance_id="i_b",
        user_id="u-b1",
        username="b1",
        display_name="B1",
    )
    now = datetime.now(timezone.utc)
    await e.cal_svc.create_event(
        calendar_id=cal.id,
        summary="Mixer",
        start=now.isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
        created_by="uid-anna",
        attendees=["u-a1", "u-a2", "u-b1"],
    )
    by_instance = {row[0]: row[2] for row in e.sent}
    assert set(by_instance) == {"i_a", "i_b"}
    assert by_instance["i_a"]["attendee_user_ids"] == ["u-a1", "u-a2"]
    assert by_instance["i_b"]["attendee_user_ids"] == ["u-b1"]


async def test_create_event_no_attendees_no_envelope(federated_cal_env):
    """Pure-local event (no attendees) doesn't federate — even with
    federation wired."""
    e = federated_cal_env
    cal = await e.cal_svc.create_calendar(name="Anna", owner_username="anna")
    now = datetime.now(timezone.utc)
    await e.cal_svc.create_event(
        calendar_id=cal.id,
        summary="Solo",
        start=now.isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
        created_by="uid-anna",
    )
    assert e.sent == []


async def _seed_remote_invite(env, *, organiser_instance: str = "i_org"):
    """Seed a row that mimics what PersonalCalendarInboundHandlers
    would write on receipt of a cross-household invite — gives
    set_rsvp / clear_rsvp something to operate on."""
    cal = await env.cal_svc.create_calendar(name="Anna", owner_username="anna")
    now = datetime.now(timezone.utc)
    invite = CalendarEvent(
        id="ri_anna_invite",
        calendar_id=cal.id,
        summary="Garden party",
        start=now,
        end=now + timedelta(hours=2),
        created_by="u-bob-remote",
        origin="remote_invite",
        remote_event_id="org-evt-1",
        remote_instance_id=organiser_instance,
    )
    await env.cal_repo.save_event(invite)
    return invite


async def test_set_rsvp_writes_row_and_publishes_back(federated_cal_env):
    """Accepting a cross-household invite stores an RSVP row AND fires
    PERSONAL_CALENDAR_RSVP_UPDATED back to the organiser."""
    e = federated_cal_env
    invite = await _seed_remote_invite(e)
    await e.cal_svc.set_rsvp(
        event_id=invite.id,
        user_id="uid-anna",
        status="accepted",
    )
    rsvps = await e.cal_repo.list_rsvps(invite.id)
    assert len(rsvps) == 1
    assert rsvps[0].status == "accepted"
    assert rsvps[0].user_id == "uid-anna"
    # Outbound went to the organiser instance with the organiser's
    # event_id (the remote one), not our local row id.
    assert any(
        row[0] == "i_org"
        and "PERSONAL_CALENDAR_RSVP_UPDATED" in str(row[1]).upper()
        and row[2]["event_id"] == "org-evt-1"
        and row[2]["status"] == "accepted"
        for row in e.sent
    )


async def test_set_rsvp_rejects_invalid_status(federated_cal_env):
    e = federated_cal_env
    invite = await _seed_remote_invite(e)
    with pytest.raises(ValueError, match="RSVP status"):
        await e.cal_svc.set_rsvp(
            event_id=invite.id,
            user_id="uid-anna",
            status="going",  # space-RSVP word, not a personal-RSVP one
        )


async def test_set_rsvp_rejects_local_event(federated_cal_env):
    """Local-authored events have no organiser to reply to — RSVP is
    meaningless and the service rejects it."""
    e = federated_cal_env
    cal = await e.cal_svc.create_calendar(name="Anna", owner_username="anna")
    now = datetime.now(timezone.utc)
    ev = await e.cal_svc.create_event(
        calendar_id=cal.id,
        summary="Local",
        start=now.isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
        created_by="uid-anna",
    )
    with pytest.raises(ValueError, match="inbound invites"):
        await e.cal_svc.set_rsvp(
            event_id=ev.id,
            user_id="uid-anna",
            status="accepted",
        )


async def test_set_rsvp_unknown_event_raises(federated_cal_env):
    e = federated_cal_env
    with pytest.raises(KeyError):
        await e.cal_svc.set_rsvp(
            event_id="nope",
            user_id="uid-anna",
            status="accepted",
        )


async def test_clear_rsvp_removes_row_and_publishes_delete(federated_cal_env):
    e = federated_cal_env
    invite = await _seed_remote_invite(e)
    await e.cal_svc.set_rsvp(
        event_id=invite.id,
        user_id="uid-anna",
        status="accepted",
    )
    e.sent.clear()
    await e.cal_svc.clear_rsvp(event_id=invite.id, user_id="uid-anna")
    rsvps = await e.cal_repo.list_rsvps(invite.id)
    assert rsvps == []
    assert any(
        "PERSONAL_CALENDAR_RSVP_DELETED" in str(row[1]).upper()
        and "status" not in row[2]
        for row in e.sent
    )


async def test_clear_rsvp_unknown_event_is_noop(federated_cal_env):
    """Clearing on a vanished event is a no-op — keeps the SPA's
    "delete what's there" flow simple even if the user double-fires."""
    e = federated_cal_env
    e.sent.clear()
    await e.cal_svc.clear_rsvp(event_id="ghost", user_id="uid-anna")
    assert e.sent == []


async def test_clear_rsvp_rejects_local_event(federated_cal_env):
    e = federated_cal_env
    cal = await e.cal_svc.create_calendar(name="Anna", owner_username="anna")
    now = datetime.now(timezone.utc)
    ev = await e.cal_svc.create_event(
        calendar_id=cal.id,
        summary="Local",
        start=now.isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
        created_by="uid-anna",
    )
    with pytest.raises(ValueError, match="inbound invites"):
        await e.cal_svc.clear_rsvp(event_id=ev.id, user_id="uid-anna")


async def test_update_event_with_attendees_revalidates(federated_cal_env):
    """Re-validation runs on update too — adding a local user via PATCH
    is rejected with the same 422 as POST."""
    e = federated_cal_env
    cal = await e.cal_svc.create_calendar(name="Anna", owner_username="anna")
    now = datetime.now(timezone.utc)
    ev = await e.cal_svc.create_event(
        calendar_id=cal.id,
        summary="Picnic",
        start=now.isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
        created_by="uid-anna",
    )
    await e.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("lina", "uid-lina-local", "Lina"),
    )
    with pytest.raises(ValueError, match="household member"):
        await e.cal_svc.update_event(
            ev.id,
            attendees=["uid-lina-local"],
        )


async def test_update_event_without_attendees_keeps_existing(federated_cal_env):
    """PATCH that doesn't carry attendees keeps them as-is — and
    federation outbound goes back to those existing attendees."""
    e = federated_cal_env
    cal = await e.cal_svc.create_calendar(name="Anna", owner_username="anna")
    await _seed_paired(e.db, instance_id="i_smith")
    await _seed_remote(
        e.db,
        instance_id="i_smith",
        user_id="u-bob",
        username="bob",
        display_name="Bob",
    )
    now = datetime.now(timezone.utc)
    ev = await e.cal_svc.create_event(
        calendar_id=cal.id,
        summary="Picnic",
        start=now.isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
        created_by="uid-anna",
        attendees=["u-bob"],
    )
    e.sent.clear()
    await e.cal_svc.update_event(ev.id, summary="Picnic — moved to 4pm")
    # Update envelope went to the same paired instance.
    assert any(
        row[0] == "i_smith" and "PERSONAL_CALENDAR_EVENT_UPDATED" in str(row[1]).upper()
        for row in e.sent
    )


async def test_update_event_promotes_legacy_to_group(federated_cal_env):
    """A legacy event with no ``client_event_uuid`` can be stamped with
    one via ``update_event`` so all copies share the group id (#327)."""
    e = federated_cal_env
    cal = await e.cal_svc.create_calendar(name="Anna", owner_username="anna")
    now = datetime.now(timezone.utc)
    ev = await e.cal_svc.create_event(
        calendar_id=cal.id,
        summary="Picnic",
        start=now.isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
        created_by="uid-anna",
    )
    assert ev.client_event_uuid is None
    grp = uuid.uuid4().hex
    await e.cal_svc.update_event(ev.id, client_event_uuid=grp)
    saved = await e.cal_svc.get_event(ev.id)
    assert saved.client_event_uuid == grp


async def test_update_event_does_not_clear_existing_group(federated_cal_env):
    """Passing no ``client_event_uuid`` (None) must NOT clear an event's
    existing group id — None means "no change", never "clear"."""
    e = federated_cal_env
    cal = await e.cal_svc.create_calendar(name="Anna", owner_username="anna")
    now = datetime.now(timezone.utc)
    grp = uuid.uuid4().hex
    ev = await e.cal_svc.create_event(
        calendar_id=cal.id,
        summary="Picnic",
        start=now.isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
        created_by="uid-anna",
        client_event_uuid=grp,
    )
    assert ev.client_event_uuid == grp
    # Update an unrelated field without passing client_event_uuid.
    await e.cal_svc.update_event(ev.id, summary="Picnic — renamed")
    saved = await e.cal_svc.get_event(ev.id)
    assert saved.summary == "Picnic — renamed"
    assert saved.client_event_uuid == grp
    # A malformed uuid (cleans to None) is also a no-op — never a clear.
    await e.cal_svc.update_event(ev.id, client_event_uuid="not a uuid!!")
    saved2 = await e.cal_svc.get_event(ev.id)
    assert saved2.client_event_uuid == grp


async def test_attendee_on_unconfirmed_instance_rejected(federated_cal_env):
    """A remote user whose home instance exists but is in
    pending_sent (not confirmed) is rejected at create time."""
    e = federated_cal_env
    cal = await e.cal_svc.create_calendar(name="Anna", owner_username="anna")
    # Seed an instance with non-confirmed status + a remote user.
    await e.db.enqueue(
        """
        INSERT INTO remote_instances(
            id, display_name, remote_identity_pk,
            key_self_to_remote, key_remote_to_self,
            remote_inbox_url, local_inbox_id, status, source
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            "i_pending",
            "Pending Home",
            "ab" * 32,
            "00",
            "00",
            "https://i_pending.example/inbox/x",
            "i_pending_local",
            "pending_sent",
            "manual",
        ),
    )
    await e.db.enqueue(
        "INSERT INTO remote_users(user_id, instance_id, remote_username,"
        " display_name) VALUES(?,?,?,?)",
        ("u-pending", "i_pending", "p", "Pending"),
    )
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="confirmed paired"):
        await e.cal_svc.create_event(
            calendar_id=cal.id,
            summary="No-go",
            start=now.isoformat(),
            end=(now + timedelta(hours=1)).isoformat(),
            created_by="uid-anna",
            attendees=["u-pending"],
        )


async def test_publish_federation_event_swallows_outbound_error(federated_cal_env):
    """If send_event raises (e.g. transient peer outage), create_event
    still succeeds — the outbox retry loop is responsible for
    redelivery."""
    e = federated_cal_env
    cal = await e.cal_svc.create_calendar(name="Anna", owner_username="anna")
    await _seed_paired(e.db, instance_id="i_smith")
    await _seed_remote(
        e.db,
        instance_id="i_smith",
        user_id="u-bob",
        username="bob",
        display_name="Bob",
    )

    # Swap in a federation that raises.
    class _Boom:
        async def send_event(self, **kw):
            raise RuntimeError("peer down")

    e.cal_svc._federation = _Boom()
    now = datetime.now(timezone.utc)
    ev = await e.cal_svc.create_event(
        calendar_id=cal.id,
        summary="Resilient",
        start=now.isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
        created_by="uid-anna",
        attendees=["u-bob"],
    )
    # Event saved despite the outbound boom.
    assert (await e.cal_repo.get_event(ev.id)) is not None


async def test_remote_invite_event_does_not_re_federate(federated_cal_env):
    """Saving a remote_invite row through update_event mustn't bounce
    the envelope back to the organiser — the bridge would loop. The
    `_publish_federation_event` helper short-circuits on origin."""
    e = federated_cal_env
    cal = await e.cal_svc.create_calendar(name="Anna", owner_username="anna")
    invite = await _seed_remote_invite(e)
    e.sent.clear()
    # The invite row already has origin='remote_invite'; PATCHing its
    # summary does NOT emit a PERSONAL_CALENDAR_EVENT_UPDATED envelope.
    # (The model is: organiser owns the event; recipient can RSVP, not
    # edit. But the service doesn't enforce edit-blocking — the route
    # would, in production. Here we just verify no envelope is sent.)
    await e.cal_svc.update_event(invite.id, summary="Renamed locally")
    assert not any(
        "PERSONAL_CALENDAR_EVENT_UPDATED" in str(row[1]).upper() for row in e.sent
    )
    # Sanity: cal still resolves.
    assert (await e.cal_svc.get_calendar(cal.id)) is not None


async def test_set_rsvp_outbound_swallows_error(federated_cal_env):
    """Like create — RSVP outbound failure doesn't block the local
    upsert.  (Federation outbox retries; UI shouldn't error.)"""
    e = federated_cal_env
    invite = await _seed_remote_invite(e)

    class _Boom:
        async def send_event(self, **kw):
            raise RuntimeError("peer down")

    e.cal_svc._federation = _Boom()
    await e.cal_svc.set_rsvp(
        event_id=invite.id,
        user_id="uid-anna",
        status="accepted",
    )
    # RSVP row exists despite the boom.
    rsvps = await e.cal_repo.list_rsvps(invite.id)
    assert len(rsvps) == 1


async def test_publish_rsvp_skips_when_remote_pointers_missing(federated_cal_env):
    """If for some reason a row has origin='remote_invite' but the
    remote_event_id / remote_instance_id columns are NULL (legacy
    data), the RSVP outbound silently skips instead of raising."""
    e = federated_cal_env
    cal = await e.cal_svc.create_calendar(name="Anna", owner_username="anna")
    now = datetime.now(timezone.utc)
    # Invite WITHOUT remote pointers — exercises the guard.
    bad = CalendarEvent(
        id="ri_legacy",
        calendar_id=cal.id,
        summary="Legacy",
        start=now,
        end=now + timedelta(hours=1),
        created_by="u-stranger",
        origin="remote_invite",
    )
    await e.cal_repo.save_event(bad)
    e.sent.clear()
    await e.cal_svc.set_rsvp(
        event_id="ri_legacy",
        user_id="uid-anna",
        status="declined",
    )
    # Local row exists; no envelope sent.
    assert len(await e.cal_repo.list_rsvps("ri_legacy")) == 1
    assert e.sent == []


async def test_delete_event_with_attendees_fans_envelope(federated_cal_env):
    """Deleting an event with cross-household attendees emits
    PERSONAL_CALENDAR_EVENT_DELETED to each attendee's instance."""
    e = federated_cal_env
    cal = await e.cal_svc.create_calendar(name="Anna", owner_username="anna")
    await _seed_paired(e.db, instance_id="i_smith")
    await _seed_remote(
        e.db,
        instance_id="i_smith",
        user_id="u-bob",
        username="bob",
        display_name="Bob",
    )
    now = datetime.now(timezone.utc)
    ev = await e.cal_svc.create_event(
        calendar_id=cal.id,
        summary="Cancelled",
        start=now.isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
        created_by="uid-anna",
        attendees=["u-bob"],
    )
    e.sent.clear()
    await e.cal_svc.delete_event(ev.id)
    assert any(
        row[0] == "i_smith" and "PERSONAL_CALENDAR_EVENT_DELETED" in str(row[1]).upper()
        for row in e.sent
    )


# ── SpaceCalendarService — per-occurrence + federation (Phase A) ────────────


@pytest.fixture
async def space_cal_env(env):
    """env + a SpaceCalendarService with a seeded space."""
    from socialhome.services.calendar_service import SpaceCalendarService

    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("alice", "uid-alice", "Alice"),
    )
    kp = generate_identity_keypair()
    await env.db.enqueue(
        """INSERT INTO spaces(
            id, name, owner_instance_id, owner_username, identity_public_key,
            config_sequence, space_type, join_mode
        ) VALUES(?,?,?,?,?,0,'private','invite_only')""",
        ("sp-cal", "TestSpace", env.iid, "alice", kp.public_key.hex()),
    )
    from socialhome.infrastructure.event_bus import EventBus

    env.bus = EventBus()
    env.space_cal_svc = SpaceCalendarService(env.space_cal_repo, env.bus)
    yield env


async def test_rsvp_non_recurring_defaults_occurrence_to_event_start(space_cal_env):
    """RSVP without occurrence_at on a non-recurring event → uses event.start."""
    env = space_cal_env
    # Future-relative (not a hardcoded date) — rsvp() rejects an occurrence
    # that has already ended, so a fixed past start makes this a time-bomb.
    now = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0)
    event = await env.space_cal_svc.create_event(
        space_id="sp-cal",
        summary="Birthday",
        start=now.isoformat(),
        end=(now + timedelta(hours=2)).isoformat(),
        created_by="uid-alice",
    )
    await env.space_cal_svc.rsvp(
        event_id=event.id,
        user_id="uid-alice",
        status=RSVPStatus.GOING,
    )
    rsvps = await env.space_cal_svc.list_rsvps(event.id)
    assert len(rsvps) == 1
    # occurrence_at should equal the event's start
    assert rsvps[0].occurrence_at == now.isoformat()


async def test_rsvp_recurring_requires_occurrence_at(space_cal_env):
    """RSVP without occurrence_at on a recurring event → ValueError."""
    env = space_cal_env
    seed = _SEED
    event = await env.space_cal_svc.create_event(
        space_id="sp-cal",
        summary="Weekly standup",
        start=seed.isoformat(),
        end=(seed + timedelta(minutes=30)).isoformat(),
        created_by="uid-alice",
        rrule="FREQ=WEEKLY;COUNT=4",
    )
    with pytest.raises(ValueError, match="occurrence_at"):
        await env.space_cal_svc.rsvp(
            event_id=event.id,
            user_id="uid-alice",
            status=RSVPStatus.GOING,
        )


async def test_rsvp_recurring_rejects_invalid_occurrence(space_cal_env):
    """RSVP with occurrence_at that doesn't match the rrule → ValueError."""
    env = space_cal_env
    seed = _SEED
    event = await env.space_cal_svc.create_event(
        space_id="sp-cal",
        summary="Weekly standup",
        start=seed.isoformat(),
        end=(seed + timedelta(minutes=30)).isoformat(),
        created_by="uid-alice",
        rrule="FREQ=WEEKLY;COUNT=4",
    )
    bad_occ = seed + timedelta(days=1)  # one day off the weekly cadence
    with pytest.raises(ValueError, match="not a valid occurrence"):
        await env.space_cal_svc.rsvp(
            event_id=event.id,
            user_id="uid-alice",
            status=RSVPStatus.GOING,
            occurrence_at=bad_occ,
        )


async def test_rsvp_recurring_separate_occurrences(space_cal_env):
    """RSVPs on two different occurrences yield two distinct rows."""
    env = space_cal_env
    seed = _SEED
    event = await env.space_cal_svc.create_event(
        space_id="sp-cal",
        summary="Weekly standup",
        start=seed.isoformat(),
        end=(seed + timedelta(minutes=30)).isoformat(),
        created_by="uid-alice",
        rrule="FREQ=WEEKLY;COUNT=4",
    )
    occ1 = seed
    occ2 = seed + timedelta(weeks=1)
    await env.space_cal_svc.rsvp(
        event_id=event.id,
        user_id="uid-alice",
        status=RSVPStatus.GOING,
        occurrence_at=occ1,
    )
    await env.space_cal_svc.rsvp(
        event_id=event.id,
        user_id="uid-alice",
        status=RSVPStatus.DECLINED,
        occurrence_at=occ2,
    )
    by_occ1 = await env.space_cal_svc.list_rsvps(event.id, occurrence_at=occ1)
    by_occ2 = await env.space_cal_svc.list_rsvps(event.id, occurrence_at=occ2)
    assert len(by_occ1) == 1 and by_occ1[0].status == RSVPStatus.GOING
    assert len(by_occ2) == 1 and by_occ2[0].status == RSVPStatus.DECLINED


async def test_rsvp_status_must_be_user_settable(space_cal_env):
    """User-driven RSVP can't set host-controlled statuses (requested/waitlist)."""
    env = space_cal_env
    now = _SEED
    event = await env.space_cal_svc.create_event(
        space_id="sp-cal",
        summary="Birthday",
        start=now.isoformat(),
        end=now.isoformat(),
        created_by="uid-alice",
    )
    with pytest.raises(ValueError, match="must be one of"):
        await env.space_cal_svc.rsvp(
            event_id=event.id,
            user_id="uid-alice",
            status=RSVPStatus.WAITLIST,
        )


# ── Phase C: capacity + request-to-join + waitlist ─────────────────────────


async def test_create_event_auto_rsvps_creator_as_going(space_cal_env):
    env = space_cal_env
    now = _SEED
    event = await env.space_cal_svc.create_event(
        space_id="sp-cal",
        summary="Birthday party",
        start=now.isoformat(),
        end=now.isoformat(),
        created_by="uid-alice",
    )
    rsvps = await env.space_cal_svc.list_rsvps(event.id)
    assert len(rsvps) == 1
    assert rsvps[0].user_id == "uid-alice"
    assert rsvps[0].status == RSVPStatus.GOING


async def test_capped_event_member_rsvp_becomes_requested(space_cal_env):
    env = space_cal_env
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("bob", "uid-bob", "Bob"),
    )
    now = _SEED
    event = await env.space_cal_svc.create_event(
        space_id="sp-cal",
        summary="Capped event",
        start=now.isoformat(),
        end=now.isoformat(),
        created_by="uid-alice",
        capacity=5,
    )
    await env.space_cal_svc.rsvp(
        event_id=event.id,
        user_id="uid-bob",
        status=RSVPStatus.GOING,
    )
    rsvps = await env.space_cal_svc.list_rsvps(event.id)
    bob = [r for r in rsvps if r.user_id == "uid-bob"][0]
    assert bob.status == RSVPStatus.REQUESTED


async def test_creator_skips_approval_even_when_capped(space_cal_env):
    env = space_cal_env
    now = _SEED
    event = await env.space_cal_svc.create_event(
        space_id="sp-cal",
        summary="Capped event",
        start=now.isoformat(),
        end=now.isoformat(),
        created_by="uid-alice",
        capacity=2,
    )
    # Creator's auto-RSVP from create_event lands as GOING.
    rsvps = await env.space_cal_svc.list_rsvps(event.id)
    alice = [r for r in rsvps if r.user_id == "uid-alice"][0]
    assert alice.status == RSVPStatus.GOING


async def test_approve_promotes_requested_to_going(space_cal_env):
    env = space_cal_env
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("bob", "uid-bob", "Bob"),
    )
    now = _SEED
    event = await env.space_cal_svc.create_event(
        space_id="sp-cal",
        summary="Capped event",
        start=now.isoformat(),
        end=now.isoformat(),
        created_by="uid-alice",
        capacity=5,
    )
    await env.space_cal_svc.rsvp(
        event_id=event.id,
        user_id="uid-bob",
        status=RSVPStatus.GOING,
    )
    new_status = await env.space_cal_svc.approve_rsvp(
        event_id=event.id,
        user_id="uid-bob",
    )
    assert new_status == RSVPStatus.GOING
    rsvps = await env.space_cal_svc.list_rsvps(event.id)
    bob = [r for r in rsvps if r.user_id == "uid-bob"][0]
    assert bob.status == RSVPStatus.GOING


async def test_approve_lands_on_waitlist_when_full(space_cal_env):
    env = space_cal_env
    for u in ("bob", "carol"):
        await env.db.enqueue(
            "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
            (u, f"uid-{u}", u.title()),
        )
    now = _SEED
    event = await env.space_cal_svc.create_event(
        space_id="sp-cal",
        summary="Tiny event",
        start=now.isoformat(),
        end=now.isoformat(),
        created_by="uid-alice",
        capacity=1,
    )
    # Capacity is 1, alice already takes the seat.
    await env.space_cal_svc.rsvp(
        event_id=event.id,
        user_id="uid-bob",
        status=RSVPStatus.GOING,
    )
    bob_status = await env.space_cal_svc.approve_rsvp(
        event_id=event.id,
        user_id="uid-bob",
    )
    assert bob_status == RSVPStatus.WAITLIST


async def test_decline_promotes_waitlist(space_cal_env):
    env = space_cal_env
    for u in ("bob", "carol"):
        await env.db.enqueue(
            "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
            (u, f"uid-{u}", u.title()),
        )
    now = _SEED
    event = await env.space_cal_svc.create_event(
        space_id="sp-cal",
        summary="Limited",
        start=now.isoformat(),
        end=now.isoformat(),
        created_by="uid-alice",
        capacity=1,
    )
    # bob requests, gets waitlisted on approval.
    await env.space_cal_svc.rsvp(
        event_id=event.id,
        user_id="uid-bob",
        status=RSVPStatus.GOING,
    )
    await env.space_cal_svc.approve_rsvp(event_id=event.id, user_id="uid-bob")
    # alice declines (gives up her seat) — bob should auto-promote.
    await env.space_cal_svc.rsvp(
        event_id=event.id,
        user_id="uid-alice",
        status=RSVPStatus.DECLINED,
    )
    rsvps = await env.space_cal_svc.list_rsvps(event.id)
    bob = [r for r in rsvps if r.user_id == "uid-bob"][0]
    assert bob.status == RSVPStatus.GOING


async def test_deny_removes_request(space_cal_env):
    env = space_cal_env
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("bob", "uid-bob", "Bob"),
    )
    now = _SEED
    event = await env.space_cal_svc.create_event(
        space_id="sp-cal",
        summary="Capped",
        start=now.isoformat(),
        end=now.isoformat(),
        created_by="uid-alice",
        capacity=10,
    )
    await env.space_cal_svc.rsvp(
        event_id=event.id,
        user_id="uid-bob",
        status=RSVPStatus.GOING,
    )
    await env.space_cal_svc.deny_rsvp(event_id=event.id, user_id="uid-bob")
    rsvps = await env.space_cal_svc.list_rsvps(event.id)
    bobs = [r for r in rsvps if r.user_id == "uid-bob"]
    assert bobs == []


async def test_list_pending_only_returns_requested(space_cal_env):
    env = space_cal_env
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("bob", "uid-bob", "Bob"),
    )
    now = _SEED
    event = await env.space_cal_svc.create_event(
        space_id="sp-cal",
        summary="Capped",
        start=now.isoformat(),
        end=now.isoformat(),
        created_by="uid-alice",
        capacity=2,
    )
    await env.space_cal_svc.rsvp(
        event_id=event.id,
        user_id="uid-bob",
        status=RSVPStatus.GOING,
    )
    pending = await env.space_cal_svc.list_pending(event.id)
    assert len(pending) == 1
    assert pending[0].user_id == "uid-bob"
    assert pending[0].status == RSVPStatus.REQUESTED


async def test_capacity_raise_promotes_waitlist(space_cal_env):
    env = space_cal_env
    for u in ("bob", "carol"):
        await env.db.enqueue(
            "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
            (u, f"uid-{u}", u.title()),
        )
    now = _SEED
    event = await env.space_cal_svc.create_event(
        space_id="sp-cal",
        summary="Capped",
        start=now.isoformat(),
        end=now.isoformat(),
        created_by="uid-alice",
        capacity=1,
    )
    await env.space_cal_svc.rsvp(
        event_id=event.id,
        user_id="uid-bob",
        status=RSVPStatus.GOING,
    )
    await env.space_cal_svc.approve_rsvp(event_id=event.id, user_id="uid-bob")
    # bob is waitlisted (alice has the only seat).
    rsvps = await env.space_cal_svc.list_rsvps(event.id)
    assert any(
        r.user_id == "uid-bob" and r.status == RSVPStatus.WAITLIST for r in rsvps
    )
    # Raise capacity — bob should promote.
    await env.space_cal_svc.update_event(event.id, capacity=2)
    rsvps2 = await env.space_cal_svc.list_rsvps(event.id)
    bob = [r for r in rsvps2 if r.user_id == "uid-bob"][0]
    assert bob.status == RSVPStatus.GOING


async def test_uncapped_event_keeps_old_behaviour(space_cal_env):
    """No capacity → "going" is direct, no approval flow."""
    env = space_cal_env
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("bob", "uid-bob", "Bob"),
    )
    now = _SEED
    event = await env.space_cal_svc.create_event(
        space_id="sp-cal",
        summary="Open event",
        start=now.isoformat(),
        end=now.isoformat(),
        created_by="uid-alice",
    )
    await env.space_cal_svc.rsvp(
        event_id=event.id,
        user_id="uid-bob",
        status=RSVPStatus.GOING,
    )
    rsvps = await env.space_cal_svc.list_rsvps(event.id)
    bob = [r for r in rsvps if r.user_id == "uid-bob"][0]
    assert bob.status == RSVPStatus.GOING


async def test_rsvp_to_ended_event_rejected(space_cal_env):
    """Phase E: RSVPs to occurrences whose window is fully in the past
    are rejected at the service layer."""
    env = space_cal_env
    past = datetime.now(timezone.utc) - timedelta(days=1)
    event = await env.space_cal_svc.create_event(
        space_id="sp-cal",
        summary="Past event",
        start=past.isoformat(),
        end=(past + timedelta(hours=1)).isoformat(),
        created_by="uid-alice",
    )
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("bob", "uid-bob", "Bob"),
    )
    with pytest.raises(ValueError, match="already ended"):
        await env.space_cal_svc.rsvp(
            event_id=event.id,
            user_id="uid-bob",
            status=RSVPStatus.GOING,
        )


async def test_rsvp_during_event_window_allowed(space_cal_env):
    """While an event is happening (started but not ended), RSVPs go through."""
    env = space_cal_env
    now = datetime.now(timezone.utc)
    # Event that started 30 min ago and lasts 2 h.
    started = now - timedelta(minutes=30)
    event = await env.space_cal_svc.create_event(
        space_id="sp-cal",
        summary="Currently happening",
        start=started.isoformat(),
        end=(now + timedelta(hours=1, minutes=30)).isoformat(),
        created_by="uid-alice",
    )
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("bob", "uid-bob", "Bob"),
    )
    # Should NOT raise.
    await env.space_cal_svc.rsvp(
        event_id=event.id,
        user_id="uid-bob",
        status=RSVPStatus.GOING,
    )


async def test_negative_capacity_rejected(space_cal_env):
    env = space_cal_env
    now = _SEED
    with pytest.raises(ValueError, match="capacity"):
        await env.space_cal_svc.create_event(
            space_id="sp-cal",
            summary="Bad",
            start=now.isoformat(),
            end=now.isoformat(),
            created_by="uid-alice",
            capacity=-1,
        )


async def test_member_left_cleans_up_rsvps(space_cal_env):
    """Phase E: SpaceMemberLeft subscriber drops the user's RSVPs in the space."""
    from socialhome.domain.events import SpaceMemberLeft

    env = space_cal_env
    env.space_cal_svc.wire()
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("bob", "uid-bob", "Bob"),
    )
    future = datetime.now(timezone.utc) + timedelta(days=10)
    event = await env.space_cal_svc.create_event(
        space_id="sp-cal",
        summary="Anniversary",
        start=future.isoformat(),
        end=(future + timedelta(hours=1)).isoformat(),
        created_by="uid-alice",
    )
    await env.space_cal_svc.rsvp(
        event_id=event.id,
        user_id="uid-bob",
        status=RSVPStatus.GOING,
    )
    rsvps_before = await env.space_cal_svc.list_rsvps(event.id)
    assert any(r.user_id == "uid-bob" for r in rsvps_before)
    # Bob leaves the space.
    await env.bus.publish(SpaceMemberLeft(space_id="sp-cal", user_id="uid-bob"))
    rsvps_after = await env.space_cal_svc.list_rsvps(event.id)
    assert not any(r.user_id == "uid-bob" for r in rsvps_after)
    # Alice (the creator) is still RSVPed.
    assert any(r.user_id == "uid-alice" for r in rsvps_after)


async def test_rsvp_publishes_federation_event(space_cal_env):
    """rsvp() calls broadcast_to_space_members on the federation service."""
    env = space_cal_env

    class _FakeFed:
        def __init__(self):
            self.calls: list[tuple] = []

        async def broadcast_to_space_members(self, space_id, event_type, payload):
            self.calls.append((space_id, event_type, payload))

    fed = _FakeFed()
    env.space_cal_svc.attach_federation(fed)
    # Future-relative so the RSVP isn't rejected by the past-occurrence
    # lock — a fixed calendar date silently rots into the past and flakes
    # once the wall clock catches up to it (hit on 2026-06-01).
    now = datetime.now(timezone.utc) + timedelta(days=1)
    event = await env.space_cal_svc.create_event(
        space_id="sp-cal",
        summary="Anniversary",
        start=now.isoformat(),
        end=now.isoformat(),
        created_by="uid-alice",
    )
    await env.space_cal_svc.rsvp(
        event_id=event.id,
        user_id="uid-alice",
        status=RSVPStatus.GOING,
    )
    # remove_rsvp also fires
    await env.space_cal_svc.remove_rsvp(
        event_id=event.id,
        user_id="uid-alice",
    )
    rsvp_calls = [c for c in fed.calls if c[1].value.startswith("space_rsvp")]
    assert len(rsvp_calls) == 2
    assert rsvp_calls[0][0] == "sp-cal"
    assert rsvp_calls[0][1].value == "space_rsvp_updated"
    assert rsvp_calls[0][2]["status"] == RSVPStatus.GOING
    assert rsvp_calls[0][2]["occurrence_at"] == now.isoformat()
    assert rsvp_calls[1][1].value == "space_rsvp_deleted"
    assert "status" not in rsvp_calls[1][2]
    # The space rides the PAYLOAD too. A mesh-relayed envelope carries no
    # plaintext routing ``space_id`` (a relay must not learn which space
    # is being served), and an RSVP names only its parent event — so
    # without this copy the receiver's §24.11 space-writer gate cannot
    # attribute the write to a space at all.
    assert all(c[2]["space_id"] == "sp-cal" for c in rsvp_calls)


async def test_create_event_publishes_federation_event(space_cal_env):
    """create_event broadcasts SPACE_CALENDAR_EVENT_CREATED with full payload.

    Regression guard for the outbound-publisher gap that left calendar
    events stuck on the host instance — Beta would create the event but
    Alpha / Carol's RSVP attempts would 404 because the event row never
    federated.
    """
    env = space_cal_env

    class _FakeFed:
        def __init__(self):
            self.calls: list[tuple] = []

        async def broadcast_to_space_members(self, space_id, event_type, payload):
            self.calls.append((space_id, event_type, payload))

    fed = _FakeFed()
    env.space_cal_svc.attach_federation(fed)
    now = _SEED
    event = await env.space_cal_svc.create_event(
        space_id="sp-cal",
        summary="Anniversary",
        description="our 5th",
        start=now.isoformat(),
        end=(now + timedelta(hours=2)).isoformat(),
        created_by="uid-alice",
        cover_url="https://cdn.example/cover.jpg",
        location="Pier 39",
    )
    created_calls = [
        c for c in fed.calls if c[1].value == "space_calendar_event_created"
    ]
    assert len(created_calls) == 1
    space_id, evt_type, payload = created_calls[0]
    assert space_id == "sp-cal"
    assert payload["event_id"] == event.id
    assert payload["calendar_id"] == "sp-cal"
    assert payload["summary"] == "Anniversary"
    assert payload["description"] == "our 5th"
    assert payload["start"] == now.isoformat()
    assert payload["end"] == (now + timedelta(hours=2)).isoformat()
    assert payload["created_by"] == "uid-alice"
    assert payload["cover_url"] == "https://cdn.example/cover.jpg"
    assert payload["location"] == "Pier 39"
    # See the RSVP test above: the payload carries the space so a
    # mesh-relayed envelope stays attributable.
    assert payload["space_id"] == "sp-cal"
    # Stored event surfaces the field through the read path too.
    assert event.location == "Pier 39"


async def test_update_event_clears_location_on_explicit_none(space_cal_env):
    env = space_cal_env
    now = _SEED
    event = await env.space_cal_svc.create_event(
        space_id="sp-cal",
        summary="Drinks",
        start=now.isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
        created_by="uid-alice",
        location="Hotel bar",
    )
    assert event.location == "Hotel bar"
    updated = await env.space_cal_svc.update_event(event.id, location=None)
    assert updated.location is None


async def test_update_event_publishes_federation_event(space_cal_env):
    """update_event broadcasts SPACE_CALENDAR_EVENT_UPDATED."""
    env = space_cal_env

    class _FakeFed:
        def __init__(self):
            self.calls: list[tuple] = []

        async def broadcast_to_space_members(self, space_id, event_type, payload):
            self.calls.append((space_id, event_type, payload))

    fed = _FakeFed()
    env.space_cal_svc.attach_federation(fed)
    now = _SEED
    event = await env.space_cal_svc.create_event(
        space_id="sp-cal",
        summary="Old",
        start=now.isoformat(),
        end=now.isoformat(),
        created_by="uid-alice",
    )
    await env.space_cal_svc.update_event(event.id, summary="New summary")
    updated_calls = [
        c for c in fed.calls if c[1].value == "space_calendar_event_updated"
    ]
    assert len(updated_calls) == 1
    assert updated_calls[0][2]["summary"] == "New summary"


async def test_delete_event_publishes_federation_event(space_cal_env):
    """delete_event broadcasts SPACE_CALENDAR_EVENT_DELETED."""
    env = space_cal_env

    class _FakeFed:
        def __init__(self):
            self.calls: list[tuple] = []

        async def broadcast_to_space_members(self, space_id, event_type, payload):
            self.calls.append((space_id, event_type, payload))

    fed = _FakeFed()
    env.space_cal_svc.attach_federation(fed)
    now = _SEED
    event = await env.space_cal_svc.create_event(
        space_id="sp-cal",
        summary="Bye",
        start=now.isoformat(),
        end=now.isoformat(),
        created_by="uid-alice",
    )
    await env.space_cal_svc.delete_event(event.id)
    deleted_calls = [
        c for c in fed.calls if c[1].value == "space_calendar_event_deleted"
    ]
    assert len(deleted_calls) == 1
    assert deleted_calls[0][0] == "sp-cal"
    assert deleted_calls[0][2]["event_id"] == event.id


async def test_create_event_empty_summary(env):
    """Empty event summary raises ValueError."""
    await env.db.enqueue(
        "INSERT OR IGNORE INTO users(username,user_id,display_name) VALUES(?,?,?)",
        ("bob", "u2", "B"),
    )
    cal = await env.cal_svc.create_calendar(name="C", owner_username="bob")
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="empty"):
        await env.cal_svc.create_event(
            calendar_id=cal.id,
            summary="  ",
            start=now.isoformat(),
            end=(now + timedelta(hours=1)).isoformat(),
            created_by="u2",
        )


async def test_create_event_nonexistent_calendar(env):
    """Creating an event in a nonexistent calendar raises KeyError."""
    now = datetime.now(timezone.utc)
    with pytest.raises(KeyError):
        await env.cal_svc.create_event(
            calendar_id="nonexistent",
            summary="X",
            start=now.isoformat(),
            end=(now + timedelta(hours=1)).isoformat(),
            created_by="u1",
        )


async def test_create_event_end_before_start(env):
    """Event with end < start raises ValueError."""
    await env.db.enqueue(
        "INSERT OR IGNORE INTO users(username,user_id,display_name) VALUES(?,?,?)",
        ("carl", "u3", "C"),
    )
    cal = await env.cal_svc.create_calendar(name="C", owner_username="carl")
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="before start"):
        await env.cal_svc.create_event(
            calendar_id=cal.id,
            summary="Bad",
            start=(now + timedelta(hours=2)).isoformat(),
            end=now.isoformat(),
            created_by="u3",
        )


async def test_create_event_invalid_datetime(env):
    """Invalid datetime string raises ValueError."""
    await env.db.enqueue(
        "INSERT OR IGNORE INTO users(username,user_id,display_name) VALUES(?,?,?)",
        ("dan", "u4", "D"),
    )
    cal = await env.cal_svc.create_calendar(name="C", owner_username="dan")
    with pytest.raises(ValueError, match="invalid datetime"):
        await env.cal_svc.create_event(
            calendar_id=cal.id,
            summary="X",
            start="not-a-date",
            end="also-not",
            created_by="u4",
        )


async def test_get_nonexistent_event(env):
    """Getting a nonexistent event raises KeyError."""
    with pytest.raises(KeyError):
        await env.cal_svc.get_event("nonexistent")


async def test_delete_nonexistent_event(env):
    """Deleting a nonexistent event raises KeyError."""
    with pytest.raises(KeyError):
        await env.cal_svc.delete_event("nonexistent")


async def test_list_calendars(env):
    """list_calendars returns calendars for the given user."""
    await env.db.enqueue(
        "INSERT OR IGNORE INTO users(username,user_id,display_name) VALUES(?,?,?)",
        ("eve", "u5", "E"),
    )
    await env.cal_svc.create_calendar(name="C1", owner_username="eve")
    cals = await env.cal_svc.list_calendars("eve")
    assert len(cals) >= 1


async def test_space_calendar_service_list(env):
    """SpaceCalendarService.list_events_in_range works."""
    from socialhome.services.calendar_service import SpaceCalendarService

    svc = SpaceCalendarService(env.space_cal_repo)
    # Need a space
    kp2 = generate_identity_keypair()
    await env.db.enqueue(
        "INSERT OR IGNORE INTO users(username,user_id,display_name) VALUES(?,?,?)",
        ("spown", "uid-sp", "SP"),
    )
    sid = uuid.uuid4().hex
    await env.db.enqueue(
        """INSERT INTO spaces(id, name, owner_instance_id, owner_username,
           identity_public_key, config_sequence, space_type, join_mode)
           VALUES(?,?,?,?,?,0,'private','invite_only')""",
        (sid, "SpCal", env.iid, "spown", kp2.public_key.hex()),
    )
    now = datetime.now(timezone.utc)
    events = await svc.list_events_in_range(
        sid,
        start=(now - timedelta(hours=1)).isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
    )
    assert isinstance(events, list)


# ─── CalendarService publishes domain events (B1) ─────────────────────


async def _seed_user(db, username="owner"):
    await db.enqueue(
        "INSERT OR IGNORE INTO users(username, user_id, display_name) VALUES(?,?,?)",
        (username, f"uid-{username}", username),
    )


async def test_create_event_publishes_calendar_event_created(env):
    """CalendarService.create_event publishes CalendarEventCreated on the bus."""
    from socialhome.domain.events import CalendarEventCreated

    class _RecordingBus:
        def __init__(self):
            self.events = []

        def subscribe(self, *a, **kw):
            pass

        async def publish(self, event):
            self.events.append(event)

    await _seed_user(env.db)
    bus = _RecordingBus()
    svc = CalendarService(env.cal_repo, bus=bus)
    cal = await svc.create_calendar(name="Test", owner_username="owner")
    now = datetime.now(timezone.utc)
    await svc.create_event(
        calendar_id=cal.id,
        summary="Dinner",
        start=now.isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
        created_by="uid-owner",
    )
    assert any(isinstance(e, CalendarEventCreated) for e in bus.events)


async def test_delete_event_publishes_calendar_event_deleted(env):
    """CalendarService.delete_event publishes CalendarEventDeleted on the bus."""
    from socialhome.domain.events import CalendarEventDeleted

    class _RecordingBus:
        def __init__(self):
            self.events = []

        def subscribe(self, *a, **kw):
            pass

        async def publish(self, event):
            self.events.append(event)

    await _seed_user(env.db, "deleter")
    bus = _RecordingBus()
    svc = CalendarService(env.cal_repo, bus=bus)
    cal = await svc.create_calendar(name="Del", owner_username="deleter")
    now = datetime.now(timezone.utc)
    event = await svc.create_event(
        calendar_id=cal.id,
        summary="To delete",
        start=now.isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
        created_by="uid-deleter",
    )
    await svc.delete_event(event.id)
    assert any(isinstance(e, CalendarEventDeleted) for e in bus.events)


# ── Default-calendar seeding ────────────────────────────────────────────


async def test_seed_default_calendar_creates_row_when_missing(env):
    """First call seeds a 'Calendar' row owned by the user."""
    await _seed_user(env.db, "alice")
    cal = await env.cal_svc.seed_default_calendar_for("alice")
    assert cal.owner_username == "alice"
    assert cal.name == "Calendar"
    rows = await env.cal_repo.list_calendars_for_user("alice")
    assert [c.id for c in rows] == [cal.id]


async def test_seed_default_calendar_is_idempotent(env):
    """Second call returns the existing row without creating a duplicate."""
    await _seed_user(env.db, "bob")
    first = await env.cal_svc.seed_default_calendar_for("bob")
    second = await env.cal_svc.seed_default_calendar_for("bob")
    assert first.id == second.id
    rows = await env.cal_repo.list_calendars_for_user("bob")
    assert len(rows) == 1


async def test_seed_default_calendar_returns_existing_named_differently(env):
    """A user who already has a calendar named 'Work' keeps that one —
    we never silently add a second 'Calendar' row alongside it."""
    await _seed_user(env.db, "carol")
    work = await env.cal_svc.create_calendar(
        name="Work",
        owner_username="carol",
        color="#123456",
    )
    seeded = await env.cal_svc.seed_default_calendar_for("carol")
    assert seeded.id == work.id
    assert seeded.name == "Work"
    rows = await env.cal_repo.list_calendars_for_user("carol")
    assert len(rows) == 1


async def test_seed_default_calendar_bypasses_household_features_gate(env):
    """Seeding must work even when calendar feature is currently disabled —
    the row needs to exist so it's there when the household later turns
    the feature on."""

    class _DisabledHouseholdFeatures:
        async def require_enabled(self, feature: str) -> None:
            raise PermissionError(f"{feature} disabled")

    await _seed_user(env.db, "dana")
    env.cal_svc.attach_household_features(_DisabledHouseholdFeatures())
    cal = await env.cal_svc.seed_default_calendar_for("dana")
    assert cal.owner_username == "dana"


async def test_backfill_default_calendars_only_creates_missing(env):
    """Backfill skips users that already have a calendar; counts new rows."""
    await _seed_user(env.db, "eve")
    await _seed_user(env.db, "frank")
    await _seed_user(env.db, "gina")
    # Eve already has a calendar; Frank + Gina don't.
    await env.cal_svc.create_calendar(name="Mine", owner_username="eve")
    created = await env.cal_svc.backfill_default_calendars(["eve", "frank", "gina"])
    assert created == 2
    assert len(await env.cal_repo.list_calendars_for_user("eve")) == 1
    assert len(await env.cal_repo.list_calendars_for_user("frank")) == 1
    assert len(await env.cal_repo.list_calendars_for_user("gina")) == 1
    # Re-run is a no-op.
    again = await env.cal_svc.backfill_default_calendars(["eve", "frank", "gina"])
    assert again == 0


async def test_wire_subscribes_to_user_provisioned(env):
    """``wire()`` registers a UserProvisioned handler that seeds on publish."""
    from socialhome.domain.events import UserProvisioned
    from socialhome.infrastructure.event_bus import EventBus

    bus = EventBus()
    svc = CalendarService(env.cal_repo, bus=bus)
    svc.wire()
    assert bus.handler_count(UserProvisioned) == 1

    await _seed_user(env.db, "harry")
    await bus.publish(
        UserProvisioned(user_id="uid-harry", username="harry", is_admin=False),
    )
    rows = await env.cal_repo.list_calendars_for_user("harry")
    assert len(rows) == 1
    assert rows[0].name == "Calendar"


async def test_wire_is_noop_without_bus(env):
    """A bus-less service is constructed in unit tests that don't exercise
    the seeding path; ``wire()`` must not crash on it."""
    svc = CalendarService(env.cal_repo, bus=None)
    svc.wire()  # no exception


# ── client_event_uuid (issue #327) ─────────────────────────────────────


async def test_create_event_persists_client_event_uuid(env):
    """The composer's mint-once UUID lands on the row + round-trips through
    the dict shape on read."""
    await env.db.enqueue(
        "INSERT OR IGNORE INTO users(username,user_id,display_name) VALUES(?,?,?)",
        ("uli", "u-uli", "Uli"),
    )
    cal = await env.cal_svc.create_calendar(name="C", owner_username="uli")
    now = datetime.now(timezone.utc)
    ev = await env.cal_svc.create_event(
        calendar_id=cal.id,
        summary="UUID test",
        start=now.isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
        created_by="u-uli",
        client_event_uuid="abcdef0123456789abcdef0123456789",
    )
    assert ev.client_event_uuid == "abcdef0123456789abcdef0123456789"
    got = await env.cal_repo.get_event(ev.id)
    assert got is not None
    assert got.client_event_uuid == "abcdef0123456789abcdef0123456789"


async def test_create_event_normalises_uuid_with_dashes(env):
    """Canonical 8-4-4-4-12 dashed form is accepted + lowercased."""
    await env.db.enqueue(
        "INSERT OR IGNORE INTO users(username,user_id,display_name) VALUES(?,?,?)",
        ("vera", "u-vera", "Vera"),
    )
    cal = await env.cal_svc.create_calendar(name="C", owner_username="vera")
    now = datetime.now(timezone.utc)
    ev = await env.cal_svc.create_event(
        calendar_id=cal.id,
        summary="dashed",
        start=now.isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
        created_by="u-vera",
        client_event_uuid="ABCDEF01-2345-6789-ABCD-EF0123456789",
    )
    assert ev.client_event_uuid == "abcdef01-2345-6789-abcd-ef0123456789"


async def test_create_event_rejects_malformed_uuid(env):
    """Garbage / overlong inputs collapse to ``None`` — the SPA's
    content-key fallback covers them and a malicious client can't pump
    arbitrary bytes into the column."""
    await env.db.enqueue(
        "INSERT OR IGNORE INTO users(username,user_id,display_name) VALUES(?,?,?)",
        ("walt", "u-walt", "Walt"),
    )
    cal = await env.cal_svc.create_calendar(name="C", owner_username="walt")
    now = datetime.now(timezone.utc)
    for bad in (
        "",
        "not-a-uuid-yo",
        "g" * 32,  # right length, wrong charset
        "abcdef0123456789abcdef012345678",  # 31 chars
        "abcdef0123456789abcdef01234567890",  # 33 chars
        "x" * 200,
    ):
        ev = await env.cal_svc.create_event(
            calendar_id=cal.id,
            summary=f"bad-{bad[:4]}",
            start=now.isoformat(),
            end=(now + timedelta(hours=1)).isoformat(),
            created_by="u-walt",
            client_event_uuid=bad,
        )
        assert ev.client_event_uuid is None, bad


async def test_create_event_default_is_none(env):
    """No ``client_event_uuid`` parameter → row has ``NULL``."""
    await env.db.enqueue(
        "INSERT OR IGNORE INTO users(username,user_id,display_name) VALUES(?,?,?)",
        ("xena", "u-xena", "Xena"),
    )
    cal = await env.cal_svc.create_calendar(name="C", owner_username="xena")
    now = datetime.now(timezone.utc)
    ev = await env.cal_svc.create_event(
        calendar_id=cal.id,
        summary="no uuid",
        start=now.isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
        created_by="u-xena",
    )
    assert ev.client_event_uuid is None


# ── Server-authoritative fan-out copies (#660 follow-up) ───────────────────


class _CopyRecordingBus:
    """Bus stub that records every published domain event."""

    def __init__(self):
        self.events = []

    def subscribe(self, *a, **kw):
        pass

    async def publish(self, event):
        self.events.append(event)


async def test_list_event_copies_batches_distinct_uuids(env):
    """``list_event_copies`` asks the repo ONCE for the distinct set of
    non-None ``client_event_uuid``s — never one call per event."""
    calls: list[list[str]] = []
    real = env.cal_repo.list_copies_for_client_event_uuids

    async def _spy(uuids):
        calls.append(list(uuids))
        return await real(uuids)

    env.cal_repo.list_copies_for_client_event_uuids = _spy  # type: ignore[method-assign]

    await _seed_user(env.db, "copyowner")
    cal = await env.cal_svc.create_calendar(name="C", owner_username="copyowner")
    now = datetime.now(timezone.utc)
    grp = uuid.uuid4().hex
    ev = await env.cal_svc.create_event(
        calendar_id=cal.id,
        summary="Shared",
        start=now.isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
        created_by="uid-copyowner",
        client_event_uuid=grp,
    )
    plain = await env.cal_svc.create_event(
        calendar_id=cal.id,
        summary="Solo",
        start=now.isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
        created_by="uid-copyowner",
    )

    groups = await env.cal_svc.list_event_copies([ev, ev, plain])
    assert calls == [[grp]], calls
    assert set(groups) == {grp}
    assert [c.event_id for c in groups[grp]] == [ev.id]
    assert groups[grp][0].owner_username == "copyowner"
    assert isinstance(groups[grp], tuple)


async def test_list_event_copies_empty_input_skips_repo(env):
    """No events / no uuids → ``{}`` without touching the repo."""
    calls: list[list[str]] = []

    async def _spy(uuids):
        calls.append(list(uuids))
        return {}

    env.cal_repo.list_copies_for_client_event_uuids = _spy  # type: ignore[method-assign]

    await _seed_user(env.db, "emptyowner")
    cal = await env.cal_svc.create_calendar(name="C", owner_username="emptyowner")
    now = datetime.now(timezone.utc)
    plain = await env.cal_svc.create_event(
        calendar_id=cal.id,
        summary="Solo",
        start=now.isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
        created_by="uid-emptyowner",
    )
    assert await env.cal_svc.list_event_copies([]) == {}
    assert await env.cal_svc.list_event_copies([plain]) == {}
    assert calls == []


async def test_create_event_twice_with_same_uuid_updates_in_place(env):
    """REGRESSION: a repeated POST of the same shared event (same
    ``(calendar_id, client_event_uuid)``) resolves to an UPDATE of the
    existing row instead of minting a duplicate copy."""
    await _seed_user(env.db, "dup")
    bus = _CopyRecordingBus()
    svc = CalendarService(env.cal_repo, bus=bus)
    cal = await svc.create_calendar(name="C", owner_username="dup")
    now = datetime.now(timezone.utc)
    grp = uuid.uuid4().hex
    first = await svc.create_event(
        calendar_id=cal.id,
        summary="Picnic",
        start=now.isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
        created_by="uid-dup",
        client_event_uuid=grp,
    )
    bus.events.clear()
    second = await svc.create_event(
        calendar_id=cal.id,
        summary="Picnic — moved",
        start=(now + timedelta(hours=2)).isoformat(),
        end=(now + timedelta(hours=3)).isoformat(),
        created_by="uid-someone-else",
        client_event_uuid=grp,
        description="bring plates",
        location="The park",
    )

    assert second.id == first.id
    assert second.created_by == first.created_by == "uid-dup"
    assert second.summary == "Picnic — moved"
    assert second.description == "bring plates"
    assert second.location == "The park"
    assert second.client_event_uuid == grp

    rows = await env.db.fetchall(
        "SELECT id FROM calendar_events WHERE client_event_uuid=?", (grp,)
    )
    assert len(rows) == 1

    assert any(isinstance(e, CalendarEventUpdated) for e in bus.events)
    assert not any(isinstance(e, CalendarEventCreated) for e in bus.events)


async def test_create_event_resolve_to_update_federates_updated(federated_cal_env):
    """The resolve-to-update path federates
    ``PERSONAL_CALENDAR_EVENT_UPDATED``, not ``_CREATED``."""
    e = federated_cal_env
    cal = await e.cal_svc.create_calendar(name="Anna", owner_username="anna")
    await _seed_paired(e.db, instance_id="i_smith")
    await _seed_remote(
        e.db,
        instance_id="i_smith",
        user_id="u-bob",
        username="bob",
        display_name="Bob",
    )
    now = datetime.now(timezone.utc)
    grp = uuid.uuid4().hex
    await e.cal_svc.create_event(
        calendar_id=cal.id,
        summary="Picnic",
        start=now.isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
        created_by="uid-anna",
        attendees=["u-bob"],
        client_event_uuid=grp,
    )
    e.sent.clear()
    await e.cal_svc.create_event(
        calendar_id=cal.id,
        summary="Picnic — moved",
        start=now.isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
        created_by="uid-anna",
        attendees=["u-bob"],
        client_event_uuid=grp,
    )
    kinds = [str(row[1]).upper() for row in e.sent]
    assert any("PERSONAL_CALENDAR_EVENT_UPDATED" in k for k in kinds), kinds
    assert not any("PERSONAL_CALENDAR_EVENT_CREATED" in k for k in kinds), kinds


async def test_create_event_resolve_to_update_applies_resolved_tz(federated_cal_env):
    """The second create re-resolves the create-time tz chain (explicit →
    users.tz → household → UTC) rather than silently keeping the old tz."""
    env = federated_cal_env
    await env.db.enqueue(
        "UPDATE users SET tz=? WHERE username=?", ("Europe/Berlin", "anna")
    )
    cal = await env.cal_svc.create_calendar(name="C", owner_username="anna")
    now = datetime.now(timezone.utc)
    grp = uuid.uuid4().hex
    first = await env.cal_svc.create_event(
        calendar_id=cal.id,
        summary="A",
        start=now.isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
        created_by="uid-anna",
        client_event_uuid=grp,
        tz="America/New_York",
    )
    assert first.tz == "America/New_York"
    # No explicit tz on the retry → the chain resolves to the owner's tz,
    # not "keep whatever the row had".
    second = await env.cal_svc.create_event(
        calendar_id=cal.id,
        summary="A",
        start=now.isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
        created_by="uid-anna",
        client_event_uuid=grp,
    )
    assert second.id == first.id
    assert second.tz == "Europe/Berlin"


async def test_create_event_different_uuid_still_creates_second_row(env):
    """A different ``client_event_uuid`` (or none at all) keeps the
    legacy insert behaviour."""
    await _seed_user(env.db, "multi")
    cal = await env.cal_svc.create_calendar(name="C", owner_username="multi")
    now = datetime.now(timezone.utc)
    a = await env.cal_svc.create_event(
        calendar_id=cal.id,
        summary="A",
        start=now.isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
        created_by="uid-multi",
        client_event_uuid=uuid.uuid4().hex,
    )
    b = await env.cal_svc.create_event(
        calendar_id=cal.id,
        summary="B",
        start=now.isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
        created_by="uid-multi",
        client_event_uuid=uuid.uuid4().hex,
    )
    c = await env.cal_svc.create_event(
        calendar_id=cal.id,
        summary="C",
        start=now.isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
        created_by="uid-multi",
    )
    d = await env.cal_svc.create_event(
        calendar_id=cal.id,
        summary="D",
        start=now.isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
        created_by="uid-multi",
    )
    assert len({a.id, b.id, c.id, d.id}) == 4


async def test_create_event_integrity_error_retries_as_update(env):
    """A racing double-submit that trips ``ux_calendar_events_fanout``
    between the lookup and the INSERT surfaces as an update, not a 500."""
    await _seed_user(env.db, "racer")
    cal = await env.cal_svc.create_calendar(name="C", owner_username="racer")
    now = datetime.now(timezone.utc)
    grp = uuid.uuid4().hex
    first = await env.cal_svc.create_event(
        calendar_id=cal.id,
        summary="First",
        start=now.isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
        created_by="uid-racer",
        client_event_uuid=grp,
    )

    real_find = env.cal_repo.find_by_client_event_uuid
    state = {"first": True}

    async def _racy_find(calendar_id, client_event_uuid):
        # Simulate the concurrent writer landing *after* our lookup:
        # the pre-insert probe sees nothing, the INSERT then collides.
        if state["first"]:
            state["first"] = False
            return None
        return await real_find(calendar_id, client_event_uuid)

    env.cal_repo.find_by_client_event_uuid = _racy_find  # type: ignore[method-assign]

    real_save = env.cal_repo.save_event
    saves = {"n": 0}

    async def _colliding_save(event):
        saves["n"] += 1
        if saves["n"] == 1:
            raise sqlite3.IntegrityError(
                "UNIQUE constraint failed: index 'ux_calendar_events_fanout'"
            )
        return await real_save(event)

    env.cal_repo.save_event = _colliding_save  # type: ignore[method-assign]

    second = await env.cal_svc.create_event(
        calendar_id=cal.id,
        summary="Retried",
        start=now.isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
        created_by="uid-racer",
        client_event_uuid=grp,
    )
    assert second.id == first.id
    assert second.summary == "Retried"


async def test_update_event_rejects_uuid_already_used_on_calendar(env):
    """Stamping a ``client_event_uuid`` that another local row on the SAME
    calendar already occupies violates ``ux_calendar_events_fanout``.

    Two indistinguishable copies of one shared event on one calendar is a
    client error, so it surfaces as ``ValueError`` (→ 422), never a raw
    ``sqlite3.IntegrityError`` (→ 500).
    """
    await _seed_user(env.db, "clash")
    cal = await env.cal_svc.create_calendar(name="C", owner_username="clash")
    now = datetime.now(timezone.utc)
    grp = uuid.uuid4().hex
    taken = await env.cal_svc.create_event(
        calendar_id=cal.id,
        summary="Taken",
        start=now.isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
        created_by="uid-clash",
        client_event_uuid=grp,
    )
    legacy = await env.cal_svc.create_event(
        calendar_id=cal.id,
        summary="Legacy",
        start=now.isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
        created_by="uid-clash",
    )
    with pytest.raises(ValueError):
        await env.cal_svc.update_event(legacy.id, client_event_uuid=grp)
    # Neither row moved.
    assert (await env.cal_svc.get_event(legacy.id)).client_event_uuid is None
    assert (await env.cal_svc.get_event(taken.id)).client_event_uuid == grp


async def test_update_event_keeps_own_uuid_without_clashing(env):
    """Re-stamping an event with the uuid it already has is a no-op, not
    a self-collision against ``ux_calendar_events_fanout``."""
    await _seed_user(env.db, "selfsame")
    cal = await env.cal_svc.create_calendar(name="C", owner_username="selfsame")
    now = datetime.now(timezone.utc)
    grp = uuid.uuid4().hex
    ev = await env.cal_svc.create_event(
        calendar_id=cal.id,
        summary="Mine",
        start=now.isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
        created_by="uid-selfsame",
        client_event_uuid=grp,
    )
    updated = await env.cal_svc.update_event(
        ev.id, summary="Mine, edited", client_event_uuid=grp
    )
    assert updated.client_event_uuid == grp
    assert updated.summary == "Mine, edited"


async def test_update_event_reraises_unrelated_integrity_error(env):
    """Only ``ux_calendar_events_fanout`` maps to a 422.

    ``update_event`` relabels an ``IntegrityError`` as a ``ValueError``
    so a fan-out uuid collision surfaces as a 422 instead of a 500.
    That relabelling is index-specific: a future CHECK or FK violation
    on the same statement is a genuine server-side fault and must reach
    the caller unchanged, not be blamed on the client's uuid.
    """
    await _seed_user(env.db, "otherint")
    cal = await env.cal_svc.create_calendar(name="C", owner_username="otherint")
    now = datetime.now(timezone.utc)
    ev = await env.cal_svc.create_event(
        calendar_id=cal.id,
        summary="Mine",
        start=now.isoformat(),
        end=(now + timedelta(hours=1)).isoformat(),
        created_by="uid-otherint",
    )

    async def _other_violation(event):
        raise sqlite3.IntegrityError(
            "CHECK constraint failed: ck_calendar_events_origin"
        )

    env.cal_repo.save_event = _other_violation  # type: ignore[method-assign]

    with pytest.raises(sqlite3.IntegrityError):
        await env.cal_svc.update_event(ev.id, summary="Edited")
