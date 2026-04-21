"""Content-report service.

Any household member can file a report on a post, comment, user or
space. Household admins triage — they list pending reports and mark
each ``resolved`` or ``dismissed``.

Federation (§CP.R1): when a report targets content in a space, the
service fires a ``SPACE_REPORT`` federation event to the content's
owning instance **and** to every other instance that hosts a member of
that space (transitive delivery). This way all admins who share the
space — not just the author's household — see the report. Inbound
federated reports land via :meth:`create_report_from_remote`.

Rate limiting: a single reporter can file at most
:data:`MAX_REPORTS_PER_DAY` reports in a rolling 24 h window. A
``UNIQUE(reporter, target_type, target_id)`` schema constraint also
prevents the same pair twice.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..domain.events import ReportFiled, ReportResolved
from ..domain.federation import FederationEventType
from ..domain.report import (
    ContentReport,
    DuplicateReportError,
    ReportCategory,
    ReportRateLimitedError,
    ReportStatus,
    ReportTargetType,
)
from ..infrastructure.event_bus import EventBus
from ..repositories.report_repo import AbstractReportRepo
from ..repositories.user_repo import AbstractUserRepo

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..repositories.space_post_repo import AbstractSpacePostRepo
    from ..repositories.space_repo import AbstractSpaceRepo

log = logging.getLogger(__name__)


#: Per-reporter rolling-24h cap.
MAX_REPORTS_PER_DAY: int = 20


class ReportService:
    __slots__ = (
        "_reports",
        "_users",
        "_bus",
        "_federation",
        "_own_instance_id",
        "_space_repo",
        "_space_post_repo",
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
    ) -> None:
        self._reports = report_repo
        self._users = user_repo
        self._bus = bus
        self._space_repo = space_repo
        self._space_post_repo = space_post_repo
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

    async def create_report(
        self,
        *,
        reporter_user_id: str,
        target_type: str,
        target_id: str,
        category: str,
        notes: str | None = None,
        forward_gfs: bool = True,
    ) -> tuple[ContentReport, bool]:
        """File a new report. Returns ``(report, federated)`` where
        ``federated`` is true iff the report was mirrored to a directly-
        paired peer instance hosting the target.

        When ``forward_gfs`` is True (the default), the report is also
        auto-forwarded to every paired GFS via a background task so
        global-space fraud lands on the GFS admin portal without a
        manual "escalate" step.

        Raises :class:`DuplicateReportError` if the reporter already
        filed on the same target, or :class:`ReportRateLimitedError`
        if they're over the daily cap.
        """
        try:
            tt = ReportTargetType(target_type)
            cat = ReportCategory(category)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

        count = await self._reports.count_recent_by_reporter(
            reporter_user_id,
            hours=24,
        )
        if count >= MAX_REPORTS_PER_DAY:
            raise ReportRateLimitedError(
                f"report cap reached ({MAX_REPORTS_PER_DAY}/day)",
            )

        clean_notes = (notes or "").strip() or None
        report = ContentReport(
            id=uuid.uuid4().hex,
            target_type=tt,
            target_id=target_id,
            reporter_user_id=reporter_user_id,
            category=cat,
            notes=clean_notes,
            status=ReportStatus.PENDING,
            created_at=datetime.now(timezone.utc),
        )
        try:
            await self._reports.save(report)
        except Exception as exc:
            # The UNIQUE index surfaces as an IntegrityError-flavoured
            # exception from aiosqlite. Convert to a domain error.
            msg = str(exc).lower()
            if "unique" in msg or "constraint" in msg:
                raise DuplicateReportError(
                    f"already reported this {tt.value}",
                ) from exc
            raise
        await self._bus.publish(
            ReportFiled(
                report_id=report.id,
                target_type=tt.value,
                target_id=target_id,
                category=cat.value,
                reporter_user_id=reporter_user_id,
            )
        )

        federated = await self._maybe_federate(
            tt=tt,
            cat=cat,
            target_id=target_id,
            reporter_user_id=reporter_user_id,
            notes=clean_notes,
            occurred_at=report.created_at,
        )

        # Auto-forward to every paired GFS by default (opt-out via
        # forward_gfs=False). Background task — never blocks the caller.
        if forward_gfs and tt in (
            ReportTargetType.SPACE,
            ReportTargetType.USER,
            ReportTargetType.POST,
            ReportTargetType.COMMENT,
        ):
            import asyncio

            asyncio.create_task(
                self._forward_to_gfs(
                    tt=tt,
                    cat=cat,
                    target_id=target_id,
                    reporter_user_id=reporter_user_id,
                    notes=clean_notes,
                )
            )

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
    ) -> ContentReport | None:
        """Persist an inbound ``SPACE_REPORT`` federation event.

        Returns the new report on success, or ``None`` if the event was
        malformed or a duplicate (replay). Skips the per-reporter
        rate-limit — that cap is anti-flood for local users.
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
        report = ContentReport(
            id=uuid.uuid4().hex,
            target_type=tt,
            target_id=target_id,
            reporter_user_id=reporter_user_id,
            reporter_instance_id=reporter_instance_id,
            category=cat,
            notes=(notes or "").strip() or None,
            status=ReportStatus.PENDING,
            created_at=datetime.now(timezone.utc),
        )
        try:
            await self._reports.save(report)
        except Exception as exc:
            msg = str(exc).lower()
            if "unique" in msg or "constraint" in msg:
                # Replay / duplicate — harmless, ignore.
                return None
            raise
        await self._bus.publish(
            ReportFiled(
                report_id=report.id,
                target_type=tt.value,
                target_id=target_id,
                category=cat.value,
                reporter_user_id=reporter_user_id,
            )
        )
        return report

    # ── Federation helpers ─────────────────────────────────────────────

    async def _maybe_federate(
        self,
        *,
        tt: ReportTargetType,
        cat: ReportCategory,
        target_id: str,
        reporter_user_id: str,
        notes: str | None,
        occurred_at: datetime,
    ) -> bool:
        """If the target is hosted on paired peers, send SPACE_REPORT
        transitively to every instance that hosts a member of the space
        containing the target — so admins on all hosting households see
        the same report (spec §CP.R1).

        Returns ``True`` iff at least one event was dispatched.
        """
        if self._federation is None:
            return False
        targets = await self._resolve_target_instances(tt, target_id)
        peers = {t for t in targets if t and t != self._own_instance_id}
        if not peers:
            return False
        payload = {
            "target_type": tt.value,
            "target_id": target_id,
            "category": cat.value,
            "notes": notes,
            "reporter_user_id": reporter_user_id,
            "occurred_at": occurred_at.isoformat(),
        }
        dispatched = 0
        for peer in peers:
            try:
                await self._federation.send_event(
                    to_instance_id=peer,
                    event_type=FederationEventType.SPACE_REPORT,
                    payload=payload,
                )
                dispatched += 1
            except Exception as exc:  # pragma: no cover
                log.debug(
                    "report federation failed for target=%s %s to %s: %s",
                    tt.value,
                    target_id,
                    peer,
                    exc,
                )
        return dispatched > 0

    async def _forward_to_gfs(
        self,
        *,
        tt: ReportTargetType,
        cat: ReportCategory,
        target_id: str,
        reporter_user_id: str,
        notes: str | None,
    ) -> None:
        """Fan a fraud report out to every paired GFS.

        Runs in a background task — never raises. Failures are logged
        and dropped; the local report row is the source of truth.
        """
        if self._gfs_connection_service is None or self._signing_key is None:
            return
        try:
            connections = await self._gfs_connection_service.list_connections()
        except Exception:  # pragma: no cover
            return
        if not connections:
            return

        # Map report-target-type to the GFS enum (space | instance).
        if tt is ReportTargetType.SPACE:
            gfs_target_type = "space"
            gfs_target_id = target_id
        elif tt in (ReportTargetType.POST, ReportTargetType.COMMENT):
            # Resolve to the owning space for GFS's coarser target space.
            if self._space_post_repo is None:
                return
            if tt is ReportTargetType.POST:
                hit = await self._space_post_repo.get(target_id)
                if hit is None:
                    return
                gfs_target_id, _ = hit
            else:
                comment = await self._space_post_repo.get_comment(target_id)
                if comment is None:
                    return
                gfs_target_id = getattr(comment, "space_id", None) or getattr(
                    comment, "post_id", ""
                )
            if not gfs_target_id:
                return
            gfs_target_type = "space"
        else:  # USER
            instance = await self._users.get_instance_for_user(target_id)
            if not instance:
                return
            gfs_target_type = "instance"
            gfs_target_id = instance

        for conn in connections:
            try:
                await self._gfs_connection_service.report_fraud(
                    conn.id,
                    target_type=gfs_target_type,
                    target_id=gfs_target_id,
                    category=cat.value,
                    notes=notes,
                    reporter_instance_id=self._own_instance_id,
                    reporter_user_id=reporter_user_id,
                    signing_key=self._signing_key,
                )
            except Exception as exc:  # pragma: no cover
                log.debug("GFS forward to %s failed: %s", conn.id, exc)

    async def _resolve_target_instance(
        self,
        tt: ReportTargetType,
        target_id: str,
    ) -> str | None:
        """Return the primary hosting instance id for this target.

        For space-scoped content this is the author / owner; transitive
        fan-out to the rest of the space's instances is handled by
        :meth:`_resolve_target_instances`.
        """
        if tt is ReportTargetType.POST and self._space_post_repo is not None:
            hit = await self._space_post_repo.get(target_id)
            if hit is None:
                return None
            _, post = hit
            return await self._users.get_instance_for_user(post.author)
        if tt is ReportTargetType.COMMENT and self._space_post_repo is not None:
            comment = await self._space_post_repo.get_comment(target_id)
            if comment is None:
                return None
            return await self._users.get_instance_for_user(comment.author)
        if tt is ReportTargetType.SPACE and self._space_repo is not None:
            space = await self._space_repo.get(target_id)
            if space is None:
                return None
            return space.owner_instance_id
        if tt is ReportTargetType.USER:
            return await self._users.get_instance_for_user(target_id)
        return None

    async def _resolve_target_instances(
        self,
        tt: ReportTargetType,
        target_id: str,
    ) -> list[str]:
        """Return every instance that should see a ``SPACE_REPORT`` for
        this target (§CP.R1 transitive delivery).

        For space-scoped targets that means: the owner instance +
        every distinct instance hosting a member of the space.
        """
        out: list[str] = []
        primary = await self._resolve_target_instance(tt, target_id)
        if primary:
            out.append(primary)

        space_id: str | None = None
        if tt is ReportTargetType.SPACE:
            space_id = target_id
        elif tt is ReportTargetType.POST and self._space_post_repo is not None:
            hit = await self._space_post_repo.get(target_id)
            if hit is not None:
                space_id = hit[0]
        elif tt is ReportTargetType.COMMENT and self._space_post_repo is not None:
            comment = await self._space_post_repo.get_comment(target_id)
            if comment is not None:
                parent = await self._space_post_repo.get(comment.post_id)
                if parent is not None:
                    space_id = parent[0]

        if space_id and self._space_repo is not None:
            try:
                member_instances = await self._space_repo.list_member_instances(
                    space_id
                )
            except Exception:
                member_instances = []
            out.extend(member_instances)
        # De-dupe while preserving order.
        seen: set[str] = set()
        deduped: list[str] = []
        for inst in out:
            if inst and inst not in seen:
                seen.add(inst)
                deduped.append(inst)
        return deduped

    async def list_pending(self, *, actor_username: str) -> list[ContentReport]:
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
        if existing is None:
            raise KeyError(f"report {report_id!r} not found")
        if existing.status is not ReportStatus.PENDING:
            from ..domain.space import ModerationAlreadyDecidedError

            raise ModerationAlreadyDecidedError(
                f"report {report_id!r} is already {existing.status.value}",
            )
        status = ReportStatus.DISMISSED if dismissed else ReportStatus.RESOLVED
        await self._reports.resolve(
            report_id,
            resolved_by=actor.user_id,
            status=status,
        )
        await self._bus.publish(
            ReportResolved(
                report_id=report_id,
                resolved_by=actor.user_id,
            )
        )

    async def _require_admin(self, username: str):
        user = await self._users.get(username)
        if user is None:
            raise KeyError(f"user {username!r} not found")
        if not getattr(user, "is_admin", False):
            raise PermissionError("household admin required")
        return user
