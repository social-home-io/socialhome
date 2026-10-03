"""Tests for socialhome.domain.federation."""

from __future__ import annotations

from socialhome.domain.federation import (
    ARCHIVED_ALLOWED_REMOVAL_TYPES,
    DELIVERY_ERROR_MESH_DEFERRED,
    DELIVERY_ERROR_QUEUED,
    BroadcastResult,
    DecryptedPayload,
    DeliveryResult,
    FederationEnvelope,
    FederationEvent,
    FederationEventType,
    PAIRING_EVENTS,
    SPACE_READER_EVENT_TYPES,
    SPACE_SESSION_ALLOWED_EVENT_TYPES,
    SPACE_WRITE_EVENT_TYPES,
    STRUCTURAL_EVENTS,
    is_relay_only,
)


def test_broadcast_result_all_ok():
    """BroadcastResult.all_ok is True iff all deliveries succeeded."""
    r1 = BroadcastResult(attempted=2, succeeded=2, failed=0)
    assert r1.all_ok
    r2 = BroadcastResult(attempted=2, succeeded=1, failed=1)
    assert not r2.all_ok
    r3 = BroadcastResult(attempted=0, succeeded=0, failed=0)
    assert not r3.all_ok


def test_delivery_error_queued_keeps_wire_value():
    """The constant names the outbox-queued reason; the log/wire literal
    stays ``"delivery_failed"`` so existing consumers keep matching."""
    assert DELIVERY_ERROR_QUEUED == "delivery_failed"


def test_broadcast_result_terminal_failures_excludes_queued():
    """``terminal_failures`` is the mesh-path subset of ``ok=False``: an
    outbox-queued direct-peer failure self-heals and is NOT a loss, while
    ``failed`` still counts every ``ok=False`` result."""
    queued = DeliveryResult(instance_id="direct", ok=False, error=DELIVERY_ERROR_QUEUED)
    lost = DeliveryResult(instance_id="mesh", ok=False, error="no_route")
    fine = DeliveryResult(instance_id="ok", ok=True)
    r = BroadcastResult(
        attempted=3, succeeded=1, failed=2, results=(queued, lost, fine)
    )
    assert r.failed == 2
    assert r.terminal_failures == (lost,)
    assert not r.all_ok

    only_queued = BroadcastResult(attempted=1, succeeded=0, failed=1, results=(queued,))
    assert only_queued.failed == 1
    assert only_queued.terminal_failures == ()


def test_broadcast_result_terminal_failures_excludes_a_deferred_mesh_send():
    """A mesh member in its route cooldown has one re-send armed — late, not
    lost — so it is not a terminal failure (the re-send warns on its own)."""
    deferred = DeliveryResult(
        instance_id="mesh", ok=False, error=DELIVERY_ERROR_MESH_DEFERRED
    )
    r = BroadcastResult(attempted=1, succeeded=0, failed=1, results=(deferred,))
    assert r.failed == 1
    assert r.terminal_failures == ()


def test_delivery_result():
    """DeliveryResult carries instance_id and ok flag."""
    d = DeliveryResult(instance_id="p1", ok=True, status_code=200)
    assert d.ok


def test_federation_event():
    """FederationEvent carries the validated payload and space_id."""
    e = FederationEvent(
        msg_id="m1",
        event_type=FederationEventType.SPACE_POST_CREATED,
        from_instance="a",
        to_instance="b",
        timestamp="now",
        payload={"key": "val"},
        space_id="s1",
    )
    assert e.space_id == "s1"


def test_decrypted_payload():
    """DecryptedPayload wraps event_type and a payload dict."""
    dp = DecryptedPayload(
        event_type=FederationEventType.DM_MESSAGE,
        payload={"x": 1},
    )
    assert dp.payload["x"] == 1


def test_federation_envelope():
    """FederationEnvelope exposes routing fields and proto_version=1."""
    env = FederationEnvelope(
        msg_id="m1",
        event_type=FederationEventType.PAIRING_ACCEPT,
        from_instance="a",
        to_instance="b",
        timestamp="now",
        encrypted_payload="enc",
        signature="sig",
    )
    assert env.proto_version == 1


def test_pairing_events_subset():
    """PAIRING_EVENTS contains pairing types but not structural ones."""
    assert FederationEventType.PAIRING_INTRO in PAIRING_EVENTS
    assert FederationEventType.SPACE_POST_CREATED not in PAIRING_EVENTS


def test_structural_events_subset():
    """STRUCTURAL_EVENTS contains SPACE_CREATED."""
    assert FederationEventType.SPACE_CREATED in STRUCTURAL_EVENTS


def test_app_session_event_type_round_trip():
    """APP_SESSION wire value round-trips through the str enum."""
    assert FederationEventType("app_session") == FederationEventType.APP_SESSION
    assert FederationEventType.APP_SESSION == "app_session"


def test_app_message_event_type_round_trip():
    """APP_MESSAGE wire value round-trips through the str enum."""
    assert FederationEventType("app_message") == FederationEventType.APP_MESSAGE
    assert FederationEventType.APP_MESSAGE == "app_message"


def test_space_sync_rejected_event_type_round_trip():
    """SPACE_SYNC_REJECTED wire value round-trips through the str enum."""
    assert (
        FederationEventType("space_sync_rejected")
        == FederationEventType.SPACE_SYNC_REJECTED
    )
    assert FederationEventType.SPACE_SYNC_REJECTED == "space_sync_rejected"


def test_space_route_stale_event_type_round_trip():
    """SPACE_ROUTE_STALE wire value round-trips through the str enum.

    Membership in the enum is what makes the §24.11 validator accept
    the type — see ``tests/federation/test_inbound_validator.py``.
    """
    assert (
        FederationEventType("space_route_stale")
        == FederationEventType.SPACE_ROUTE_STALE
    )
    assert FederationEventType.SPACE_ROUTE_STALE == "space_route_stale"


# ─── Space write / read partition (the read-only Follower gate) ──────────


def test_every_space_event_type_is_classified_write_or_read():
    """Default-deny over the whole enum, same idiom as the peer-class list.

    A ``SPACE_*`` / ``BAZAAR_*`` type added tomorrow has to be put in
    :data:`SPACE_WRITE_EVENT_TYPES` (a space-content mutation — refused
    from a household holding only Follower seats) or deliberately into
    :data:`SPACE_READER_EVENT_TYPES` (a reader may legitimately send it).
    Forgetting the classification fails here rather than silently
    handing a Follower a new write surface.
    """
    scoped = {
        e
        for e in FederationEventType
        if e.name.startswith("SPACE_") or e.name.startswith("BAZAAR_")
    }
    unclassified = scoped - SPACE_WRITE_EVENT_TYPES - SPACE_READER_EVENT_TYPES
    assert not unclassified, (
        "classify these as a space write or an explicit reader event: "
        f"{sorted(e.name for e in unclassified)}"
    )


def test_the_write_and_reader_sets_are_disjoint():
    assert not (SPACE_WRITE_EVENT_TYPES & SPACE_READER_EVENT_TYPES)


def test_the_classification_covers_only_space_scoped_types():
    """Neither set reaches outside the ``SPACE_``/``BAZAAR_`` families —
    the gate is about space content, not about DMs or presence."""
    for event_type in SPACE_WRITE_EVENT_TYPES | SPACE_READER_EVENT_TYPES:
        assert event_type.name.startswith(("SPACE_", "BAZAAR_"))


def test_the_content_mutations_are_writes():
    """Spot-check the families the §24.11 gate exists for — a follower
    household must not be able to post, task, sticky, vote, RSVP, bid,
    pin a zone, ship media bytes, or edit/delete anyone's row."""
    for event_type in (
        FederationEventType.SPACE_POST_CREATED,
        FederationEventType.SPACE_POST_UPDATED,
        FederationEventType.SPACE_POST_DELETED,
        FederationEventType.SPACE_MEDIA_BLOB,
        FederationEventType.SPACE_COMMENT_CREATED,
        FederationEventType.SPACE_TASK_CREATED,
        FederationEventType.SPACE_STICKY_UPDATED,
        FederationEventType.SPACE_CALENDAR_EVENT_DELETED,
        FederationEventType.SPACE_POLL_VOTE_CAST,
        FederationEventType.SPACE_RSVP_UPDATED,
        FederationEventType.SPACE_SCHEDULE_FINALIZED,
        FederationEventType.SPACE_GALLERY_ITEM_CREATED,
        FederationEventType.SPACE_LOCATION_UPDATED,
        FederationEventType.SPACE_ZONE_UPSERTED,
        FederationEventType.BAZAAR_LISTING_CREATED,
        FederationEventType.BAZAAR_BID_PLACED,
        FederationEventType.BAZAAR_OFFER_ACCEPTED,
    ):
        assert event_type in SPACE_WRITE_EVENT_TYPES


def test_a_reader_may_still_speak_the_non_content_vocabulary():
    """A Follower household is a real participant on the transport: it
    reports, syncs, rekeys and leaves like anybody else."""
    for event_type in (
        FederationEventType.SPACE_REPORT,
        FederationEventType.SPACE_KEY_EXCHANGE,
        FederationEventType.SPACE_KEY_EXCHANGE_ACK,
        FederationEventType.SPACE_SYNC_BEGIN,
        FederationEventType.SPACE_SYNC_CHUNK,
        FederationEventType.SPACE_INSTANCE_LEFT,
        FederationEventType.SPACE_MEMBER_PROFILE_UPDATED,
        FederationEventType.SPACE_ROUTED,
        FederationEventType.SPACE_INVITE_TOKEN_REDEEM_ACK,
    ):
        assert event_type in SPACE_READER_EVENT_TYPES


# ─── is_relay_only: accepted by the connection server vs delivered ──────


def test_relay_only_when_never_delivered_but_relay_accepted():
    """A household we have only ever handed to the relay has never been
    proven reachable — that is exactly the relay-only state."""
    assert is_relay_only("2026-09-20 10:00:00", None) is True


def test_relay_only_when_relay_acceptance_is_newer_than_delivery():
    assert is_relay_only("2026-09-20 10:00:05", "2026-09-20 10:00:00") is True


def test_not_relay_only_when_delivery_is_as_recent_as_relay():
    """A proven delivery at or after the latest relay acceptance means the
    household is really there — not relay-only."""
    assert is_relay_only("2026-09-20 10:00:00", "2026-09-20 10:00:00") is False
    assert is_relay_only("2026-09-20 10:00:00", "2026-09-20 11:00:00") is False


def test_not_relay_only_without_any_relay_acceptance():
    assert is_relay_only(None, None) is False
    assert is_relay_only(None, "2026-09-20 10:00:00") is False


# ─── Federated moderation (v_43) ────────────────────────────────────────


def test_moderation_event_types_round_trip():
    assert (
        FederationEventType("space_moderation_submitted")
        is FederationEventType.SPACE_MODERATION_SUBMITTED
    )
    assert (
        FederationEventType("space_moderation_decided")
        is FederationEventType.SPACE_MODERATION_DECIDED
    )


def test_a_submission_is_a_space_write_and_a_decision_is_not():
    """A submission is content (frozen by an archive, refused from a
    Follower household); a decision is a verdict whose sender the handler
    checks for content authority, and it must still land in an archived
    space (a reject works there)."""
    sub = FederationEventType.SPACE_MODERATION_SUBMITTED
    dec = FederationEventType.SPACE_MODERATION_DECIDED
    assert sub in SPACE_WRITE_EVENT_TYPES
    assert sub not in ARCHIVED_ALLOWED_REMOVAL_TYPES
    assert dec in SPACE_READER_EVENT_TYPES


def test_moderation_events_may_come_from_a_link_joined_household():
    """A link-joined member submits, and a link-joined moderator decides."""
    assert (
        FederationEventType.SPACE_MODERATION_SUBMITTED
        in SPACE_SESSION_ALLOWED_EVENT_TYPES
    )
    assert (
        FederationEventType.SPACE_MODERATION_DECIDED
        in SPACE_SESSION_ALLOWED_EVENT_TYPES
    )
