"""Social Home Apps — domain dataclasses for the app registry.

Pure data, no I/O. ``InstalledApp`` is the row shape of the
``installed_apps`` table; ``AppManifest`` is the parsed per-app
``manifest.json``; ``AppCatalogEntry`` is one item of the remote
``catalog.json`` published by the ``socialhome-apps`` repo.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import ClassVar

from .errors import CodedError, WrongTypeError

#: How long a game invite (a pending app session) waits for its invitee.
#: Past it the invite is no longer replayed into the app and its "Needs
#: you" item reads as expired. Correspondence games are slow: days, not
#: hours.
PENDING_SESSION_TTL_SECONDS: int = 14 * 24 * 60 * 60  # 14 days

#: How long a game being played (an ``active`` session row) stays known
#: after its last move or held message (owner decision 2026-10-29): a
#: correspondence game may rest for weeks. Past it the game can be picked
#: up again only through Social Home's sheet ("Continue game with Ben").
ACTIVE_SESSION_TTL_SECONDS: int = 90 * 24 * 60 * 60  # 90 days

#: ``APP_SESSION`` verb: "pick this game up again" — a game this
#: household no longer knows (from before the mailbox, or quiet past
#: :data:`ACTIVE_SESSION_TTL_SECONDS`) re-registered through the sheet.
#: The other side gets it as an invite (Needs you → Play), never silently.
SESSION_VERB_CONTINUE: str = "continue"

#: ``app_pending_sessions.state`` (0131): an invite waiting in Needs you…
SESSION_INVITED: str = "invited"
#: …or a game being played (Play pressed, or this person invited / moved).
SESSION_ACTIVE: str = "active"

#: The mailbox (D11) keeps one held message per (app, person, session) —
#: the latest — up to this size. A bigger message still goes out live, it
#: just isn't kept for a closed app (live messages may be up to 256 KiB).
HELD_MESSAGE_MAX_BYTES: int = 64 * 1024


def app_view_key(app_id: str) -> str:
    """What a tab claims while it shows *app_id* — the SPA's ``dm.active``
    frame, the one "this is on screen" marker a WebSocket carries. A
    conversation id never starts with ``app:``. A "Your turn" row is
    skipped while the person has the game in front of them."""
    return f"app:{app_id}"


#: Invites go through Social Home's invite sheet (D3). Backstops on what
#: one person can send in one app: at most this many invites nobody has
#: answered yet (a move, a decline) and that weren't withdrawn…
INVITES_OPEN_MAX: int = 10
#: …and at most this many new invites in any 24 hours.
INVITES_PER_DAY_MAX: int = 30

#: Invites for an app this household doesn't have (or turned off) wait as
#: Needs-you rows (D10): one per (invitee, app, sender person) — a repeat
#: joins it — and at most this many senders' rows per invitee and app.
#: The receiver's own bound: the sender-side caps above don't hold for a
#: household that ignores them.
WAITING_SENDERS_MAX: int = 5

#: Keys of a session row's ``payload`` only Social Home writes (sent
#: through the sheet / answered / withdrawn / a seat at a group table,
#: v_76). An invite from another household is stored without them, so it
#: can't pose as one of ours — nor a 1:1 invite as a group seat.
SESSION_MARKS: frozenset[str] = frozenset({"sent", "answered", "withdrawn", "group"})

#: ``APP_SESSION {verb: "close", reason}`` reasons Social Home itself sends:
#: the invitee said no…
CLOSE_DECLINED: str = "declined"
#: …the challenger took the invite back (also what an uninstall tells a
#: household below v_72 about a pending invite)…
CLOSE_WITHDRAWN: str = "withdrawn"
#: …or the other household uninstalled the app, so the game is over
#: (D9, v_72 ``MIN_FOR_APP_SESSION_ENDED``).
CLOSE_ENDED: str = "ended"

#: An uninstall tells each opponent household its games ended in one
#: ``APP_SESSION {verb: "close", reason: "ended", sessions: [...]}`` —
#: at most this many sessions per event (sender chunks, receiver drops
#: the rest). One household realistically has a handful; 100 entries are
#: ≈ 13 KB sealed (≈ 130 B each).
ENDED_SESSIONS_MAX: int = 100

#: An uninstall counts and announces a game only when it moved in this
#: long. Social Home can't tell a finished game from a resting one (both
#: are ``active`` rows until the 90-day TTL); a game quiet for a month is
#: most likely over, and telling its opponent "your game ended" weeks
#: later would be noise. Quieter games are left to the TTL.
UNINSTALL_GAME_RECENT_SECONDS: int = 30 * 24 * 60 * 60

#: The uninstall dialog names at most this many opponents ("Ben, Clara and
#: Ömer will be told") and counts the rest.
UNINSTALL_NAMES_MAX: int = 3


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


class AppNeedsNewerCoreError(AppError, CodedError):
    """Install asked for an app that needs a newer Social Home: its
    ``min_core_version`` is past this one, or it asks for a permission this
    one doesn't know (never silently granted)."""

    status = 409
    code = "APP_NEEDS_NEWER_SOCIAL_HOME"
    detail = "This app needs a newer Social Home."


class AppNewPermissionsError(AppError, CodedError):
    """An update asks for permissions the household hasn't agreed to (D7):
    the catalog's — or the downloaded manifest's — permissions grow past
    the installed ones and the admin's ``accept_permissions`` doesn't
    cover the growth. ``params.permissions`` names only the new ones
    (comma-separated, :data:`APP_PERMISSIONS` order); the installed
    version stays."""

    status = 409
    code = "APP_NEW_PERMISSIONS"
    detail = "This version asks for new permissions."


class AppRequestNotAllowedError(AppError, CodedError):
    """ "Ask an admin" for an app that can't be asked for: the caller is an
    admin (they add apps themselves) or the app is already in the
    household."""

    status = 409
    code = "APP_REQUEST_NOT_NEEDED"
    detail = "This app doesn't need a request."


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


class AppSessionUnknownError(AppError, CodedError):
    """A message (or a withdrawal) for a game this household doesn't know
    the sender plays with that person: not one opened through the invite
    sheet, not one they took in with Play (D3). An app can't message an
    old opponent on a new session id."""

    status = 403
    code = "APP_SESSION_UNKNOWN"
    detail = "This app can only message games you started or accepted."


class AppSessionTakenError(AppError, CodedError):
    """ "Continue game" for a session id this household already knows for
    the person with someone else (or still waiting as an invite): a session
    id is never moved to another opponent. 409."""

    status = 409
    code = "APP_SESSION_TAKEN"
    detail = "That game is already known with someone else."


class AppInviteLimitError(AppError, CodedError):
    """Too many invites from one person in one app: :data:`INVITES_OPEN_MAX`
    unanswered (``APP_INVITES_OPEN_LIMIT``) or :data:`INVITES_PER_DAY_MAX`
    in 24 hours (``APP_INVITES_DAILY_LIMIT``); ``params.limit`` is the
    number. 429."""

    status = 429
    code = "APP_INVITES_OPEN_LIMIT"
    detail = "Too many invites are waiting for an answer."

    def __init__(self, *, daily: bool) -> None:
        if daily:
            super().__init__(
                "Too many invites today.",
                code="APP_INVITES_DAILY_LIMIT",
                params={"limit": INVITES_PER_DAY_MAX},
            )
        else:
            super().__init__(params={"limit": INVITES_OPEN_MAX})


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


#: The plain-word permissions (owner decision D2) by name. What the
#: household agreed to is the *installed* manifest (an update that grows
#: them waits for an admin), so an app's permissions are read from
#: ``installed_apps.manifest_json`` through :func:`app_permissions` — no
#: separate grant table. Enforced twice: in the SPA's bridge (the
#: sandbox's only way out) and on every ``/api/apps/{id}/…`` route.
#: "Saves its own data" — the per-app, per-person key-value store.
PERMISSION_STORAGE: str = "storage"
#: "Lets you pick people" — contacts, and games (sessions) with people:
#: invites, moves, the mailbox.
PERMISSION_PEOPLE: str = "people"
#: "Sends to other households" — anything that leaves this household, and
#: anything another household's copy of the app sends here.
PERMISSION_FEDERATION: str = "federation"


class AppPermissionDeniedError(AppError, CodedError):
    """The app asked for something outside the permissions the household
    agreed to when it installed it (403, ``params.permission``)."""

    status = 403
    code = "APP_PERMISSION_DENIED"
    detail = "This app doesn't have that permission."

    def __init__(self, permission: str) -> None:
        super().__init__(params={"permission": permission})
        self.permission = permission


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
    """Parsed ``manifest.json`` from an app bundle.

    ``names`` are the bundle's own ``i18n`` names, ``(locale, name)``
    pairs: the app's name in a reader's language when the catalog's held
    copy isn't there yet (e.g. right after a restart, or with the catalog
    unreachable)."""

    entry: str
    icon: str | None
    capabilities: tuple[str, ...]
    min_age: int = 0
    names: tuple[tuple[str, str], ...] = ()
    #: Group play (v_76, SDK 1.9): how many players a game takes, whether
    #: it may be opened as a table (``tables``) and whether a game being
    #: played still takes people (``late_join``). Without ``tables`` an
    #: app keeps 1:1 invites only.
    players_min: int = 2
    players_max: int = 2
    tables: bool = False
    late_join: bool = False

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
        players_min, players_max = _parse_players(data.get("players"))
        tables = data.get("tables", False)
        late_join = data.get("late_join", False)
        if not isinstance(tables, bool) or not isinstance(late_join, bool):
            raise ValueError("manifest.tables / late_join must be booleans")  # noqa: TRY004 — one refusal type
        return cls(
            entry=entry,
            icon=icon,
            capabilities=tuple(caps),
            min_age=min_age,
            names=tuple(
                (t.locale, t.name)
                for t in _parse_i18n(data.get("i18n"))
                if t.name is not None
            ),
            players_min=players_min,
            players_max=players_max,
            tables=tables,
            late_join=late_join,
        )

    def to_dict(self) -> dict:
        """What the household keeps of the manifest (``manifest_json``):
        :meth:`from_dict` reads it back."""
        out: dict = {
            "entry": self.entry,
            "icon": self.icon,
            "capabilities": list(self.capabilities),
        }
        if self.names:
            out["i18n"] = {locale: {"name": name} for locale, name in self.names}
        if self.tables or (self.players_min, self.players_max) != (2, 2):
            out["players"] = {"min": self.players_min, "max": self.players_max}
        if self.tables:
            out["tables"] = True
        if self.late_join:
            out["late_join"] = True
        return out


#: The most players any table seats, whatever an app's manifest says
#: (``GROUP_MAX_PLAYERS`` in :mod:`socialhome.domain.app_group`; repeated
#: here so the manifest parser stays free of the group module).
MANIFEST_PLAYERS_MAX: int = 30


def _parse_players(raw: object) -> tuple[int, int]:
    """A manifest's ``players: {min, max}`` — ``(2, 2)`` when absent. ``min``
    is at least 2 and at most ``max``; a ``max`` past
    :data:`MANIFEST_PLAYERS_MAX` is cut to it (design 2.1)."""
    if raw is None:
        return 2, 2
    if not isinstance(raw, dict):
        raise ValueError("manifest.players must be an object {min, max}")  # noqa: TRY004 — one refusal type
    lo, hi = raw.get("min", 2), raw.get("max", 2)
    if (
        not isinstance(lo, int)
        or isinstance(lo, bool)
        or not isinstance(hi, int)
        or isinstance(hi, bool)
    ):
        raise ValueError("manifest.players.min / max must be integers")  # noqa: TRY004 — one refusal type
    hi = min(hi, MANIFEST_PLAYERS_MAX)
    if lo < 2 or hi < lo:
        raise ValueError("manifest.players needs 2 <= min <= max")
    return lo, hi


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

    def name_in(self, locale: str | None) -> str:
        """The app's name in *locale* from its bundle's ``i18n`` (base
        language first: ``de-AT`` → ``de``), else its installed name —
        the rule :meth:`AppCatalogEntry.name_in` uses."""
        if locale:
            names = dict(self.manifest.names)
            hit = names.get(locale) or names.get(locale.split("-")[0])
            if hit:
                return hit
        return self.name

    @property
    def permissions(self) -> tuple[str, ...]:
        """What the household agreed to: the installed manifest's
        capabilities as permissions (:func:`app_permissions` — the one
        mapping, ``people`` implied by ``federation``)."""
        return app_permissions(self.manifest.capabilities)

    def require_permission(self, permission: str) -> None:
        """Raise :class:`AppPermissionDeniedError` unless granted."""
        if permission not in self.permissions:
            raise AppPermissionDeniedError(permission)


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

    @property
    def sent(self) -> bool:
        """This person sent this invite through the invite sheet (a game
        they started). The marks live in ``payload`` and only Social Home
        writes them: an inbound invite is stored without them
        (:data:`SESSION_MARKS`)."""
        return self.state == SESSION_ACTIVE and self.payload.get("sent") is True

    @property
    def answered(self) -> bool:
        """The other side of an invite this person sent said something (a
        move, a decline): it no longer counts as open."""
        return self.payload.get("answered") is True

    @property
    def withdrawn(self) -> bool:
        """This person took back the invite they sent: nothing more goes
        out in it."""
        return self.payload.get("withdrawn") is True

    @property
    def closed_by_other(self) -> bool:
        """The other side closed this session: its ``close`` waits in the
        mailbox (D11) for the person's app."""
        held = self.held or {}
        return (
            held.get("kind") == "session"
            and (held.get("payload") or {}).get("verb") == "close"
        )

    def is_game_going(self, recent_cut: str) -> bool:
        """A 1:1 game this person is playing: the game started (an invite
        they got and accepted, or one they sent that was answered), nobody
        took it back or closed it, and someone moved since *recent_cut*
        (:data:`UNINSTALL_GAME_RECENT_SECONDS` — Social Home can't tell a
        finished game from a resting one, so a game quiet that long counts
        as over). A seat at a group table is not a 1:1 game."""
        return (
            self.state == SESSION_ACTIVE
            and not self.is_group
            and not self.withdrawn
            and not self.closed_by_other
            and not (self.sent and not self.answered)
            and self.last_change >= recent_cut
        )

    @property
    def is_group(self) -> bool:
        """A seat at a group table (v_76, :mod:`socialhome.domain.app_group`):
        the row keeps Needs you, the TTL and the uninstall cascade for the
        person, but none of the 1:1 paths (moves, the mailbox's
        ``held_json``, contacts, accept / decline) apply to it."""
        return self.payload.get("group") is True

    def is_with(self, instance_id: str, user_ref: str | None) -> bool:
        """Whether the other side of this session is *user_ref* on
        *instance_id* — the only one whose messages it may hold."""
        return (
            bool(user_ref)
            and self.from_instance == instance_id
            and self.from_user == user_ref
        )


@dataclass(slots=True, frozen=True)
class AppUninstallPreview:
    """What uninstalling an app loses (D9) — the dialog's counts.

    ``people`` — people in this household with saved data (``app_kv``);
    ``games`` — games being played with people in other households;
    ``invites`` — open invites with people in other households, either
    way; ``told`` — up to :data:`UNINSTALL_NAMES_MAX` opponents who will be
    told, as the admin sees them anywhere else ("Ben · Familie Okafor"),
    ``told_more`` the rest; ``untold`` — games with a household on an
    older Social Home (below v_72), which hears nothing about them;
    ``tables`` — group games (v_76) still waiting for players or being
    played that someone here has a place at with someone of another
    household (they end, or this household's people leave them; never
    counted in ``games`` / ``invites``). Counts and names only: never
    what the games hold, nor who here plays whom.
    """

    people: int
    games: int
    invites: int
    told: tuple[str, ...] = ()
    told_more: int = 0
    untold: int = 0
    tables: int = 0


@dataclass(slots=True, frozen=True)
class AppCatalogStatus:
    """How current the catalog this household holds is.

    ``checked_at`` — when the catalog last answered (UTC ISO 8601), or
    ``None`` if it never has since start. ``stale`` — the latest attempt to
    reach it failed, so what is served is the answer from ``checked_at``."""

    checked_at: str | None = None
    stale: bool = False


#: The plain-word permissions an app can ask for (D2), in the order the
#: SPA lists them. ``people`` is implied by ``federation`` for a catalog
#: entry or an install that predates it (federation without picking whom
#: to play with is useless) — see :func:`app_permissions`.
APP_PERMISSIONS: tuple[str, ...] = ("storage", "people", "federation")
_KNOWN_PERMISSIONS: frozenset[str] = frozenset(APP_PERMISSIONS)


def app_permissions(declared: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """The plain-word permissions for *declared* (a manifest's
    ``capabilities`` or a catalog entry's ``permissions``), in
    :data:`APP_PERMISSIONS` order, ``people`` implied by ``federation``.
    Unknown ids are dropped here — :attr:`AppCatalogEntry.unknown_permissions`
    keeps them so the entry reads "needs a newer Social Home"."""
    have = set(declared)
    if "federation" in have:
        have.add("people")
    return tuple(p for p in APP_PERMISSIONS if p in have)


def unknown_capabilities(declared: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """The ids in *declared* this Social Home doesn't know, in order, each
    once — a manifest with any is refused at install / update
    (:class:`AppNeedsNewerCoreError`), never granted blindly or dropped."""
    seen: list[str] = []
    for c in declared:
        if c not in _KNOWN_PERMISSIONS and c not in seen:
            seen.append(c)
    return tuple(seen)


def new_permissions(
    installed: tuple[str, ...] | list[str], candidate: tuple[str, ...] | list[str]
) -> tuple[str, ...]:
    """The plain-word permissions *candidate* (a new version's catalog
    ``permissions`` or manifest ``capabilities``) asks for beyond
    *installed* (what the household agreed to), in
    :data:`APP_PERMISSIONS` order. Empty: same or fewer — the update needs
    no new yes."""
    have = set(app_permissions(installed))
    return tuple(p for p in app_permissions(candidate) if p not in have)


#: Receiver bounds for catalog schema v2 (r2-proposal §7). A value past a
#: bound is capped (text) or ignored (anything else) — one bad field never
#: drops the entry, and a publisher can't inflate what every household
#: holds and serves.
#: Largest catalog body read (the whole ``catalog.json``).
MAX_CATALOG_BYTES: int = 256 * 1024
CATALOG_NAME_MAX: int = 40
CATALOG_DESCRIPTION_MAX: int = 300
#: Most locales kept per ``i18n`` / changelog ``notes`` map, and most
#: ``languages`` / unknown permission ids kept.
CATALOG_LOCALES_MAX: int = 12
CATALOG_CHANGELOG_MAX: int = 5
CATALOG_CHANGELOG_NOTE_MAX: int = 500
CATALOG_PUBLISHER_MAX: int = 60
CATALOG_HOMEPAGE_MAX: int = 300
#: Largest ``icon_url`` (a ``data:`` URI) kept; a bigger one shows the
#: placeholder icon.
CATALOG_ICON_MAX: int = 16 * 1024

#: A locale tag as the catalog keys it (``de``, ``pt-BR``).
_LOCALE_RE = re.compile(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})?")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _capped(value: object, limit: int) -> str | None:
    """*value* as a stripped string cut to *limit* characters (an ellipsis
    marks the cut), or ``None`` when it isn't a non-empty string."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def _locale(value: object) -> str | None:
    """*value* when it is a locale tag (``de``, ``pt-BR``), else ``None``."""
    if isinstance(value, str) and _LOCALE_RE.fullmatch(value):
        return value
    return None


def core_version_satisfies(
    min_core_version: str | None, core_version: str, *, today: date
) -> bool:
    """Is this Social Home (*core_version*) new enough for an app that
    needs *min_core_version*?

    No requirement → yes. A release compares as a version. A core version
    that isn't a plain release — a development build (``2026.10.12.dev2``,
    ``0.0.0+unknown``: built from a checkout) — runs the newest code, so
    it is ahead of every release out by *today* (UTC): only a requirement
    dated after *today* (CalVer ``YYYY.M.D``) holds it back — no Social
    Home has that yet, so the Store says "Needs a newer Social Home"."""
    if not min_core_version:
        return True
    need = parse_app_version(min_core_version)
    if need is None:
        return True
    have = parse_app_version(core_version)
    if have is None or have == (0, 0, 0):
        return need[:3] <= (today.year, today.month, today.day)
    return have >= need


@dataclass(slots=True, frozen=True)
class AppCatalogText:
    """One locale's name and description of a catalog entry (``i18n``)."""

    locale: str
    name: str | None
    description: str | None


@dataclass(slots=True, frozen=True)
class AppChangelogEntry:
    """One version's "What's new" (``changelog``); ``notes`` per locale."""

    version: str
    date: str | None
    notes: tuple[tuple[str, str], ...]


@dataclass(slots=True, frozen=True)
class AppCatalogEntry:
    """One entry of the remote ``catalog.json``.

    The v1 fields are required as before. The schema-2 fields (``i18n``,
    ``bundle_size``, ``permissions``, ``min_age``, ``languages``,
    ``publisher``, ``homepage``, ``min_core_version``, ``changelog``) are
    optional and bounded (the ``CATALOG_*`` limits): text past a limit is
    cut, any other bad value reads as unknown — one bad field never drops
    the entry."""

    app_id: str
    name: str
    latest_version: str
    description: str
    icon_url: str | None
    capabilities: tuple[str, ...]
    bundle_url: str
    bundle_sha256: str
    i18n: tuple[AppCatalogText, ...] = ()
    bundle_size: int | None = None
    #: Known plain-word permissions (:func:`app_permissions`).
    permissions: tuple[str, ...] = ()
    #: Permission ids this Social Home doesn't know — the app needs a
    #: newer one (never silently granted).
    unknown_permissions: tuple[str, ...] = ()
    #: The publisher's age rating, ``None`` when not given (or invalid).
    min_age: int | None = None
    languages: tuple[str, ...] = ()
    publisher: str | None = None
    homepage: str | None = None
    min_core_version: str | None = None
    changelog: tuple[AppChangelogEntry, ...] = ()
    #: Group play (SDK 1.9): the app's players range as ``(min, max)``
    #: ("2–8 players"), ``None`` when the catalog doesn't say. ``tables``:
    #: the catalog says the app can be opened as a game in a space — taken
    #: as true when the entry carries a ``players`` range (only group-
    #: capable apps declare one), so a space's "Games allowed here" can
    #: offer an app this household hasn't added yet.
    players: tuple[int, int] | None = None
    tables: bool = False

    _REQUIRED: ClassVar[tuple[str, ...]] = (
        "app_id",
        "name",
        "latest_version",
        "description",
        "bundle_url",
        "bundle_sha256",
    )

    def name_in(self, locale: str | None) -> str:
        """The app's name in *locale* (``i18n``; base language first:
        ``de-AT`` → ``de``), else the catalog's own — as the SPA's
        ``localized()`` picks it."""
        if locale:
            texts = {t.locale: t.name for t in self.i18n if t.name}
            hit = texts.get(locale) or texts.get(locale.split("-")[0])
            if hit:
                return hit
        return self.name

    @classmethod
    def from_dict(cls, data: dict) -> AppCatalogEntry:
        if not isinstance(data, dict):
            raise WrongTypeError("catalog entry must be an object")
        missing = [k for k in cls._REQUIRED if not data.get(k)]
        if missing:
            raise ValueError(f"catalog entry missing fields: {missing}")
        caps = data.get("capabilities", [])
        if not isinstance(caps, list):
            raise WrongTypeError("catalog entry capabilities must be a list")
        capabilities = tuple(str(c) for c in caps)
        raw_perms = data.get("permissions")
        if isinstance(raw_perms, list) and all(isinstance(p, str) for p in raw_perms):
            declared: tuple[str, ...] = tuple(raw_perms)
        else:
            # v1 (or a bad list): the manifest capabilities say it.
            declared = capabilities
        icon = data.get("icon_url")
        app_id = str(data["app_id"])
        players = _parse_catalog_players(data.get("players"))
        return cls(
            app_id=app_id,
            name=_capped(str(data["name"]), CATALOG_NAME_MAX) or app_id,
            latest_version=str(data["latest_version"]),
            description=_capped(str(data["description"]), CATALOG_DESCRIPTION_MAX)
            or "",
            icon_url=icon
            if isinstance(icon, str) and len(icon) <= CATALOG_ICON_MAX
            else None,
            capabilities=capabilities,
            bundle_url=str(data["bundle_url"]),
            bundle_sha256=str(data["bundle_sha256"]),
            i18n=_parse_i18n(data.get("i18n")),
            bundle_size=_parse_size(data.get("bundle_size")),
            permissions=app_permissions(declared),
            unknown_permissions=tuple(
                sorted({p[:40] for p in declared if p not in _KNOWN_PERMISSIONS})
            )[:CATALOG_LOCALES_MAX],
            min_age=_parse_min_age(data.get("min_age")),
            languages=_parse_languages(data.get("languages")),
            publisher=_capped(data.get("publisher"), CATALOG_PUBLISHER_MAX),
            homepage=_parse_homepage(data.get("homepage")),
            min_core_version=_parse_core_version(data.get("min_core_version")),
            changelog=_parse_changelog(data.get("changelog")),
            players=players,
            tables=data.get("tables") is True or players is not None,
        )


def _parse_catalog_players(raw: object) -> tuple[int, int] | None:
    """``players {min, max}`` — whole numbers, ``2 ≤ min ≤ max ≤ 30`` —
    else ``None`` (one bad field never drops the entry)."""
    if not isinstance(raw, dict):
        return None
    lo, hi = raw.get("min"), raw.get("max")
    if (
        isinstance(lo, bool)
        or isinstance(hi, bool)
        or not isinstance(lo, int)
        or not isinstance(hi, int)
    ):
        return None
    if not 2 <= lo <= hi <= MANIFEST_PLAYERS_MAX:
        return None
    return (lo, hi)


def _parse_i18n(raw: object) -> tuple[AppCatalogText, ...]:
    """``i18n`` → per-locale texts (bad locales / values skipped)."""
    if not isinstance(raw, dict):
        return ()
    out: list[AppCatalogText] = []
    for key, value in raw.items():
        if len(out) >= CATALOG_LOCALES_MAX:
            break
        locale = _locale(key)
        if locale is None or not isinstance(value, dict):
            continue
        name = _capped(value.get("name"), CATALOG_NAME_MAX)
        description = _capped(value.get("description"), CATALOG_DESCRIPTION_MAX)
        if name is None and description is None:
            continue
        out.append(AppCatalogText(locale, name, description))
    return tuple(out)


def _parse_size(raw: object) -> int | None:
    """``bundle_size`` in bytes, within the download cap, else ``None``."""
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw if 0 < raw <= MAX_BUNDLE_DOWNLOAD_BYTES else None


def _parse_min_age(raw: object) -> int | None:
    """``min_age`` when it is one of the ratings the gate knows."""
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw if raw in _VALID_MIN_AGES else None


def _parse_languages(raw: object) -> tuple[str, ...]:
    """``languages`` → distinct locale tags (at most the locale cap)."""
    if not isinstance(raw, list):
        return ()
    out: list[str] = []
    for item in raw:
        if len(out) >= CATALOG_LOCALES_MAX:
            break
        locale = _locale(item)
        if locale is not None and locale not in out:
            out.append(locale)
    return tuple(out)


def _parse_homepage(raw: object) -> str | None:
    """``homepage`` when it is a short ``https://`` URL — shown as text,
    never fetched."""
    if (
        not isinstance(raw, str)
        or not raw.startswith("https://")
        or len(raw) > CATALOG_HOMEPAGE_MAX
        or any(c.isspace() for c in raw)
    ):
        return None
    return raw


def _parse_core_version(raw: object) -> str | None:
    """``min_core_version`` when it parses as a version."""
    if isinstance(raw, str) and parse_app_version(raw) is not None:
        return raw
    return None


def _parse_changelog(raw: object) -> tuple[AppChangelogEntry, ...]:
    """``changelog`` → at most :data:`CATALOG_CHANGELOG_MAX` entries with a
    parseable version; notes per locale, each cut to
    :data:`CATALOG_CHANGELOG_NOTE_MAX`."""
    if not isinstance(raw, list):
        return ()
    out: list[AppChangelogEntry] = []
    for item in raw:
        if len(out) >= CATALOG_CHANGELOG_MAX:
            break
        if not isinstance(item, dict):
            continue
        version = item.get("version")
        if not isinstance(version, str) or parse_app_version(version) is None:
            continue
        date = item.get("date")
        raw_notes = item.get("notes")
        notes: list[tuple[str, str]] = []
        if isinstance(raw_notes, dict):
            for key, text in raw_notes.items():
                if len(notes) >= CATALOG_LOCALES_MAX:
                    break
                locale = _locale(key)
                note = _capped(text, CATALOG_CHANGELOG_NOTE_MAX)
                if locale is not None and note is not None:
                    notes.append((locale, note))
        out.append(
            AppChangelogEntry(
                version=version,
                date=date
                if isinstance(date, str) and _DATE_RE.fullmatch(date)
                else None,
                notes=tuple(notes),
            )
        )
    return tuple(out)


def changelog_since(
    changelog: tuple[AppChangelogEntry, ...], installed: str, latest: str
) -> tuple[AppChangelogEntry, ...]:
    """ "What's new" for an update: the *changelog* entries newer than the
    *installed* version and no newer than *latest* (an entry past the
    offered version isn't in it), newest first."""
    out = [
        c
        for c in changelog
        if is_newer_version(c.version, installed)
        and not is_newer_version(c.version, latest)
    ]
    out.sort(key=lambda c: parse_app_version(c.version) or (), reverse=True)
    return tuple(out)
