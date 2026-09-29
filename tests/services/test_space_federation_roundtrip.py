# SPDX-License-Identifier: MPL-2.0
"""Guard: every federated ``Space`` field round-trips through the
hand-rolled federation snapshot/rebuild pair.

This closes a recurring bug class. THREE per-space fields have shipped
that *should* federate but silently didn't, because the federation
send (:func:`_space_metadata_for_federation`) and receive
(:func:`stub_space_from_metadata`) are hand-rolled dicts — adding a
``Space`` field doesn't force you to wire it:

* ``delegated_admin_authority`` — dropped; a §D1b joiner never enabled
  delegation and rejected the space signing seed.
* ``roster_sequence`` — dropped; a delegated admin's offline roster
  gossip restarted at 1 and got dropped by the version-guarded CRDT.
* ``config_hlc`` — dropped; concurrent same-sequence edits couldn't
  tie-break deterministically.

The existing ``SpaceFeatures`` round-trip test only covers the nested
``features`` subset, NOT the top-level ``Space`` row fields. This test
is the landmine detector for the *whole* ``Space`` dataclass:

1. An **exhaustiveness guard** partitions every ``Space`` field into
   three documented sets. A newly-added field that's in none of them
   fails CI — the author MUST classify it (and, if federated, wire it).
2. A **round-trip assertion** builds a fully-non-default ``Space``,
   snapshots it, rebuilds the stub, and asserts each federated field
   survives — so a *dropped* field (in a set but not on the wire) also
   fails.
3. A **fail-soft** assertion proves an older sender (meta missing the
   newer keys) still rebuilds with sensible defaults.
"""

from __future__ import annotations

import dataclasses
import json

from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatureAccess,
    SpaceFeatures,
    SpaceType,
)
from socialhome.services.space_service import (
    _space_metadata_for_federation,
    stub_space_from_metadata,
)

# ── The three classifications (the load-bearing contract) ─────────────────
#
# Derived by reading ``_space_metadata_for_federation`` (the meta keys) and
# ``stub_space_from_metadata`` (how each is rebuilt). Verified, not guessed.

#: Fields that travel in ``space_meta`` AND rebuild IDENTICALLY on the stub.
#: A drop here → the round-trip assertion below mismatches → CI fails.
FEDERATED_ROUNDTRIP: frozenset[str] = frozenset(
    {
        # Space @here policy: every member household enforces it locally on
        # the posts it receives, so it must mirror the host's value.
        "allow_here_mention",
        "name",
        "owner_username",
        "identity_public_key",
        "config_sequence",
        "features",
        "space_type",
        "join_mode",
        "roster_sequence",
        "config_hlc",
        "description",
        "emoji",
        "archived",
        "about_markdown",
        "cover_hash",
        "icon_hash",
        "tz",
        "min_age",
        "category",
        # Retention is config, not content: member households mirror it so a
        # remote admin's settings show (and re-save) the host's real values.
        # The host alone enforces it (the sweep skips mirrored spaces) and
        # pins it against an inbound snapshot.
        "retention_days",
        "retention_exempt_types",
    }
)

#: Fields that federate but are intentionally rebuilt DIFFERENTLY on the
#: stub, each with the documented reason + expected-value rule asserted in
#: ``test_federated_transformed_fields``.
FEDERATED_TRANSFORMED: dict[str, str] = {
    # The stub's identity is the ``space_id`` the inbound handler was given
    # (the authenticated event's subject), not anything inside meta.
    "id": "set to the passed space_id, not from meta",
    # SECURITY (§D1b): the owner is the AUTHENTICATED envelope sender
    # (``host_instance_id``), never the issuer-controlled
    # ``meta['owner_instance_id']`` — closes an owner-spoof footgun where a
    # malicious issuer stamps a forged owner on a brand-new stub.
    "owner_instance_id": "set to host_instance_id (authenticated sender)",
}

#: Fields that MUST NOT federate (absent from ``space_meta`` entirely), each
#: with a one-line reason. The stub rebuilds them at their dataclass default.
INTENTIONALLY_LOCAL: dict[str, str] = {
    # Secret-ish join secret minted + checked host-side; never leaves the host.
    "join_code": "host-local join secret; must not leak to members",
    # Geo-gate coordinates are host-local matching state (raw GPS never
    # federates per §2 / §23.8.6).
    "lat": "host-local geo-gate coordinate (raw GPS must not federate)",
    "lon": "host-local geo-gate coordinate (raw GPS must not federate)",
    "radius_km": "host-local geo-gate radius",
    # Host-side automation toggle; runtime/local concern.
    "bot_enabled": "host-local automation toggle",
    # Hard-dissolve is delivered via its own dedicated termination event /
    # archive flow, not the config snapshot (see #582).
    "dissolved": "hard-dissolve rides its own termination event, not meta",
    # The reason string is host-bookkeeping; the receiver derives read-only
    # state from ``archived`` alone.
    "archived_reason": "host bookkeeping; receivers act on ``archived``",
}


def test_every_space_field_is_classified() -> None:
    """Exhaustiveness guard — every ``Space`` field must appear in exactly
    one of the three classification sets.

    THE load-bearing assertion: a newly-added ``Space`` field lands in none
    of the sets, so this fails and forces the author to classify it (and, if
    federated, wire it into ``_space_metadata_for_federation`` +
    ``stub_space_from_metadata`` and add it to ``FEDERATED_ROUNDTRIP``).
    """
    all_fields = {f.name for f in dataclasses.fields(Space)}
    classified = (
        FEDERATED_ROUNDTRIP
        | frozenset(FEDERATED_TRANSFORMED)
        | frozenset(INTENTIONALLY_LOCAL)
    )

    unclassified = all_fields - classified
    assert not unclassified, (
        "Unclassified Space field(s): "
        f"{sorted(unclassified)}. A new Space field MUST be classified as "
        "FEDERATED_ROUNDTRIP (travels in space_meta and rebuilds identically "
        "— also wire it into _space_metadata_for_federation + "
        "stub_space_from_metadata), FEDERATED_TRANSFORMED (federates but is "
        "rebuilt differently, e.g. owner_instance_id), or INTENTIONALLY_LOCAL "
        "(host-only/runtime — must not federate). This is the field-drop "
        "guard: classify it or CI stays red."
    )

    stale = classified - all_fields
    assert not stale, (
        f"Classification names a Space field that no longer exists: "
        f"{sorted(stale)}. Remove it from the classification set."
    )

    # The three sets must be disjoint — a field can't be both local and
    # federated.
    assert FEDERATED_ROUNDTRIP.isdisjoint(FEDERATED_TRANSFORMED), (
        "A field is in both FEDERATED_ROUNDTRIP and FEDERATED_TRANSFORMED."
    )
    assert FEDERATED_ROUNDTRIP.isdisjoint(INTENTIONALLY_LOCAL), (
        "A field is in both FEDERATED_ROUNDTRIP and INTENTIONALLY_LOCAL."
    )
    assert frozenset(FEDERATED_TRANSFORMED).isdisjoint(INTENTIONALLY_LOCAL), (
        "A field is in both FEDERATED_TRANSFORMED and INTENTIONALLY_LOCAL."
    )


def _fully_non_default_space() -> Space:
    """A ``Space`` with EVERY field set to a distinctive non-default value.

    Non-default so a dropped/zeroed field on the wire surfaces as a mismatch
    rather than coincidentally matching the dataclass default.
    """
    features = SpaceFeatures(
        calendar=False,
        todo=False,
        location=True,
        location_mode="zone_only",
        stickies=False,
        pages=False,
        gallery=False,
        bazaar=False,
        posts_access=SpaceFeatureAccess.ADMIN_ONLY,
        pages_access=SpaceFeatureAccess.MODERATED,
        stickies_access=SpaceFeatureAccess.ADMIN_ONLY,
        calendar_access=SpaceFeatureAccess.MODERATED,
        tasks_access=SpaceFeatureAccess.ADMIN_ONLY,
        allow_subscriber_comment=True,
        allow_subscriber_react=True,
        delegated_admin_authority=True,
        allowed_post_types=("image", "video"),
    )
    return Space(
        id="space-xyz",
        name="Distinctive Name",
        owner_instance_id="origin.example",
        owner_username="alice",
        identity_public_key="PUBKEY-AAAA",
        config_sequence=7,
        features=features,
        space_type=SpaceType.PUBLIC,
        join_mode=JoinMode.REQUEST,
        roster_sequence=42,
        config_hlc="12345-7",
        description="A distinctive description",
        emoji="🚀",
        retention_days=30,
        retention_exempt_types=("event",),
        join_code="SECRET-CODE",
        lat=12.3456,
        lon=65.4321,
        radius_km=2.5,
        bot_enabled=True,
        dissolved=True,
        archived=True,
        archived_reason="dissolved",
        allow_here_mention=True,
        about_markdown="# About\nrich text",
        cover_hash="covh123",
        icon_hash="iconh456",
        tz="Europe/Berlin",
        min_age=18,
        category="gaming",
    )


def test_federated_roundtrip_fields_survive() -> None:
    """Every FEDERATED_ROUNDTRIP field rebuilds identically on the stub.

    A field present in the set but dropped from
    ``_space_metadata_for_federation`` (or not rebuilt by
    ``stub_space_from_metadata``) mismatches here — closing the bug class
    that bit ``delegated_admin_authority`` / ``roster_sequence`` /
    ``config_hlc``.
    """
    space = _fully_non_default_space()
    meta = _space_metadata_for_federation(space)
    stub = stub_space_from_metadata(
        space.id, host_instance_id="host.example", meta=meta
    )

    for name in sorted(FEDERATED_ROUNDTRIP):
        assert getattr(stub, name) == getattr(space, name), (
            f"Federated Space field {name!r} did NOT round-trip: "
            f"origin={getattr(space, name)!r} stub={getattr(stub, name)!r}. "
            "Either it was dropped from _space_metadata_for_federation, not "
            "rebuilt in stub_space_from_metadata, or it's misclassified."
        )


def test_federated_transformed_fields() -> None:
    """FEDERATED_TRANSFORMED fields federate but rebuild differently — assert
    the documented transform."""
    space = _fully_non_default_space()
    meta = _space_metadata_for_federation(space)
    stub = stub_space_from_metadata(
        space.id, host_instance_id="host.example", meta=meta
    )

    # ``id`` is the passed space_id, not anything in meta.
    assert stub.id == space.id

    # SECURITY: owner is the authenticated sender, NOT meta's claim. Prove the
    # claim is actively ignored by making them differ.
    assert space.owner_instance_id != "host.example"
    assert meta["owner_instance_id"] == space.owner_instance_id
    assert stub.owner_instance_id == "host.example", (
        "owner_instance_id must be the authenticated host_instance_id, not "
        "the issuer-controlled meta['owner_instance_id'] (owner-spoof guard)."
    )


def test_intentionally_local_fields_do_not_leak() -> None:
    """INTENTIONALLY_LOCAL fields MUST NOT appear in the federation snapshot.

    Their *values* must not show up in ``meta`` (especially the secret
    ``join_code``). The stub rebuilds them at their dataclass defaults.
    """
    space = _fully_non_default_space()
    meta = _space_metadata_for_federation(space)
    meta_json = json.dumps(meta)

    # No INTENTIONALLY_LOCAL field name is a top-level meta key.
    for name in INTENTIONALLY_LOCAL:
        assert name not in meta, (
            f"Host-local field {name!r} leaked into the federation snapshot "
            f"({INTENTIONALLY_LOCAL[name]})."
        )

    # The secret join_code value must not appear anywhere in the serialized
    # snapshot (the canonical "secret must not leak" assertion this guard is
    # modelled on).
    assert space.join_code is not None
    assert space.join_code not in meta_json, (
        "The secret join_code value leaked into the federation snapshot."
    )

    # The stub rebuilds host-local fields at their dataclass defaults — it
    # has no host-local state from the wire.
    stub = stub_space_from_metadata(
        space.id, host_instance_id="host.example", meta=meta
    )
    defaults = Space(
        id="x",
        name="x",
        owner_instance_id="x",
        owner_username="x",
        identity_public_key="x",
        config_sequence=0,
        features=SpaceFeatures(),
        space_type=SpaceType.PRIVATE,
        join_mode=JoinMode.INVITE_ONLY,
    )
    for name in INTENTIONALLY_LOCAL:
        assert getattr(stub, name) == getattr(defaults, name), (
            f"Host-local field {name!r} should rebuild at its dataclass "
            f"default on the stub, got {getattr(stub, name)!r}."
        )


def test_older_sender_meta_fails_soft() -> None:
    """An older sender's meta missing the newer keys still rebuilds with
    sensible defaults — the guard must not force-break back-compat.

    Covers the documented fail-soft defaults: ``config_hlc`` → "0-0",
    ``roster_sequence`` → ``config_sequence`` (its pre-fix anchor),
    ``min_age`` → 0, ``category`` → "general", and the feature toggles
    fall back to the SpaceFeatures defaults.
    """
    older_meta = {
        "name": "Legacy Space",
        "owner_instance_id": "origin.example",
        "owner_username": "bob",
        "identity_public_key": "PUBKEY-OLD",
        "config_sequence": 9,
        "space_type": "private",
        "join_mode": "invite_only",
        # No config_hlc, roster_sequence, features, min_age, category,
        # archived, tz, cover_hash, icon_hash, about_markdown.
    }
    stub = stub_space_from_metadata(
        "legacy-space", host_instance_id="host.example", meta=older_meta
    )

    assert stub.config_hlc == "0-0"
    assert stub.roster_sequence == older_meta["config_sequence"]
    assert stub.min_age == 0
    assert stub.category == "general"
    assert stub.tz == "UTC"
    assert stub.archived is False
    assert stub.features == SpaceFeatures()
    assert stub.retention_days is None
    assert stub.retention_exempt_types == ()
    # Transform still holds for the older shape.
    assert stub.owner_instance_id == "host.example"
    assert stub.id == "legacy-space"
