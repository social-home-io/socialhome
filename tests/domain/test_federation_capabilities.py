"""Tests for the federation protocol-version constants."""

from __future__ import annotations

from socialhome.domain import federation_capabilities as fc


def test_ours_is_current_version():
    assert fc.OURS == 37


def test_cross_household_group_dm_capability_threshold():
    """v_37 — group conversations can span households. Not space-scoped:
    a behind household only keeps its own people out of groups."""
    assert fc.FederationCapability.MIN_FOR_CROSS_HOUSEHOLD_GROUP_DM == 37
    assert fc.FederationCapability.MIN_FOR_CROSS_HOUSEHOLD_GROUP_DM <= fc.OURS
    assert fc.FederationCapability.MIN_FOR_CROSS_HOUSEHOLD_GROUP_DM not in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert "Cross-household group chats" in fc.features_missing_below(36)
    assert "Cross-household group chats" not in fc.features_missing_below(37)


def test_owner_bound_content_id_capability_threshold():
    """v_36 — every federated row with an owner gets a creator-bound id.
    Space-scoped: a behind member household's new rows keep today's
    first-come rules for the whole space."""
    assert fc.FederationCapability.MIN_FOR_OWNER_BOUND_CONTENT_ID == 36
    assert fc.FederationCapability.MIN_FOR_OWNER_BOUND_CONTENT_ID <= fc.OURS
    assert fc.FederationCapability.MIN_FOR_OWNER_BOUND_CONTENT_ID in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert "Creator-bound content ids" in fc.features_missing_below(35)
    assert "Creator-bound content ids" not in fc.features_missing_below(36)
    assert fc.space_features_missing_below(35) == ["Creator-bound content ids"]


def test_moment_origin_signature_capability_threshold():
    """v_35 — relayed moments carry their origin household's signature.
    Not space-scoped: moments are household-broadcast, not space content."""
    assert fc.FederationCapability.MIN_FOR_MOMENT_ORIGIN_SIGNATURE == 35
    assert fc.FederationCapability.MIN_FOR_MOMENT_ORIGIN_SIGNATURE <= fc.OURS
    assert fc.FederationCapability.MIN_FOR_MOMENT_ORIGIN_SIGNATURE not in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert "Signed relayed moments" in fc.features_missing_below(34)
    assert "Signed relayed moments" not in fc.features_missing_below(35)
    assert fc.space_features_missing_below(34) == ["Creator-bound content ids"]


def test_remote_subscriber_role_capability_threshold():
    """v_30 — a cross-household Follower seat. A sub-v_30 peer's
    ``space_remote_members.role`` CHECK rejects ``'subscriber'`` and takes
    the whole roster event down with it, so the threshold is a real gate,
    not a nicety. Space-scoped: a behind household silently misses a member
    of the space."""
    assert fc.FederationCapability.MIN_FOR_REMOTE_SUBSCRIBER_ROLE == 30
    assert fc.FederationCapability.MIN_FOR_REMOTE_SUBSCRIBER_ROLE <= fc.OURS
    labels = dict(fc.CAPABILITY_FEATURES).values()
    assert "Cross-household Follower seats" in labels
    assert "Cross-household Follower seats" in fc.features_missing_below(29)
    assert "Cross-household Follower seats" not in fc.features_missing_below(30)
    assert fc.FederationCapability.MIN_FOR_REMOTE_SUBSCRIBER_ROLE in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert "Cross-household Follower seats" in fc.space_features_missing_below(29)
    assert "Cross-household Follower seats" not in fc.space_features_missing_below(30)


def test_routed_origin_signature_capability_threshold():
    """v_31 — the household at ``path[0]`` of a ``SPACE_ROUTED`` envelope
    signs it, so the endpoint stops taking the relay-supplied origin on
    faith (#692). The threshold is read at the RECEIVER to decide what an
    unsigned inner event means: a forgery from a current peer, or the
    legacy window of a household that has not upgraded yet. Space-scoped:
    the mesh carries space content."""
    assert fc.FederationCapability.MIN_FOR_ROUTED_ORIGIN_SIGNATURE == 31
    assert fc.FederationCapability.MIN_FOR_ROUTED_ORIGIN_SIGNATURE <= fc.OURS
    labels = dict(fc.CAPABILITY_FEATURES).values()
    assert "Authenticated mesh-routed origin" in labels
    assert "Authenticated mesh-routed origin" in fc.features_missing_below(30)
    assert "Authenticated mesh-routed origin" not in fc.features_missing_below(31)
    assert fc.FederationCapability.MIN_FOR_ROUTED_ORIGIN_SIGNATURE in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert "Authenticated mesh-routed origin" in fc.space_features_missing_below(30)
    assert "Authenticated mesh-routed origin" not in fc.space_features_missing_below(31)


def test_route_stale_nack_capability_threshold():
    assert fc.FederationCapability.MIN_FOR_ROUTE_STALE_NACK == 28
    assert fc.FederationCapability.MIN_FOR_ROUTE_STALE_NACK <= fc.OURS
    assert fc.FederationCapability.MIN_FOR_INVITE_BOOTSTRAP_REDEEM == 29
    assert fc.FederationCapability.MIN_FOR_INVITE_BOOTSTRAP_REDEEM <= fc.OURS


def test_route_stale_nack_feature_label():
    labels = dict(fc.CAPABILITY_FEATURES).values()
    assert "Mesh route-stale nack" in labels
    assert "Mesh route-stale nack" in fc.features_missing_below(27)
    assert "Mesh route-stale nack" not in fc.features_missing_below(28)
    # Space-scoped: a behind hop stalls space content for the whole space
    # (same class as authenticated route discovery), so the per-space
    # compatibility banner warns about it.
    assert fc.FederationCapability.MIN_FOR_ROUTE_STALE_NACK in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert "Mesh route-stale nack" in fc.space_features_missing_below(27)
    assert "Mesh route-stale nack" not in fc.space_features_missing_below(28)


def test_ours_is_at_least_27_and_identity_anchor_capability():
    assert fc.OURS >= 27
    assert fc.FederationCapability.MIN_FOR_IDENTITY_ANCHOR == 26


def test_user_move_capability_threshold():
    assert fc.FederationCapability.MIN_FOR_USER_MOVE == 27
    assert fc.FederationCapability.MIN_FOR_USER_MOVE <= fc.OURS


def test_user_identity_key_capability_threshold():
    assert fc.FederationCapability.MIN_FOR_USER_IDENTITY_KEY == 25
    assert fc.FederationCapability.MIN_FOR_USER_IDENTITY_KEY <= fc.OURS


def test_user_identity_key_feature_label():
    labels = dict(fc.CAPABILITY_FEATURES).values()
    assert "Per-user identity binding" in labels
    assert "Per-user identity binding" in fc.features_missing_below(24)
    assert "Per-user identity binding" not in fc.features_missing_below(25)
    # Per-user surface, not space-scoped — its lag affects only the two
    # parties, so it is NOT in the per-space compatibility banner.
    assert "Per-user identity binding" not in fc.space_features_missing_below(24)


def test_admin_authoritative_ops_capability_threshold():
    assert fc.FederationCapability.MIN_FOR_ADMIN_AUTHORITATIVE_OPS == 24
    assert fc.FederationCapability.MIN_FOR_ADMIN_AUTHORITATIVE_OPS <= fc.OURS


def test_admin_authoritative_ops_feature_label():
    labels = dict(fc.CAPABILITY_FEATURES).values()
    assert "Admin authoritative config offline" in labels
    assert "Admin authoritative config offline" in fc.features_missing_below(23)
    assert "Admin authoritative config offline" not in fc.features_missing_below(24)
    # Space-scoped: a behind member household won't accept a delegated
    # admin's offline config edit.
    assert "Admin authoritative config offline" in fc.space_features_missing_below(23)


def test_space_roster_gossip_capability_threshold():
    assert fc.FederationCapability.MIN_FOR_SPACE_ROSTER_GOSSIP == 23
    assert fc.FederationCapability.MIN_FOR_SPACE_ROSTER_GOSSIP <= fc.OURS


def test_space_roster_gossip_feature_label():
    labels = dict(fc.CAPABILITY_FEATURES).values()
    assert "Space roster gossip" in labels
    assert "Space roster gossip" in fc.features_missing_below(22)
    assert "Space roster gossip" not in fc.features_missing_below(23)
    # Space-scoped: a behind member household won't converge its roster.
    assert "Space roster gossip" in fc.space_features_missing_below(22)


def test_space_admin_key_share_capability_threshold():
    assert fc.FederationCapability.MIN_FOR_SPACE_ADMIN_KEY_SHARE == 22
    assert fc.FederationCapability.MIN_FOR_SPACE_ADMIN_KEY_SHARE <= fc.OURS


def test_space_admin_key_share_feature_label():
    labels = dict(fc.CAPABILITY_FEATURES).values()
    assert "Space delegated admin authority" in labels
    assert "Space delegated admin authority" in fc.features_missing_below(21)
    assert "Space delegated admin authority" not in fc.features_missing_below(22)
    # Space-scoped: a behind admin household can't receive the signing seed.
    assert "Space delegated admin authority" in fc.space_features_missing_below(21)


def test_authenticated_route_discovery_capability_threshold():
    assert fc.FederationCapability.MIN_FOR_AUTHENTICATED_ROUTE_DISCOVERY == 21
    assert fc.FederationCapability.MIN_FOR_AUTHENTICATED_ROUTE_DISCOVERY <= fc.OURS


def test_authenticated_route_discovery_feature_label():
    labels = dict(fc.CAPABILITY_FEATURES).values()
    assert "Authenticated mesh route discovery" in labels
    assert "Authenticated mesh route discovery" in fc.features_missing_below(20)
    assert "Authenticated mesh route discovery" not in fc.features_missing_below(21)
    # Space-scoped: a behind member household is mesh-unreachable.
    assert "Authenticated mesh route discovery" in fc.space_features_missing_below(20)


def test_space_sync_rejected_capability_threshold():
    assert fc.FederationCapability.MIN_FOR_SPACE_SYNC_REJECTED == 20
    assert fc.FederationCapability.MIN_FOR_SPACE_SYNC_REJECTED <= fc.OURS


def test_space_sync_rejected_feature_label():
    assert "Space sync reject reconcile" in dict(fc.CAPABILITY_FEATURES).values()
    assert "Space sync reject reconcile" in fc.features_missing_below(19)
    assert "Space sync reject reconcile" not in fc.features_missing_below(20)


def test_instance_resync_capability_threshold():
    assert fc.FederationCapability.MIN_FOR_INSTANCE_RESYNC == 19
    assert fc.FederationCapability.MIN_FOR_INSTANCE_RESYNC <= fc.OURS


def test_remote_admin_action_capability_threshold():
    assert fc.FederationCapability.MIN_FOR_REMOTE_ADMIN_ACTION == 15
    assert fc.FederationCapability.MIN_FOR_REMOTE_ADMIN_ACTION <= fc.OURS


def test_admin_proposals_capability_threshold():
    assert fc.FederationCapability.MIN_FOR_ADMIN_PROPOSALS == 16
    assert fc.FederationCapability.MIN_FOR_ADMIN_PROPOSALS <= fc.OURS


def test_app_channel_capability_threshold():
    assert fc.FederationCapability.MIN_FOR_APP_CHANNEL == 17
    assert fc.FederationCapability.MIN_FOR_APP_CHANNEL <= fc.OURS


def test_ours_is_at_least_18_and_app_user_routing_constant():
    from socialhome.domain.federation_capabilities import OURS, FederationCapability

    assert OURS >= 18
    assert FederationCapability.MIN_FOR_APP_USER_ROUTING == 18


def test_media_channel_capability_threshold():
    assert fc.FederationCapability.MIN_FOR_MEDIA_CHANNEL == 14
    # Gating constants must never exceed what this build advertises, or
    # a sender would gate a feature on a version no peer can reach.
    assert fc.FederationCapability.MIN_FOR_MEDIA_CHANNEL <= fc.OURS


def test_named_thresholds_are_monotonic_and_bounded():
    """Every named capability threshold is a positive int ≤ OURS."""
    named = {
        k: v
        for k, v in vars(fc.FederationCapability).items()
        if k.startswith("MIN_FOR_")
    }
    assert named  # sanity: there are named thresholds
    for name, version in named.items():
        assert isinstance(version, int), name
        assert 1 <= version <= fc.OURS, f"{name}={version} out of range"


def test_roster_snapshot_capability_threshold():
    """v_32 — the host's whole roster in one event, so a member household's
    roster mirror (which every content author is now bound to) heals after
    missed gossip. Space-scoped: a household without it may attribute
    content wrongly for the whole space."""
    assert fc.FederationCapability.MIN_FOR_ROSTER_SNAPSHOT == 32
    assert fc.FederationCapability.MIN_FOR_ROSTER_SNAPSHOT <= fc.OURS
    assert "Space roster snapshot" in fc.features_missing_below(31)
    assert "Space roster snapshot" not in fc.features_missing_below(32)
    assert fc.FederationCapability.MIN_FOR_ROSTER_SNAPSHOT in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )


def test_gallery_album_sync_capability_threshold():
    """v_33 — the gallery-album lifecycle events. Space-scoped: a behind
    member household never sees an album made after it joined, nor what is
    uploaded into it, so the per-space banner names the gap."""
    assert fc.FederationCapability.MIN_FOR_GALLERY_ALBUM_SYNC == 33
    assert fc.FederationCapability.MIN_FOR_GALLERY_ALBUM_SYNC <= fc.OURS
    assert fc.FederationCapability.MIN_FOR_GALLERY_ALBUM_SYNC in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert "Shared gallery albums" in dict(fc.CAPABILITY_FEATURES).values()


def test_owner_bound_album_id_capability_threshold():
    """v_34 — owner-bound album ids. Space-scoped: a behind member
    household's new albums keep the first-come rule for the whole space."""
    assert fc.FederationCapability.MIN_FOR_OWNER_BOUND_ALBUM_ID == 34
    assert fc.FederationCapability.MIN_FOR_OWNER_BOUND_ALBUM_ID <= fc.OURS
    assert fc.FederationCapability.MIN_FOR_OWNER_BOUND_ALBUM_ID in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert "Creator-bound album ids" in dict(fc.CAPABILITY_FEATURES).values()
    assert "Creator-bound album ids" in fc.features_missing_below(33)
    assert "Creator-bound album ids" not in fc.features_missing_below(34)
