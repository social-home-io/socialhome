"""Pinning a global space's join contact from GFS listings (v_84).

The first listing pins the space authority key and, through the contact it
signed, the OWNER household's identity key. After that the authority key
moves only along the owner's own v_44 rotation chain: a cert signed by the
pinned owner key for the new key at a higher epoch. Nothing a GFS controls
(its own ``authority_rotation_seq``, which GFS lists it) moves a pin.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import orjson

from socialhome.authority_cert import sign_authority_cert
from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    generate_identity_keypair,
    generate_x25519_keypair,
    sign_ed25519,
)
from socialhome.federation.join_contact_pin import (
    candidate_from_listing,
    load_pin,
    merge_pin,
    usable_contact,
)
from socialhome.space_join_contact import sign_join_contact

GFS_A = "https://gfs-a.example"
GFS_B = "https://gfs-b.example"
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


class _Host:
    def __init__(self) -> None:
        self.ident = generate_identity_keypair()
        self.keywrap = generate_x25519_keypair()
        self.instance_id = derive_instance_id(self.ident.public_key)

    def fields(self, keywrap=None) -> dict:
        kw = keywrap or self.keywrap
        return {
            "instance_id": self.instance_id,
            "identity_pk": self.ident.public_key.hex(),
            "keywrap_pk": kw.public_key.hex(),
            "keywrap_sig": b64url_encode(
                sign_ed25519(self.ident.private_key, kw.public_key)
            ),
        }

    def cert(self, space_kp, epoch: int, space_id: str = "sp-1") -> dict:
        return sign_authority_cert(
            space_id=space_id,
            owner_instance_id=self.instance_id,
            owner_seed=self.ident.private_key,
            owner_pk_hex=self.ident.public_key.hex(),
            authority_pk_hex=space_kp.public_key.hex(),
            key_epoch=epoch,
        )


def _item(host, space_kp, *, at=T0, keywrap=None, cert=None, **over) -> dict:
    contact = sign_join_contact(
        space_id="sp-1",
        space_seed=space_kp.private_key,
        issued_at=at.isoformat(),
        **host.fields(keywrap),
    )
    item = {
        "space_id": "sp-1",
        "owning_instance": host.instance_id,
        "identity_public_key": space_kp.public_key.hex(),
        "authority_rotation_seq": 0,
        "join_contact": contact,
        **over,
    }
    if cert is not None:
        item["authority_cert"] = cert
    return item


def _see(pin, item, gfs=GFS_A):
    return merge_pin(pin, candidate_from_listing(item, gfs_url=gfs), space_id="sp-1")


def test_a_valid_listing_offers_a_usable_contact():
    host, space = _Host(), generate_identity_keypair()
    pin = _see(None, _item(host, space))
    contact, gfs = usable_contact(pin)
    assert contact["instance_id"] == host.instance_id
    assert gfs == GFS_A


def test_a_contact_that_does_not_hold_is_not_offered():
    host, space = _Host(), generate_identity_keypair()
    assert usable_contact(_see(None, _item(host, space, owning_instance="x"))) is None
    other = _Host()
    bad = _item(host, space)
    bad["join_contact"]["keywrap_sig"] = other.fields()["keywrap_sig"]
    assert usable_contact(_see(None, bad)) is None
    assert candidate_from_listing({"space_id": "sp-1"}, gfs_url=GFS_A) is None


def test_the_pinning_gfs_cannot_move_the_key_with_its_own_counter():
    """``authority_rotation_seq`` is the GFS's own counter: a higher one
    from the very GFS that gave the pin, with no owner cert, is a conflict."""
    host, space, forged = (
        _Host(),
        generate_identity_keypair(),
        generate_identity_keypair(),
    )
    pin = _see(None, _item(host, space))
    pin = _see(pin, _item(host, forged, authority_rotation_seq=7))
    assert usable_contact(pin) is None
    assert load_pin(pin)["conflict"] is True


def test_the_owners_rotation_cert_moves_the_pin_from_any_gfs():
    """The v_44 chain: a cert signed by the OWNER key pinned with the first
    contact, for the new key, at a higher epoch."""
    host, space, rotated = (
        _Host(),
        generate_identity_keypair(),
        generate_identity_keypair(),
    )
    pin = _see(None, _item(host, space))
    moved = _see(
        pin,
        _item(host, rotated, at=T0 + timedelta(hours=1), cert=host.cert(rotated, 1)),
        GFS_B,
    )
    assert load_pin(moved)["authority_pk"] == rotated.public_key.hex()
    assert load_pin(moved)["key_epoch"] == 1
    assert usable_contact(moved) is not None
    # The same cert again cannot move it anywhere else (epoch must rise).
    again = _see(moved, _item(host, space, cert=host.cert(space, 1)))
    assert load_pin(again)["conflict"] is True


def test_a_cert_from_anyone_but_the_pinned_owner_is_a_conflict():
    host, space, rotated = (
        _Host(),
        generate_identity_keypair(),
        generate_identity_keypair(),
    )
    impostor = _Host()
    pin = _see(None, _item(host, space))
    # A cert for the right owner id cannot be made by another key; one for
    # the impostor's own id names another owner.
    forged = _see(pin, _item(host, rotated, cert=impostor.cert(rotated, 5)))
    assert load_pin(forged)["conflict"] is True


def test_a_contact_naming_another_owner_key_is_a_conflict():
    """Same authority key, but its contact now names another household key:
    the owner the pin vouches for changed — refused."""
    host, space, other = _Host(), generate_identity_keypair(), _Host()
    pin = _see(None, _item(host, space))
    # A valid contact for another household under the SAME authority key
    # (whoever holds the space seed could sign one).
    pin2 = _see(pin, _item(other, space, at=T0 + timedelta(hours=1)))
    assert usable_contact(pin2) is None
    assert load_pin(pin2)["conflict"] is True


def test_an_older_contact_is_not_taken_back():
    """No replay of a pre-rotation key-wrap key: the newest ``issued_at``
    wins, an older one is ignored."""
    host, space = _Host(), generate_identity_keypair()
    old_kw, new_kw = generate_x25519_keypair(), generate_x25519_keypair()
    pin = _see(None, _item(host, space, at=T0, keywrap=old_kw))
    pin = _see(pin, _item(host, space, at=T0 + timedelta(days=1), keywrap=new_kw))
    pin = _see(pin, _item(host, space, at=T0, keywrap=old_kw), GFS_B)
    assert usable_contact(pin)[0]["keywrap_pk"] == new_kw.public_key.hex()


def test_another_authority_from_another_gfs_is_a_lasting_conflict():
    host, space, forged = (
        _Host(),
        generate_identity_keypair(),
        generate_identity_keypair(),
    )
    pin = _see(None, _item(host, space))
    pin = _see(pin, _item(host, forged), GFS_B)
    assert usable_contact(pin) is None
    pin = _see(pin, _item(host, space))
    assert usable_contact(pin) is None


def test_garbage_reads_as_nothing():
    assert load_pin(None) is None
    assert load_pin("not json") is None
    assert load_pin(orjson.dumps([1]).decode()) is None
    assert merge_pin("x", None, space_id="sp") == "x"


def test_a_contactless_listing_does_not_reset_freshness():
    """The probe: day-1 contact, then a listing without one, then the
    replayed day-0 contact — the old key-wrap key must not come back."""
    host, space = _Host(), generate_identity_keypair()
    old_kw, new_kw = generate_x25519_keypair(), generate_x25519_keypair()
    pin = _see(None, _item(host, space, at=T0, keywrap=old_kw))
    pin = _see(pin, _item(host, space, at=T0 + timedelta(days=1), keywrap=new_kw))
    bare = _item(host, space)
    bare.pop("join_contact")
    pin = _see(pin, bare)
    assert usable_contact(pin) is None
    pin = _see(pin, _item(host, space, at=T0, keywrap=old_kw), GFS_B)
    assert usable_contact(pin) is None
    # The owner's next, newer contact is taken again.
    later = T0 + timedelta(days=2)
    pin = _see(pin, _item(host, space, at=later, keywrap=new_kw))
    assert usable_contact(pin)[0]["keywrap_pk"] == new_kw.public_key.hex()


def test_a_contact_from_the_future_is_refused():
    """A seed holder (a demoted admin) or a clock jump must not freeze a
    binding by signing it far ahead: more than a few minutes in the future
    is not a contact."""
    host, space = _Host(), generate_identity_keypair()
    ahead = datetime.now(UTC) + timedelta(hours=1)
    assert usable_contact(_see(None, _item(host, space, at=ahead))) is None
    near = datetime.now(UTC) + timedelta(minutes=1)
    assert usable_contact(_see(None, _item(host, space, at=near))) is not None


def test_a_transfer_inside_the_owner_household_keeps_the_pin():
    """Ownership moves between people of the owner household only
    (``transfer_ownership``): the owner key the pin holds does not change,
    so the next listing is taken as before."""
    host, space = _Host(), generate_identity_keypair()
    pin = _see(None, _item(host, space))
    pin = _see(pin, _item(host, space, at=T0 + timedelta(hours=2)))
    assert usable_contact(pin) is not None
    assert load_pin(pin)["conflict"] is False
