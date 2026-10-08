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


def test_ours_is_v31_with_routed_origin_signature_capability():
    """v_31 authenticates the mesh-routed origin (v_30 cross-household
    Follower seats, v_29 the
    invite-link bootstrap redeem, v_28 the mesh
    route-stale nack, v_27 the move-out link,
    v_26 the identity_anchor field anchoring user_id derivation, v_25
    per-user identity binding, v_24 admin-authoritative offline config edits,
    v_23 peer-replicated space roster gossip, v_22 the delegated-admin
    signing-seed share, v_21 authenticated mesh route discovery, v_20
    SPACE_SYNC_REJECTED)."""
    assert OURS == 53
    assert FederationCapability.MIN_FOR_FORWARDED_INVITE_LINK == 52
    assert FederationCapability.MIN_FOR_STRICT_MEMBER_PUBLISH == 50
    assert FederationCapability.MIN_FOR_PRIVATE_CHANNELS == 51
    assert FederationCapability.MIN_FOR_MEMBER_GFS_PUBLISH == 49
    assert FederationCapability.MIN_FOR_HOST_SEQUENCED_PAGES == 48
    assert FederationCapability.MIN_FOR_FORWARDED_ROLE_CHANGE == 47
    assert FederationCapability.MIN_FOR_AUTHORITY_EPOCH_ECHO == 46
    assert FederationCapability.MIN_FOR_SPACE_AUTHORITY_ROTATION == 44
    assert FederationCapability.MIN_FOR_FEDERATED_MODERATION == 43
    assert FederationCapability.MIN_FOR_CONTENT_ACCESS_ENFORCEMENT == 42
    assert FederationCapability.MIN_FOR_SPACE_MODERATOR_ROLE == 41
    assert FederationCapability.MIN_FOR_TASK_PRIORITY_LABELS == 40
    assert FederationCapability.MIN_FOR_SPACE_TIMETABLE == 39
    assert FederationCapability.MIN_FOR_MOMENT_NO_RELAY == 38
    assert FederationCapability.MIN_FOR_CROSS_HOUSEHOLD_GROUP_DM == 37
    assert FederationCapability.MIN_FOR_OWNER_BOUND_CONTENT_ID == 36
    assert FederationCapability.MIN_FOR_MOMENT_ORIGIN_SIGNATURE == 35
    assert FederationCapability.MIN_FOR_GALLERY_ALBUM_SYNC == 33
    assert FederationCapability.MIN_FOR_OWNER_BOUND_ALBUM_ID == 34
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
    assert FederationCapability.MIN_FOR_REMOTE_SUBSCRIBER_ROLE == 30
    assert FederationCapability.MIN_FOR_ROUTED_ORIGIN_SIGNATURE == 31
    assert (
        FederationCapability.MIN_FOR_ADMIN_AUTHORITATIVE_OPS,
        "Admins changing settings while the owner is away",
    ) in CAPABILITY_FEATURES
    assert (
        FederationCapability.MIN_FOR_USER_IDENTITY_KEY,
        "Checking each person's identity",
    ) in CAPABILITY_FEATURES
    assert (
        FederationCapability.MIN_FOR_IDENTITY_ANCHOR,
        "Stable identity when a username changes",
    ) in CAPABILITY_FEATURES
    assert (
        FederationCapability.MIN_FOR_USER_MOVE,
        "Moving to another household",
    ) in CAPABILITY_FEATURES
    assert (
        FederationCapability.MIN_FOR_ROUTE_STALE_NACK,
        "Noticing when a path through other households breaks",
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
    assert "Syncing when a direct link fails" not in missing
    # Media DataChannel (v14) is above 13 -> missing.
    assert "Faster photo and video transfer" in missing
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
    assert "Event times in the right time zone" not in missing
    assert "Photos and files in direct messages" not in missing
    assert "Sharing your home location" not in missing
    assert "Apps that work across households" not in missing
    assert "Apps that reach the right person" not in missing


def test_space_features_missing_below_v13():
    """A v13 member household lacks the space features above v13."""
    assert space_features_missing_below(13) == [
        "Faster photo and video transfer",
        "Admin actions from other households",
        "Changes that need several admins to agree",
        "Finding a safe path through other households",
        "Admins running a space without the owner",
        "Member lists shared between households",
        "Admins changing settings while the owner is away",
        "Noticing when a path through other households breaks",
        "Joining by link without being connected",
        "Followers from other households",
        "Checking who sent a passed-on message",
        "Up-to-date member lists for every household",
        "Shared gallery albums",
        "Albums tied to their creator",
        "Posts tied to their creator",
        "Space timetables",
        "Task priority and labels",
        "Space moderators",
        "Admin-only space features",
        "Reviewing posts from other households",
        "Locking out removed admins",
        "Space reports for moderators",
        "Catching up on missed security updates",
        "Role changes from member households",
        "Shared pages without lost edits",
    ]


def test_space_features_missing_below_v16():
    """Above v16 the space-scoped features are authenticated route
    discovery (v_21), delegated admin authority (v_22), and roster
    gossip (v_23)."""
    assert space_features_missing_below(16) == [
        "Finding a safe path through other households",
        "Admins running a space without the owner",
        "Member lists shared between households",
        "Admins changing settings while the owner is away",
        "Noticing when a path through other households breaks",
        "Joining by link without being connected",
        "Followers from other households",
        "Checking who sent a passed-on message",
        "Up-to-date member lists for every household",
        "Shared gallery albums",
        "Albums tied to their creator",
        "Posts tied to their creator",
        "Space timetables",
        "Task priority and labels",
        "Space moderators",
        "Admin-only space features",
        "Reviewing posts from other households",
        "Locking out removed admins",
        "Space reports for moderators",
        "Catching up on missed security updates",
        "Role changes from member households",
        "Shared pages without lost edits",
    ]


def test_space_features_missing_below_v22():
    """A v22 member household still lacks roster gossip (v_23) and admin
    authoritative config (v_24)."""
    assert space_features_missing_below(22) == [
        "Member lists shared between households",
        "Admins changing settings while the owner is away",
        "Noticing when a path through other households breaks",
        "Joining by link without being connected",
        "Followers from other households",
        "Checking who sent a passed-on message",
        "Up-to-date member lists for every household",
        "Shared gallery albums",
        "Albums tied to their creator",
        "Posts tied to their creator",
        "Space timetables",
        "Task priority and labels",
        "Space moderators",
        "Admin-only space features",
        "Reviewing posts from other households",
        "Locking out removed admins",
        "Space reports for moderators",
        "Catching up on missed security updates",
        "Role changes from member households",
        "Shared pages without lost edits",
    ]


def test_space_features_missing_below_v23():
    """A v23 member household still lacks admin authoritative config (v_24)."""
    assert space_features_missing_below(23) == [
        "Admins changing settings while the owner is away",
        "Noticing when a path through other households breaks",
        "Joining by link without being connected",
        "Followers from other households",
        "Checking who sent a passed-on message",
        "Up-to-date member lists for every household",
        "Shared gallery albums",
        "Albums tied to their creator",
        "Posts tied to their creator",
        "Space timetables",
        "Task priority and labels",
        "Space moderators",
        "Admin-only space features",
        "Reviewing posts from other households",
        "Locking out removed admins",
        "Space reports for moderators",
        "Catching up on missed security updates",
        "Role changes from member households",
        "Shared pages without lost edits",
    ]


def test_space_features_missing_below_v24():
    """A v24 member household still lacks the mesh route-stale nack (v_28).
    v_25 / v_26 / v_27 are per-user surfaces, not space-scoped, so they do
    not appear here even though a v24 member lacks them too."""
    assert space_features_missing_below(24) == [
        "Noticing when a path through other households breaks",
        "Joining by link without being connected",
        "Followers from other households",
        "Checking who sent a passed-on message",
        "Up-to-date member lists for every household",
        "Shared gallery albums",
        "Albums tied to their creator",
        "Posts tied to their creator",
        "Space timetables",
        "Task priority and labels",
        "Space moderators",
        "Admin-only space features",
        "Reviewing posts from other households",
        "Locking out removed admins",
        "Space reports for moderators",
        "Catching up on missed security updates",
        "Role changes from member households",
        "Shared pages without lost edits",
    ]
    assert space_features_missing_below(27) == [
        "Noticing when a path through other households breaks",
        "Joining by link without being connected",
        "Followers from other households",
        "Checking who sent a passed-on message",
        "Up-to-date member lists for every household",
        "Shared gallery albums",
        "Albums tied to their creator",
        "Posts tied to their creator",
        "Space timetables",
        "Task priority and labels",
        "Space moderators",
        "Admin-only space features",
        "Reviewing posts from other households",
        "Locking out removed admins",
        "Space reports for moderators",
        "Catching up on missed security updates",
        "Role changes from member households",
        "Shared pages without lost edits",
    ]


def test_space_features_missing_below_v32_is_empty():
    """Above v32 only the v36 content ids, the v39 space timetables and the
    v40 task priority / labels are space-scoped; a v40 member lacks none. A
    v31 member lacks the roster snapshot; a v30 member also the
    authenticated mesh-routed origin; a v29
    one also lacks cross-household Follower seats, a v28 one the
    invite-link bootstrap redeem."""
    assert space_features_missing_below(28) == [
        "Joining by link without being connected",
        "Followers from other households",
        "Checking who sent a passed-on message",
        "Up-to-date member lists for every household",
        "Shared gallery albums",
        "Albums tied to their creator",
        "Posts tied to their creator",
        "Space timetables",
        "Task priority and labels",
        "Space moderators",
        "Admin-only space features",
        "Reviewing posts from other households",
        "Locking out removed admins",
        "Space reports for moderators",
        "Catching up on missed security updates",
        "Role changes from member households",
        "Shared pages without lost edits",
    ]
    assert space_features_missing_below(29) == [
        "Followers from other households",
        "Checking who sent a passed-on message",
        "Up-to-date member lists for every household",
        "Shared gallery albums",
        "Albums tied to their creator",
        "Posts tied to their creator",
        "Space timetables",
        "Task priority and labels",
        "Space moderators",
        "Admin-only space features",
        "Reviewing posts from other households",
        "Locking out removed admins",
        "Space reports for moderators",
        "Catching up on missed security updates",
        "Role changes from member households",
        "Shared pages without lost edits",
    ]
    assert space_features_missing_below(30) == [
        "Checking who sent a passed-on message",
        "Up-to-date member lists for every household",
        "Shared gallery albums",
        "Albums tied to their creator",
        "Posts tied to their creator",
        "Space timetables",
        "Task priority and labels",
        "Space moderators",
        "Admin-only space features",
        "Reviewing posts from other households",
        "Locking out removed admins",
        "Space reports for moderators",
        "Catching up on missed security updates",
        "Role changes from member households",
        "Shared pages without lost edits",
    ]
    assert space_features_missing_below(31) == [
        "Up-to-date member lists for every household",
        "Shared gallery albums",
        "Albums tied to their creator",
        "Posts tied to their creator",
        "Space timetables",
        "Task priority and labels",
        "Space moderators",
        "Admin-only space features",
        "Reviewing posts from other households",
        "Locking out removed admins",
        "Space reports for moderators",
        "Catching up on missed security updates",
        "Role changes from member households",
        "Shared pages without lost edits",
    ]
    assert space_features_missing_below(32) == [
        "Shared gallery albums",
        "Albums tied to their creator",
        "Posts tied to their creator",
        "Space timetables",
        "Task priority and labels",
        "Space moderators",
        "Admin-only space features",
        "Reviewing posts from other households",
        "Locking out removed admins",
        "Space reports for moderators",
        "Catching up on missed security updates",
        "Role changes from member households",
        "Shared pages without lost edits",
    ]
    assert space_features_missing_below(33) == [
        "Albums tied to their creator",
        "Posts tied to their creator",
        "Space timetables",
        "Task priority and labels",
        "Space moderators",
        "Admin-only space features",
        "Reviewing posts from other households",
        "Locking out removed admins",
        "Space reports for moderators",
        "Catching up on missed security updates",
        "Role changes from member households",
        "Shared pages without lost edits",
    ]
    assert space_features_missing_below(34) == [
        "Posts tied to their creator",
        "Space timetables",
        "Task priority and labels",
        "Space moderators",
        "Admin-only space features",
        "Reviewing posts from other households",
        "Locking out removed admins",
        "Space reports for moderators",
        "Catching up on missed security updates",
        "Role changes from member households",
        "Shared pages without lost edits",
    ]
    assert space_features_missing_below(35) == [
        "Posts tied to their creator",
        "Space timetables",
        "Task priority and labels",
        "Space moderators",
        "Admin-only space features",
        "Reviewing posts from other households",
        "Locking out removed admins",
        "Space reports for moderators",
        "Catching up on missed security updates",
        "Role changes from member households",
        "Shared pages without lost edits",
    ]
    assert space_features_missing_below(36) == [
        "Space timetables",
        "Task priority and labels",
        "Space moderators",
        "Admin-only space features",
        "Reviewing posts from other households",
        "Locking out removed admins",
        "Space reports for moderators",
        "Catching up on missed security updates",
        "Role changes from member households",
        "Shared pages without lost edits",
    ]
    assert space_features_missing_below(38) == [
        "Space timetables",
        "Task priority and labels",
        "Space moderators",
        "Admin-only space features",
        "Reviewing posts from other households",
        "Locking out removed admins",
        "Space reports for moderators",
        "Catching up on missed security updates",
        "Role changes from member households",
        "Shared pages without lost edits",
    ]
    assert space_features_missing_below(39) == [
        "Task priority and labels",
        "Space moderators",
        "Admin-only space features",
        "Reviewing posts from other households",
        "Locking out removed admins",
        "Space reports for moderators",
        "Catching up on missed security updates",
        "Role changes from member households",
        "Shared pages without lost edits",
    ]
    assert space_features_missing_below(40) == [
        "Space moderators",
        "Admin-only space features",
        "Reviewing posts from other households",
        "Locking out removed admins",
        "Space reports for moderators",
        "Catching up on missed security updates",
        "Role changes from member households",
        "Shared pages without lost edits",
    ]
    assert space_features_missing_below(41) == [
        "Admin-only space features",
        "Reviewing posts from other households",
        "Locking out removed admins",
        "Space reports for moderators",
        "Catching up on missed security updates",
        "Role changes from member households",
        "Shared pages without lost edits",
    ]
    assert space_features_missing_below(42) == [
        "Reviewing posts from other households",
        "Locking out removed admins",
        "Space reports for moderators",
        "Catching up on missed security updates",
        "Role changes from member households",
        "Shared pages without lost edits",
    ]
    assert space_features_missing_below(43) == [
        "Locking out removed admins",
        "Space reports for moderators",
        "Catching up on missed security updates",
        "Role changes from member households",
        "Shared pages without lost edits",
    ]
    assert space_features_missing_below(44) == [
        "Space reports for moderators",
        "Catching up on missed security updates",
        "Role changes from member households",
        "Shared pages without lost edits",
    ]
    assert space_features_missing_below(45) == [
        "Catching up on missed security updates",
        "Role changes from member households",
        "Shared pages without lost edits",
    ]
    assert space_features_missing_below(46) == [
        "Role changes from member households",
        "Shared pages without lost edits",
    ]


def test_space_scoped_min_versions_are_capability_constants():
    """Every space-scoped threshold is a MIN_FOR_* int — no magic numbers."""
    declared = {
        v
        for k, v in vars(FederationCapability).items()
        if k.startswith("MIN_FOR_") and isinstance(v, int)
    }
    assert SPACE_SCOPED_MIN_VERSIONS <= declared
