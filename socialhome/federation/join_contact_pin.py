"""Pinning a global space's join contact from GFS listings (v_84).

A GFS listing carries the owner's join contact (its keys, signed by the
space authority key — :mod:`socialhome.space_join_contact`) and the space
authority key itself (``identity_public_key``). The GFS serves both, so the
first one a household sees is trusted on first use (TOFU), exactly like a
follower's first pin of the same key. After that:

* a listing with the SAME authority key refreshes the contact (from any GFS:
  it must verify under the pinned key);
* a listing with ANOTHER key is taken only from the GFS that gave the pin
  and only with a higher ``authority_rotation_seq`` — the owner-certified
  rotation (v_44) that GFS re-pinned after checking the owner's cert;
* anything else is a **conflict**: the contact is dropped and stays dropped
  (fail closed) — two GFSs listing the space with different authorities,
  or one that swapped the key. Nothing is sealed to such a space; its
  people ask with an invite link instead.

Pure functions over JSON strings (the ``public_space_cache.join_contact``
column, migration 0143).
"""

from __future__ import annotations

import logging
from typing import Any

import orjson

from ..space_join_contact import parse_join_contact, verify_join_contact
from .keywrap_seal import verify_keywrap_binding

log = logging.getLogger(__name__)


def candidate_from_listing(item: dict, *, gfs_url: str) -> str | None:
    """The pin candidate one directory row offers (JSON), or ``None`` when
    it names no authority key. Its ``contact`` is ``None`` unless it
    verifies under the row's own authority key, names the listed owner and
    binds its key-wrap key to its identity key end to end."""
    space_id = str(item.get("space_id") or "")
    authority_pk = item.get("identity_public_key")
    if not space_id or not isinstance(authority_pk, str) or not authority_pk:
        return None
    seq = item.get("authority_rotation_seq")
    seq = seq if isinstance(seq, int) and not isinstance(seq, bool) else 0
    contact = parse_join_contact(item.get("join_contact"))
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
    return _dump(
        {
            "contact": contact,
            "authority_pk": authority_pk,
            "seq": seq,
            "gfs": gfs_url,
            "conflict": False,
        }
    )


def merge_pin(
    existing: str | None, candidate: str | None, *, space_id: str
) -> str | None:
    """The pin to store after a listing offered *candidate* (see the module
    docstring for the rules)."""
    old = load_pin(existing)
    new = load_pin(candidate)
    if new is None:
        return existing
    if old is None:
        return candidate
    if old.get("conflict"):
        return existing
    if new["authority_pk"] == old["authority_pk"]:
        # Same key: the contact verified under it is fresh, wherever it came
        # from; the pin stays with its GFS.
        return _dump(
            {**old, "contact": new["contact"], "seq": max(old["seq"], new["seq"])}
        )
    if new["gfs"] == old["gfs"] and new["seq"] > old["seq"]:
        return candidate
    log.warning(
        "public_space_discovery: %s lists space %s under another authority key than"
        " the one pinned from %s — asking to join it is refused",
        new["gfs"],
        space_id,
        old["gfs"],
    )
    return _dump({**old, "contact": None, "conflict": True})


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
    return {
        "contact": parse_join_contact(contact) if contact is not None else None,
        "authority_pk": value["authority_pk"],
        "seq": value.get("seq") if isinstance(value.get("seq"), int) else 0,
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


def _dump(value: dict) -> str:
    return orjson.dumps(value, option=orjson.OPT_SORT_KEYS).decode()


__all__ = ["candidate_from_listing", "load_pin", "merge_pin", "usable_contact"]
