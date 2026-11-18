"""Every federation event is classified for the pairing confirm step."""

from __future__ import annotations

from socialhome.domain.federation import FederationEventType
from socialhome.domain.pairing_events import (
    CONFIRMS_PAIRING_EVENT_TYPES,
    ENDS_PAIRING_EVENT_TYPES,
    PAIRING_HANDSHAKE_EVENT_TYPES,
)

_SETS = {
    "confirms": CONFIRMS_PAIRING_EVENT_TYPES,
    "ends": ENDS_PAIRING_EVENT_TYPES,
    "handshake": PAIRING_HANDSHAKE_EVENT_TYPES,
}


def test_every_event_type_is_classified_exactly_once():
    """Tripwire: a new ``FederationEventType`` fails here until somebody
    decides whether it may confirm a waiting pairing (the peer only sends
    it once its own side is confirmed), ends one, or says nothing — add it
    to ONE set in ``socialhome/domain/pairing_events.py``. Unclassified, it
    confirms nothing."""
    where = {
        t: [name for name, members in _SETS.items() if t in members]
        for t in FederationEventType
    }
    unclassified = sorted(t.name for t, sets in where.items() if not sets)
    twice = sorted(t.name for t, sets in where.items() if len(sets) > 1)
    assert unclassified == [], f"classify in pairing_events.py: {unclassified}"
    assert twice == [], f"in more than one set: {twice}"


def test_the_cancels_and_the_handshake_never_confirm():
    assert ENDS_PAIRING_EVENT_TYPES == {
        FederationEventType.PAIRING_ABORT,
        FederationEventType.UNPAIR,
    }
    assert all(t.name.startswith("PAIRING_") for t in PAIRING_HANDSHAKE_EVENT_TYPES)
    # Every other pairing message is part of the handshake.
    assert {
        t for t in FederationEventType if t.name.startswith("PAIRING_")
    } == PAIRING_HANDSHAKE_EVENT_TYPES | {FederationEventType.PAIRING_ABORT}


def test_what_a_freshly_confirmed_household_sends_first_confirms():
    """The announcements its ``PairingConfirmed`` subscribers send, and a
    space post its outbox may deliver before them."""
    for t in (
        FederationEventType.INSTANCE_CAPABILITIES_UPDATED,
        FederationEventType.USERS_SYNC,
        FederationEventType.SPACE_POST_CREATED,
        FederationEventType.SPACE_SYNC_BEGIN,
    ):
        assert t in CONFIRMS_PAIRING_EVENT_TYPES
