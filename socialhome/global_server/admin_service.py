"""GFS admin operations (accept / reject / ban / policy / branding / reports).

One service class that aggregates the admin-portal business logic. Routes
in :mod:`server` are thin JSON shells over these methods. Every public
method writes an audit-log row via the admin repo so the admin sees
exactly what happened in the portal's Audit tab.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import asdict
from typing import TYPE_CHECKING

from ..crypto import b64url_decode, verify_ed25519
from .domain import GfsAppeal, GfsFraudReport

if TYPE_CHECKING:
    from .federation import GfsFederationService
    from .repositories import (
        AbstractGfsAdminRepo,
        AbstractGfsFederationRepo,
    )


log = logging.getLogger(__name__)


#: Household reporters hard-capped at this many fraud reports per 24 h
#: (secondary anti-flood beyond the one-per-target UNIQUE index).
MAX_REPORTS_PER_REPORTER_PER_DAY: int = 100


class GfsAdminService:
    """Admin-portal operations surface."""

    __slots__ = (
        "_fed_repo",
        "_admin_repo",
        "_federation",
        "_fraud_threshold",
        "_cluster",
    )

    def __init__(
        self,
        *,
        fed_repo: "AbstractGfsFederationRepo",
        admin_repo: "AbstractGfsAdminRepo",
        federation: "GfsFederationService",
        fraud_threshold: int = 5,
    ) -> None:
        self._fed_repo = fed_repo
        self._admin_repo = admin_repo
        self._federation = federation
        self._fraud_threshold = fraud_threshold
        self._cluster = None  # attached in GfsApp

    def attach_cluster(self, cluster) -> None:
        """Wire the :class:`ClusterService` for NODE_SYNC_REPORT fan-out.

        Called once both services are built. Non-cluster deployments
        leave this as ``None`` — ``sync_report`` is a no-op when
        cluster is disabled.
        """
        self._cluster = cluster

    # ── Overview ──────────────────────────────────────────────────────

    async def overview(self) -> dict:
        clients_active = len(await self._fed_repo.list_instances(status="active"))
        clients_pending = len(await self._fed_repo.list_instances(status="pending"))
        spaces_active = len(await self._fed_repo.list_spaces(status="active"))
        spaces_pending = len(await self._fed_repo.list_spaces(status="pending"))
        open_reports = len(await self._admin_repo.list_fraud_reports(status="pending"))
        return {
            "clients": {"active": clients_active, "pending": clients_pending},
            "spaces": {"active": spaces_active, "pending": spaces_pending},
            "open_reports": open_reports,
        }

    # ── Clients ───────────────────────────────────────────────────────

    async def list_clients(
        self,
        *,
        status: str | None = None,
        admin_ip: str | None = None,
    ) -> list[dict]:
        items = await self._fed_repo.list_instances(status=status)
        return [asdict(c) for c in items]

    async def accept_client(self, instance_id: str, *, admin_ip: str) -> None:
        await self._fed_repo.set_instance_status(instance_id, "active")
        await self._log("accept_client", "instance", instance_id, admin_ip)

    async def reject_client(self, instance_id: str, *, admin_ip: str) -> None:
        await self._fed_repo.delete_instance(instance_id)
        await self._log("reject_client", "instance", instance_id, admin_ip)

    async def ban_client(
        self,
        instance_id: str,
        *,
        reason: str | None,
        admin_ip: str,
    ) -> None:
        await self._fed_repo.set_instance_status(instance_id, "banned")
        # Side effects (spec §24.9): remove all the instance's spaces.
        owned = await self._fed_repo.list_spaces_for_instance(instance_id)
        for sp in owned:
            await self._fed_repo.set_space_status(sp.space_id, "banned")
        await self._log(
            "ban_client",
            "instance",
            instance_id,
            admin_ip,
            {"reason": reason, "removed_spaces": [s.space_id for s in owned]},
        )

    async def unban_client(self, instance_id: str, *, admin_ip: str) -> None:
        await self._fed_repo.set_instance_status(instance_id, "pending")
        await self._log("unban_client", "instance", instance_id, admin_ip)

    # ── Spaces ────────────────────────────────────────────────────────

    async def list_spaces(
        self,
        *,
        status: str | None = None,
    ) -> list[dict]:
        items = await self._fed_repo.list_spaces(status=status)
        return [asdict(s) for s in items]

    async def accept_space(self, space_id: str, *, admin_ip: str) -> None:
        await self._fed_repo.set_space_status(space_id, "active")
        await self._log("accept_space", "space", space_id, admin_ip)

    async def reject_space(self, space_id: str, *, admin_ip: str) -> None:
        await self._fed_repo.delete_space(space_id)
        await self._log("reject_space", "space", space_id, admin_ip)

    async def ban_space(
        self,
        space_id: str,
        *,
        reason: str | None,
        admin_ip: str,
    ) -> None:
        await self._fed_repo.set_space_status(space_id, "banned")
        await self._log(
            "ban_space",
            "space",
            space_id,
            admin_ip,
            {"reason": reason},
        )

    async def unban_space(self, space_id: str, *, admin_ip: str) -> None:
        await self._fed_repo.set_space_status(space_id, "pending")
        await self._log("unban_space", "space", space_id, admin_ip)

    # ── Policy ────────────────────────────────────────────────────────

    async def get_policy(self) -> dict:
        cfg = await self._admin_repo.get_configs(
            [
                "auto_accept_clients",
                "auto_accept_spaces",
                "fraud_threshold",
            ]
        )
        return {
            "auto_accept_clients": cfg.get("auto_accept_clients", "1") == "1",
            "auto_accept_spaces": cfg.get("auto_accept_spaces", "0") == "1",
            "fraud_threshold": int(cfg.get("fraud_threshold", "5")),
        }

    async def set_policy(
        self,
        *,
        auto_accept_clients: bool | None = None,
        auto_accept_spaces: bool | None = None,
        fraud_threshold: int | None = None,
        admin_ip: str,
    ) -> dict:
        if auto_accept_clients is not None:
            await self._admin_repo.set_config(
                "auto_accept_clients",
                "1" if auto_accept_clients else "0",
            )
        if auto_accept_spaces is not None:
            await self._admin_repo.set_config(
                "auto_accept_spaces",
                "1" if auto_accept_spaces else "0",
            )
        if fraud_threshold is not None:
            clean = max(1, int(fraud_threshold))
            await self._admin_repo.set_config("fraud_threshold", str(clean))
            self._fraud_threshold = clean
        await self._log("set_policy", None, None, admin_ip)
        return await self.get_policy()

    # ── Branding ──────────────────────────────────────────────────────

    async def get_branding(self) -> dict:
        cfg = await self._admin_repo.get_configs(
            [
                "server_name",
                "landing_markdown",
                "header_image_file",
            ]
        )
        return {
            "server_name": cfg.get("server_name") or "My Global Server",
            "landing_markdown": cfg.get("landing_markdown") or "",
            "header_image_file": cfg.get("header_image_file") or "",
        }

    async def set_branding(
        self,
        *,
        server_name: str | None = None,
        landing_markdown: str | None = None,
        header_image_file: str | None = None,
        admin_ip: str,
    ) -> dict:
        if server_name is not None:
            await self._admin_repo.set_config("server_name", server_name)
        if landing_markdown is not None:
            await self._admin_repo.set_config("landing_markdown", landing_markdown)
        if header_image_file is not None:
            await self._admin_repo.set_config("header_image_file", header_image_file)
        await self._log("set_branding", None, None, admin_ip)
        return await self.get_branding()

    # ── Fraud reports ─────────────────────────────────────────────────

    async def record_fraud_report(
        self,
        *,
        target_type: str,
        target_id: str,
        category: str,
        notes: str | None,
        reporter_instance_id: str,
        reporter_user_id: str | None,
        signed_body: bytes,
        signature: str,
    ) -> tuple[bool, bool]:
        """Persist an inbound fraud report. Returns ``(was_new, auto_banned)``.

        Expects the caller (``_handle_report`` in server.py) to have already
        verified the Ed25519 signature against the reporter's public key
        (duplicates the existing publish-path pattern).
        """
        # Secondary anti-flood: reporter cap per rolling 24h.
        since = int(time.time()) - 86400
        recent = await self._admin_repo.count_reports_by_reporter(
            reporter_instance_id,
            since=since,
        )
        if recent >= MAX_REPORTS_PER_REPORTER_PER_DAY:
            log.warning(
                "GFS fraud report rate-limited for reporter=%s (%d in 24h)",
                reporter_instance_id,
                recent,
            )
            return False, False

        report = GfsFraudReport(
            id=uuid.uuid4().hex,
            target_type=target_type,
            target_id=target_id,
            category=category,
            notes=(notes or "").strip() or None,
            reporter_instance_id=reporter_instance_id,
            reporter_user_id=reporter_user_id,
            status="pending",
            created_at=int(time.time()),
        )
        saved = await self._admin_repo.save_fraud_report(report)
        if not saved:
            return False, False

        # Phase Z — fan the new row out to every cluster peer so the
        # threshold count aggregates cluster-wide.
        if self._cluster is not None:
            try:
                await self._cluster.sync_report(report)
            except Exception:  # pragma: no cover
                log.debug("cluster sync_report failed", exc_info=True)

        # Threshold check — count distinct reporters for this target.
        distinct = await self._admin_repo.count_reporters_for_target(
            target_type,
            target_id,
        )
        auto_banned = False
        if distinct >= self._fraud_threshold:
            if target_type == "space":
                await self._fed_repo.set_space_status(target_id, "banned")
            elif target_type == "instance":
                await self._fed_repo.set_instance_status(target_id, "banned")
            await self._admin_repo.mark_pending_reports_acted(
                target_type,
                target_id,
                reviewed_by="auto",
            )
            auto_banned = True
            await self._log(
                "auto_ban_by_threshold",
                target_type,
                target_id,
                admin_ip="auto",
                metadata={
                    "threshold": self._fraud_threshold,
                    "distinct_reporters": distinct,
                },
            )
        return True, auto_banned

    async def list_fraud_reports(
        self,
        *,
        status: str | None = None,
    ) -> list[dict]:
        items = await self._admin_repo.list_fraud_reports(status=status)
        return [asdict(r) for r in items]

    async def review_fraud_report(
        self,
        report_id: str,
        *,
        action: str,  # 'dismiss' | 'ban_target' | 'ban_instance'
        admin_ip: str,
    ) -> dict:
        report = await self._admin_repo.get_fraud_report(report_id)
        if report is None:
            raise KeyError(f"report {report_id!r} not found")
        if report.status != "pending":
            from ..domain.space import ModerationAlreadyDecidedError

            raise ModerationAlreadyDecidedError(
                f"report {report_id!r} is already {report.status}",
            )

        if action == "dismiss":
            await self._admin_repo.set_fraud_report_status(
                report_id,
                status="dismissed",
                reviewed_by="admin",
            )
        elif action == "ban_target":
            if report.target_type == "space":
                await self._fed_repo.set_space_status(report.target_id, "banned")
            else:
                await self._fed_repo.set_instance_status(report.target_id, "banned")
            await self._admin_repo.mark_pending_reports_acted(
                report.target_type,
                report.target_id,
                reviewed_by="admin",
            )
        elif action == "ban_instance":
            # Look up the space owner; ban the owning household.
            if report.target_type == "space":
                space = await self._fed_repo.get_space(report.target_id)
                if space is None:
                    raise KeyError(
                        f"space {report.target_id!r} not found",
                    )
                await self.ban_client(
                    space.owning_instance,
                    reason="fraud-report",
                    admin_ip=admin_ip,
                )
            else:
                await self.ban_client(
                    report.target_id,
                    reason="fraud-report",
                    admin_ip=admin_ip,
                )
            await self._admin_repo.mark_pending_reports_acted(
                report.target_type,
                report.target_id,
                reviewed_by="admin",
            )
        else:
            raise ValueError(f"invalid action: {action!r}")

        await self._log(
            "review_fraud_report",
            "report",
            report_id,
            admin_ip,
            {
                "action": action,
                "target_type": report.target_type,
                "target_id": report.target_id,
            },
        )
        return {"id": report_id, "action": action}

    # ── Appeals ───────────────────────────────────────────────────────

    async def record_appeal(
        self,
        *,
        target_type: str,
        target_id: str,
        message: str,
    ) -> GfsAppeal:
        appeal = GfsAppeal(
            id=uuid.uuid4().hex,
            target_type=target_type,
            target_id=target_id,
            message=message.strip(),
            status="pending",
            created_at=int(time.time()),
        )
        await self._admin_repo.save_appeal(appeal)
        return appeal

    async def list_appeals(self, *, status: str | None = None) -> list[dict]:
        items = await self._admin_repo.list_appeals(status=status)
        return [asdict(a) for a in items]

    async def decide_appeal(
        self,
        appeal_id: str,
        *,
        action: str,
        admin_ip: str,
    ) -> dict:
        appeal = await self._admin_repo.get_appeal(appeal_id)
        if appeal is None:
            raise KeyError(f"appeal {appeal_id!r} not found")
        if action == "lift":
            if appeal.target_type == "space":
                await self.unban_space(appeal.target_id, admin_ip=admin_ip)
            else:
                await self.unban_client(appeal.target_id, admin_ip=admin_ip)
            await self._admin_repo.set_appeal_status(
                appeal_id,
                status="lifted",
                decided_by="admin",
            )
        elif action == "dismiss":
            await self._admin_repo.set_appeal_status(
                appeal_id,
                status="dismissed",
                decided_by="admin",
            )
        else:
            raise ValueError(f"invalid action: {action!r}")
        await self._log(
            "decide_appeal",
            "appeal",
            appeal_id,
            admin_ip,
            {"action": action},
        )
        return {"id": appeal_id, "action": action}

    # ── Audit log ─────────────────────────────────────────────────────

    async def list_audit_log(
        self,
        *,
        action: str | None = None,
        since: int | None = None,
        limit: int = 200,
    ) -> list[dict]:
        return await self._admin_repo.list_admin_actions(
            action=action,
            since=since,
            limit=limit,
        )

    # ── Internals ─────────────────────────────────────────────────────

    async def _log(
        self,
        action: str,
        target_type: str | None,
        target_id: str | None,
        admin_ip: str | None,
        metadata: dict | None = None,
    ) -> None:
        await self._admin_repo.log_admin_action(
            action=action,
            target_type=target_type,
            target_id=target_id,
            metadata=metadata or {},
            admin_ip=admin_ip,
        )


# ─── Ed25519 signature verification for inbound GFS_FRAUD_REPORT ───────


def verify_report_signature(
    body_without_sig: dict,
    signature: str,
    public_key_hex: str,
) -> bool:
    """Verify the Ed25519 signature over the canonical-JSON payload.

    Mirrors the publish-path verification in :mod:`federation`. Called by
    the ``/gfs/report`` route after it looks up the reporter's public
    key in ``client_instances``.
    """
    import json

    canonical = json.dumps(
        body_without_sig,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    try:
        raw_key = bytes.fromhex(public_key_hex)
        raw_sig = b64url_decode(signature)
    except ValueError, TypeError:
        return False
    return verify_ed25519(raw_key, canonical, raw_sig)
