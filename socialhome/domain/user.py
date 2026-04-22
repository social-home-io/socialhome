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


@dataclass(slots=True, frozen=True)
class User:
    """A user whose home instance is this one (§4.1.9).

    The fields marked "sensitive" must never appear in API responses. They are
    kept here so the service layer has access to them for local operations
    (e.g. age gating, internal mail), but route handlers are expected to strip
    them via :data:`socialhome.security.SENSITIVE_FIELDS` before returning
    anything to a client.
    """

    user_id: str  # derive_user_id(own_instance_pk, username)
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

    # Bookkeeping
    created_at: str | None = None  # ISO-8601 UTC

    # Provisioning source: 'manual' (standalone or explicit admin) vs 'ha'
    # (mirrored from a Home Assistant person.* entity). Admins manage 'ha'
    # rows via the HA Users admin panel.
    source: str = "manual"  # 'manual' | 'ha'

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
    :func:`socialhome.crypto.user_assertion_signed_bytes`.

    ``picture_url`` is carried for display convenience but is **not** covered
    by the signature — it may be changed at any time by the home instance.
    """

    user_id: str
    instance_id: str
    username: str
    display_name: str
    issued_at: str  # ISO-8601 UTC
    signature: str  # base64url Ed25519 signature

    # Informational cache-busting hash; bytes travel via USER_UPDATED
    # federation events, not the identity assertion itself.
    picture_hash: str | None = None
    public_key: str | None = None  # base64url P-256 ECDH SPKI (§12.5)
    public_key_version: int = 0


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
    """Build the cache-busting URL the frontend uses to fetch the WebP."""
    if not picture_hash:
        return None
    return f"/api/users/{user_id}/picture?v={picture_hash}"
