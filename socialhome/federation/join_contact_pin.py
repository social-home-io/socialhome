"""Pinning a global space's join contact from GFS listings (v_84).

A GFS listing carries the owner's join contact (its keys, signed by the
space authority key — :mod:`socialhome.space_join_contact`), the space
authority key itself (``identity_public_key``) and, with a contact, the
owner's v_44 rotation cert for that key. The GFS serves all three, so the
first listing a household sees is trusted on first use (TOFU), exactly like
a follower's first pin of the same key. That first contact pins two keys:
the **space authority key** and, through the contact it signed, the
**owner household's identity key**. After that:

* the authority key moves only along the owner's own chain: a v_44 cert
  (:func:`~socialhome.authority_cert.verify_authority_cert`) signed by the
  PINNED owner key, for the new key, at a higher ``key_epoch`` than the
  pin holds. Nothing a GFS controls — its own ``authority_rotation_seq``,
  which GFS lists it, how often — moves a pin;
* under the same key, a contact must name the same owner key, and only a
  newer one (signed ``issued_at``) is taken: the pin keeps the newest
  ``issued_at`` it ever took as a high-water mark that never goes down —
  also across a listing without a contact — so an older key-wrap key
  cannot be replayed. A contact signed more than
  :data:`MAX_FUTURE_SKEW` ahead is no contact (a seed holder or a clock
  jump cannot freeze a binding by dating it in the future);
* anything else — another authority without that chain, another owner, a
  GFS that swapped the key — is a **conflict**: the contact is dropped and
  stays dropped (fail closed). Nothing is sealed to such a space through
  the GFS; its people ask once paired with its owner household (a pairing
  is asked over the pairing, this pin is not consulted) or join with an
  invite link. (Any one GFS can therefore close the GFS route to a space
  for the households that see it — a documented, accepted lever.)

The owner household key does not change in the protocol today: an
ownership transfer moves the space between people OF the owner household
(``SpaceService.transfer_ownership``), and a household's identity key is
permanent. So there is no owner-change chain to follow; a contact naming
another owner key is a conflict.

The pin lives in its own table (``public_space_join_pins``), never in the
display cache: a GFS that withholds a listing until the cache row is
purged and then relists the space under its own key meets the old pin.

Pure functions over JSON strings.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import orjson

from ..authority_cert import (
    InvalidAuthorityCert,
    UnsupportedAuthorityCertSuite,
    verify_authority_cert,
)
from ..space_join_contact import (
    contact_issued_at,
    parse_join_contact,
    verify_join_contact,
)
from .keywrap_seal import verify_keywrap_binding

log = logging.getLogger(__name__)

#: How far in the future a contact's signed ``issued_at`` may be.
MAX_FUTURE_SKEW: timedelta = timedelta(minutes=5)


def candidate_from_listing(item: dict, *, gfs_url: str) -> str | None:
    """What one directory row offers (JSON), or ``None`` when it names no
    authority key. Its ``contact`` is ``None`` unless it verifies under the
    row's own authority key, names the listed owner and binds its key-wrap
    key to its identity key end to end. The cert is carried unchecked —
    :func:`merge_pin` checks it against the PINNED owner key."""
    space_id = str(item.get("space_id") or "")
    authority_pk = item.get("identity_public_key")
    if not space_id or not isinstance(authority_pk, str) or not authority_pk:
        return None
    contact = parse_join_contact(item.get("join_contact"))
    if contact is not None and _from_the_future(contact):
        log.warning(
            "public_space_discovery: %s listed a join contact for %s dated in the"
            " future — ignored",
            gfs_url,
            space_id,
        )
        contact = None
    if contact is not None and not _contact_holds(
        contact,
        space_id=space_id,
        owner=str(item.get("owning_instance") or ""),
        authority_pk=authority_pk,
    ):
        log.warning(
            "public_space_discovery: %s listed a join contact for %s that does not"
            " verify — ignored",
            gfs_url,
            space_id,
        )
        contact = None
    cert = item.get("authority_cert")
    return _dump(
        {
            "contact": contact,
            "authority_pk": authority_pk.lower(),
            "cert": cert if isinstance(cert, dict) else None,
            "gfs": gfs_url,
        }
    )


def merge_pin(
    existing: str | None, candidate: str | None, *, space_id: str
) -> str | None:
    """The pin to store after a listing offered *candidate* (see the module
    docstring for the rules)."""
    old = load_pin(existing)
    new = _load_candidate(candidate)
    if new is None:
        return existing
    if old is None:
        return _first_pin(new, space_id=space_id)
    if old["conflict"]:
        return existing
    if new["authority_pk"] == old["authority_pk"]:
        return _same_key(old, new, space_id=space_id)
    rotated = _rotation(old, new, space_id=space_id)
    if rotated is not None:
        return rotated
    return _conflict(
        old,
        f"{new['gfs']} lists it under another authority key than the one pinned "
        f"from {old['gfs']}, with no rotation cert from its owner",
        space_id=space_id,
    )


def load_pin(raw: str | None) -> dict[str, Any] | None:
    """The stored pin as a dict, or ``None`` for nothing / garbage."""
    if not raw:
        return None
    try:
        value = orjson.loads(raw)
    except ValueError:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("authority_pk"), str):
        return None
    contact = value.get("contact")
    epoch = value.get("key_epoch")
    return {
        "contact": parse_join_contact(contact) if contact is not None else None,
        "authority_pk": value["authority_pk"],
        "contact_issued_at": str(value.get("contact_issued_at") or ""),
        "owner_pk": str(value.get("owner_pk") or ""),
        "owner_instance": str(value.get("owner_instance") or ""),
        "key_epoch": epoch
        if isinstance(epoch, int) and not isinstance(epoch, bool)
        else 0,
        "gfs": str(value.get("gfs") or ""),
        "conflict": value.get("conflict") is True,
    }


def usable_contact(raw: str | None) -> tuple[dict[str, str], str] | None:
    """``(contact, gfs_url)`` an "Ask to join" may be sealed to — a pin with
    a contact and no conflict — or ``None``."""
    pin = load_pin(raw)
    if pin is None or pin["conflict"] or pin["contact"] is None:
        return None
    return pin["contact"], pin["gfs"]


# ─── Rules ───────────────────────────────────────────────────────────────


def _first_pin(new: dict[str, Any], *, space_id: str) -> str:
    """TOFU: the authority key, and the owner key its contact names."""
    contact = new["contact"]
    return _dump(
        {
            "contact": contact,
            "authority_pk": new["authority_pk"],
            "contact_issued_at": contact["issued_at"] if contact else "",
            "owner_pk": contact["identity_pk"].lower() if contact else "",
            "owner_instance": contact["instance_id"] if contact else "",
            "key_epoch": _cert_epoch_for(new, new["authority_pk"], space_id=space_id),
            "gfs": new["gfs"],
            "conflict": False,
        }
    )


def _same_key(old: dict[str, Any], new: dict[str, Any], *, space_id: str) -> str:
    contact = new["contact"]
    if contact is None:
        # The listing carries no contact (an owner that stopped listing one,
        # or a GFS that dropped it): nothing to ask through, pin unchanged.
        return _dump({**old, "contact": None})
    if old["owner_pk"] and contact["identity_pk"].lower() != old["owner_pk"]:
        return _conflict(
            old,
            f"{new['gfs']} lists a contact naming another owner key",
            space_id=space_id,
        )
    mark = _parse(old["contact_issued_at"])
    new_at = contact_issued_at(contact)
    if mark is not None and (new_at is None or new_at < mark):
        # Older than the newest contact this pin ever took (a replayed
        # key-wrap key) — even if the held one was dropped meanwhile.
        return _dump(old)
    return _dump(
        {
            **old,
            "contact": contact,
            "contact_issued_at": contact["issued_at"],
            "owner_pk": old["owner_pk"] or contact["identity_pk"].lower(),
            "owner_instance": old["owner_instance"] or contact["instance_id"],
        }
    )


def _rotation(old: dict[str, Any], new: dict[str, Any], *, space_id: str) -> str | None:
    """The pin after an owner-certified rotation to ``new``'s key, or
    ``None`` when ``new`` carries no such chain from the pinned owner."""
    if not old["owner_pk"] or not old["owner_instance"] or new["cert"] is None:
        return None
    try:
        cert = verify_authority_cert(
            new["cert"],
            space_id=space_id,
            owner_instance_id=old["owner_instance"],
            known_owner_pk_hex=old["owner_pk"],
        )
    except InvalidAuthorityCert, UnsupportedAuthorityCertSuite:
        return None
    if (
        cert.authority_pk_hex != new["authority_pk"]
        or cert.key_epoch <= old["key_epoch"]
    ):
        return None
    contact = new["contact"]
    if contact is not None and contact["identity_pk"].lower() != old["owner_pk"]:
        return None
    mark = _parse(old["contact_issued_at"])
    if contact is not None and mark is not None:
        at = contact_issued_at(contact)
        if at is None or at < mark:
            contact = None  # an older contact rides no rotation back
    log.info(
        "public_space_discovery: space %s rotated its authority key (epoch %d), "
        "certified by its owner",
        space_id,
        cert.key_epoch,
    )
    return _dump(
        {
            **old,
            "contact": contact,
            "contact_issued_at": (
                contact["issued_at"] if contact else old["contact_issued_at"]
            ),
            "authority_pk": new["authority_pk"],
            "key_epoch": cert.key_epoch,
            "gfs": new["gfs"],
        }
    )


def _conflict(old: dict[str, Any], why: str, *, space_id: str) -> str:
    log.warning(
        "public_space_discovery: space %s: %s — asking to join it through the "
        "GFS is refused",
        space_id,
        why,
    )
    return _dump({**old, "contact": None, "conflict": True})


def _cert_epoch_for(new: dict[str, Any], authority_pk: str, *, space_id: str) -> int:
    """The epoch of a listing's cert for its own key at first sight, from
    the owner its contact names; ``0`` without a verifying one."""
    contact = new["contact"]
    if contact is None or new["cert"] is None:
        return 0
    try:
        cert = verify_authority_cert(
            new["cert"],
            space_id=space_id,
            owner_instance_id=contact["instance_id"],
            known_owner_pk_hex=contact["identity_pk"],
        )
    except InvalidAuthorityCert, UnsupportedAuthorityCertSuite:
        return 0
    return cert.key_epoch if cert.authority_pk_hex == authority_pk else 0


def _load_candidate(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        value = orjson.loads(raw)
    except ValueError:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("authority_pk"), str):
        return None
    contact = value.get("contact")
    cert = value.get("cert")
    return {
        "contact": parse_join_contact(contact) if contact is not None else None,
        "authority_pk": value["authority_pk"].lower(),
        "cert": cert if isinstance(cert, dict) else None,
        "gfs": str(value.get("gfs") or ""),
    }


def _contact_holds(
    contact: dict[str, str], *, space_id: str, owner: str, authority_pk: str
) -> bool:
    if contact["instance_id"] != owner:
        return False
    if not verify_join_contact(
        contact, space_id=space_id, space_public_key_hex=authority_pk
    ):
        return False
    try:
        identity = bytes.fromhex(contact["identity_pk"])
        keywrap = bytes.fromhex(contact["keywrap_pk"])
    except ValueError:
        return False
    return verify_keywrap_binding(
        instance_id=contact["instance_id"],
        identity_pub=identity,
        keywrap_pub=keywrap,
        keywrap_sig=contact["keywrap_sig"],
    )


def _parse(raw: str) -> datetime | None:
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else None


def _from_the_future(contact: dict[str, str]) -> bool:
    at = contact_issued_at(contact)
    return at is None or at > datetime.now(UTC) + MAX_FUTURE_SKEW


def _dump(value: dict) -> str:
    return orjson.dumps(value, option=orjson.OPT_SORT_KEYS).decode()


__all__ = ["candidate_from_listing", "load_pin", "merge_pin", "usable_contact"]
