"""Tests for socialhome.domain.federation_capabilities feature labelling."""

from __future__ import annotations

from socialhome.domain.federation_capabilities import (
    CAPABILITY_FEATURES,
    OURS,
    SPACE_SCOPED_MIN_VERSIONS,
    FederationCapability,
    features_missing_below,
    space_features_missing_below,
)


def test_ours_is_v29_with_invite_bootstrap_capability():
    """v_29 introduces the invite-link bootstrap redeem (v_28 the mesh
    route-stale nack, v_27 the move-out link,
    v_26 the identity_anchor field anchoring user_id derivation, v_25
    per-user identity binding, v_24 admin-authoritative offline config edits,
    v_23 peer-replicated space roster gossip, v_22 the delegated-admin
    signing-seed share, v_21 authenticated mesh route discovery, v_20
    SPACE_SYNC_REJECTED)."""
    assert OURS == 29
    assert FederationCapability.MIN_FOR_INSTANCE_RESYNC == 19
    assert FederationCapability.MIN_FOR_SPACE_SYNC_REJECTED == 20
    assert FederationCapability.MIN_FOR_AUTHENTICATED_ROUTE_DISCOVERY == 21
    assert FederationCapability.MIN_FOR_SPACE_ADMIN_KEY_SHARE == 22
    assert FederationCapability.MIN_FOR_SPACE_ROSTER_GOSSIP == 23
    assert FederationCapability.MIN_FOR_ADMIN_AUTHORITATIVE_OPS == 24
    assert FederationCapability.MIN_FOR_USER_IDENTITY_KEY == 25
    assert FederationCapability.MIN_FOR_IDENTITY_ANCHOR == 26
    assert FederationCapability.MIN_FOR_USER_MOVE == 27
    assert FederationCapability.MIN_FOR_ROUTE_STALE_NACK == 28
    assert FederationCapability.MIN_FOR_INVITE_BOOTSTRAP_REDEEM == 29
    assert (
        FederationCapability.MIN_FOR_ADMIN_AUTHORITATIVE_OPS,
        "Admin authoritative config offline",
    ) in CAPABILITY_FEATURES
    assert (
        FederationCapability.MIN_FOR_USER_IDENTITY_KEY,
        "Per-user identity binding",
    ) in CAPABILITY_FEATURES
    assert (
        FederationCapability.MIN_FOR_IDENTITY_ANCHOR,
        "UUID identity anchor",
    ) in CAPABILITY_FEATURES
    assert (
        FederationCapability.MIN_FOR_USER_MOVE,
        "User move-out link",
    ) in CAPABILITY_FEATURES
    assert (
        FederationCapability.MIN_FOR_ROUTE_STALE_NACK,
        "Mesh route-stale nack",
    ) in CAPABILITY_FEATURES


def test_features_built_from_min_for_constants():
    """Every CAPABILITY_FEATURES version maps to a MIN_FOR_* constant.

    The labels are a single source of truth derived FROM the constants
    — never a second hardcoded copy of the version numbers.
    """
    declared = {
        v
        for k, v in vars(FederationCapability).items()
        if k.startswith("MIN_FOR_") and isinstance(v, int)
    }
    feature_versions = {ver for ver, _ in CAPABILITY_FEATURES}
    assert feature_versions == declared
    assert len(CAPABILITY_FEATURES) == len(declared)


def test_features_missing_below_ours_is_empty():
    """A peer at OURS lacks nothing."""
    assert features_missing_below(OURS) == []


def test_features_missing_below_v1_lists_everything():
    """A v1 peer lacks every labelled feature."""
    missing = features_missing_below(1)
    assert missing == [label for _, label in sorted(CAPABILITY_FEATURES)]
    assert len(missing) == len(CAPABILITY_FEATURES)


def test_features_missing_below_mid_version():
    """A mid-version peer lacks only features above its version."""
    missing = features_missing_below(13)
    # Sync HTTPS fallback (v13) is supported -> not missing.
    assert "Sync HTTPS fallback" not in missing
    # Media DataChannel (v14) is above 13 -> missing.
    assert "Media DataChannel" in missing
    expected = [label for ver, label in sorted(CAPABILITY_FEATURES) if ver > 13]
    assert missing == expected


def test_space_features_missing_below_ours_is_empty():
    """A member household at OURS lacks no space feature."""
    assert space_features_missing_below(OURS) == []


def test_space_features_missing_below_v1_lists_only_space_scoped():
    """A v1 member household lacks exactly the space-scoped labels."""
    missing = space_features_missing_below(1)
    expected = [
        label
        for ver, label in sorted(CAPABILITY_FEATURES)
        if ver in SPACE_SCOPED_MIN_VERSIONS
    ]
    assert missing == expected
    # Non-space features are excluded even though a v1 peer lacks them too.
    assert "Calendar timezones" not in missing
    assert "DM media" not in missing
    assert "Home-location sharing" not in missing
    assert "App federation channel" not in missing
    assert "App user routing" not in missing


def test_space_features_missing_below_v13():
    """A v13 member household lacks the space features above v13."""
    assert space_features_missing_below(13) == [
        "Media DataChannel",
        "Remote admin actions",
        "Multi-admin approvals",
        "Authenticated mesh route discovery",
        "Space delegated admin authority",
        "Space roster gossip",
        "Admin authoritative config offline",
        "Mesh route-stale nack",
        "Invite-link bootstrap redeem",
    ]


def test_space_features_missing_below_v16():
    """Above v16 the space-scoped features are authenticated route
    discovery (v_21), delegated admin authority (v_22), and roster
    gossip (v_23)."""
    assert space_features_missing_below(16) == [
        "Authenticated mesh route discovery",
        "Space delegated admin authority",
        "Space roster gossip",
        "Admin authoritative config offline",
        "Mesh route-stale nack",
        "Invite-link bootstrap redeem",
    ]


def test_space_features_missing_below_v22():
    """A v22 member household still lacks roster gossip (v_23) and admin
    authoritative config (v_24)."""
    assert space_features_missing_below(22) == [
        "Space roster gossip",
        "Admin authoritative config offline",
        "Mesh route-stale nack",
        "Invite-link bootstrap redeem",
    ]


def test_space_features_missing_below_v23():
    """A v23 member household still lacks admin authoritative config (v_24)."""
    assert space_features_missing_below(23) == [
        "Admin authoritative config offline",
        "Mesh route-stale nack",
        "Invite-link bootstrap redeem",
    ]


def test_space_features_missing_below_v24():
    """A v24 member household still lacks the mesh route-stale nack (v_28).
    v_25 / v_26 / v_27 are per-user surfaces, not space-scoped, so they do
    not appear here even though a v24 member lacks them too."""
    assert space_features_missing_below(24) == [
        "Mesh route-stale nack",
        "Invite-link bootstrap redeem",
    ]
    assert space_features_missing_below(27) == [
        "Mesh route-stale nack",
        "Invite-link bootstrap redeem",
    ]


def test_space_features_missing_below_v29_is_empty():
    """Nothing space-scoped lives above v29 — a v29 member lacks none.
    A v28 member still lacks the invite-link bootstrap redeem."""
    assert space_features_missing_below(28) == ["Invite-link bootstrap redeem"]
    assert space_features_missing_below(29) == []


def test_space_scoped_min_versions_are_capability_constants():
    """Every space-scoped threshold is a MIN_FOR_* int — no magic numbers."""
    declared = {
        v
        for k, v in vars(FederationCapability).items()
        if k.startswith("MIN_FOR_") and isinstance(v, int)
    }
    assert SPACE_SCOPED_MIN_VERSIONS <= declared
