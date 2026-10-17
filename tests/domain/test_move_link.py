"""Tests for socialhome.domain.move_link."""

from __future__ import annotations

from socialhome.domain.move_link import MoveLink
from socialhome.domain.user import UserIdentityAssertion


def _assertion() -> UserIdentityAssertion:
    """A binding-bearing assertion signed by the *new* home — it carries the
    P↔new_id binding (new_user_id == user_id, new_instance_id == instance_id)."""
    return UserIdentityAssertion(
        user_id="new_uid",
        instance_id="new_home",
        username="alice",
        display_name="Alice",
        issued_at="2026-06-16T00:00:00+00:00",
        signature="new-home-instance-sig",
        user_identity_public_key="deadbeefuserpk",
        user_sig_suite="ed25519",
        user_signature="user-self-sig",
        identity_anchor="anchoruuid",
    )


def _link() -> MoveLink:
    return MoveLink(
        suite="ed25519",
        user_public_key="deadbeefuserpk",
        old_user_id="old_uid",
        old_instance_id="old_home",
        issued_at="2026-06-16T00:00:00+00:00",
        new_instance_public_key="cafef00d",
        new_home_assertion=_assertion(),
        user_signature="user-sig-b64url",
        release_signature="release-sig-b64url",
    )


def test_move_link_wire_roundtrip():
    """A full MoveLink (with its embedded assertion) round-trips through the
    wire dict unchanged."""
    link = _link()
    assert MoveLink.from_wire_dict(link.to_wire_dict()) == link


def test_move_link_new_user_id_and_instance_id_properties():
    """``new_user_id``/``new_instance_id`` delegate to the embedded assertion —
    the destination identity lives in the binding, not on the link directly."""
    link = _link()
    assert link.new_user_id == "new_uid"
    assert link.new_instance_id == "new_home"
