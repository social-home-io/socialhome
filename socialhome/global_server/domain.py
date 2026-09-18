"""GFS domain types — row-shaped dataclasses for the Global Federation Server.

Aligned with spec §24.6. Adds fraud-report, appeal, admin-session, and
cluster-node dataclasses for the full admin portal.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class ClientInstance:
    """A registered household instance."""

    instance_id: str
    display_name: str
    public_key: str  # Ed25519 verify key (hex)
    inbox_url: str
    status: str = "pending"  # 'pending' | 'active' | 'banned'
    auto_accept: bool = False
    connected_at: str = ""  # ISO 8601
    #: Published X25519 *key-wrap* public key (hex), used to seal a payload
    #: (Phase 5b: the per-space content key) to this household when it is not a
    #: paired peer. Empty when an older HFS published none → such a household
    #: can't be sealed-to yet (the handoff degrades gracefully). See
    #: ``federation/keywrap_seal.py``.
    keywrap_public_key: str = ""
    #: KEM suite tag for ``keywrap_public_key`` (e.g. ``"x25519"``). Carried so
    #: the Phase-2 PQ migration (``x25519+mlkem768``) is wire-additive — empty
    #: when none was published.
    kem_suite: str = ""
    #: ``b64url(sign_ed25519(identity_seed, keywrap_public_key))`` — the
    #: household's self-signature over its key-wrap pubkey. Lets a remote sealer
    #: verify the GFS-served key-wrap key is bound to this household's identity
    #: end-to-end (``federation/keywrap_seal.verify_keywrap_binding``) and never
    #: trust a substituted value. Empty when an older HFS published none → that
    #: household can't be sealed-to yet (the handoff degrades gracefully).
    keywrap_sig: str = ""


@dataclass(slots=True, frozen=True)
class GlobalSpace:
    """A global (discoverable) space published to this GFS."""

    space_id: str
    owning_instance: str
    name: str = ""
    description: str | None = None
    about_markdown: str | None = None
    cover_url: str | None = None
    #: Space icon (avatar) — a self-contained ``data:image/webp;base64,…``
    #: URI so the public page renders it on the GFS origin. None → emoji.
    icon_url: str | None = None
    min_age: int = 0
    #: Discovery category (§23.50) — normalizes to ``"general"`` if unknown.
    category: str = "general"
    #: How the owning household lets people in — ``invite_only`` / ``open`` /
    #: ``request``, straight off the owner's signed publish body. An
    #: ``invite_only`` global space is LISTED here for discovery but is NOT
    #: publicly readable: the GFS refuses a subscription for it and relays no
    #: content key. Fail-closed default: a row published before this field
    #: existed reads as ``invite_only`` until the owner's next publish
    #: (which happens automatically on its next GFS-WS connect).
    join_mode: str = "invite_only"
    accent_color: str = "#6366f1"
    primary_color: str = "#6366f1"
    status: str = "pending"  # 'pending' | 'active' | 'banned'
    subscriber_count: int = 0
    posts_per_week: float = 0.0
    published_at: str = ""  # ISO 8601
    #: The space's Ed25519 *authority* verify key (hex), TOFU-pinned on first
    #: publish (Phase 5a). Lets the GFS authorize a relay by a space-authority
    #: signature (any seed-holder — owner or delegated admin) without learning
    #: the space content. Empty when an older HFS published no pubkey → such a
    #: space can only be relayed by its owning instance.
    identity_public_key: str = ""
    #: The OWNER withdrew this listing (``DELETE /gfs/spaces/{id}/unpublish``).
    #: Reversible: the owner's next signed publish clears it. Deliberately
    #: distinct from ``status='banned'``, which is a GFS MODERATOR action and
    #: is sticky against re-publish — conflating the two let an owner
    #: permanently lock itself out of its own listing. Withdrawal hides the
    #: space from DISCOVERY only (listing, detail route, public pages);
    #: existing subscribers and the relay keep working.
    withdrawn: bool = False


@dataclass(slots=True, frozen=True)
class GfsSubscriber:
    """A subscriber row: instance + inbox URL for fan-out delivery."""

    instance_id: str
    inbox_url: str


@dataclass(slots=True, frozen=True)
class GfsSubscriberWithKeys:
    """A subscriber joined with its registered identity + key-wrap keys.

    Returned by the Phase-5b-c reconcile query (``GET /gfs/spaces/{id}/
    subscribers``) so a verified seed-holder can re-seal the per-space content
    key to each subscriber. Carries only already-GFS-registered public material
    (the subscriber's Ed25519 identity pubkey + its X25519 key-wrap pubkey +
    the self-signature binding them) — no inbox URL, no private data.
    ``keywrap_public_key`` / ``keywrap_sig`` are empty for an older subscriber
    that registered no key-wrap key (it can't be sealed-to yet → skipped)."""

    instance_id: str
    identity_public_key: str
    keywrap_public_key: str = ""
    keywrap_sig: str = ""


@dataclass(slots=True, frozen=True)
class GfsFraudReport:
    """A household-admin fraud report against a space or instance."""

    id: str
    target_type: str  # 'space' | 'instance'
    target_id: str
    category: str
    notes: str | None
    reporter_instance_id: str
    reporter_user_id: str | None
    status: str  # 'pending' | 'dismissed' | 'acted'
    created_at: int  # unix epoch
    reviewed_by: str | None = None
    reviewed_at: int | None = None


@dataclass(slots=True, frozen=True)
class GfsAppeal:
    """A banned household's one-shot appeal message."""

    id: str
    target_type: str  # 'space' | 'instance'
    target_id: str
    message: str
    status: str  # 'pending' | 'lifted' | 'dismissed'
    created_at: int
    decided_at: int | None = None
    decided_by: str | None = None


@dataclass(slots=True, frozen=True)
class AdminSession:
    token: str
    expires_at: int
    created_at: int


@dataclass(slots=True, frozen=True)
class ClusterNode:
    """A GFS cluster node (spec §24.10.3)."""

    node_id: str
    url: str
    public_key: str = ""
    status: str = "unknown"  # 'online' | 'offline' | 'syncing' | 'unknown'
    last_seen: str | None = None
    added_at: str = ""
    active_sync_sessions: int = 0

    @property
    def address(self) -> str:
        """Back-compat alias — older callers use ``address`` for ``url``."""
        return self.url


@dataclass(slots=True, frozen=True)
class RtcConnection:
    """Transport mode per client instance (spec §24.12).

    ``transport`` is ``'webrtc'`` when the household's DataChannel is up,
    ``'inbox'`` when falling back to HTTPS push. ``last_ping_at`` is
    bumped by each RTC-ping or HTTPS inbox fallback write so the admin UI
    can show online/offline per peer.
    """

    instance_id: str
    transport: str = "https"
    connected_at: str = ""
    last_ping_at: str = ""


@dataclass(slots=True, frozen=True)
class GfsHighlightPublication:
    """A SH instance's opt-in to share a single highlight via this GFS.

    GFS holds zero highlight bytes — only the routing metadata + a cached
    Ed25519 signature so the publish can be re-verified during audits
    or signalling. The highlight itself streams over WebRTC author →
    viewer once the public landing page bootstraps a peer connection.

    ``expires_at`` is unix epoch and mirrors the author's
    ``highlights.expires_at`` so a publication can never outlive the
    highlight it advertises.
    """

    highlight_id: str
    instance_id: str
    expires_at: int
    published_at: int
    publish_signature: str


@dataclass(slots=True, frozen=True)
class GfsHighlightToken:
    """One revocable share link under a :class:`GfsHighlightPublication`.

    Authors mint multiple tokens per publication — one per platform
    or recipient — so they can revoke a single audience without
    pulling the rest. ``revoked_at`` is ``None`` while active; setting
    it to a unix epoch makes the resolver return ``None`` immediately
    on the next public landing-page hit.
    """

    token: str
    highlight_id: str
    instance_id: str
    label: str | None
    created_at: int
    revoked_at: int | None


@dataclass(slots=True, frozen=True)
class GfsUserRegistration:
    """A user opted in to public-Momentum via this GFS (§Momentum-public).

    Lives only on the GFS. Carries the routing fields the broker
    needs (``instance_id``, ``home_instance_pk``) plus the
    user-supplied directory metadata (``display_name``,
    ``picture_url``) shown by the public ``/moments`` listing.
    """

    user_id: str
    instance_id: str
    username: str
    display_name: str
    picture_url: str | None
    home_instance_pk: str
    registered_at: int
    status: str = "active"  # 'active' | 'suspended'
    bio: str | None = None
    picture_digest: str | None = None


@dataclass(slots=True, frozen=True)
class GfsUserPicture:
    """Avatar bytes mirrored onto the GFS for the public directory.

    Households running public Momentum may sit behind home-network
    NAT, so a public browser can't always hit the home instance's
    ``/api/users/{id}/picture`` endpoint. Mirroring the bytes onto
    the GFS makes the directory standalone-renderable.
    """

    user_id: str
    bytes_: bytes
    mime: str
    digest: str
    updated_at: int


@dataclass(slots=True, frozen=True)
class GfsMomentFollow:
    """A follower instance has subscribed to a registered author.

    Used by the broker's fan-out: when the author's instance pushes
    a ``moment_public`` frame, the broker reads
    ``followers_of(author_user_id)`` and forwards an
    ``incoming_public_moment`` frame over each unique
    ``follower_instance_id``'s persistent WS.
    """

    follower_user_id: str
    follower_instance_id: str
    followed_user_id: str
    created_at: int


# Backwards-compatible aliases for the pre-spec stub names so existing
# tests / imports keep working through the transition.
GfsInstance = ClientInstance
GfsSpace = GlobalSpace
