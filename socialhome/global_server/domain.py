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
    #: ``request``, straight off the owner's signed publish body. Purely a
    #: MEMBERSHIP gate, surfaced on the directory listing; it says nothing
    #: about readability (see ``allow_subscribers``). Fail-closed default: a
    #: row published before this field existed reads as ``invite_only`` until
    #: the owner's next publish (which happens automatically on its next
    #: GFS-WS connect).
    join_mode: str = "invite_only"
    #: The owner's readability opt-in, straight off the signed publish body.
    #: When False this space is LISTED here for discovery but is NOT publicly
    #: readable: the GFS refuses ``POST /gfs/subscribe`` for it, purges any
    #: seats it already had, and the owner relays neither content nor content
    #: key. Independent of ``join_mode`` — an ``invite_only`` space with this
    #: ON is a broadcast space. Fail-closed default: a row published before
    #: this field existed reads as False until the owner's next publish.
    allow_subscribers: bool = False
    accent_color: str = "#6366f1"
    primary_color: str = "#6366f1"
    status: str = "pending"  # 'pending' | 'active' | 'banned'
    subscriber_count: int = 0
    posts_per_week: float = 0.0
    published_at: str = ""  # ISO 8601
    #: The space's Ed25519 *authority* verify key (hex), TOFU-pinned on first
    #: publish (Phase 5a); moved afterwards only by an owner-signed
    #: ``authority_cert`` with a higher epoch (v_44). Lets the GFS authorize a relay by a space-authority
    #: signature (any seed-holder — owner or delegated admin) without learning
    #: the space content. Empty when an older HFS published no pubkey → such a
    #: space can only be relayed by its owning instance.
    identity_public_key: str = ""
    #: The owner's cert for ``identity_public_key`` (v_44,
    #: :mod:`socialhome.authority_cert`) — ``None`` while the space still uses
    #: its creation-time key (epoch 0). Stored only after verifying it against
    #: the owner's registered key; served on ``GET /gfs/spaces/{id}`` so a
    #: subscriber can heal its pin. Names no member and no reason.
    authority_cert: dict | None = None
    #: This server's own count of accepted re-pins (migration 0013): +1 each
    #: time an owner cert moved the pin. The public directory serves THIS,
    #: never the cert's wall-clock-based epoch, so followers can order pins
    #: without learning when the owner revoked an admin.
    authority_rotation_seq: int = 0
    #: The OWNER withdrew this listing (``DELETE /gfs/spaces/{id}/unpublish``).
    #: Reversible: the owner's next signed publish clears it. Deliberately
    #: distinct from ``status='banned'``, which is a GFS MODERATOR action and
    #: is sticky against re-publish — conflating the two let an owner
    #: permanently lock itself out of its own listing. Withdrawal hides the
    #: space from DISCOVERY only (listing, detail route, public pages);
    #: existing subscribers and the relay keep working.
    withdrawn: bool = False
    #: The owner's ``gfs_publish_mode`` (v_50, GFS migration 0015), learned
    #: only from its household-signed epoch notice and never written by a
    #: publish or cluster upsert. Served on the public directory so a
    #: household reads it (over its cookie-less session) before it would ever
    #: send an identified member publish — and never sends one into a space
    #: listed as ``strict``.
    member_publish_mode: str = "trusted"


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
    #: Legacy / display only — NOT a trust anchor. Older builds (and
    #: old-version nodes sharing the DB during a rolling upgrade) write it
    #: by trust-on-first-use; membership never reads it.
    public_key: str = ""
    status: str = "unknown"  # 'online' | 'offline' | 'syncing' | 'unknown'
    last_seen: str | None = None
    added_at: str = ""
    active_sync_sessions: int = 0
    #: The key an operator approved (``POST /admin/api/cluster/peers``) —
    #: written only by admin add-peer; ``""`` = none (the node is a member
    #: only if it holds our own identity key).
    approved_key: str = ""
    #: The URL the operator approved with that key — written only by admin
    #: add-peer, together with ``approved_key``. Outbound traffic to an
    #: approved node goes here, never to ``url``: an old-version node
    #: sharing the DB rewrites ``url`` on any HELLO it TOFU-verifies.
    approved_url: str = ""

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


@dataclass(slots=True, frozen=True)
class GfsQueuedEnvelope:
    """One sealed household-to-household envelope waiting for its recipient.

    Written by ``POST /gfs/envelope`` when the addressed household has no
    live ``/gfs/ws`` socket, drained in insertion order on its next
    authenticated hello. ``sealed`` is the verbatim
    ``{kem_suite, eph_pk, ciphertext}`` dict the sender produced — opaque to
    this server, never parsed and never logged. There is deliberately no
    sender field: the routing envelope names only the recipient.
    """

    id: int
    to_instance: str
    sealed: dict
    created_at: int
    expires_at: int
    #: ``"envelope"`` (a §D2b sealed blob, pushed as ``{type: "envelope",
    #: sealed}``) or ``"relay"`` (a member-published space item, migration
    #: 0014 — ``sealed`` then holds the identity-free fan-out frame, pushed as
    #: ``{type: "relay", **frame}``). Opaque either way: never parsed for
    #: content, never logged.
    frame_type: str = "envelope"


#: How far an OWNER epoch notice may raise a space's confirmed content epoch
#: beyond the stored one — unless it stays within
#: :data:`MAX_EPOCH_CLOCK_LEAD_S` of wall-clock now, which is how far a v_44
#: post-restore rotation (epoch lifted to unix seconds) may legitimately jump.
MAX_EPOCH_STEP: int = 1000

#: How far ahead of wall-clock now (seconds) an owner may ever set the epoch.
MAX_EPOCH_CLOCK_LEAD_S: int = 24 * 60 * 60

#: Minimum spacing (seconds) between two raises of ``current`` that do NOT
#: come from the owner (a delegated admin's notice, an authorized host relay).
MIN_EPOCH_STEP_INTERVAL_S: int = 60


def epoch_ceiling(current: int | None, now: int) -> int:
    """The highest content epoch an OWNER notice may confirm.

    ``max(current + MAX_EPOCH_STEP, now + MAX_EPOCH_CLOCK_LEAD_S)`` — the
    restored owner's wall-clock epoch still lands, an absurd value does not.
    Nobody but the owner raises by more than +1 (see :class:`GfsSpaceEpoch`)."""
    base = current if current is not None else 0
    return max(base + MAX_EPOCH_STEP, now + MAX_EPOCH_CLOCK_LEAD_S)


@dataclass(slots=True, frozen=True)
class GfsSpaceEpoch:
    """What this server knows about a space's content epoch (v_49).

    Two tiers, because the GFS can tell the space OWNER apart (its registered
    household key) but cannot tell a legitimate delegated admin from a
    demoted one whose seed still matches until the re-pin:

    * ``confirmed`` — the epoch the OWNER last announced (owner-signed
      notice). Only the owner moves it, by any amount up to
      :func:`epoch_ceiling`; ``previous`` is the confirmed epoch before it
      and ``confirmed_at`` when it was confirmed.
    * ``current`` — the newest epoch seen at all (``>= confirmed``). Seed-only
      statements (a delegated admin's notice, an authorized host relay) raise
      it by exactly +1, at most once per :data:`MIN_EPOCH_STEP_INTERVAL_S`
      (``raised_at``); writer certs never raise anything.

    Kept off :class:`GlobalSpace` so the public directory never shows it,
    and reset whenever the space authority key is re-pinned.
    """

    space_id: str
    current: int
    confirmed: int
    previous: int | None
    confirmed_at: int
    raised_at: int

    def admits(self, epoch: int, *, now: int, grace_s: int) -> bool:
        """Whether a cert for ``epoch`` is fresh enough to relay.

        * never beyond ``current + 1`` (one rotation may be in flight);
        * anything from ``confirmed`` up is admitted — seed-only raises move
          ``current`` but never the floor, so no seed holder can strand the
          writers on the owner's real epoch (a cert can't either: certs never
          raise);
        * an epoch below ``confirmed`` is admitted back to ``previous`` only
          for ``grace_s`` after the owner confirmed the newer one — that is
          what retires a writer the owner's rotation removed.

        Receivers apply their own (exact) freshness rule on top.
        """
        if epoch > self.current + 1:
            return False
        if epoch >= self.confirmed:
            return True
        if self.previous is None or epoch < self.previous:
            return False
        return now - self.confirmed_at <= grace_s

    def may_step(self, epoch: int, *, now: int) -> bool:
        """Whether a seed-only statement may raise ``current`` to ``epoch``:
        exactly +1, and not within :data:`MIN_EPOCH_STEP_INTERVAL_S` of the
        last raise."""
        return (
            epoch == self.current + 1
            and now - self.raised_at >= MIN_EPOCH_STEP_INTERVAL_S
        )


@dataclass(slots=True, frozen=True)
class GfsSpaceStrictState:
    """What this server knows for strict-mode member publish (v_50,
    migration 0015): the owner's ``publish_mode`` and the pinned writer group
    keys of the newest and the previous content epoch.

    Kept off :class:`GlobalSpace` so the public directory never shows it. The
    keys are cleared on an authority re-pin; the mode (the owner's household-
    signed statement) is not.
    """

    space_id: str
    publish_mode: str = "trusted"
    mode_at: int | None = None
    writer_key_epoch: int | None = None
    writer_key_pk: str | None = None
    writer_key_prev_epoch: int | None = None
    writer_key_prev_pk: str | None = None

    @property
    def strict(self) -> bool:
        return self.publish_mode == "strict"

    def writer_pk_for(self, epoch: int) -> str | None:
        """The pinned writer public key (b64url) for ``epoch``, or ``None``.
        Only the newest and the previous pin are kept; freshness (the grace
        on the previous epoch) is :meth:`GfsSpaceEpoch.admits`'s job."""
        if self.writer_key_epoch is not None and epoch == self.writer_key_epoch:
            return self.writer_key_pk
        if (
            self.writer_key_prev_epoch is not None
            and epoch == self.writer_key_prev_epoch
        ):
            return self.writer_key_prev_pk
        return None


#: How long a channel's previous content epoch stays open after the newer
#: one was announced (seconds) — the receivers' and the space path's grace.
CHANNEL_EPOCH_GRACE_S: int = 600

#: A channel epoch may rise by at most one per this many seconds since the
#: last raise (a rotation that came faster lands on the retry): nobody can be
#: told apart from a legitimate seed holder here, so inflation is bounded by
#: time, not by identity.
CHANNEL_EPOCH_STEP_INTERVAL_S: int = 60

#: A channel with no registration, notice or publish for this long is
#: dropped by the maintenance sweep (its households re-register on their
#: next connect if they still use it).
CHANNEL_IDLE_TTL_SECONDS: int = 30 * 24 * 60 * 60

#: A channel that never received an epoch notice or a seat is dropped after
#: this long (a registration nobody uses costs a row for a day at most).
CHANNEL_UNUSED_TTL_SECONDS: int = 24 * 60 * 60


@dataclass(slots=True, frozen=True)
class GfsChannel:
    """One opaque channel (v_51, migration 0016) — a PRIVATE space's member
    relay, keyed by a random id. Nothing here names a space, a space key or
    an owner household.

    The content epoch has ONE tier (unlike :class:`GfsSpaceEpoch`): this
    server cannot tell an owner from a delegated admin without learning who
    owns the channel, so every channel-key-signed notice is equal and
    inflation is bounded by time instead — :meth:`step_allowance`."""

    channel_id: str
    channel_suite: str
    channel_pk: str
    registered_at: int
    last_active_at: int
    epoch: int | None = None
    epoch_prev: int | None = None
    epoch_raised_at: int | None = None
    publish_mode: str = "trusted"
    publish_mode_at: int | None = None
    writer_key_epoch: int | None = None
    writer_key_pk: str | None = None
    writer_key_prev_epoch: int | None = None
    writer_key_prev_pk: str | None = None

    @property
    def strict(self) -> bool:
        return self.publish_mode == "strict"

    def admits(
        self, epoch: int, *, now: int, grace_s: int = CHANNEL_EPOCH_GRACE_S
    ) -> bool:
        """Whether a cert / pass / anonymous publish at ``epoch`` is open
        here: the current epoch, the next one (a rotation whose notice is
        still on its retry), or the previous one for ``grace_s`` after the
        raise. Nothing before the first notice (fail closed)."""
        if self.epoch is None:
            return False
        if epoch in (self.epoch, self.epoch + 1):
            return True
        return (
            self.epoch_prev is not None
            and epoch == self.epoch_prev
            and now - (self.epoch_raised_at or 0) <= grace_s
        )

    def step_allowance(self, epoch: int, *, now: int) -> int:
        """Seconds until a notice for ``epoch`` may land: ``0`` = now. The
        first notice after a registration sets any epoch (bounded
        by the caller); after that the epoch
        rises by at most one per :data:`CHANNEL_EPOCH_STEP_INTERVAL_S` since
        the last raise. ``-1`` = never (not ahead of the current epoch)."""
        if self.epoch is None:
            return 0
        if epoch <= self.epoch:
            return -1
        needed = (epoch - self.epoch) * CHANNEL_EPOCH_STEP_INTERVAL_S
        elapsed = now - (self.epoch_raised_at or 0)
        return max(0, needed - elapsed)

    def writer_pk_for(self, epoch: int) -> str | None:
        """The pinned channel writer public key (b64url) for ``epoch``."""
        if self.writer_key_epoch is not None and epoch == self.writer_key_epoch:
            return self.writer_key_pk
        if (
            self.writer_key_prev_epoch is not None
            and epoch == self.writer_key_prev_epoch
        ):
            return self.writer_key_prev_pk
        return None


@dataclass(slots=True, frozen=True)
class GfsChannelSubscriber:
    """One fan-out seat on a channel and the epoch of the pass behind it."""

    channel_id: str
    instance_id: str
    pass_epoch: int


@dataclass(slots=True, frozen=True)
class GfsInviteToken:
    """One owner-minted space invite, parked on this server's bulletin board.

    ``GET /join/{gfs_token}`` looks the row up and renders ``blob`` back as a
    ``socialhome://invite#<blob>`` code. The GFS is a bulletin board here and
    nothing more: it holds an OPAQUE string it never parses, shows the space
    name it already publishes on ``/spaces/{id}``, and hands the string to
    anyone with the link.

    ``uses`` / ``max_uses`` exist on the table (migration 0001) and are
    deliberately absent here: a use counter would turn this server into a
    record of how many strangers opened a family's invite. Nothing reads or
    writes them — see ``0012_gfs_invite_tokens_blob.sql``.
    """

    gfs_token: str
    space_id: str
    source_instance_id: str
    blob: str
    created_at: int
    expires_at: int


# Backwards-compatible aliases for the pre-spec stub names so existing
# tests / imports keep working through the transition.
GfsInstance = ClientInstance
GfsSpace = GlobalSpace
