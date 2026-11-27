"""v_97 ``ITEM_SPECS`` — the registry of event-shaped space items.

The registry is a security boundary: every person-action item type must
be in the space write vocabulary (so the writer-seat gate judges it, as it
judges the legacy event), no targeted secret may become an item (an item
reaches every seat of a channel), and every type is one a household seated
from an invite link may send (so the peer-class gate stays vacuous).
"""

from __future__ import annotations

import pytest

from socialhome.domain.federation import (
    SPACE_SESSION_ALLOWED_EVENT_TYPES,
    SPACE_WRITE_EVENT_TYPES,
    FederationEventType,
)
from socialhome.domain.gfs_channel import RING_CLASS_EPHEMERAL, RING_CLASSES
from socialhome.domain.space import SpaceFeatures
from socialhome.domain.space_item import (
    BUDGET_CLASSES,
    EVENT_ITEM_SIZE_BUCKETS,
    IDEMPOTENCY_CLASSES,
    ITEM_SIZE_BUCKETS,
    ITEM_SPECS,
    SUPPORTED_ITEM_TYPES,
    item_spec,
    pad_json_object,
    reader_cert_permits,
    required_scope,
)
from socialhome.domain.writer_cert import (
    WRITER_SCOPE_COMMENT,
    WRITER_SCOPE_WRITE,
    WriterCert,
)

E = FederationEventType

#: Targeted secrets and per-recipient traffic — never an item.
TARGETED = {
    E.SPACE_MEDIA_BLOB,
    E.BAZAAR_THREAD_MESSAGE,
    E.BAZAAR_OFFER_MADE,
    E.BAZAAR_OFFER_UPDATED,
    E.SPACE_APP_SESSION,
    E.SPACE_MODERATION_SUBMITTED,
    E.SPACE_KEY_EXCHANGE,
    E.SPACE_KEY_EXCHANGE_REKEY,
    E.SPACE_ADMIN_KEY_SHARE,
    E.SPACE_ROSTER_SNAPSHOT,
    E.SPACE_INVITE,
    E.SPACE_JOIN_REQUEST,
    E.SPACE_REPORT,
    E.SPACE_SYNC_PAGE,
    E.SPACE_MAILBOX_DIRECTORY,
    E.SPACE_ROUTED,
}


def test_every_key_is_its_event_types_value():
    for key, spec in ITEM_SPECS.items():
        assert key == spec.event_type.value


@pytest.mark.parametrize("item_type", sorted(ITEM_SPECS))
def test_every_spec_is_classified(item_type):
    spec = ITEM_SPECS[item_type]
    assert spec.idempotency in IDEMPOTENCY_CLASSES
    assert spec.budget in BUDGET_CLASSES
    assert spec.ring_class in RING_CLASSES
    assert spec.scope in (WRITER_SCOPE_WRITE, WRITER_SCOPE_COMMENT)


def test_every_person_item_is_a_gated_space_write():
    """The writer-seat gate fires on SPACE_WRITE_EVENT_TYPES only: a person
    item outside it would skip the follower / kicked-household refusal."""
    person = {spec.event_type for spec in ITEM_SPECS.values()}
    assert person <= SPACE_WRITE_EVENT_TYPES


@pytest.mark.security
def test_no_host_statement_is_an_item():
    """Host statements keep their NEVER_DROP event until v_98 orders them
    (owner 2026-11-27, option A): none is in the registry."""
    host = {
        E.SPACE_MEMBER_JOINED,
        E.SPACE_MEMBER_LEFT,
        E.SPACE_MEMBER_BANNED,
        E.SPACE_MEMBER_UNBANNED,
        E.SPACE_MEMBER_ROLE_CHANGED,
        E.SPACE_CONFIG_CHANGED,
        E.SPACE_AGE_GATE_UPDATED,
        E.SPACE_DISSOLVED,
        E.SPACE_MODERATION_DECIDED,
        E.SPACE_ADMIN_PROPOSAL_UPDATED,
    }
    assert not host & {spec.event_type for spec in ITEM_SPECS.values()}


def test_no_targeted_secret_is_an_item():
    assert not {spec.event_type for spec in ITEM_SPECS.values()} & TARGETED


def test_every_item_is_allowed_from_a_link_seated_household():
    every = {spec.event_type for spec in ITEM_SPECS.values()}
    assert every <= SPACE_SESSION_ALLOWED_EVENT_TYPES


def test_v2_types_never_collide_with_v1_types():
    assert not set(ITEM_SPECS) & SUPPORTED_ITEM_TYPES


def test_only_the_location_pin_is_ephemeral():
    ephemeral = {
        t for t, s in ITEM_SPECS.items() if s.ring_class == RING_CLASS_EPHEMERAL
    }
    assert ephemeral == {E.SPACE_LOCATION_UPDATED.value}


def test_item_spec_lookup():
    assert item_spec("space_chat_message_created") is not None
    assert item_spec("post") is None
    assert item_spec(None) is None
    assert item_spec("nope") is None


def test_required_scope_covers_v2_types():
    assert required_scope("space_task_created") == WRITER_SCOPE_WRITE
    assert required_scope("space_comment_created") == WRITER_SCOPE_COMMENT
    assert required_scope("comment") == WRITER_SCOPE_COMMENT
    with pytest.raises(ValueError):
        required_scope("nope")


def _reader_cert() -> WriterCert:
    return WriterCert(
        cert_suite="ed25519",
        space_id="sp",
        epoch=1,
        instance_pk="a" * 43,
        scope=WRITER_SCOPE_COMMENT,
        issued_at=1,
        cert_sig="x",
        reader=True,
    )


def test_a_reader_cert_carries_v2_comments_only_while_the_switch_is_on():
    cert = _reader_cert()
    on = SpaceFeatures(allow_subscriber_comment=True)
    off = SpaceFeatures(allow_subscriber_comment=False)
    assert reader_cert_permits(cert, "space_comment_created", on)
    assert not reader_cert_permits(cert, "space_comment_created", off)
    # The author's own comment delete always (owner F6).
    assert reader_cert_permits(cert, "space_comment_deleted", off)


def test_a_reader_cert_never_carries_a_v2_type_without_a_reader_mapping():
    cert = _reader_cert()
    every = SpaceFeatures(allow_subscriber_comment=True, allow_subscriber_react=True)
    for item_type in (
        "space_post_created",
        "space_task_created",
        "space_chat_message_created",
        "space_member_banned",
    ):
        assert not reader_cert_permits(cert, item_type, every)


def test_the_2_kib_bucket_is_for_event_shaped_items_only():
    assert 2048 in EVENT_ITEM_SIZE_BUCKETS
    assert 2048 not in ITEM_SIZE_BUCKETS
    assert list(EVENT_ITEM_SIZE_BUCKETS) == sorted(EVENT_ITEM_SIZE_BUCKETS)
    padded = pad_json_object({"x": "y" * 1100}, buckets=EVENT_ITEM_SIZE_BUCKETS)
    assert len(padded) == 2048
