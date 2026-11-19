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


# ─── A listing without a contact (an invite-only space) ──────────────────


def _bare(host, space_kp, **over) -> dict:
    """An invite-only space's listing as a GFS serves it: no join contact,
    and so no cert either (``public_space_dict`` keeps it back)."""
    item = _item(host, space_kp, **over)
    item.pop("join_contact")
    return item


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]


def test_an_invite_only_spaces_rotation_is_no_conflict(caplog):
    """An invite-only listing pinned the authority key and NO owner. The
    owner's own rotation then showed up as a bare other key (no contact, so
    no cert): a "conflict" WARNING on every household that had seen the
    listing, and the GFS route to the space closed for good. Nothing is
    offered and nothing was vouched for: the pin stays as it is."""
    host, space, rotated = (
        _Host(),
        generate_identity_keypair(),
        generate_identity_keypair(),
    )
    pin = _see(None, _bare(host, space))
    assert load_pin(pin)["owner_instance"] == host.instance_id
    assert load_pin(pin)["owner_pk"] == ""
    with caplog.at_level("WARNING"):
        after = _see(pin, _bare(host, rotated), GFS_B)
    assert after == pin
    assert load_pin(after)["conflict"] is False
    assert _warnings(caplog) == []


def test_a_rotated_invite_only_space_can_take_requests_later():
    """Once such a space lists a contact, it comes with the owner's cert:
    the owner household the FIRST listing named (an instance id is the hash
    of its identity key) certifies the new key, and the pin follows."""
    host, space, rotated = (
        _Host(),
        generate_identity_keypair(),
        generate_identity_keypair(),
    )
    pin = _see(None, _bare(host, space))
    pin = _see(pin, _bare(host, rotated))
    taken = _see(pin, _item(host, rotated, cert=host.cert(rotated, 1)), GFS_B)
    held = load_pin(taken)
    assert held["conflict"] is False
    assert held["authority_pk"] == rotated.public_key.hex()
    assert held["key_epoch"] == 1
    assert held["owner_pk"] == host.ident.public_key.hex()
    assert usable_contact(taken)[0]["instance_id"] == host.instance_id
    # The same cert cannot move the pin again (the epoch must rise).
    again = _see(taken, _item(host, space, cert=host.cert(space, 1)))
    assert load_pin(again)["conflict"] is True


def test_an_invite_only_pin_refuses_a_contact_without_its_owners_chain():
    """Only the owner named at first sight moves the key. Another key with
    a contact and no cert, a cert by another household (under its own id or
    claiming the owner's), a contact for another household: conflicts."""
    host, space, rotated = (
        _Host(),
        generate_identity_keypair(),
        generate_identity_keypair(),
    )
    impostor = _Host()
    pin = _see(None, _bare(host, space))
    assert load_pin(_see(pin, _item(host, rotated)))["conflict"] is True
    own_id = _item(impostor, rotated, cert=impostor.cert(rotated, 5))
    assert load_pin(_see(pin, own_id))["conflict"] is True
    claimed = impostor.cert(rotated, 5)
    claimed["owner_instance_id"] = host.instance_id
    assert load_pin(_see(pin, _item(host, rotated, cert=claimed)))["conflict"] is True
    # …and under the SAME key, a contact must name that owner household.
    assert load_pin(_see(pin, _item(impostor, space)))["conflict"] is True
    taken = _see(pin, _item(host, space))
    assert usable_contact(taken)[0]["instance_id"] == host.instance_id
    assert load_pin(taken)["owner_pk"] == host.ident.public_key.hex()


def test_a_pin_that_names_no_owner_stays_fail_closed(caplog):
    """A pin stored before the owner id was pinned (or from a listing that
    names no owner): a bare other key is still no conflict, but there is
    nobody's chain to follow, so a contact under another key is refused
    even with a cert."""
    host, space, rotated = (
        _Host(),
        generate_identity_keypair(),
        generate_identity_keypair(),
    )
    pin = _see(None, _bare(host, space, owning_instance=""))
    assert load_pin(pin)["owner_instance"] == ""
    with caplog.at_level("WARNING"):
        assert _see(pin, _bare(host, rotated)) == pin
    assert _warnings(caplog) == []
    moved = _see(pin, _item(host, rotated, cert=host.cert(rotated, 1)))
    assert load_pin(moved)["conflict"] is True


def test_a_bare_other_key_still_conflicts_with_a_pinned_owner_key():
    """Unchanged: a pin that vouches for an owner key (a space that takes
    requests) treats another key without the owner's chain as a conflict,
    contact or not."""
    host, space, forged = (
        _Host(),
        generate_identity_keypair(),
        generate_identity_keypair(),
    )
    pin = _see(None, _item(host, space))
    assert load_pin(_see(pin, _bare(host, forged), GFS_B))["conflict"] is True
