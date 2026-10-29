"""Social Home Apps — domain dataclasses for the app registry.

Pure data, no I/O. ``InstalledApp`` is the row shape of the
``installed_apps`` table; ``AppManifest`` is the parsed per-app
``manifest.json``; ``AppCatalogEntry`` is one item of the remote
``catalog.json`` published by the ``socialhome-apps`` repo.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import ClassVar

from .errors import CodedError, WrongTypeError

#: How long a game invite (a pending app session) waits for its invitee.
#: Past it the invite is no longer replayed into the app and its "Needs
#: you" item reads as expired. Correspondence games are slow: days, not
#: hours.
PENDING_SESSION_TTL_SECONDS: int = 14 * 24 * 60 * 60  # 14 days

#: ``app_pending_sessions.state`` (0131): an invite waiting in Needs you…
SESSION_INVITED: str = "invited"
#: …or a game being played (Play pressed, or this person invited / moved).
SESSION_ACTIVE: str = "active"

#: The mailbox (D11) keeps one held message per (app, person, session) —
#: the latest — up to this size. A bigger message still goes out live, it
#: just isn't kept for a closed app (live messages may be up to 256 KiB).
HELD_MESSAGE_MAX_BYTES: int = 64 * 1024


class AppError(Exception):
    """Base for app-registry domain errors."""


class AppNotFoundError(AppError):
    """No installed app with the given id."""


class AppNotEnabledError(AppError):
    """The app exists but is disabled by the admin."""


class AppAlreadyInstalledError(AppError):
    """Install requested for an app id that is already installed."""


class AppIntegrityError(AppError):
    """Downloaded bundle failed sha256 / manifest / path validation."""


#: Bundle limits — a bundle is a catalog download (third-party bytes), so
#: what it can cost is capped before it is hashed, held or unpacked. Chess
#: 2026.10.28 is 13 KB compressed and 42 KB / 2 files unpacked; these leave
#: two orders of magnitude for richer apps while ruling out OOM, a zip bomb
#: or filling the disk.
#: Largest bundle download (compressed ``.tgz``) read into memory.
MAX_BUNDLE_DOWNLOAD_BYTES: int = 8 * 1024 * 1024
#: Largest total of a bundle's unpacked files (declared tar member sizes).
MAX_BUNDLE_EXTRACTED_BYTES: int = 32 * 1024 * 1024
#: Most tar members (files + directories) a bundle may hold.
MAX_BUNDLE_FILES: int = 256


class AppBundleTooLargeError(AppIntegrityError, CodedError):
    """The bundle is past a :data:`MAX_BUNDLE_DOWNLOAD_BYTES` /
    :data:`MAX_BUNDLE_EXTRACTED_BYTES` / :data:`MAX_BUNDLE_FILES` limit.

    Still an :class:`AppIntegrityError`, so the install / update cleanup
    paths remove a partial unpack as for any other bad bundle."""

    status = 422
    code = "APP_BUNDLE_TOO_LARGE"
    detail = "The app bundle is over the size limit."


class AppCatalogDisabledError(AppIntegrityError, CodedError):
    """Install asked for while the app catalog is turned off
    (``apps_catalog_url = ""``)."""

    status = 409
    code = "APP_CATALOG_DISABLED"
    detail = "The app catalog is turned off."


class AppDownloadError(AppError, CodedError):
    """The bundle could not be downloaded (timeout, network or HTTP error)."""

    status = 502
    code = "APP_DOWNLOAD_FAILED"
    detail = "Could not download the app bundle."


class AppCatalogUnreachableError(AppError, CodedError):
    """The app catalog could not be fetched (network, HTTP or a body that
    isn't a catalog) and no earlier answer is held to fall back on."""

    status = 503
    code = "APP_CATALOG_UNREACHABLE"
    detail = "Could not reach the app catalog."


class AppQuotaExceededError(AppError):
    """A per-(app, user) storage quota (key count / value size) was exceeded."""


class AppAgeRestrictedError(AppError):
    """A protected minor is below the app's minimum age requirement."""


class AppContactNotFoundError(AppError):
    """The session/message target is not a legitimate contact of the actor.

    Raised when a user tries to open a session with — or send a message to —
    a person who is not in their challengeable roster (the same set
    ``list_contacts`` returns: paired-household members minus personal
    blocks).  Closes the authorization gap where a crafted ``target`` could
    address an arbitrary user / household the actor has no relationship with.
    """


class AppChallengeHandledError(CodedError):
    """The game invite no longer waits: the app took it in on another
    device, it was declined a moment ago, withdrawn, or it expired."""

    status = 409
    code = "APP_CHALLENGE_HANDLED"
    detail = "game invite already handled"


#: A comparable app version: dot-separated non-negative integers — CalVer
#: ``YYYY.M.D`` or ``YYYY.M.D.N`` for apps from ``socialhome-apps`` (a
#: dev bundle's ``1.0.0`` parses the same way).
_VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+)*")


def parse_app_version(version: str) -> tuple[int, ...] | None:
    """*version* as a tuple of ints (``"2026.10.28.1"`` →
    ``(2026, 10, 28, 1)``), or ``None`` when it isn't dot-separated
    integers. Compared as tuples, ``2026.6.4 < 2026.10.28 < 2026.10.28.1``
    — a string compare gets the first pair wrong."""
    if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
        return None
    return tuple(int(part) for part in version.split("."))


def is_newer_version(candidate: str, current: str) -> bool:
    """Is *candidate* strictly newer than *current*?

    Fail-closed: an older or equal catalog version is never an update (a
    catalog rolled back to an older release must not downgrade an app),
    and neither is a version either side can't parse."""
    new = parse_app_version(candidate)
    old = parse_app_version(current)
    if new is None or old is None:
        return False
    return new > old


_VALID_MIN_AGES: frozenset[int] = frozenset({0, 13, 16, 18})


def app_age_allows(min_age: int, *, protected: bool, declared_age: int | None) -> bool:
    """The app age gate: may an account use an app with *min_age*?

    ``min_age <= 0`` → no restriction; an unprotected account (no child
    protection) → allowed; a protected account → only at or above
    *min_age* (no declared age counts as 0, so the gate fails closed).
    One rule for the runtime, the app list, the WebSocket frames about an
    app and the "new app" row — nobody learns of an app they can't use."""
    if min_age <= 0 or not protected:
        return True
    return int(declared_age or 0) >= min_age


@dataclass(slots=True, frozen=True)
class AppManifest:
    """Parsed ``manifest.json`` from an app bundle."""

    entry: str
    icon: str | None
    capabilities: tuple[str, ...]
    min_age: int = 0

    @classmethod
    def from_dict(cls, data: dict) -> AppManifest:
        entry = data.get("entry")
        if not isinstance(entry, str) or not entry:
            raise ValueError("manifest.entry must be a non-empty string")
        if (
            entry.startswith("/")
            or "\\" in entry
            or any(seg in ("", "..") for seg in entry.split("/"))
        ):
            raise ValueError("manifest.entry must be a relative path inside the bundle")
        caps = data.get("capabilities", [])
        if not isinstance(caps, list) or not all(isinstance(c, str) for c in caps):
            raise ValueError("manifest.capabilities must be a list of strings")
        icon = data.get("icon")
        if icon is not None and not isinstance(icon, str):
            raise ValueError("manifest.icon must be a string or null")
        raw_min_age = data.get("min_age", 0)
        try:
            min_age = int(raw_min_age)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"manifest.min_age must be an integer, got {raw_min_age!r}"
            ) from exc
        if min_age not in _VALID_MIN_AGES:
            raise ValueError(
                f"manifest.min_age must be one of {sorted(_VALID_MIN_AGES)}, got {min_age!r}"
            )
        return cls(entry=entry, icon=icon, capabilities=tuple(caps), min_age=min_age)


@dataclass(slots=True, frozen=True)
class InstalledApp:
    """Row shape of the ``installed_apps`` table."""

    app_id: str
    name: str
    version: str
    enabled: bool
    manifest: AppManifest
    bundle_path: str
    bundle_sha256: str
    source_url: str
    installed_by: str | None
    installed_at: str
    min_age: int = 0


@dataclass(slots=True, frozen=True)
class AppKvEntry:
    """One row of the per-user ``app_kv`` store."""

    app_id: str
    user_id: str
    key: str
    value_json: str
    updated_at: str


@dataclass(slots=True, frozen=True)
class AppPendingSession:
    """One app session this household knows for one person.

    One row of the ``app_pending_sessions`` table — generic and
    app-agnostic. ``from_instance`` / ``from_user`` name the person on the
    other side of the game (who invited them, or whom they invited).
    ``state`` is :data:`SESSION_INVITED` while the invite waits in Needs
    you (``payload`` is the decoded invite, handed to the app on Play) and
    :data:`SESSION_ACTIVE` once the game is played. ``held`` is the mailbox
    (D11): the latest message from the other side the person's app hasn't
    taken yet — ``{"kind": "message"|"session", "payload": {...}}`` — or
    ``None``. ``updated_at`` is the last change (``None`` on rows from
    before the mailbox: read ``created_at``).
    """

    app_id: str
    user_id: str
    session_id: str
    from_instance: str
    from_user: str | None
    payload: dict
    created_at: str
    state: str = SESSION_INVITED
    held: dict | None = None
    updated_at: str | None = None

    @property
    def last_change(self) -> str:
        """When the row last changed (UTC ISO 8601)."""
        return self.updated_at or self.created_at

    def is_with(self, instance_id: str, user_ref: str | None) -> bool:
        """Whether the other side of this session is *user_ref* on
        *instance_id* — the only one whose messages it may hold."""
        return (
            bool(user_ref)
            and self.from_instance == instance_id
            and self.from_user == user_ref
        )


@dataclass(slots=True, frozen=True)
class AppCatalogStatus:
    """How current the catalog this household holds is.

    ``checked_at`` — when the catalog last answered (UTC ISO 8601), or
    ``None`` if it never has since start. ``stale`` — the latest attempt to
    reach it failed, so what is served is the answer from ``checked_at``."""

    checked_at: str | None = None
    stale: bool = False


@dataclass(slots=True, frozen=True)
class AppCatalogEntry:
    """One entry of the remote ``catalog.json``."""

    app_id: str
    name: str
    latest_version: str
    description: str
    icon_url: str | None
    capabilities: tuple[str, ...]
    bundle_url: str
    bundle_sha256: str

    _REQUIRED: ClassVar[tuple[str, ...]] = (
        "app_id",
        "name",
        "latest_version",
        "description",
        "bundle_url",
        "bundle_sha256",
    )

    @classmethod
    def from_dict(cls, data: dict) -> AppCatalogEntry:
        missing = [k for k in cls._REQUIRED if not data.get(k)]
        if missing:
            raise ValueError(f"catalog entry missing fields: {missing}")
        caps = data.get("capabilities", [])
        if not isinstance(caps, list):
            raise WrongTypeError("catalog entry capabilities must be a list")
        return cls(
            app_id=str(data["app_id"]),
            name=str(data["name"]),
            latest_version=str(data["latest_version"]),
            description=str(data["description"]),
            icon_url=data.get("icon_url"),
            capabilities=tuple(str(c) for c in caps),
            bundle_url=str(data["bundle_url"]),
            bundle_sha256=str(data["bundle_sha256"]),
        )
