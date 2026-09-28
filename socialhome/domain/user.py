"""User-facing domain types (§4.1).

This module defines:

* :class:`User` — a user local to this instance.
* :class:`RemoteUser` — a user whose home instance is a remote peer.
* :class:`MemberRef` — the cross-instance user reference used throughout the
  space data model (§4.1.5).
* :class:`UserIdentityAssertion` — the signed identity envelope that
  accompanies every cross-instance user reference (§4.1.4).
* :class:`UserStatus` — optional presence/status metadata.

The spec forbids some fields from ever leaving the instance. See
:data:`SENSITIVE_FIELDS` in :mod:`socialhome.security` — ``email``,
``phone``, ``date_of_birth``, GPS coordinates, push tokens and CP flags
must never appear in any API response or federation payload.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import unicodedata


# Usernames that a platform adapter must never provision, because the spec
# (§4.1.9) reserves them for special routing or display semantics.
RESERVED_USERNAMES: frozenset[str] = frozenset(
    {
        "system",
        "admin",
        "root",
        "bot",
        "socialhome",
        "system-integration",
    }
)


# Canonical pseudo-user that authors bot-bridge posts. The row is never
# written to the ``users`` table; it exists only as a marker on
# ``post.author`` / ``space_post.author`` so feed readers can branch the
# renderer on ``author == SYSTEM_AUTHOR`` without a JOIN. The real
# identity (which bot, which household member) lives on ``post.bot_id``.
SYSTEM_AUTHOR: str = "system-integration"
SYSTEM_USERNAME: str = SYSTEM_AUTHOR  # alias for call sites that prefer "username"


@dataclass(slots=True, frozen=True)
class UserStatus:
    """Optional emoji / status-text presence indicator.

    Stored alongside :class:`User` (and serialised to ``status_json`` on the
    ``remote_users`` row when federated).
    """

    emoji: str | None = None
    text: str | None = None
    expires_at: str | None = None  # ISO-8601 UTC; ``None`` = no expiry

    @property
    def is_set(self) -> bool:
        return bool(self.emoji or self.text)

    def is_expired(self, now: datetime) -> bool:
        """``True`` once ``expires_at`` has passed (``now`` is tz-aware).

        An unparseable ``expires_at`` counts as expired — a status whose
        deadline can't be read is not one we should keep showing.
        """
        if not self.expires_at:
            return False
        try:
            deadline = datetime.fromisoformat(self.expires_at)
        except ValueError:
            return True
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return deadline <= now


#: Longest status text (code points) — matches the SPA input's cap.
STATUS_TEXT_MAX_CHARS: int = 80
#: Longest status emoji (code points). A single emoji grapheme can span
#: several code points (skin-tone modifier, ZWJ family / profession
#: sequences, keycaps, tag-sequence flags); 16 covers all of them while
#: keeping the field from becoming a second text line.
STATUS_EMOJI_MAX_CODEPOINTS: int = 16


def clean_status_text(value: object) -> str | None:
    """Validate a status text; ``None`` / blank → ``None``.

    Raises :class:`ValueError` when it isn't a string, is too long, or
    carries control characters / line breaks (the status is one line).
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("The status text must be text.")
    text = value.strip()
    if not text:
        return None
    if len(text) > STATUS_TEXT_MAX_CHARS:
        raise ValueError(
            f"Keep the status to {STATUS_TEXT_MAX_CHARS} characters or fewer."
        )
    if any(unicodedata.category(ch) in ("Cc", "Zl", "Zp") for ch in text):
        raise ValueError("Keep the status to a single line.")
    return text


def clean_status_emoji(value: object) -> str | None:
    """Validate a status emoji; ``None`` / blank → ``None``.

    A sanity check, not a full emoji parser: at most
    :data:`STATUS_EMOJI_MAX_CODEPOINTS` code points, no whitespace or
    control characters, and at least one non-ASCII code point (so plain
    words like ``"busy"`` belong in the text, while keycaps such as
    ``1️⃣`` still pass).
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Pick a single emoji for your status.")
    emoji = value.strip()
    if not emoji:
        return None
    if len(emoji) > STATUS_EMOJI_MAX_CODEPOINTS:
        raise ValueError("Pick a single emoji for your status.")
    if any(unicodedata.category(ch) in ("Cc", "Zs", "Zl", "Zp") for ch in emoji):
        raise ValueError("Pick a single emoji for your status.")
    if all(ord(ch) < 0x80 for ch in emoji):
        raise ValueError("Pick a single emoji for your status.")
    return emoji


@dataclass(slots=True, frozen=True)
class User:
    """A user whose home instance is this one (§4.1.9).

    The fields marked "sensitive" must never appear in API responses. They are
    kept here so the service layer has access to them for local operations
    (e.g. age gating, internal mail), but route handlers are expected to strip
    them via :data:`socialhome.security.SENSITIVE_FIELDS` before returning
    anything to a client.
    """

    user_id: str  # derive_user_id(own_instance_pk, identity_anchor)
    username: str  # local username; primary key
    display_name: str
    is_admin: bool = False
    # Short hex digest of the current profile picture (or None). Bytes
    # live in ``user_profile_pictures``; presence of the hash gates the
    # synthetic ``/api/users/{id}/picture?v=<hash>`` URL the frontend
    # uses. Replaces the former ``picture_url`` column (§23 profile).
    picture_hash: str | None = None
    state: str = "active"  # 'active' | 'inactive'
    bio: str | None = None
    locale: str | None = None  # IETF tag e.g. "en", "nl"; None = follow browser
    theme: str = "auto"  # 'light' | 'dark' | 'auto'
    emoji_skin_tone_default: str | None = None

    # Status / presence (optional, user-set)
    status: UserStatus = field(default_factory=UserStatus)

    # End-to-end encryption (§12.5)
    public_key: str | None = None  # base64url P-256 ECDH SPKI
    public_key_version: int = 0

    # Onboarding
    is_new_member: bool = True

    # Soft-delete (§23.56)
    deleted_at: str | None = None
    grace_until: str | None = None

    # Sensitive — never leave the instance
    email: str | None = None
    phone: str | None = None
    date_of_birth: str | None = None  # ISO 8601 date
    declared_age: int | None = None
    is_minor: bool = False
    child_protection_enabled: bool = False

    # Preferences are free-form JSON owned by the frontend.
    preferences_json: str = "{}"

    # IANA timezone name (``"Europe/Berlin"``) — the user's personal
    # wall-clock anchor for the SPA forms. Defaults to ``"UTC"`` at the
    # schema level (``users.tz NOT NULL DEFAULT 'UTC'``); the SPA auto-
    # detects the browser's resolved zone on first login and writes it
    # via ``set_tz`` so the common case (user lives in the household)
    # silently picks up the right value. Editable from the user
    # settings page for the cross-TZ-user case (a household member who
    # actually lives in a different zone than the household).
    tz: str = "UTC"

    # Bookkeeping
    created_at: str | None = None  # ISO-8601 UTC

    # Last time this user had an active WS session — populated by
    # :class:`OnlineStatusService` on disconnect so "Last seen 2 h ago"
    # survives a server restart. ``None`` means "never seen", which is
    # the default for new accounts.
    last_seen_at: str | None = None  # ISO-8601 UTC

    # Provisioning source: 'manual' (standalone or explicit admin) vs 'ha'
    # (mirrored from a Home Assistant auth user). Admins manage 'ha'
    # rows via the HA Users admin panel.
    source: str = "manual"  # 'manual' | 'ha'

    # Stable identifier from the external auth provider that owns this
    # row, scoped by ``source``. For ``source='ha'`` this is the 32-hex
    # HA ``user_id`` (``config/auth/list[].id``) — persisted so the
    # picture lifter and future presence / device-tracker bridges can
    # join via ``person.attributes.user_id`` without re-resolving the
    # username. ``None`` for ``source='manual'``.
    external_id: str | None = None

    # Immutable per-user UUID (uuid4 hex) that ``user_id`` derives from
    # (``derive_user_id(own_instance_pk, identity_anchor)``), freeing the
    # cryptographic id from the mutable username (§v_26). New users get a
    # fresh uuid4 at provision; existing rows were backfilled to their
    # username by migration 0041 so their ``user_id`` stays stable.
    # Defaulted ``None`` so the many other ``User`` call sites that don't
    # set it (reads from older code paths, test fixtures) still construct.
    identity_anchor: str | None = None

    # Public, mutable, per-household-unique @handle — the public-facing name
    # (distinct from the login ``username``, which under ``source='ha'`` is
    # HA-controlled). Backfilled = username for existing rows (migration 0043);
    # new users get ``handle = username`` at provision. Unsigned display
    # metadata (federated via USER_UPDATED), never an identity input. Defaulted
    # ``None`` so older call sites / fixtures still construct.
    handle: str | None = None

    def is_active(self) -> bool:
        return self.state == "active" and self.deleted_at is None


@dataclass(slots=True, frozen=True)
class RemoteUser:
    """A user whose home instance is a remote peer (§4.1.10).

    Primary key is :attr:`user_id` — it is already globally unique and
    cryptographically meaningful, so no surrogate id is needed.
    """

    user_id: str
    instance_id: str
    remote_username: str
    display_name: str
    alias: str | None = None  # local display override — never federated
    visible_to: str = '"all"'  # JSON-encoded visibility (see §12)
    # Same cache-busting semantics as :class:`User.picture_hash`; the
    # federation inbound handler stores the bytes into
    # ``user_profile_pictures`` and sets this hash.
    picture_hash: str | None = None
    bio: str | None = None
    status_json: str | None = None  # JSON {emoji, text, expires_at}
    public_key: str | None = None
    public_key_version: int = 0
    synced_at: str | None = None
    #: Public, per-household-unique @handle mirrored from the peer's
    #: ``USER_UPDATED`` payload — unsigned display metadata, the remote-side
    #: cache of :attr:`User.handle`. ``None`` for a peer at an older proto
    #: version that doesn't ship the field yet.
    handle: str | None = None
    #: ISO-8601 UTC timestamp marking the user as locally deprovisioned —
    #: set by the inbound ``USER_REMOVED`` handler. Filters the user out of
    #: member lists / autocomplete via ``list_remote_for_instance`` and
    #: drives the §24.11 deprovisioned-author drop on the inbound pipeline
    #: (any subsequent user-scoped envelope from this user is silently
    #: discarded). ``None`` for active users.
    deprovisioned_at: str | None = None


@dataclass(slots=True, frozen=True)
class MemberRef:
    """A canonical cross-instance user reference (§4.1.5).

    The database stores only :attr:`user_id` — this object is reconstructed by
    the service layer by joining against ``users`` or ``remote_users``. The
    ``__str__`` implementation returns the bare ``user_id`` so this value can
    be passed directly wherever a string id is expected.
    """

    user_id: str
    instance_id: str
    username: str

    def is_local(self, own_instance_id: str) -> bool:
        return self.instance_id == own_instance_id

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.user_id


@dataclass(slots=True, frozen=True)
class UserIdentityAssertion:
    """Signed identity envelope sent with every cross-instance user reference
    (§4.1.4).

    The ``signature`` is a base64url-encoded Ed25519 signature over the
    canonical byte encoding in
    :func:`socialhome.crypto.instance_assertion_signed_bytes` — the **INSTANCE**
    signature, proving "this user is hosted at ``instance_id``". For a legacy
    assertion those bytes are exactly
    :func:`socialhome.crypto.user_assertion_signed_bytes`; for a binding-bearing
    assertion they are extended to also commit to the user pubkey + suite, so
    the household vouches for the *specific* user key (closing the key-transplant
    flaw: a swapped user key can't reuse the instance signature).

    The optional ``user_*`` binding fields carry a second, **USER** self-
    signature proving "the user holds their own identity key" (independent of
    the hosting instance). When present, both signatures are checked by
    :func:`socialhome.crypto.verify_user_identity_assertion` — the instance sig
    over the extended bytes (binding) and the user self-sig (possession). They
    are nullable / defaulted ``None`` so a legacy or first-revision payload that
    omits them still parses (backward compat); a present
    ``user_identity_public_key`` is what gates the second check.

    ``picture_url`` is carried for display convenience but is **not** covered
    by the signature — it may be changed at any time by the home instance.
    """

    user_id: str
    instance_id: str
    username: str
    display_name: str
    issued_at: str  # ISO-8601 UTC
    signature: str  # base64url Ed25519 INSTANCE signature

    # Informational cache-busting hash; bytes travel via USER_UPDATED
    # federation events, not the identity assertion itself.
    picture_hash: str | None = None
    public_key: str | None = None  # base64url P-256 ECDH SPKI (§12.5)
    public_key_version: int = 0

    # Per-user identity binding (independent user identity, Phase 1). The
    # USER self-signature in ``user_signature`` (base64url) is verified
    # against ``user_identity_public_key`` (hex Ed25519) over the canonical
    # bytes in :func:`socialhome.crypto.user_identity_signed_bytes`.
    # ``user_pq_public_key`` is reserved for the Phase-2 PQ hybrid and is
    # ``None`` in Phase 1. All four default ``None`` so older / first-
    # revision payloads omitting the binding still parse.
    user_identity_public_key: str | None = None  # hex Ed25519 user pubkey
    user_pq_public_key: str | None = None  # hex; reserved for PQ (None in P1)
    user_sig_suite: str | None = None  # e.g. "ed25519"
    user_signature: str | None = None  # base64url USER self-signature

    # Immutable per-user anchor (uuid4 hex for new users, username for legacy
    # rows) that ``user_id`` derives from — ``derive_user_id(instance_pk,
    # identity_anchor)``. When present it is the derivation input the verifier
    # checks ``user_id`` against (else it falls back to ``username`` for legacy
    # compat) and BOTH signatures (instance + user self-sig) commit to it under
    # the ``identity-anchor`` domain tag, so a swapped anchor breaks the sigs,
    # not just the derivation check. Defaults ``None`` so legacy / first-
    # revision payloads that omit it still parse with byte-identical signatures.
    identity_anchor: str | None = None

    def to_wire_dict(self) -> dict[str, object]:
        """Serialise to the federation wire shape (plain JSON-able dict).

        Carries the full field set — including the nullable Phase-1 binding
        fields and the anchor — so a binding-bearing assertion round-trips
        losslessly. ``None`` values are kept (not pruned) so the receiver's
        ``from_wire_dict`` reconstructs identical defaults.
        """
        return {
            "user_id": self.user_id,
            "instance_id": self.instance_id,
            "username": self.username,
            "display_name": self.display_name,
            "issued_at": self.issued_at,
            "signature": self.signature,
            "picture_hash": self.picture_hash,
            "public_key": self.public_key,
            "public_key_version": self.public_key_version,
            "user_identity_public_key": self.user_identity_public_key,
            "user_pq_public_key": self.user_pq_public_key,
            "user_sig_suite": self.user_sig_suite,
            "user_signature": self.user_signature,
            "identity_anchor": self.identity_anchor,
        }

    @classmethod
    def from_wire_dict(cls, data: dict[str, object]) -> "UserIdentityAssertion":
        """Reconstruct from the wire shape produced by :meth:`to_wire_dict`.

        The optional binding/anchor fields default to ``None`` when absent so a
        legacy / first-revision payload still parses.
        """
        return cls(
            user_id=str(data["user_id"]),
            instance_id=str(data["instance_id"]),
            username=str(data["username"]),
            display_name=str(data["display_name"]),
            issued_at=str(data["issued_at"]),
            signature=str(data["signature"]),
            picture_hash=data.get("picture_hash"),  # type: ignore[arg-type]
            public_key=data.get("public_key"),  # type: ignore[arg-type]
            public_key_version=int(data.get("public_key_version") or 0),  # type: ignore[call-overload]
            user_identity_public_key=data.get("user_identity_public_key"),  # type: ignore[arg-type]
            user_pq_public_key=data.get("user_pq_public_key"),  # type: ignore[arg-type]
            user_sig_suite=data.get("user_sig_suite"),  # type: ignore[arg-type]
            user_signature=data.get("user_signature"),  # type: ignore[arg-type]
            identity_anchor=data.get("identity_anchor"),  # type: ignore[arg-type]
        )


@dataclass(slots=True, frozen=True)
class DisplayableUser:
    """Alias-resolved user reference returned by API responses (§4.1.6).

    All alias resolution (space alias → personal alias → global display name)
    is applied server-side so the frontend always renders :attr:`display_name`.
    """

    user_id: str
    display_name: str
    username: str
    instance_id: str
    picture_url: str | None
    is_local: bool
    has_space_alias: bool = False

    @classmethod
    def from_local_user(
        cls,
        user: User,
        own_instance_id: str,
        *,
        alias: str | None = None,
        space_alias: str | None = None,
    ) -> "DisplayableUser":
        display = space_alias or alias or user.display_name
        return cls(
            user_id=user.user_id,
            display_name=display,
            username=user.username,
            instance_id=own_instance_id,
            picture_url=_picture_url(user.user_id, user.picture_hash),
            is_local=True,
            has_space_alias=space_alias is not None,
        )

    @classmethod
    def from_remote_user(
        cls,
        remote: RemoteUser,
        *,
        alias: str | None = None,
        space_alias: str | None = None,
    ) -> "DisplayableUser":
        display = space_alias or alias or remote.display_name
        return cls(
            user_id=remote.user_id,
            display_name=display,
            username=remote.remote_username,
            instance_id=remote.instance_id,
            picture_url=_picture_url(remote.user_id, remote.picture_hash),
            is_local=False,
            has_space_alias=space_alias is not None,
        )


def _picture_url(user_id: str, picture_hash: str | None) -> str | None:
    """Build the cache-busting URL the frontend uses to fetch the WebP.

    Returned **without** a leading slash so the SPA's ``<img src>``
    resolves it against ``document.baseURI`` (which the backend sets
    to the HA Supervisor ingress prefix when behind ingress, ``/``
    otherwise). An absolute path would bypass the base and 404 under
    ingress. See ``client/src/baseUrl.ts`` for the matching client
    helpers used for ``fetch`` and ``WebSocket`` URLs.
    """
    if not picture_hash:
        return None
    return f"api/users/{user_id}/picture?v={picture_hash}"
