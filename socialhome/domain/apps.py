"""Social Home Apps — domain dataclasses for the app registry.

Pure data, no I/O. ``InstalledApp`` is the row shape of the
``installed_apps`` table; ``AppManifest`` is the parsed per-app
``manifest.json``; ``AppCatalogEntry`` is one item of the remote
``catalog.json`` published by the ``socialhome-apps`` repo.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from .errors import WrongTypeError


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


_VALID_MIN_AGES: frozenset[int] = frozenset({0, 13, 16, 18})


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
    """A pending app-session invite stored for an offline recipient.

    One row of the ``app_pending_sessions`` table — a generic, app-agnostic
    stash so an inbound ``APP_SESSION`` invite survives until the recipient
    next opens the app. ``payload`` is the decoded invite dict.
    """

    app_id: str
    user_id: str
    session_id: str
    from_instance: str
    from_user: str | None
    payload: dict
    created_at: str


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
