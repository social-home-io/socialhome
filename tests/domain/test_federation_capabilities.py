"""Tests for the federation protocol-version constants."""

from __future__ import annotations

import re

from socialhome.domain import federation_capabilities as fc


def test_ours_is_current_version():
    assert fc.OURS == 72


def test_local_community_area_capability_threshold():
    """v_59 — the paired-household directory carries a local community's
    area. Sender-gated (a peer below gets no coordinates at all), not
    space-scoped, labelled."""
    assert fc.FederationCapability.MIN_FOR_LOCAL_COMMUNITY_AREA == 59
    assert fc.FederationCapability.MIN_FOR_LOCAL_COMMUNITY_AREA <= fc.OURS
    assert fc.FederationCapability.MIN_FOR_LOCAL_COMMUNITY_AREA not in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert fc.features_missing_below(58) == [
        "Local communities",
        "Names for a space's followers",
        "Names for new followers right away",
        "Public moments encrypted from the GFS",
        "Photos and videos in public moments",
        "Time-stamped public moments",
        "Choose how long followers keep your moments",
        "Deletes reach every copy of a moment",
        "Reactions reach authors further away",
        "Reactions reach authors you follow through a GFS",
        "Everyone sees the full reaction count",
        "A highlight taken away stays away",
        "Reporters in other households hear their report was reviewed",
        "Games end cleanly when the other household removes the app",
    ]
    assert fc.features_missing_below(59) == [
        "Names for a space's followers",
        "Names for new followers right away",
        "Public moments encrypted from the GFS",
        "Photos and videos in public moments",
        "Time-stamped public moments",
        "Choose how long followers keep your moments",
        "Deletes reach every copy of a moment",
        "Reactions reach authors further away",
        "Reactions reach authors you follow through a GFS",
        "Everyone sees the full reaction count",
        "A highlight taken away stays away",
        "Reporters in other households hear their report was reviewed",
        "Games end cleanly when the other household removes the app",
    ]
    assert fc.features_missing_below(60) == [
        "Names for new followers right away",
        "Public moments encrypted from the GFS",
        "Photos and videos in public moments",
        "Time-stamped public moments",
        "Choose how long followers keep your moments",
        "Deletes reach every copy of a moment",
        "Reactions reach authors further away",
        "Reactions reach authors you follow through a GFS",
        "Everyone sees the full reaction count",
        "A highlight taken away stays away",
        "Reporters in other households hear their report was reviewed",
        "Games end cleanly when the other household removes the app",
    ]
    assert fc.features_missing_below(61) == [
        "Public moments encrypted from the GFS",
        "Photos and videos in public moments",
        "Time-stamped public moments",
        "Choose how long followers keep your moments",
        "Deletes reach every copy of a moment",
        "Reactions reach authors further away",
        "Reactions reach authors you follow through a GFS",
        "Everyone sees the full reaction count",
        "A highlight taken away stays away",
        "Reporters in other households hear their report was reviewed",
        "Games end cleanly when the other household removes the app",
    ]
    assert fc.features_missing_below(62) == [
        "Photos and videos in public moments",
        "Time-stamped public moments",
        "Choose how long followers keep your moments",
        "Deletes reach every copy of a moment",
        "Reactions reach authors further away",
        "Reactions reach authors you follow through a GFS",
        "Everyone sees the full reaction count",
        "A highlight taken away stays away",
        "Reporters in other households hear their report was reviewed",
        "Games end cleanly when the other household removes the app",
    ]
    assert fc.features_missing_below(63) == [
        "Time-stamped public moments",
        "Choose how long followers keep your moments",
        "Deletes reach every copy of a moment",
        "Reactions reach authors further away",
        "Reactions reach authors you follow through a GFS",
        "Everyone sees the full reaction count",
        "A highlight taken away stays away",
        "Reporters in other households hear their report was reviewed",
        "Games end cleanly when the other household removes the app",
    ]
    assert fc.features_missing_below(64) == [
        "Choose how long followers keep your moments",
        "Deletes reach every copy of a moment",
        "Reactions reach authors further away",
        "Reactions reach authors you follow through a GFS",
        "Everyone sees the full reaction count",
        "A highlight taken away stays away",
        "Reporters in other households hear their report was reviewed",
        "Games end cleanly when the other household removes the app",
    ]
    assert fc.features_missing_below(65) == [
        "Deletes reach every copy of a moment",
        "Reactions reach authors further away",
        "Reactions reach authors you follow through a GFS",
        "Everyone sees the full reaction count",
        "A highlight taken away stays away",
        "Reporters in other households hear their report was reviewed",
        "Games end cleanly when the other household removes the app",
    ]
    assert fc.features_missing_below(66) == [
        "Reactions reach authors further away",
        "Reactions reach authors you follow through a GFS",
        "Everyone sees the full reaction count",
        "A highlight taken away stays away",
        "Reporters in other households hear their report was reviewed",
        "Games end cleanly when the other household removes the app",
    ]
    assert fc.features_missing_below(67) == [
        "Reactions reach authors you follow through a GFS",
        "Everyone sees the full reaction count",
        "A highlight taken away stays away",
        "Reporters in other households hear their report was reviewed",
        "Games end cleanly when the other household removes the app",
    ]
    assert fc.features_missing_below(68) == [
        "Everyone sees the full reaction count",
        "A highlight taken away stays away",
        "Reporters in other households hear their report was reviewed",
        "Games end cleanly when the other household removes the app",
    ]
    assert fc.features_missing_below(69) == [
        "A highlight taken away stays away",
        "Reporters in other households hear their report was reviewed",
        "Games end cleanly when the other household removes the app",
    ]
    assert fc.features_missing_below(70) == [
        "Reporters in other households hear their report was reviewed",
        "Games end cleanly when the other household removes the app",
    ]
    assert fc.features_missing_below(71) == [
        "Games end cleanly when the other household removes the app",
    ]
    assert fc.features_missing_below(72) == []
    assert fc.CAPABILITY_FEATURE_KEYS[59] == "local_communities"


def test_member_picture_reset_sync_capability_threshold():
    """v_58 — a §25.6 ``members`` record's ``null`` picture hash is an
    authoritative reset from a v_58 provider. Receiver-side, degraded
    fallback (an older provider's ``null`` is ignored): labelled, not
    space-scoped."""
    assert fc.FederationCapability.MIN_FOR_MEMBER_PICTURE_RESET_SYNC == 58
    assert fc.FederationCapability.MIN_FOR_MEMBER_PICTURE_RESET_SYNC <= fc.OURS
    assert fc.FederationCapability.MIN_FOR_MEMBER_PICTURE_RESET_SYNC not in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert fc.features_missing_below(57) == [
        "Removed profile pictures in space catch-up",
        "Local communities",
        "Names for a space's followers",
        "Names for new followers right away",
        "Public moments encrypted from the GFS",
        "Photos and videos in public moments",
        "Time-stamped public moments",
        "Choose how long followers keep your moments",
        "Deletes reach every copy of a moment",
        "Reactions reach authors further away",
        "Reactions reach authors you follow through a GFS",
        "Everyone sees the full reaction count",
        "A highlight taken away stays away",
        "Reporters in other households hear their report was reviewed",
        "Games end cleanly when the other household removes the app",
    ]
    assert fc.features_missing_below(58) == [
        "Local communities",
        "Names for a space's followers",
        "Names for new followers right away",
        "Public moments encrypted from the GFS",
        "Photos and videos in public moments",
        "Time-stamped public moments",
        "Choose how long followers keep your moments",
        "Deletes reach every copy of a moment",
        "Reactions reach authors further away",
        "Reactions reach authors you follow through a GFS",
        "Everyone sees the full reaction count",
        "A highlight taken away stays away",
        "Reporters in other households hear their report was reviewed",
        "Games end cleanly when the other household removes the app",
    ]
    assert fc.CAPABILITY_FEATURE_KEYS[58] == "synced_picture_resets"


def test_multi_gfs_publish_capability_threshold():
    """v_57 — the writer group key in trusted spaces, so members publish
    anonymously to every GFS on the owner's list. A lagging household keeps
    publishing identified to its own servers: labelled, not space-scoped."""
    assert fc.FederationCapability.MIN_FOR_MULTI_GFS_PUBLISH == 57
    assert fc.FederationCapability.MIN_FOR_MULTI_GFS_PUBLISH <= fc.OURS
    assert fc.FederationCapability.MIN_FOR_MULTI_GFS_PUBLISH not in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert fc.features_missing_below(56) == [
        "Posting to every GFS a space is listed on",
        "Removed profile pictures in space catch-up",
        "Local communities",
        "Names for a space's followers",
        "Names for new followers right away",
        "Public moments encrypted from the GFS",
        "Photos and videos in public moments",
        "Time-stamped public moments",
        "Choose how long followers keep your moments",
        "Deletes reach every copy of a moment",
        "Reactions reach authors further away",
        "Reactions reach authors you follow through a GFS",
        "Everyone sees the full reaction count",
        "A highlight taken away stays away",
        "Reporters in other households hear their report was reviewed",
        "Games end cleanly when the other household removes the app",
    ]
    assert "Posting to every GFS a space is listed on" not in (
        fc.features_missing_below(57)
    )
    assert fc.CAPABILITY_FEATURE_KEYS[57] == "multi_gfs_posting"


def test_sync_series_rows_capability_threshold():
    """v_56 — §25.6 calendar series rows and numbered sync chunks; an older
    requester gets the expanded occurrences. Not a lagging-household feature
    (the fallback keeps it working), so it carries no label."""
    assert fc.FederationCapability.MIN_FOR_SYNC_SERIES_ROWS == 56
    assert fc.FederationCapability.MIN_FOR_SYNC_SERIES_ROWS <= fc.OURS
    assert fc.FederationCapability.MIN_FOR_SYNC_SERIES_ROWS not in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert fc.features_missing_below(55) == [
        "Recurring events in space catch-up",
        "Posting to every GFS a space is listed on",
        "Removed profile pictures in space catch-up",
        "Local communities",
        "Names for a space's followers",
        "Names for new followers right away",
        "Public moments encrypted from the GFS",
        "Photos and videos in public moments",
        "Time-stamped public moments",
        "Choose how long followers keep your moments",
        "Deletes reach every copy of a moment",
        "Reactions reach authors further away",
        "Reactions reach authors you follow through a GFS",
        "Everyone sees the full reaction count",
        "A highlight taken away stays away",
        "Reporters in other households hear their report was reviewed",
        "Games end cleanly when the other household removes the app",
    ]
    assert fc.CAPABILITY_FEATURE_KEYS[56] == "synced_recurring_events"


def test_space_chat_capability_threshold():
    """v_55 — the four ``SPACE_CHAT_*`` events and the ``chat_messages``
    catch-up resource. Space-scoped: a lagging member household misses the
    space's chat."""
    assert fc.FederationCapability.MIN_FOR_SPACE_CHAT == 55
    assert fc.FederationCapability.MIN_FOR_SPACE_CHAT in fc.SPACE_SCOPED_MIN_VERSIONS
    assert fc.features_missing_below(54) == [
        "Chat in spaces",
        "Recurring events in space catch-up",
        "Posting to every GFS a space is listed on",
        "Removed profile pictures in space catch-up",
        "Local communities",
        "Names for a space's followers",
        "Names for new followers right away",
        "Public moments encrypted from the GFS",
        "Photos and videos in public moments",
        "Time-stamped public moments",
        "Choose how long followers keep your moments",
        "Deletes reach every copy of a moment",
        "Reactions reach authors further away",
        "Reactions reach authors you follow through a GFS",
        "Everyone sees the full reaction count",
        "A highlight taken away stays away",
        "Reporters in other households hear their report was reviewed",
        "Games end cleanly when the other household removes the app",
    ]
    assert fc.features_missing_below(55) == [
        "Recurring events in space catch-up",
        "Posting to every GFS a space is listed on",
        "Removed profile pictures in space catch-up",
        "Local communities",
        "Names for a space's followers",
        "Names for new followers right away",
        "Public moments encrypted from the GFS",
        "Photos and videos in public moments",
        "Time-stamped public moments",
        "Choose how long followers keep your moments",
        "Deletes reach every copy of a moment",
        "Reactions reach authors further away",
        "Reactions reach authors you follow through a GFS",
        "Everyone sees the full reaction count",
        "A highlight taken away stays away",
        "Reporters in other households hear their report was reviewed",
        "Games end cleanly when the other household removes the app",
    ]
    assert fc.space_features_missing_below(54) == [
        "Chat in spaces",
        "Names for a space's followers",
    ]
    assert fc.space_feature_keys_missing_below(54) == [
        "space_chat",
        "follower_author_names",
    ]
    assert fc.CAPABILITY_FEATURE_KEYS[55] == "space_chat"


def test_gfs_relay_key_exchange_capability_threshold():
    """v_54 — the key-wrap key rides ``INSTANCE_CAPABILITIES_UPDATED`` so
    a pair made without a GFS reach can turn the fallback on later.
    Per-pair, not space-scoped."""
    assert fc.FederationCapability.MIN_FOR_GFS_RELAY_KEY_EXCHANGE == 54
    assert fc.FederationCapability.MIN_FOR_GFS_RELAY_KEY_EXCHANGE not in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert fc.features_missing_below(53) == [
        "Turning on the GFS fallback for an existing connection",
        "Chat in spaces",
        "Recurring events in space catch-up",
        "Posting to every GFS a space is listed on",
        "Removed profile pictures in space catch-up",
        "Local communities",
        "Names for a space's followers",
        "Names for new followers right away",
        "Public moments encrypted from the GFS",
        "Photos and videos in public moments",
        "Time-stamped public moments",
        "Choose how long followers keep your moments",
        "Deletes reach every copy of a moment",
        "Reactions reach authors further away",
        "Reactions reach authors you follow through a GFS",
        "Everyone sees the full reaction count",
        "A highlight taken away stays away",
        "Reporters in other households hear their report was reviewed",
        "Games end cleanly when the other household removes the app",
    ]
    assert fc.features_missing_below(54) == [
        "Chat in spaces",
        "Recurring events in space catch-up",
        "Posting to every GFS a space is listed on",
        "Removed profile pictures in space catch-up",
        "Local communities",
        "Names for a space's followers",
        "Names for new followers right away",
        "Public moments encrypted from the GFS",
        "Photos and videos in public moments",
        "Time-stamped public moments",
        "Choose how long followers keep your moments",
        "Deletes reach every copy of a moment",
        "Reactions reach authors further away",
        "Reactions reach authors you follow through a GFS",
        "Everyone sees the full reaction count",
        "A highlight taken away stays away",
        "Reporters in other households hear their report was reviewed",
        "Games end cleanly when the other household removes the app",
    ]


def test_gfs_relay_routes_capability_threshold():
    """v_53 — shared-GFS route discovery (``GFS_RELAY_PROBE`` / ``_ACK``).
    Per-pair, not space-scoped: an older peer is simply never probed and
    holds no relay routes."""
    assert fc.FederationCapability.MIN_FOR_GFS_RELAY_ROUTES == 53
    assert fc.FederationCapability.MIN_FOR_GFS_RELAY_ROUTES not in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert fc.features_missing_below(52) == [
        "Reaching a paired household through a shared GFS",
        "Turning on the GFS fallback for an existing connection",
        "Chat in spaces",
        "Recurring events in space catch-up",
        "Posting to every GFS a space is listed on",
        "Removed profile pictures in space catch-up",
        "Local communities",
        "Names for a space's followers",
        "Names for new followers right away",
        "Public moments encrypted from the GFS",
        "Photos and videos in public moments",
        "Time-stamped public moments",
        "Choose how long followers keep your moments",
        "Deletes reach every copy of a moment",
        "Reactions reach authors further away",
        "Reactions reach authors you follow through a GFS",
        "Everyone sees the full reaction count",
        "A highlight taken away stays away",
        "Reporters in other households hear their report was reviewed",
        "Games end cleanly when the other household removes the app",
    ]
    assert fc.features_missing_below(53) == [
        "Turning on the GFS fallback for an existing connection",
        "Chat in spaces",
        "Recurring events in space catch-up",
        "Posting to every GFS a space is listed on",
        "Removed profile pictures in space catch-up",
        "Local communities",
        "Names for a space's followers",
        "Names for new followers right away",
        "Public moments encrypted from the GFS",
        "Photos and videos in public moments",
        "Time-stamped public moments",
        "Choose how long followers keep your moments",
        "Deletes reach every copy of a moment",
        "Reactions reach authors further away",
        "Reactions reach authors you follow through a GFS",
        "Everyone sees the full reaction count",
        "A highlight taken away stays away",
        "Reporters in other households hear their report was reviewed",
        "Games end cleanly when the other household removes the app",
    ]


def test_forwarded_invite_link_capability_threshold():
    """v_52 — a member household's admin mints invite links on the host.
    A host-side feature (only the host must run it), so not space-scoped."""
    assert fc.FederationCapability.MIN_FOR_FORWARDED_INVITE_LINK == 52
    assert fc.FederationCapability.MIN_FOR_FORWARDED_INVITE_LINK not in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert fc.features_missing_below(52) == [
        "Reaching a paired household through a shared GFS",
        "Turning on the GFS fallback for an existing connection",
        "Chat in spaces",
        "Recurring events in space catch-up",
        "Posting to every GFS a space is listed on",
        "Removed profile pictures in space catch-up",
        "Local communities",
        "Names for a space's followers",
        "Names for new followers right away",
        "Public moments encrypted from the GFS",
        "Photos and videos in public moments",
        "Time-stamped public moments",
        "Choose how long followers keep your moments",
        "Deletes reach every copy of a moment",
        "Reactions reach authors further away",
        "Reactions reach authors you follow through a GFS",
        "Everyone sees the full reaction count",
        "A highlight taken away stays away",
        "Reporters in other households hear their report was reviewed",
        "Games end cleanly when the other household removes the app",
    ]


def test_space_authority_rotation_capability_threshold():
    """v_44 — owner-certified rotation of the space authority key
    (``SPACE_AUTHORITY_ROTATED`` + the cert on key shares / space_meta /
    snapshots). Space-scoped: a household below it stays pinned to the
    revoked key, so its roster mirror freezes and the banner says so."""
    assert fc.FederationCapability.MIN_FOR_SPACE_AUTHORITY_ROTATION == 44
    assert fc.FederationCapability.MIN_FOR_SPACE_AUTHORITY_ROTATION <= fc.OURS
    assert fc.FederationCapability.MIN_FOR_SPACE_AUTHORITY_ROTATION in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert fc.space_features_missing_below(43) == [
        "Locking out removed admins",
        "Space reports for moderators",
        "Catching up on missed security updates",
        "Role changes from member households",
        "Shared pages without lost edits",
        "Chat in spaces",
        "Names for a space's followers",
    ]
    assert fc.features_missing_below(45) == [
        "Catching up on missed security updates",
        "Role changes from member households",
        "Shared pages without lost edits",
        "Members can post without the host",
        "Anonymous posting through the GFS",
        "Private spaces through the GFS",
        "Invite links from member households",
        "Reaching a paired household through a shared GFS",
        "Turning on the GFS fallback for an existing connection",
        "Chat in spaces",
        "Recurring events in space catch-up",
        "Posting to every GFS a space is listed on",
        "Removed profile pictures in space catch-up",
        "Local communities",
        "Names for a space's followers",
        "Names for new followers right away",
        "Public moments encrypted from the GFS",
        "Photos and videos in public moments",
        "Time-stamped public moments",
        "Choose how long followers keep your moments",
        "Deletes reach every copy of a moment",
        "Reactions reach authors further away",
        "Reactions reach authors you follow through a GFS",
        "Everyone sees the full reaction count",
        "A highlight taken away stays away",
        "Reporters in other households hear their report was reviewed",
        "Games end cleanly when the other household removes the app",
    ]
    assert fc.features_missing_below(46) == [
        "Role changes from member households",
        "Shared pages without lost edits",
        "Members can post without the host",
        "Anonymous posting through the GFS",
        "Private spaces through the GFS",
        "Invite links from member households",
        "Reaching a paired household through a shared GFS",
        "Turning on the GFS fallback for an existing connection",
        "Chat in spaces",
        "Recurring events in space catch-up",
        "Posting to every GFS a space is listed on",
        "Removed profile pictures in space catch-up",
        "Local communities",
        "Names for a space's followers",
        "Names for new followers right away",
        "Public moments encrypted from the GFS",
        "Photos and videos in public moments",
        "Time-stamped public moments",
        "Choose how long followers keep your moments",
        "Deletes reach every copy of a moment",
        "Reactions reach authors further away",
        "Reactions reach authors you follow through a GFS",
        "Everyone sees the full reaction count",
        "A highlight taken away stays away",
        "Reporters in other households hear their report was reviewed",
        "Games end cleanly when the other household removes the app",
    ]


def test_authority_epoch_echo_capability_threshold():
    """v_46 — members echo their held authority epochs to the owner on
    ``SPACE_SYNC_BEGIN`` and apply ``forgotten_key_epoch``. Space-scoped: a
    v_45 member never reports a rotation the restored owner forgot."""
    assert fc.FederationCapability.MIN_FOR_AUTHORITY_EPOCH_ECHO == 46
    assert fc.FederationCapability.MIN_FOR_AUTHORITY_EPOCH_ECHO <= fc.OURS
    assert fc.FederationCapability.MIN_FOR_AUTHORITY_EPOCH_ECHO in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert fc.space_features_missing_below(45) == [
        "Catching up on missed security updates",
        "Role changes from member households",
        "Shared pages without lost edits",
        "Chat in spaces",
        "Names for a space's followers",
    ]
    assert fc.features_missing_below(46) == [
        "Role changes from member households",
        "Shared pages without lost edits",
        "Members can post without the host",
        "Anonymous posting through the GFS",
        "Private spaces through the GFS",
        "Invite links from member households",
        "Reaching a paired household through a shared GFS",
        "Turning on the GFS fallback for an existing connection",
        "Chat in spaces",
        "Recurring events in space catch-up",
        "Posting to every GFS a space is listed on",
        "Removed profile pictures in space catch-up",
        "Local communities",
        "Names for a space's followers",
        "Names for new followers right away",
        "Public moments encrypted from the GFS",
        "Photos and videos in public moments",
        "Time-stamped public moments",
        "Choose how long followers keep your moments",
        "Deletes reach every copy of a moment",
        "Reactions reach authors further away",
        "Reactions reach authors you follow through a GFS",
        "Everyone sees the full reaction count",
        "A highlight taken away stays away",
        "Reporters in other households hear their report was reviewed",
        "Games end cleanly when the other household removes the app",
    ]


def test_space_report_scope_capability_threshold():
    """v_45 — space-scoped ``SPACE_REPORT`` + ``SPACE_REPORT_DECIDED``,
    sent only to reviewer households at or above it."""
    assert fc.FederationCapability.MIN_FOR_SPACE_REPORT_SCOPE == 45
    assert fc.FederationCapability.MIN_FOR_SPACE_REPORT_SCOPE in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert fc.space_features_missing_below(44) == [
        "Space reports for moderators",
        "Catching up on missed security updates",
        "Role changes from member households",
        "Shared pages without lost edits",
        "Chat in spaces",
        "Names for a space's followers",
    ]


def test_federated_moderation_capability_threshold():
    """v_43 — federated moderation: SPACE_MODERATION_SUBMITTED / _DECIDED
    and the approval block. Space-scoped: a behind member household can't
    hold, review or accept reviewed items, so the banner names the gap."""
    assert fc.FederationCapability.MIN_FOR_FEDERATED_MODERATION == 43
    assert fc.FederationCapability.MIN_FOR_FEDERATED_MODERATION <= fc.OURS
    assert fc.FederationCapability.MIN_FOR_FEDERATED_MODERATION in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert fc.space_features_missing_below(42) == [
        "Reviewing posts from other households",
        "Locking out removed admins",
        "Space reports for moderators",
        "Catching up on missed security updates",
        "Role changes from member households",
        "Shared pages without lost edits",
        "Chat in spaces",
        "Names for a space's followers",
    ]
    assert "Reviewing posts from other households" not in fc.features_missing_below(43)


def test_content_access_enforcement_capability_threshold():
    """v_42 — ADMIN_ONLY feature access enforced on every household, with
    the write's ``actor_user_id`` on the wire. Space-scoped: a behind member
    household neither enforces the level for its users nor names actors."""
    assert fc.FederationCapability.MIN_FOR_CONTENT_ACCESS_ENFORCEMENT == 42
    assert fc.FederationCapability.MIN_FOR_CONTENT_ACCESS_ENFORCEMENT <= fc.OURS
    assert fc.FederationCapability.MIN_FOR_CONTENT_ACCESS_ENFORCEMENT in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert fc.space_features_missing_below(41) == [
        "Admin-only space features",
        "Reviewing posts from other households",
        "Locking out removed admins",
        "Space reports for moderators",
        "Catching up on missed security updates",
        "Role changes from member households",
        "Shared pages without lost edits",
        "Chat in spaces",
        "Names for a space's followers",
    ]
    assert "Admin-only space features" not in fc.features_missing_below(42)


def test_space_moderator_role_capability_threshold():
    """v_41 — the space ``moderator`` seat. Space-scoped: a behind member
    household mirrors a moderator as a plain member, so the banner names
    the gap."""
    assert fc.FederationCapability.MIN_FOR_SPACE_MODERATOR_ROLE == 41
    assert fc.FederationCapability.MIN_FOR_SPACE_MODERATOR_ROLE <= fc.OURS
    assert fc.FederationCapability.MIN_FOR_SPACE_MODERATOR_ROLE in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert fc.space_features_missing_below(40) == [
        "Space moderators",
        "Admin-only space features",
        "Reviewing posts from other households",
        "Locking out removed admins",
        "Space reports for moderators",
        "Catching up on missed security updates",
        "Role changes from member households",
        "Shared pages without lost edits",
        "Chat in spaces",
        "Names for a space's followers",
    ]
    assert "Space moderators" not in fc.features_missing_below(41)


def test_task_priority_labels_capability_threshold():
    """v_40 — task priority + labels and the shared task wire codec.
    Space-scoped: a behind member household's people don't see them."""
    assert fc.FederationCapability.MIN_FOR_TASK_PRIORITY_LABELS == 40
    assert fc.FederationCapability.MIN_FOR_TASK_PRIORITY_LABELS <= fc.OURS
    assert fc.FederationCapability.MIN_FOR_TASK_PRIORITY_LABELS in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert "Task priority and labels" in fc.features_missing_below(39)
    assert fc.space_features_missing_below(39) == [
        "Task priority and labels",
        "Space moderators",
        "Admin-only space features",
        "Reviewing posts from other households",
        "Locking out removed admins",
        "Space reports for moderators",
        "Catching up on missed security updates",
        "Role changes from member households",
        "Shared pages without lost edits",
        "Chat in spaces",
        "Names for a space's followers",
    ]
    assert "Task priority and labels" not in fc.features_missing_below(40)


def test_space_timetable_capability_threshold():
    """v_39 — space timetables. Space-scoped: a behind member household
    is skipped by the outbound, so the space banner names the gap."""
    assert fc.FederationCapability.MIN_FOR_SPACE_TIMETABLE == 39
    assert fc.FederationCapability.MIN_FOR_SPACE_TIMETABLE <= fc.OURS
    assert fc.FederationCapability.MIN_FOR_SPACE_TIMETABLE in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert "Space timetables" in fc.features_missing_below(38)
    assert "Space timetables" in fc.space_features_missing_below(38)
    assert "Space timetables" not in fc.features_missing_below(39)


def test_moment_no_relay_capability_threshold():
    """v_38 — a protected account's moments stay one hop. Not space-scoped."""
    assert fc.FederationCapability.MIN_FOR_MOMENT_NO_RELAY == 38
    assert fc.FederationCapability.MIN_FOR_MOMENT_NO_RELAY <= fc.OURS
    assert fc.FederationCapability.MIN_FOR_MOMENT_NO_RELAY not in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert "Moments that are never passed on" in fc.features_missing_below(37)
    assert "Moments that are never passed on" not in fc.features_missing_below(38)


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
    assert "Posts tied to their creator" in fc.features_missing_below(35)
    assert "Posts tied to their creator" not in fc.features_missing_below(36)
    assert fc.space_features_missing_below(35) == [
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
        "Chat in spaces",
        "Names for a space's followers",
    ]


def test_moment_origin_signature_capability_threshold():
    """v_35 — relayed moments carry their origin household's signature.
    Not space-scoped: moments are household-broadcast, not space content."""
    assert fc.FederationCapability.MIN_FOR_MOMENT_ORIGIN_SIGNATURE == 35
    assert fc.FederationCapability.MIN_FOR_MOMENT_ORIGIN_SIGNATURE <= fc.OURS
    assert fc.FederationCapability.MIN_FOR_MOMENT_ORIGIN_SIGNATURE not in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert "Checked moments from other households" in fc.features_missing_below(34)
    assert "Checked moments from other households" not in fc.features_missing_below(35)
    assert fc.space_features_missing_below(34) == [
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
        "Chat in spaces",
        "Names for a space's followers",
    ]


def test_remote_subscriber_role_capability_threshold():
    """v_30 — a cross-household Follower seat. A sub-v_30 peer's
    ``space_remote_members.role`` CHECK rejects ``'subscriber'`` and takes
    the whole roster event down with it, so the threshold is a real gate,
    not a nicety. Space-scoped: a behind household silently misses a member
    of the space."""
    assert fc.FederationCapability.MIN_FOR_REMOTE_SUBSCRIBER_ROLE == 30
    assert fc.FederationCapability.MIN_FOR_REMOTE_SUBSCRIBER_ROLE <= fc.OURS
    labels = dict(fc.CAPABILITY_FEATURES).values()
    assert "Followers from other households" in labels
    assert "Followers from other households" in fc.features_missing_below(29)
    assert "Followers from other households" not in fc.features_missing_below(30)
    assert fc.FederationCapability.MIN_FOR_REMOTE_SUBSCRIBER_ROLE in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert "Followers from other households" in fc.space_features_missing_below(29)
    assert "Followers from other households" not in fc.space_features_missing_below(30)


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
    assert "Checking who sent a passed-on message" in labels
    assert "Checking who sent a passed-on message" in fc.features_missing_below(30)
    assert "Checking who sent a passed-on message" not in fc.features_missing_below(31)
    assert fc.FederationCapability.MIN_FOR_ROUTED_ORIGIN_SIGNATURE in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert "Checking who sent a passed-on message" in fc.space_features_missing_below(
        30
    )
    assert (
        "Checking who sent a passed-on message"
        not in fc.space_features_missing_below(31)
    )


def test_route_stale_nack_capability_threshold():
    assert fc.FederationCapability.MIN_FOR_ROUTE_STALE_NACK == 28
    assert fc.FederationCapability.MIN_FOR_ROUTE_STALE_NACK <= fc.OURS
    assert fc.FederationCapability.MIN_FOR_INVITE_BOOTSTRAP_REDEEM == 29
    assert fc.FederationCapability.MIN_FOR_INVITE_BOOTSTRAP_REDEEM <= fc.OURS


def test_route_stale_nack_feature_label():
    labels = dict(fc.CAPABILITY_FEATURES).values()
    assert "Noticing when a path through other households breaks" in labels
    assert (
        "Noticing when a path through other households breaks"
        in fc.features_missing_below(27)
    )
    assert (
        "Noticing when a path through other households breaks"
        not in fc.features_missing_below(28)
    )
    # Space-scoped: a behind hop stalls space content for the whole space
    # (same class as authenticated route discovery), so the per-space
    # compatibility banner warns about it.
    assert fc.FederationCapability.MIN_FOR_ROUTE_STALE_NACK in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert (
        "Noticing when a path through other households breaks"
        in fc.space_features_missing_below(27)
    )
    assert (
        "Noticing when a path through other households breaks"
        not in fc.space_features_missing_below(28)
    )


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
    assert "Checking each person's identity" in labels
    assert "Checking each person's identity" in fc.features_missing_below(24)
    assert "Checking each person's identity" not in fc.features_missing_below(25)
    # Per-user surface, not space-scoped — its lag affects only the two
    # parties, so it is NOT in the per-space compatibility banner.
    assert "Checking each person's identity" not in fc.space_features_missing_below(24)


def test_admin_authoritative_ops_capability_threshold():
    assert fc.FederationCapability.MIN_FOR_ADMIN_AUTHORITATIVE_OPS == 24
    assert fc.FederationCapability.MIN_FOR_ADMIN_AUTHORITATIVE_OPS <= fc.OURS


def test_admin_authoritative_ops_feature_label():
    labels = dict(fc.CAPABILITY_FEATURES).values()
    assert "Admins changing settings while the owner is away" in labels
    assert (
        "Admins changing settings while the owner is away"
        in fc.features_missing_below(23)
    )
    assert (
        "Admins changing settings while the owner is away"
        not in fc.features_missing_below(24)
    )
    # Space-scoped: a behind member household won't accept a delegated
    # admin's offline config edit.
    assert (
        "Admins changing settings while the owner is away"
        in fc.space_features_missing_below(23)
    )


def test_space_roster_gossip_capability_threshold():
    assert fc.FederationCapability.MIN_FOR_SPACE_ROSTER_GOSSIP == 23
    assert fc.FederationCapability.MIN_FOR_SPACE_ROSTER_GOSSIP <= fc.OURS


def test_space_roster_gossip_feature_label():
    labels = dict(fc.CAPABILITY_FEATURES).values()
    assert "Member lists shared between households" in labels
    assert "Member lists shared between households" in fc.features_missing_below(22)
    assert "Member lists shared between households" not in fc.features_missing_below(23)
    # Space-scoped: a behind member household won't converge its roster.
    assert "Member lists shared between households" in fc.space_features_missing_below(
        22
    )


def test_space_admin_key_share_capability_threshold():
    assert fc.FederationCapability.MIN_FOR_SPACE_ADMIN_KEY_SHARE == 22
    assert fc.FederationCapability.MIN_FOR_SPACE_ADMIN_KEY_SHARE <= fc.OURS


def test_space_admin_key_share_feature_label():
    labels = dict(fc.CAPABILITY_FEATURES).values()
    assert "Admins running a space without the owner" in labels
    assert "Admins running a space without the owner" in fc.features_missing_below(21)
    assert "Admins running a space without the owner" not in fc.features_missing_below(
        22
    )
    # Space-scoped: a behind admin household can't receive the signing seed.
    assert (
        "Admins running a space without the owner"
        in fc.space_features_missing_below(21)
    )


def test_authenticated_route_discovery_capability_threshold():
    assert fc.FederationCapability.MIN_FOR_AUTHENTICATED_ROUTE_DISCOVERY == 21
    assert fc.FederationCapability.MIN_FOR_AUTHENTICATED_ROUTE_DISCOVERY <= fc.OURS


def test_authenticated_route_discovery_feature_label():
    labels = dict(fc.CAPABILITY_FEATURES).values()
    assert "Finding a safe path through other households" in labels
    assert "Finding a safe path through other households" in fc.features_missing_below(
        20
    )
    assert (
        "Finding a safe path through other households"
        not in fc.features_missing_below(21)
    )
    # Space-scoped: a behind member household is mesh-unreachable.
    assert (
        "Finding a safe path through other households"
        in fc.space_features_missing_below(20)
    )


def test_space_sync_rejected_capability_threshold():
    assert fc.FederationCapability.MIN_FOR_SPACE_SYNC_REJECTED == 20
    assert fc.FederationCapability.MIN_FOR_SPACE_SYNC_REJECTED <= fc.OURS


def test_space_sync_rejected_feature_label():
    assert "Fixing spaces that got out of step" in dict(fc.CAPABILITY_FEATURES).values()
    assert "Fixing spaces that got out of step" in fc.features_missing_below(19)
    assert "Fixing spaces that got out of step" not in fc.features_missing_below(20)


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
    assert "Up-to-date member lists for every household" in fc.features_missing_below(
        31
    )
    assert (
        "Up-to-date member lists for every household"
        not in fc.features_missing_below(32)
    )
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
    assert "Albums tied to their creator" in dict(fc.CAPABILITY_FEATURES).values()
    assert "Albums tied to their creator" in fc.features_missing_below(33)
    assert "Albums tied to their creator" not in fc.features_missing_below(34)


def test_forwarded_role_change_capability_threshold():
    """v_47 — a stub forwards a role change to the host as the
    ``set_member_role`` remote admin action. Space-scoped: a host below it
    drops the unknown action, so the stub refuses with HOST_TOO_OLD."""
    assert fc.FederationCapability.MIN_FOR_FORWARDED_ROLE_CHANGE == 47
    assert fc.FederationCapability.MIN_FOR_FORWARDED_ROLE_CHANGE <= fc.OURS
    assert fc.FederationCapability.MIN_FOR_FORWARDED_ROLE_CHANGE in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert fc.space_features_missing_below(46) == [
        "Role changes from member households",
        "Shared pages without lost edits",
        "Chat in spaces",
        "Names for a space's followers",
    ]
    assert fc.features_missing_below(47) == [
        "Shared pages without lost edits",
        "Members can post without the host",
        "Anonymous posting through the GFS",
        "Private spaces through the GFS",
        "Invite links from member households",
        "Reaching a paired household through a shared GFS",
        "Turning on the GFS fallback for an existing connection",
        "Chat in spaces",
        "Recurring events in space catch-up",
        "Posting to every GFS a space is listed on",
        "Removed profile pictures in space catch-up",
        "Local communities",
        "Names for a space's followers",
        "Names for new followers right away",
        "Public moments encrypted from the GFS",
        "Photos and videos in public moments",
        "Time-stamped public moments",
        "Choose how long followers keep your moments",
        "Deletes reach every copy of a moment",
        "Reactions reach authors further away",
        "Reactions reach authors you follow through a GFS",
        "Everyone sees the full reaction count",
        "A highlight taken away stays away",
        "Reporters in other households hear their report was reviewed",
        "Games end cleanly when the other household removes the app",
    ]


def test_host_sequenced_pages_capability_threshold():
    """v_48 — host-sequenced space pages. Space-scoped: under a host below
    it the whole space's wiki stays last write wins."""
    assert fc.FederationCapability.MIN_FOR_HOST_SEQUENCED_PAGES == 48
    assert fc.FederationCapability.MIN_FOR_HOST_SEQUENCED_PAGES <= fc.OURS
    assert fc.FederationCapability.MIN_FOR_HOST_SEQUENCED_PAGES in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert fc.space_features_missing_below(47) == [
        "Shared pages without lost edits",
        "Chat in spaces",
        "Names for a space's followers",
    ]
    assert fc.features_missing_below(48) == [
        "Members can post without the host",
        "Anonymous posting through the GFS",
        "Private spaces through the GFS",
        "Invite links from member households",
        "Reaching a paired household through a shared GFS",
        "Turning on the GFS fallback for an existing connection",
        "Chat in spaces",
        "Recurring events in space catch-up",
        "Posting to every GFS a space is listed on",
        "Removed profile pictures in space catch-up",
        "Local communities",
        "Names for a space's followers",
        "Names for new followers right away",
        "Public moments encrypted from the GFS",
        "Photos and videos in public moments",
        "Time-stamped public moments",
        "Choose how long followers keep your moments",
        "Deletes reach every copy of a moment",
        "Reactions reach authors further away",
        "Reactions reach authors you follow through a GFS",
        "Everyone sees the full reaction count",
        "A highlight taken away stays away",
        "Reporters in other households hear their report was reviewed",
        "Games end cleanly when the other household removes the app",
    ]


def test_member_gfs_publish_capability_threshold():
    """v_49 — space writer certificates. Not space-scoped: an older member
    household simply keeps the host-signed relay path for its items."""
    assert fc.FederationCapability.MIN_FOR_MEMBER_GFS_PUBLISH == 49
    assert fc.FederationCapability.MIN_FOR_MEMBER_GFS_PUBLISH <= fc.OURS
    assert fc.FederationCapability.MIN_FOR_MEMBER_GFS_PUBLISH not in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert fc.features_missing_below(49) == [
        "Anonymous posting through the GFS",
        "Private spaces through the GFS",
        "Invite links from member households",
        "Reaching a paired household through a shared GFS",
        "Turning on the GFS fallback for an existing connection",
        "Chat in spaces",
        "Recurring events in space catch-up",
        "Posting to every GFS a space is listed on",
        "Removed profile pictures in space catch-up",
        "Local communities",
        "Names for a space's followers",
        "Names for new followers right away",
        "Public moments encrypted from the GFS",
        "Photos and videos in public moments",
        "Time-stamped public moments",
        "Choose how long followers keep your moments",
        "Deletes reach every copy of a moment",
        "Reactions reach authors further away",
        "Reactions reach authors you follow through a GFS",
        "Everyone sees the full reaction count",
        "A highlight taken away stays away",
        "Reporters in other households hear their report was reviewed",
        "Games end cleanly when the other household removes the app",
    ]


def test_strict_member_publish_capability_threshold():
    """v_50 — strict member publish. Not space-scoped: an older member
    household simply gets no writer key and its items take the host path."""
    assert fc.FederationCapability.MIN_FOR_STRICT_MEMBER_PUBLISH == 50
    assert fc.FederationCapability.MIN_FOR_STRICT_MEMBER_PUBLISH < fc.OURS
    assert fc.FederationCapability.MIN_FOR_STRICT_MEMBER_PUBLISH not in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert fc.features_missing_below(50) == [
        "Private spaces through the GFS",
        "Invite links from member households",
        "Reaching a paired household through a shared GFS",
        "Turning on the GFS fallback for an existing connection",
        "Chat in spaces",
        "Recurring events in space catch-up",
        "Posting to every GFS a space is listed on",
        "Removed profile pictures in space catch-up",
        "Local communities",
        "Names for a space's followers",
        "Names for new followers right away",
        "Public moments encrypted from the GFS",
        "Photos and videos in public moments",
        "Time-stamped public moments",
        "Choose how long followers keep your moments",
        "Deletes reach every copy of a moment",
        "Reactions reach authors further away",
        "Reactions reach authors you follow through a GFS",
        "Everyone sees the full reaction count",
        "A highlight taken away stays away",
        "Reporters in other households hear their report was reviewed",
        "Games end cleanly when the other household removes the app",
    ]


def test_private_channels_capability_threshold():
    """v_51 — opaque connection-server channels for private spaces. Not
    space-scoped: an older member household simply gets no channel grant and
    its items keep the host path."""
    assert fc.FederationCapability.MIN_FOR_PRIVATE_CHANNELS == 51
    assert fc.FederationCapability.MIN_FOR_PRIVATE_CHANNELS < fc.OURS
    assert fc.FederationCapability.MIN_FOR_PRIVATE_CHANNELS not in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert fc.features_missing_below(51) == [
        "Invite links from member households",
        "Reaching a paired household through a shared GFS",
        "Turning on the GFS fallback for an existing connection",
        "Chat in spaces",
        "Recurring events in space catch-up",
        "Posting to every GFS a space is listed on",
        "Removed profile pictures in space catch-up",
        "Local communities",
        "Names for a space's followers",
        "Names for new followers right away",
        "Public moments encrypted from the GFS",
        "Photos and videos in public moments",
        "Time-stamped public moments",
        "Choose how long followers keep your moments",
        "Deletes reach every copy of a moment",
        "Reactions reach authors further away",
        "Reactions reach authors you follow through a GFS",
        "Everyone sees the full reaction count",
        "A highlight taken away stays away",
        "Reporters in other households hear their report was reviewed",
        "Games end cleanly when the other household removes the app",
    ]


#: The pinned slug set. These are an API contract (``lacking_feature_keys`` /
#: ``lagging_feature_keys``) and SPA translation keys (``capability.<slug>``):
#: never rename one. A new feature appends a slug here AND in the module.
_PINNED_FEATURE_KEYS = {
    2: "calendar_time_zones",
    3: "photos_in_dms",
    5: "home_location_sharing",
    6: "invite_links_other_households",
    7: "space_rekey_on_leave",
    8: "remote_member_roles",
    9: "remote_member_removal",
    10: "bazaar_listings",
    11: "bazaar_sold_status",
    12: "bazaar_bids",
    13: "sync_https_fallback",
    14: "fast_media_transfer",
    15: "remote_admin_actions",
    16: "admin_proposals",
    17: "cross_household_apps",
    18: "app_user_routing",
    19: "household_resync",
    20: "space_sync_repair",
    21: "safe_route_discovery",
    22: "admin_key_share",
    23: "roster_gossip",
    24: "admin_ops_without_owner",
    25: "user_identity_keys",
    26: "identity_anchor",
    27: "user_move",
    28: "route_break_notice",
    29: "invite_link_bootstrap",
    30: "remote_followers",
    31: "routed_origin_check",
    32: "roster_snapshot",
    33: "gallery_albums",
    34: "album_owner_binding",
    35: "moment_origin_check",
    36: "content_owner_binding",
    37: "cross_household_group_chats",
    38: "moment_no_relay",
    39: "space_timetables",
    40: "task_priority_labels",
    41: "space_moderators",
    42: "admin_only_features",
    43: "federated_moderation",
    44: "removed_admin_lockout",
    45: "space_reports",
    46: "security_update_catch_up",
    47: "forwarded_role_changes",
    48: "shared_pages_no_lost_edits",
    49: "member_publish",
    50: "anonymous_gfs_posting",
    51: "private_gfs_spaces",
    52: "forwarded_invite_links",
    53: "shared_gfs_relay",
    54: "gfs_fallback_later",
    55: "space_chat",
    56: "synced_recurring_events",
    57: "multi_gfs_posting",
    58: "synced_picture_resets",
    59: "local_communities",
    60: "follower_author_names",
    61: "follower_names_on_join",
    62: "encrypted_public_moments",
    63: "public_moment_media",
    64: "public_moment_sent_at",
    65: "follower_moment_retention",
    66: "late_moment_deletes",
    67: "routed_moment_reactions",
    68: "gfs_moment_reactions",
    69: "moment_reaction_counts",
    70: "highlight_audience_change",
    71: "remote_report_answers",
    72: "app_games_end_on_uninstall",
}


def test_every_feature_has_exactly_one_stable_key():
    """Each ``CAPABILITY_FEATURES`` entry carries a slug, and the slugs are
    the pinned set — renaming one breaks the SPA's translations and any
    client matching on it."""
    versions = {ver for ver, _label in fc.CAPABILITY_FEATURES}
    assert set(fc.CAPABILITY_FEATURE_KEYS) == versions
    assert fc.CAPABILITY_FEATURE_KEYS == _PINNED_FEATURE_KEYS


def test_feature_keys_are_unique_snake_case():
    keys = list(fc.CAPABILITY_FEATURE_KEYS.values())
    assert len(keys) == len(set(keys))
    for key in keys:
        assert re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", key), key


def test_feature_keys_missing_below_parallels_the_labels():
    """Same order and length as the English labels, at every version."""
    labels_by_key = {
        fc.CAPABILITY_FEATURE_KEYS[ver]: label for ver, label in fc.CAPABILITY_FEATURES
    }
    for version in range(fc.OURS + 2):
        keys = fc.feature_keys_missing_below(version)
        assert [labels_by_key[k] for k in keys] == fc.features_missing_below(version)
        space_keys = fc.space_feature_keys_missing_below(version)
        assert [labels_by_key[k] for k in space_keys] == (
            fc.space_features_missing_below(version)
        )
    assert fc.feature_keys_missing_below(fc.OURS) == []
    assert fc.feature_keys_missing_below(18)[0] == "household_resync"


def test_space_author_profile_capability_threshold():
    """v_60 — the ``author_profile`` space item for GFS followers. Gated on
    the space's known member households (an older receiver drops the unknown
    item type), so it is space-scoped and labelled."""
    assert fc.FederationCapability.MIN_FOR_SPACE_AUTHOR_PROFILE == 60
    assert fc.FederationCapability.MIN_FOR_SPACE_AUTHOR_PROFILE <= fc.OURS
    assert fc.FederationCapability.MIN_FOR_SPACE_AUTHOR_PROFILE in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert fc.features_missing_below(59) == [
        "Names for a space's followers",
        "Names for new followers right away",
        "Public moments encrypted from the GFS",
        "Photos and videos in public moments",
        "Time-stamped public moments",
        "Choose how long followers keep your moments",
        "Deletes reach every copy of a moment",
        "Reactions reach authors further away",
        "Reactions reach authors you follow through a GFS",
        "Everyone sees the full reaction count",
        "A highlight taken away stays away",
        "Reporters in other households hear their report was reviewed",
        "Games end cleanly when the other household removes the app",
    ]
    assert fc.space_feature_keys_missing_below(59) == ["follower_author_names"]
    assert fc.CAPABILITY_FEATURE_KEYS[60] == "follower_author_names"


def test_follower_profile_bundle_capability_threshold():
    """v_61 — a new GFS follower's targeted author-profile bundle (host →
    follower through the GFS, no household-to-household event). Labelled,
    not space-scoped."""
    assert fc.FederationCapability.MIN_FOR_FOLLOWER_PROFILE_BUNDLE == 61
    assert fc.FederationCapability.MIN_FOR_FOLLOWER_PROFILE_BUNDLE <= fc.OURS
    assert fc.FederationCapability.MIN_FOR_FOLLOWER_PROFILE_BUNDLE not in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert fc.features_missing_below(60) == [
        "Names for new followers right away",
        "Public moments encrypted from the GFS",
        "Photos and videos in public moments",
        "Time-stamped public moments",
        "Choose how long followers keep your moments",
        "Deletes reach every copy of a moment",
        "Reactions reach authors further away",
        "Reactions reach authors you follow through a GFS",
        "Everyone sees the full reaction count",
        "A highlight taken away stays away",
        "Reporters in other households hear their report was reviewed",
        "Games end cleanly when the other household removes the app",
    ]
    assert fc.CAPABILITY_FEATURE_KEYS[61] == "follower_names_on_join"


def test_encrypted_public_moments_capability_threshold():
    """v_62 — public moments encrypted from the GFS (author → GFS →
    follower, no household-to-household event). Labelled, not
    space-scoped."""
    assert fc.FederationCapability.MIN_FOR_ENCRYPTED_PUBLIC_MOMENTS == 62
    assert fc.FederationCapability.MIN_FOR_ENCRYPTED_PUBLIC_MOMENTS <= fc.OURS
    assert fc.FederationCapability.MIN_FOR_ENCRYPTED_PUBLIC_MOMENTS not in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert fc.features_missing_below(61) == [
        "Public moments encrypted from the GFS",
        "Photos and videos in public moments",
        "Time-stamped public moments",
        "Choose how long followers keep your moments",
        "Deletes reach every copy of a moment",
        "Reactions reach authors further away",
        "Reactions reach authors you follow through a GFS",
        "Everyone sees the full reaction count",
        "A highlight taken away stays away",
        "Reporters in other households hear their report was reviewed",
        "Games end cleanly when the other household removes the app",
    ]
    assert fc.CAPABILITY_FEATURE_KEYS[62] == "encrypted_public_moments"


def test_encrypted_moment_media_capability_threshold():
    """v_63 — a public moment's file as an encrypted blob on the GFS
    (author → GFS → follower). Labelled, not space-scoped."""
    assert fc.FederationCapability.MIN_FOR_ENCRYPTED_MOMENT_MEDIA == 63
    assert fc.FederationCapability.MIN_FOR_ENCRYPTED_MOMENT_MEDIA <= fc.OURS
    assert fc.FederationCapability.MIN_FOR_ENCRYPTED_MOMENT_MEDIA not in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert fc.features_missing_below(62) == [
        "Photos and videos in public moments",
        "Time-stamped public moments",
        "Choose how long followers keep your moments",
        "Deletes reach every copy of a moment",
        "Reactions reach authors further away",
        "Reactions reach authors you follow through a GFS",
        "Everyone sees the full reaction count",
        "A highlight taken away stays away",
        "Reporters in other households hear their report was reviewed",
        "Games end cleanly when the other household removes the app",
    ]
    assert fc.CAPABILITY_FEATURE_KEYS[63] == "public_moment_media"


def test_public_moment_sent_at_capability_threshold():
    """v_64 — a public moment's sealed, signed send time (author → GFS →
    follower). Labelled, not space-scoped."""
    assert fc.FederationCapability.MIN_FOR_PUBLIC_MOMENT_SENT_AT == 64
    assert fc.FederationCapability.MIN_FOR_PUBLIC_MOMENT_SENT_AT <= fc.OURS
    assert fc.FederationCapability.MIN_FOR_PUBLIC_MOMENT_SENT_AT not in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert fc.features_missing_below(63)[0] == "Time-stamped public moments"
    assert fc.CAPABILITY_FEATURE_KEYS[64] == "public_moment_sent_at"


def test_long_lived_moments_capability_threshold():
    """v_65 — author-chosen moment retention (1–30 d), clamped by receivers.
    Nothing is gated on it between households; labelled, not space-scoped."""
    assert fc.FederationCapability.MIN_FOR_LONG_LIVED_MOMENTS == 65
    assert fc.FederationCapability.MIN_FOR_LONG_LIVED_MOMENTS <= fc.OURS
    assert fc.FederationCapability.MIN_FOR_LONG_LIVED_MOMENTS not in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert fc.features_missing_below(64) == [
        "Choose how long followers keep your moments",
        "Deletes reach every copy of a moment",
        "Reactions reach authors further away",
        "Reactions reach authors you follow through a GFS",
        "Everyone sees the full reaction count",
        "A highlight taken away stays away",
        "Reporters in other households hear their report was reviewed",
        "Games end cleanly when the other household removes the app",
    ]
    assert fc.CAPABILITY_FEATURE_KEYS[65] == "follower_moment_retention"


def test_late_moment_deletes_capability_threshold():
    """v_66 — a moment's delete reaches every copy, even later: the mesh
    outbox keeps it 30 days, and a GFS that proved ``moment_delete_log``
    keeps its signed certificate 30 days for followers that were away.
    Nothing is gated on it between households; labelled, not space-scoped."""
    assert fc.FederationCapability.MIN_FOR_LATE_MOMENT_DELETES == 66
    assert fc.FederationCapability.MIN_FOR_LATE_MOMENT_DELETES <= fc.OURS
    assert fc.FederationCapability.MIN_FOR_LATE_MOMENT_DELETES not in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert fc.features_missing_below(65)[0] == "Deletes reach every copy of a moment"
    assert fc.CAPABILITY_FEATURE_KEYS[66] == "late_moment_deletes"


def test_routed_moment_reactions_capability_threshold():
    """v_67 — reactions reach authors further away: a reactor sends a
    sealed ``MOMENT_REACTION_ROUTED`` only to a neighbour at this version.
    Labelled, not space-scoped."""
    assert fc.FederationCapability.MIN_FOR_ROUTED_MOMENT_REACTIONS == 67
    assert fc.FederationCapability.MIN_FOR_ROUTED_MOMENT_REACTIONS <= fc.OURS
    assert fc.FederationCapability.MIN_FOR_ROUTED_MOMENT_REACTIONS not in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert fc.features_missing_below(66)[0] == "Reactions reach authors further away"
    assert fc.CAPABILITY_FEATURE_KEYS[67] == "routed_moment_reactions"


def test_gfs_moment_reactions_capability_threshold():
    """v_68 — reactions reach authors followed through a GFS: gated on the
    GFS's signed ``moment_reactions`` capability, never ``peer_supports``.
    Labelled, not space-scoped."""
    assert fc.FederationCapability.MIN_FOR_GFS_MOMENT_REACTIONS == 68
    assert fc.FederationCapability.MIN_FOR_GFS_MOMENT_REACTIONS <= fc.OURS
    assert fc.FederationCapability.MIN_FOR_GFS_MOMENT_REACTIONS not in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert fc.features_missing_below(67)[0] == (
        "Reactions reach authors you follow through a GFS"
    )
    assert fc.CAPABILITY_FEATURE_KEYS[68] == "gfs_moment_reactions"


def test_moment_reaction_counts_capability_threshold():
    """v_69 — everyone sees the full reaction count: the author's household
    sends ``MOMENT_REACTIONS_COUNTED`` (and relays pass it on) only to a peer
    at this version. Labelled, not space-scoped."""
    assert fc.FederationCapability.MIN_FOR_MOMENT_REACTION_COUNTS == 69
    assert fc.FederationCapability.MIN_FOR_MOMENT_REACTION_COUNTS <= fc.OURS
    assert fc.FederationCapability.MIN_FOR_MOMENT_REACTION_COUNTS not in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert fc.features_missing_below(68) == [
        "Everyone sees the full reaction count",
        "A highlight taken away stays away",
        "Reporters in other households hear their report was reviewed",
        "Games end cleanly when the other household removes the app",
    ]
    assert fc.CAPABILITY_FEATURE_KEYS[69] == "moment_reaction_counts"


def test_highlight_audience_change_capability_threshold():
    """v_70 — a highlight taken away stays away: the receiver remembers a
    removal and the newest audience by the author's ``occurred_at``; a
    narrowing waits for every household it takes people from. Labelled, not
    space-scoped."""
    assert fc.FederationCapability.MIN_FOR_HIGHLIGHT_AUDIENCE_CHANGE == 70
    assert fc.FederationCapability.MIN_FOR_HIGHLIGHT_AUDIENCE_CHANGE <= fc.OURS
    assert fc.FederationCapability.MIN_FOR_HIGHLIGHT_AUDIENCE_CHANGE not in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert fc.features_missing_below(69) == [
        "A highlight taken away stays away",
        "Reporters in other households hear their report was reviewed",
        "Games end cleanly when the other household removes the app",
    ]
    assert fc.CAPABILITY_FEATURE_KEYS[70] == "highlight_audience_change"


def test_remote_report_answer_capability_threshold():
    """v_71 — the host tells a reporter's household its space report was
    reviewed (``SPACE_REPORT_ANSWERED``). Labelled; not space-scoped (a
    behind household only keeps its own reporters from hearing back)."""
    assert fc.FederationCapability.MIN_FOR_REMOTE_REPORT_ANSWER == 71
    assert fc.FederationCapability.MIN_FOR_REMOTE_REPORT_ANSWER <= fc.OURS
    assert fc.FederationCapability.MIN_FOR_REMOTE_REPORT_ANSWER not in (
        fc.SPACE_SCOPED_MIN_VERSIONS
    )
    assert fc.features_missing_below(70) == [
        "Reporters in other households hear their report was reviewed",
        "Games end cleanly when the other household removes the app",
    ]
    assert fc.features_missing_below(71) == [
        "Games end cleanly when the other household removes the app",
    ]
    assert fc.features_missing_below(72) == []
    assert fc.CAPABILITY_FEATURE_KEYS[71] == "remote_report_answers"


def test_app_session_ended_capability_threshold():
    """v_72 — an uninstalled app tells opponents their games ended."""
    assert fc.FederationCapability.MIN_FOR_APP_SESSION_ENDED == 72
    assert fc.OURS >= fc.FederationCapability.MIN_FOR_APP_SESSION_ENDED
    assert fc.features_missing_below(71) == [
        "Games end cleanly when the other household removes the app",
    ]
    assert fc.CAPABILITY_FEATURE_KEYS[72] == "app_games_end_on_uninstall"
    assert 72 not in fc.SPACE_SCOPED_MIN_VERSIONS
