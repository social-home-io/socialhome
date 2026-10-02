"""Content-report service.

Any household member can file a report on a post, comment, page, task,
sticky, calendar event, gallery item, user or space. Who triages it
depends on the report's scope (:mod:`.report_scope`):

* **Space-scoped** — content inside a space, or a member's conduct in one
  (``space_id`` set). The space's content authority (owner / admin /
  moderator, :data:`~socialhome.domain.space.CONTENT_AUTHORITY_ROLES`)
  lists and resolves it (:meth:`review_space`, :meth:`resolve_in_space`);
  household admins never see it — being a household admin is not a seat in
  the space, and a space's reports are as private as its content.
* **Household-level** — feed content, a user outside any space, a space
  itself, highlights, moments (``space_id`` ``None``). Household admins
  triage it (:meth:`list_pending`, :meth:`resolve`), as before.

The scope is derived from the target itself — a client or a peer can only
NAME a space for a member report, and the target must then be seated in it.
A report on an id this household does not hold is refused exactly like one
the reporter may not see (404), so the endpoint is no existence oracle.

**Nobody triages a report about themself.** The report's subject — the
reported member, or the reported item's author / creator — is left out of
the queue, the resolve and the notification. Sole exception: the space
OWNER when the space has no other content authority anywhere (no other
local owner / admin / moderator, no remote admin / moderator seat). Then
the owner sees the report with the reporter hidden and may only dismiss it
— otherwise it would sit forever with nobody able to clear it.

Federation (§CP.R1, v_45): a space-scoped report filed on any household
goes, as ``SPACE_REPORT``, to the space's host and to every household
holding a live ``admin`` / ``moderator`` seat at v_45 or above — one
targeted, pairwise-encrypted send each (sealed end-to-end under
``SPACE_ROUTED`` across a relay, :meth:`FederationService.send_with_mesh_fallback`).
Plain member households and older households never receive it (an older
one would file it for its household admins). A resolve / dismiss is synced
to the same households with ``SPACE_REPORT_DECIDED``; the first decision
wins and a replay changes nothing. The receiver of a report
(:meth:`create_report_from_remote`) stores it only when it reviews the
space (host, or ≥1 local content-authority member), the reporter holds a
live seat on the sending household and is not banned, the target resolves
into that same space, and the per-space caps leave room. When this
household stops reviewing a space (demotion, leave, dissolve) the reports
other households filed there are purged (:meth:`watch_seats`).

The GFS fraud forward carries only what fraud triage needs: the target
(the space for space content — public / global spaces only — or the
reported user's household), the category and this household's signed
identity. Never the reporter or the notes.

Rate limiting: a single reporter can file at most
:data:`MAX_REPORTS_PER_DAY` reports in a rolling 24 h window, and a space
holds at most :data:`MAX_PENDING_PER_SPACE` pending reports
(:data:`MAX_PENDING_PER_REPORTER` per reporter, :data:`MAX_PENDING_PER_HOUSEHOLD`
per sending household). A ``UNIQUE(reporter, target_type, target_id,
scope)`` index also prevents the same report twice.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..domain.events import (
    RemoteSpaceDissolved,
    ReportFiled,
    ReportResolved,
    SpaceConfigChanged,
    SpaceMemberLeft,
)
from ..domain.federation import FederationEventType
from ..domain.federation_capabilities import FederationCapability
from ..domain.report import (
    ContentReport,
    DuplicateReportError,
    ReportCategory,
    ReportRateLimitedError,
    ReportStatus,
    ReportTargetType,
    SpaceReportView,
)
from ..domain.space import (
    CONTENT_AUTHORITY_ROLES,
    PUBLIC_SPACE_TIERS,
    ModerationAlreadyDecidedError,
    Space,
    SpacePermissionError,
    SpaceRole,
)
from ..infrastructure.event_bus import EventBus
from ..repositories.report_repo import AbstractReportRepo
from ..repositories.user_repo import AbstractUserRepo
from .report_scope import CONTENT_TARGETS, ReportScope

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..repositories.space_post_repo import AbstractSpacePostRepo
    from ..repositories.space_remote_member_repo import (
        AbstractSpaceRemoteMemberRepo,
    )
    from ..repositories.space_repo import AbstractSpaceRepo

log = logging.getLogger(__name__)


#: Per-reporter rolling-24h cap.
MAX_REPORTS_PER_DAY: int = 20
#: Pending reports one space holds here, in all.
MAX_PENDING_PER_SPACE: int = 500
#: Pending reports one reporter holds in one space.
MAX_PENDING_PER_REPORTER: int = 20
#: Pending reports one other household's members hold in one space.
MAX_PENDING_PER_HOUSEHOLD: int = 50
#: The longest ``notes`` a report keeps.
MAX_NOTES: int = 1000

#: Targets that are never inside a space: a ``space_id`` on them is refused.
_UNSCOPED_TARGETS: frozenset[ReportTargetType] = frozenset(
    {ReportTargetType.SPACE, ReportTargetType.HIGHLIGHT, ReportTargetType.MOMENT}
)

#: The remote seats whose households review a space's reports (a remote
#: seat is never ``owner`` — the host stands for the owner).
REVIEWER_SEATS: frozenset[str] = frozenset(
    r.value for r in CONTENT_AUTHORITY_ROLES if r is not SpaceRole.OWNER
)

_NOT_FOUND = "not found"


class _Drop(Exception):
    """An inbound report this household does not store (logged)."""


class ReportService:
    __slots__ = (
        "_reports",
        "_users",
        "_bus",
        "_federation",
        "_own_instance_id",
        "_space_repo",
        "_space_post_repo",
        "_seats",
        "_scope",
        "_gfs_connection_service",
        "_signing_key",
    )

    def __init__(
        self,
        *,
        report_repo: AbstractReportRepo,
        user_repo: AbstractUserRepo,
        bus: EventBus,
        space_repo: "AbstractSpaceRepo | None" = None,
        space_post_repo: "AbstractSpacePostRepo | None" = None,
        remote_member_repo: "AbstractSpaceRemoteMemberRepo | None" = None,
        scope: ReportScope | None = None,
    ) -> None:
        self._reports = report_repo
        self._users = user_repo
        self._bus = bus
        self._space_repo = space_repo
        self._space_post_repo = space_post_repo
        self._seats = remote_member_repo
        self._scope = scope or ReportScope(space_post_repo=space_post_repo)
        self._federation: "FederationService | None" = None
        self._own_instance_id: str = ""
        self._gfs_connection_service = None
        self._signing_key: bytes | None = None

    def attach_federation(
        self,
        federation_service: "FederationService",
        own_instance_id: str,
    ) -> None:
        """Wire federation after construction (breaks the service ↔
        FederationService cycle at build time).
        """
        self._federation = federation_service
        self._own_instance_id = own_instance_id

    def attach_gfs(
        self,
        gfs_connection_service,
        *,
        signing_key: bytes,
    ) -> None:
        """Wire the GFS connection service + this instance's identity
        signing key so reports auto-forward to every paired GFS.
        """
        self._gfs_connection_service = gfs_connection_service
        self._signing_key = signing_key

    def watch_seats(self, bus: EventBus) -> None:
        """Purge the space reports other households filed here once this
        household stops reviewing the space (its last content-authority
        member demoted or gone), and every report of a dissolved space."""
        bus.subscribe(SpaceConfigChanged, self._on_seat_change)
        bus.subscribe(SpaceMemberLeft, self._on_seat_change)
        bus.subscribe(RemoteSpaceDissolved, self._on_seat_change)

    async def _on_seat_change(
        self, event: SpaceConfigChanged | SpaceMemberLeft | RemoteSpaceDissolved
    ) -> None:
        if self._space_repo is None:
            return
        space = await self._space_repo.get(event.space_id)
        if (
            isinstance(event, SpaceConfigChanged)
            and space is not None
            and not space.dissolved
            and space.owner_instance_id == self._own_instance_id
        ):
            # The host: a remote seat just became admin / moderator — that
            # household reviews again, and a demotion earlier purged its
            # copies. Re-deliver the space's pending reports to it.
            iid = str((event.payload or {}).get("instance_id") or "")
            role = str((event.payload or {}).get("role") or "")
            if iid and role in REVIEWER_SEATS:
                await self.resend_pending(event.space_id, iid)
        if space is None or space.dissolved:
            n = await self._reports.delete_for_space(event.space_id, remote_only=False)
        elif not await self._reviews_here(space):
            n = await self._reports.delete_for_space(event.space_id, remote_only=True)
        else:
            return
        if n:
            log.info(
                "reports: this household no longer reviews space %s — purged "
                "%d report(s)",
                event.space_id,
                n,
            )

    # ── Filing ────────────────────────────────────────────────────────

    async def create_report(
        self,
        *,
        reporter_user_id: str,
        target_type: str,
        target_id: str,
        category: str,
        notes: str | None = None,
        forward_gfs: bool = True,
        space_id: str | None = None,
    ) -> tuple[ContentReport, bool]:
        """File a new report. Returns ``(report, federated)`` where
        ``federated`` is true iff the report was sent to at least one
        other household (the space's reviewers, or the household hosting
        the target).

        ``space_id`` names the space a ``user`` report is about (a member's
        conduct there); for content it is optional and must match the
        space the content lives in. Content inside a space is space-scoped
        whether or not the caller names it.

        When ``forward_gfs`` is True (the default), the report is also
        auto-forwarded to every paired GFS in the background
        (:meth:`forward_to_gfs` — call that directly with
        ``forward_gfs=False`` to learn whether anything went).

        Raises :class:`DuplicateReportError` if the reporter already
        filed on the same target, :class:`ReportRateLimitedError` if
        they're over a cap, :class:`KeyError` (404) when the target is
        unknown here, not in the named space, or in a space the reporter is
        not a member of, and :class:`ValueError` (422) for a bad type /
        category, notes over :data:`MAX_NOTES` or a space on an unscoped
        target.
        """
        try:
            tt = ReportTargetType(target_type)
            cat = ReportCategory(category)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        clean_notes = (notes or "").strip() or None
        if clean_notes is not None and len(clean_notes) > MAX_NOTES:
            raise ValueError(f"notes are limited to {MAX_NOTES} characters")

        count = await self._reports.count_recent_by_reporter(
            reporter_user_id,
            hours=24,
        )
        if count >= MAX_REPORTS_PER_DAY:
            raise ReportRateLimitedError(
                f"report cap reached ({MAX_REPORTS_PER_DAY}/day)",
            )

        scope_id, subject = await self._local_scope(
            tt, target_id, requested=space_id or None, reporter=reporter_user_id
        )
        if scope_id is not None:
            if (
                await self._reports.count_pending_in_space(
                    scope_id, reporter_user_id=reporter_user_id
                )
                >= MAX_PENDING_PER_REPORTER
                or await self._reports.count_pending_in_space(scope_id)
                >= MAX_PENDING_PER_SPACE
            ):
                raise ReportRateLimitedError("this space's report queue is full")
        report = ContentReport(
            id=uuid.uuid4().hex,
            target_type=tt,
            target_id=target_id,
            reporter_user_id=reporter_user_id,
            category=cat,
            notes=clean_notes,
            status=ReportStatus.PENDING,
            created_at=datetime.now(timezone.utc),
            space_id=scope_id,
            sole_reviewer_user_id=await self._sole_reviewer(scope_id, subject),
        )
        await self._save(report, duplicate_label=tt.value)
        await self._publish_filed(report, subject)

        federated = await self._maybe_federate(report)
        if forward_gfs:
            await self.forward_to_gfs(report)
        return report, federated

    async def create_report_from_remote(
        self,
        *,
        reporter_user_id: str,
        reporter_instance_id: str,
        target_type: str,
        target_id: str,
        category: str,
        notes: str | None = None,
        space_id: str | None = None,
        origin_instance_id: str | None = None,
    ) -> ContentReport | None:
        """Persist an inbound ``SPACE_REPORT`` federation event.

        ``origin_instance_id`` is the household the reporter belongs to, as
        the payload names it — trusted only from the space's HOST (which
        re-delivers other households' reports); from anyone else the
        sending household is the origin.

        Returns the new report on success, or ``None`` if the event was
        malformed, a duplicate (replay), over a cap, or not this
        household's to review (see the module docstring for the checks).
        Notes are cut at :data:`MAX_NOTES`.
        """
        if not reporter_user_id or not reporter_instance_id:
            return None
        try:
            tt = ReportTargetType(target_type)
            cat = ReportCategory(category)
        except ValueError:
            log.debug(
                "SPACE_REPORT inbound: bad target/category: %s/%s",
                target_type,
                category,
            )
            return None
        try:
            scope_id, subject, origin = await self._remote_scope(
                tt,
                target_id,
                named=space_id or None,
                reporter=reporter_user_id,
                sender=reporter_instance_id,
                claimed_origin=origin_instance_id,
            )
        except _Drop as exc:
            if str(exc):
                log.warning(
                    "SPACE_REPORT from %s refused: %s", reporter_instance_id, exc
                )
            return None
        clean = (notes if isinstance(notes, str) else "").strip()[:MAX_NOTES] or None
        report = ContentReport(
            id=uuid.uuid4().hex,
            target_type=tt,
            target_id=target_id,
            reporter_user_id=reporter_user_id,
            reporter_instance_id=origin,
            category=cat,
            notes=clean,
            status=ReportStatus.PENDING,
            created_at=datetime.now(timezone.utc),
            space_id=scope_id,
            sole_reviewer_user_id=await self._sole_reviewer(scope_id, subject),
        )
        try:
            await self._save(report, duplicate_label=tt.value)
        except DuplicateReportError:
            return None  # Replay / duplicate — harmless, ignore.
        await self._publish_filed(report, subject)
        return report

    async def _save(self, report: ContentReport, *, duplicate_label: str) -> None:
        try:
            await self._reports.save(report)
        except Exception as exc:
            # The UNIQUE index surfaces as an IntegrityError-flavoured
            # exception from aiosqlite. Convert to a domain error.
            msg = str(exc).lower()
            if "unique" in msg or "constraint" in msg:
                raise DuplicateReportError(
                    f"already reported this {duplicate_label}",
                ) from exc
            raise

    async def _publish_filed(self, report: ContentReport, subject: str | None) -> None:
        await self._bus.publish(
            ReportFiled(
                report_id=report.id,
                target_type=report.target_type.value,
                target_id=report.target_id,
                category=report.category.value,
                reporter_user_id=report.reporter_user_id,
                space_id=report.space_id,
                subject_user_id=subject,
            )
        )

    # ── Scope ─────────────────────────────────────────────────────────

    async def _local_scope(
        self,
        tt: ReportTargetType,
        target_id: str,
        *,
        requested: str | None,
        reporter: str,
    ) -> tuple[str | None, str | None]:
        """``(space, subject)`` of a locally filed report (space ``None`` =
        household-level). Every refusal of a target the reporter may not
        see is the same ``KeyError`` as for an unknown one."""
        if tt in _UNSCOPED_TARGETS:
            if requested:
                raise ValueError(
                    "space_id applies to space content and members only",
                )
            if tt is ReportTargetType.SPACE:
                await self._require_reportable_space(target_id, reporter)
            return None, None
        if tt is ReportTargetType.USER:
            if requested is None:
                if not await self._user_known(target_id):
                    raise KeyError(_NOT_FOUND)
                return None, target_id
            await self._require_reporter_member(requested, reporter)
            if not await self._is_space_member(requested, target_id):
                raise KeyError(_NOT_FOUND)
            return requested, target_id
        scope = await self._scope.of(tt, target_id)
        if not scope.found:
            raise KeyError(_NOT_FOUND)
        if requested is not None and scope.space_id != requested:
            raise KeyError(_NOT_FOUND)  # a cross-space id
        if scope.space_id is None:
            return None, scope.author
        await self._require_reporter_member(scope.space_id, reporter)
        return scope.space_id, scope.author

    async def _require_reporter_member(self, space_id: str, user_id: str) -> None:
        """The reporter is a (local) member of the space — anyone else gets
        the same 404 as for a target that does not exist."""
        try:
            space = await self._live_space(space_id)
        except KeyError:
            raise KeyError(_NOT_FOUND) from None
        assert self._space_repo is not None  # _live_space checked it
        if await self._space_repo.get_member(space.id, user_id) is None:
            raise KeyError(_NOT_FOUND)

    async def _require_reportable_space(self, space_id: str, reporter: str) -> None:
        """A space report names a space this household knows, and — unless
        it is public / global — one the reporter is a member of."""
        if self._space_repo is None:
            return
        space = await self._space_repo.get(space_id)
        if space is None:
            raise KeyError(_NOT_FOUND)
        if space.space_type in PUBLIC_SPACE_TIERS:
            return
        if await self._space_repo.get_member(space_id, reporter) is None:
            raise KeyError(_NOT_FOUND)

    async def _user_known(self, user_id: str) -> bool:
        if not user_id:
            return False
        if await self._users.get_by_user_id(user_id) is not None:
            return True
        return bool(await self._users.get_instance_for_user(user_id))

    async def _remote_scope(
        self,
        tt: ReportTargetType,
        target_id: str,
        *,
        named: str | None,
        reporter: str,
        sender: str,
        claimed_origin: str | None = None,
    ) -> tuple[str | None, str | None, str]:
        """``(space, subject, origin household)`` of an inbound report,
        after every receiver check (space ``None`` = household-level).
        Raises :class:`_Drop`."""
        if tt in _UNSCOPED_TARGETS:
            if named:
                raise _Drop(f"a space on a {tt.value} report")
            await self._require_bound_reporter(reporter, sender)
            return None, None, sender
        subject: str | None
        if tt is ReportTargetType.USER:
            if named is None:
                await self._require_bound_reporter(reporter, sender)
                if await self._users.get_by_user_id(target_id) is None:
                    raise _Drop(f"reported user {target_id} is not ours")
                return None, target_id, sender
            space_id = named
            if not await self._is_space_member(space_id, target_id):
                raise _Drop(f"reported user {target_id} is not in space {space_id}")
            subject = target_id
        else:
            scope = await self._scope.of(tt, target_id)
            if not scope.found:
                raise _Drop(f"{tt.value} {target_id} is not held here")
            if named is not None and scope.space_id != named:
                raise _Drop(f"{tt.value} {target_id} is not in the named space {named}")
            if scope.space_id is None:
                await self._require_bound_reporter(reporter, sender)
                return None, scope.author, sender
            space_id = scope.space_id
            subject = scope.author
        try:
            space = await self._live_space(space_id)
        except KeyError:
            raise _Drop(f"space {space_id} is unknown here") from None
        if not await self._reviews_here(space):
            # Not ours to triage (a pre-v_45 sender fans out to every member
            # household): quietly not stored.
            log.info(
                "SPACE_REPORT from %s: this household does not review space %s",
                sender,
                space_id,
            )
            raise _Drop("")
        assert self._space_repo is not None
        # The reporter's own household. Only the HOST may name another one
        # (it re-delivers other households' reports, ``resend_pending``);
        # everyone else files for its own members only.
        origin = sender
        if (
            sender == space.owner_instance_id
            and isinstance(claimed_origin, str)
            and 0 < len(claimed_origin) <= 128
            and claimed_origin != self._own_instance_id
        ):
            origin = claimed_origin
        seat = (
            await self._seats.get(space_id, origin, reporter)
            if self._seats is not None
            else None
        )
        if seat is None:
            raise _Drop(f"reporter {reporter} holds no seat in {space_id} on {origin}")
        if await self._space_repo.is_banned(space_id, reporter):
            raise _Drop(f"reporter {reporter} is banned from {space_id}")
        if (
            await self._reports.count_pending_in_space(
                space_id, reporter_user_id=reporter, reporter_instance_id=origin
            )
            >= MAX_PENDING_PER_REPORTER
        ):
            raise _Drop(f"reporter cap reached in {space_id}")
        if (
            await self._reports.count_pending_in_space(
                space_id, reporter_instance_id=origin
            )
            >= MAX_PENDING_PER_HOUSEHOLD
        ):
            raise _Drop(f"household cap reached in {space_id}")
        if (
            await self._reports.count_pending_in_space(space_id)
            >= MAX_PENDING_PER_SPACE
        ):
            raise _Drop(f"space {space_id} report queue is full")
        return space_id, subject, origin

    async def _require_bound_reporter(self, reporter: str, sender: str) -> None:
        """A household-level report names a reporter of the SENDING
        household — nobody files in another household's user's name."""
        if await self._users.get_instance_for_user(reporter) != sender:
            raise _Drop(f"reporter {reporter} is not a user of the sender")

    async def _live_space(self, space_id: str) -> Space:
        if self._space_repo is None:
            raise KeyError(f"space {space_id!r} not found")
        space = await self._space_repo.get(space_id)
        if space is None or space.dissolved:
            raise KeyError(f"space {space_id!r} not found")
        return space

    async def _is_space_member(self, space_id: str, user_id: str) -> bool:
        """``user_id`` holds a live seat in the space — local or remote."""
        if self._space_repo is None or not user_id:
            return False
        if await self._space_repo.get_member(space_id, user_id) is not None:
            return True
        if self._seats is None:
            return False
        seat = await self._seats.get_including_tombstones(space_id, "", user_id)
        return seat is not None and not seat.tombstoned

    async def _reviews_here(self, space: Space) -> bool:
        """This household hosts the space, or one of its own people holds
        a content-authority seat in it."""
        if space.owner_instance_id == self._own_instance_id:
            return True
        assert self._space_repo is not None
        return any(
            m.role in CONTENT_AUTHORITY_ROLES
            for m in await self._space_repo.list_members(space.id)
        )

    # ── Federation helpers ─────────────────────────────────────────────

    async def _maybe_federate(self, report: ContentReport) -> bool:
        """Send ``SPACE_REPORT`` to the households that triage it.

        Space-scoped → the host + every v_45 household with a live admin /
        moderator seat (never a plain member household), routed with the
        space id. Household-level → the household hosting the target.

        Returns ``True`` iff at least one event was dispatched.
        """
        if self._federation is None:
            return False
        if report.space_id is not None:
            peers = await self._v45_reviewers(
                report.space_id, subject=await self._subject(report)
            )
        else:
            peers = [
                t
                for t in await self._resolve_target_instances(
                    report.target_type, report.target_id
                )
                if t != self._own_instance_id
            ]
        if not peers:
            return False
        return await self._send_report(report, peers) > 0

    async def _send_report(self, report: ContentReport, peers: list[str]) -> int:
        """One ``SPACE_REPORT`` per peer; returns how many went out."""
        assert self._federation is not None
        payload: dict = {
            "target_type": report.target_type.value,
            "target_id": report.target_id,
            "category": report.category.value,
            "notes": report.notes,
            "reporter_user_id": report.reporter_user_id,
            "occurred_at": report.created_at.isoformat(),
        }
        if report.space_id is not None:
            # Also inside the sealed payload: a mesh-relayed envelope
            # carries no routing field.
            payload["space_id"] = report.space_id
            # The reporter's own household — what the receiver keys its
            # per-household cap on when the HOST relays the report.
            payload["reporter_instance_id"] = (
                report.reporter_instance_id or self._own_instance_id
            )
        dispatched = 0
        for peer in peers:
            try:
                if report.space_id is not None:
                    result = await self._federation.send_with_mesh_fallback(
                        to_instance_id=peer,
                        event_type=FederationEventType.SPACE_REPORT,
                        payload=payload,
                        space_id=report.space_id,
                    )
                    if not result.ok:
                        log.warning(
                            "report %s did not reach reviewer household %s (%s)",
                            report.id,
                            peer,
                            result.error,
                        )
                        continue
                else:
                    await self._federation.send_event(
                        to_instance_id=peer,
                        event_type=FederationEventType.SPACE_REPORT,
                        payload=payload,
                    )
                dispatched += 1
            except Exception as exc:  # pragma: no cover
                log.warning(
                    "report federation failed for %s %s to %s: %s",
                    report.target_type.value,
                    report.target_id,
                    peer,
                    exc,
                )
        return dispatched

    async def resend_pending(self, space_id: str, instance_id: str) -> int:
        """Re-deliver a space's pending reports to a household that has
        just (again) become a reviewer — only from the host, only to a
        v_45 household. The receiver's dedupe makes a repeat harmless."""
        if (
            self._federation is None
            or self._space_repo is None
            or not instance_id
            or instance_id == self._own_instance_id
        ):
            return 0
        space = await self._space_repo.get(space_id)
        if space is None or space.owner_instance_id != self._own_instance_id:
            return 0
        if not await self._federation.peer_supports(
            instance_id, min_version=FederationCapability.MIN_FOR_SPACE_REPORT_SCOPE
        ):
            return 0
        sent = 0
        for report in await self._reports.list_by_status(
            ReportStatus.PENDING, space_id=space_id
        ):
            if await self._only_subject_reviews(
                space_id, instance_id, await self._subject(report)
            ):
                continue
            sent += await self._send_report(report, [instance_id])
        if sent:
            log.info(
                "reports: re-delivered %d pending report(s) of space %s to %s",
                sent,
                space_id,
                instance_id,
            )
        return sent

    async def reviewer_households(self, space_id: str) -> list[str]:
        """The host first, then every household with a live admin /
        moderator seat — each once, never this household."""
        if self._space_repo is None:
            return []
        space = await self._space_repo.get(space_id)
        if space is None:
            return []
        seated = (
            await self._seats.list_instances_with_roles(space_id, REVIEWER_SEATS)
            if self._seats is not None
            else []
        )
        out: list[str] = []
        for iid in (space.owner_instance_id, *seated):
            if iid and iid != self._own_instance_id and iid not in out:
                out.append(iid)
        return out

    async def _v45_reviewers(
        self, space_id: str, *, subject: str | None = None
    ) -> list[str]:
        """:meth:`reviewer_households` that triage space reports (v_45+) —
        minus a household whose only reviewer is the report's ``subject``
        (a report about X never lands where only X could read it)."""
        assert self._federation is not None
        out: list[str] = []
        for iid in await self.reviewer_households(space_id):
            if await self._only_subject_reviews(space_id, iid, subject):
                log.info(
                    "report: household %s reviews space %s only through the "
                    "report's subject — not sent",
                    iid,
                    space_id,
                )
                continue
            if await self._federation.peer_supports(
                iid, min_version=FederationCapability.MIN_FOR_SPACE_REPORT_SCOPE
            ):
                out.append(iid)
            else:
                log.info(
                    "report: reviewer household %s of space %s is below v_45 — "
                    "not sent the report",
                    iid,
                    space_id,
                )
        return out

    async def _only_subject_reviews(
        self, space_id: str, instance_id: str, subject: str | None
    ) -> bool:
        """Every live admin / moderator seat ``instance_id`` holds in the
        space belongs to ``subject`` (the host is never skipped: it stands
        for the owner and hosts the space)."""
        if not subject or self._seats is None or self._space_repo is None:
            return False
        space = await self._space_repo.get(space_id)
        if space is None or space.owner_instance_id == instance_id:
            return False
        reviewers = [
            seat
            for seat in await self._seats.list_for_instance(
                space_id, instance_id, include_tombstoned=False
            )
            if seat.role in REVIEWER_SEATS
        ]
        return bool(reviewers) and all(r.user_id == subject for r in reviewers)

    async def _send_decided(
        self, report: ContentReport, *, decided_by: str, dismissed: bool
    ) -> None:
        """Tell the other v_45 reviewer households (``SPACE_REPORT_DECIDED``)."""
        if self._federation is None or report.space_id is None:
            return
        payload = {
            "space_id": report.space_id,
            "target_type": report.target_type.value,
            "target_id": report.target_id,
            "reporter_user_id": report.reporter_user_id,
            "decision": (
                ReportStatus.DISMISSED if dismissed else ReportStatus.RESOLVED
            ).value,
            "decided_by": decided_by,
            "decided_at": datetime.now(timezone.utc).isoformat(),
        }
        for iid in await self._v45_reviewers(report.space_id):
            result = await self._federation.send_with_mesh_fallback(
                to_instance_id=iid,
                event_type=FederationEventType.SPACE_REPORT_DECIDED,
                payload=payload,
                space_id=report.space_id,
            )
            if not result.ok:
                log.warning(
                    "report decision %s did not reach %s (%s)",
                    report.id,
                    iid,
                    result.error,
                )

    async def apply_remote_decision(
        self,
        *,
        space_id: str,
        target_type: str,
        target_id: str,
        reporter_user_id: str,
        decision: str,
        decided_by: str,
    ) -> bool:
        """Apply another reviewer household's verdict (the inbound handler
        verified the decider's content-authority seat on the sender).

        The first decision wins: a report already decided here — or a
        replay — changes nothing. A decision by the report's subject is
        refused. Returns ``True`` iff a pending report was decided."""
        try:
            tt = ReportTargetType(target_type)
            status = ReportStatus(decision)
        except ValueError:
            return False
        if status is ReportStatus.PENDING:
            return False
        report = await self._reports.find_by_key(
            space_id=space_id,
            target_type=tt,
            target_id=target_id,
            reporter_user_id=reporter_user_id,
        )
        if report is None or report.status is not ReportStatus.PENDING:
            return False
        if await self._subject(report) == decided_by:
            log.warning(
                "SPACE_REPORT_DECIDED in %s refused: %s decided a report about "
                "themself",
                space_id,
                decided_by,
            )
            return False
        await self._decide(
            report, decided_by, dismissed=status is ReportStatus.DISMISSED
        )
        return True

    async def forward_to_gfs(self, report: ContentReport) -> bool:
        """Forward a fraud report to every active paired GFS in the
        background. Returns ``True`` iff something is being forwarded.

        The GFS gets the target (the space for public / global space
        content, the household of a reported user, a space itself), the
        category and this household's signature — never the reporter or
        the notes. Content inside a private / household space, and feed
        content, never goes: the GFS does not learn such a space exists.
        """
        if self._gfs_connection_service is None or self._signing_key is None:
            return False
        target = await self._gfs_target(report)
        if target is None:
            return False
        try:
            connections = await self._gfs_connection_service.list_connections()
        except Exception:  # pragma: no cover
            return False
        # ``list_connections`` also returns pending/suspended connections
        # (so the UI can surface them) — only forward to GFS that have
        # actually accepted this household.
        active = [c for c in connections if c.status == "active"]
        if not active:
            return False
        asyncio.create_task(self._send_to_gfs(active, target, report))
        return True

    async def _gfs_target(self, report: ContentReport) -> tuple[str, str] | None:
        tt = report.target_type
        if report.space_id is not None:
            if tt not in (ReportTargetType.POST, ReportTargetType.COMMENT):
                return None
            if not await self._is_public_space(report.space_id):
                return None
            return "space", report.space_id
        if tt is ReportTargetType.SPACE:
            if self._space_repo is not None and not await self._is_public_space(
                report.target_id
            ):
                return None
            return "space", report.target_id
        if tt is ReportTargetType.USER:
            instance = await self._users.get_instance_for_user(report.target_id)
            return ("instance", instance) if instance else None
        return None  # feed content: nothing the GFS hosts

    async def _send_to_gfs(
        self, connections: list, target: tuple[str, str], report: ContentReport
    ) -> None:
        """Background: one signed report per GFS. Never raises."""
        assert self._gfs_connection_service is not None
        assert self._signing_key is not None
        for conn in connections:
            try:
                await self._gfs_connection_service.report_fraud(
                    conn.id,
                    target_type=target[0],
                    target_id=target[1],
                    category=report.category.value,
                    notes=None,
                    reporter_instance_id=self._own_instance_id,
                    reporter_user_id=None,
                    signing_key=self._signing_key,
                )
            except Exception as exc:  # pragma: no cover
                log.debug("GFS forward to %s failed: %s", conn.id, exc)

    async def _is_public_space(self, space_id: str) -> bool:
        if self._space_repo is None:
            return False
        space = await self._space_repo.get(space_id)
        return space is not None and space.space_type in PUBLIC_SPACE_TIERS

    async def _resolve_target_instances(
        self,
        tt: ReportTargetType,
        target_id: str,
    ) -> list[str]:
        """The households a HOUSEHOLD-LEVEL report goes to: a user's own
        household; for a space, its host + every member household."""
        out: list[str] = []
        if tt is ReportTargetType.USER:
            instance = await self._users.get_instance_for_user(target_id)
            if instance:
                out.append(instance)
        elif tt is ReportTargetType.SPACE and self._space_repo is not None:
            space = await self._space_repo.get(target_id)
            if space is not None:
                out.append(space.owner_instance_id)
                try:
                    out.extend(await self._space_repo.list_member_instances(target_id))
                except Exception:
                    log.warning("report: member households of %s unknown", target_id)
        seen: set[str] = set()
        deduped: list[str] = []
        for inst in out:
            if inst and inst not in seen:
                seen.add(inst)
                deduped.append(inst)
        return deduped

    # ── Triage: household admins (household-level reports) ────────────

    async def list_pending(self, *, actor_username: str) -> list[ContentReport]:
        """Pending HOUSEHOLD-LEVEL reports. A space's reports are its
        content authority's (:meth:`review_space`), never listed here."""
        await self._require_admin(actor_username)
        return await self._reports.list_by_status(ReportStatus.PENDING)

    async def resolve(
        self,
        report_id: str,
        *,
        actor_username: str,
        dismissed: bool = False,
    ) -> None:
        actor = await self._require_admin(actor_username)
        existing = await self._reports.get(report_id)
        if existing is None or existing.space_id is not None:
            # A space report is not the household admin's to see.
            raise KeyError(f"report {report_id!r} not found")
        await self._decide(existing, actor.user_id, dismissed=dismissed)

    async def _require_admin(self, username: str):
        user = await self._users.get(username)
        if user is None:
            raise KeyError(f"user {username!r} not found")
        if not getattr(user, "is_admin", False):
            raise PermissionError("household admin required")
        return user

    # ── Triage: a space's content authority (space-scoped reports) ────

    async def review_space(
        self, space_id: str, *, actor_user_id: str
    ) -> list[SpaceReportView]:
        """The space's pending reports as ``actor`` may review them
        (owner / admin / moderator): never one about the actor — save the
        sole-authority owner, who sees it anonymised, dismiss-only."""
        await self._require_content_authority(space_id, actor_user_id)
        reports = await self._reports.list_by_status(
            ReportStatus.PENDING, space_id=space_id
        )
        out: list[SpaceReportView] = []
        sole_owner: bool | None = None
        for r in reports:
            scope = (
                await self._scope.of(r.target_type, r.target_id)
                if r.target_type in CONTENT_TARGETS
                else None
            )
            subject = (
                r.target_id
                if r.target_type is ReportTargetType.USER
                else (scope.author if scope is not None else None)
            )
            anonymous = False
            if subject == actor_user_id:
                if r.sole_reviewer_user_id != actor_user_id:
                    continue  # other authority existed when it was filed
                if sole_owner is None:
                    sole_owner = await self._is_sole_owner(space_id, actor_user_id)
                if not sole_owner:
                    continue
                anonymous = True
                # Nothing that could name the reporter: not who, not their
                # words.
                r = dataclasses.replace(
                    r, reporter_user_id="", reporter_instance_id=None, notes=None
                )
            if scope is None:
                gone = not await self._is_space_member(space_id, r.target_id)
                preview = None
            else:
                # Moved to another space (or household-level) reads as gone.
                gone = not scope.found or scope.gone or scope.space_id != space_id
                preview = None if gone else scope.preview
            out.append(
                SpaceReportView(
                    report=r,
                    preview=preview,
                    gone=gone,
                    anonymous=anonymous,
                    dismiss_only=anonymous,
                )
            )
        return out

    async def list_for_space(
        self, space_id: str, *, actor_user_id: str
    ) -> list[ContentReport]:
        """The reports :meth:`review_space` shows ``actor``."""
        return [
            v.report
            for v in await self.review_space(space_id, actor_user_id=actor_user_id)
        ]

    async def resolve_in_space(
        self,
        space_id: str,
        report_id: str,
        *,
        actor_user_id: str,
        dismissed: bool = False,
    ) -> ContentReport:
        """Mark one of the space's reports resolved / dismissed, and tell
        the other reviewer households. A report of another space, a
        household one, or one about the actor is 404 here (the
        sole-authority owner may dismiss one about themself)."""
        await self._require_content_authority(space_id, actor_user_id)
        existing = await self._reports.get(report_id)
        if existing is None or existing.space_id != space_id:
            raise KeyError(f"report {report_id!r} not found")
        if await self._subject(existing) == actor_user_id:
            if existing.sole_reviewer_user_id != actor_user_id or not (
                await self._is_sole_owner(space_id, actor_user_id)
            ):
                raise KeyError(f"report {report_id!r} not found")
            if not dismissed:
                raise SpacePermissionError(
                    "a report about yourself can only be dismissed"
                )
        await self._decide(existing, actor_user_id, dismissed=dismissed)
        await self._send_decided(
            existing, decided_by=actor_user_id, dismissed=dismissed
        )
        return existing

    async def _subject(self, report: ContentReport) -> str | None:
        """Whom a report is about: the reported member, or the author /
        creator of the reported item (``None`` when unknown)."""
        if report.target_type is ReportTargetType.USER:
            return report.target_id
        if report.target_type not in CONTENT_TARGETS:
            return None
        return (await self._scope.of(report.target_type, report.target_id)).author

    async def _sole_reviewer(
        self, space_id: str | None, subject: str | None
    ) -> str | None:
        """The subject, if — right now, at filing — they are the space's
        owner and its only content authority (the anonymous-dismiss
        fallback is pinned to that moment)."""
        if space_id is None or not subject or self._space_repo is None:
            return None
        return subject if await self._is_sole_owner(space_id, subject) else None

    async def _is_sole_owner(self, space_id: str, user_id: str) -> bool:
        """``user_id`` owns the space and nobody else anywhere holds content
        authority in it — no other local owner / admin / moderator, no
        remote admin / moderator seat."""
        assert self._space_repo is not None
        members = await self._space_repo.list_members(space_id)
        me = next((m for m in members if m.user_id == user_id), None)
        if me is None or me.role != SpaceRole.OWNER:
            return False
        if any(
            m.role in CONTENT_AUTHORITY_ROLES and m.user_id != user_id for m in members
        ):
            return False
        if self._seats is not None and await self._seats.list_instances_with_roles(
            space_id, REVIEWER_SEATS
        ):
            return False
        return True

    async def _require_content_authority(self, space_id: str, user_id: str) -> None:
        space = await self._live_space(space_id)
        assert self._space_repo is not None
        member = await self._space_repo.get_member(space.id, user_id)
        if member is None or member.role not in CONTENT_AUTHORITY_ROLES:
            raise SpacePermissionError(
                "only the space owner, admins and moderators can review reports"
            )

    async def display_names(self, space_id: str, user_ids: set[str]) -> dict[str, str]:
        """Names for the people a space's report rows mention — a local
        user's display name, else the remote seat's."""
        out: dict[str, str] = {}
        for uid in user_ids:
            if not uid:
                continue
            user = await self._users.get_by_user_id(uid)
            if user is not None:
                out[uid] = user.display_name or user.username
                continue
            if self._seats is not None:
                seat = await self._seats.get_including_tombstones(space_id, "", uid)
                if seat is not None and seat.display_name:
                    out[uid] = seat.display_name
        return out

    async def _decide(
        self, existing: ContentReport, actor_user_id: str, *, dismissed: bool
    ) -> None:
        if existing.status is not ReportStatus.PENDING:
            raise ModerationAlreadyDecidedError(
                f"report {existing.id!r} is already {existing.status.value}",
            )
        status = ReportStatus.DISMISSED if dismissed else ReportStatus.RESOLVED
        await self._reports.resolve(
            existing.id,
            resolved_by=actor_user_id,
            status=status,
        )
        await self._bus.publish(
            ReportResolved(
                report_id=existing.id,
                resolved_by=actor_user_id,
                space_id=existing.space_id,
            )
        )
