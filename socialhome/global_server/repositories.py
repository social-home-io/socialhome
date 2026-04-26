"""GFS repositories — data access for the Global Federation Server.

One module; all repos share the same :class:`AsyncDatabase`.

Split into three protocols:
- :class:`AbstractGfsFederationRepo` — core relay state
  (`client_instances` / `global_spaces` / `space_subscribers`).
- :class:`AbstractGfsAdminRepo` — admin-portal bookkeeping
  (`server_config`, `admin_sessions`, `admin_login_attempts`,
  `admin_audit_log`, `gfs_fraud_reports`, `gfs_appeals`,
  `gfs_pair_tokens`, `gfs_invite_tokens`).
- :class:`AbstractClusterRepo` — node registry + health.

Each concrete class is a thin wrapper over SQL. Services do the
business logic.
"""

from __future__ import annotations

import json
from typing import Any, Protocol, runtime_checkable

from ..db import AsyncDatabase
from .domain import (
    AdminSession,
    ClientInstance,
    ClusterNode,
    GfsAppeal,
    GfsFraudReport,
    GfsSubscriber,
    GlobalSpace,
    RtcConnection,
)


# ─── Federation repo ─────────────────────────────────────────────────────


@runtime_checkable
class AbstractGfsFederationRepo(Protocol):
    # Instances
    async def upsert_instance(self, instance: ClientInstance) -> None: ...
    async def get_instance(self, instance_id: str) -> ClientInstance | None: ...
    async def list_instances(
        self,
        *,
        status: str | None = None,
    ) -> list[ClientInstance]: ...
    async def set_instance_status(
        self,
        instance_id: str,
        status: str,
    ) -> None: ...
    async def delete_instance(self, instance_id: str) -> None: ...

    # Spaces
    async def upsert_space(self, space: GlobalSpace) -> None: ...
    async def get_space(self, space_id: str) -> GlobalSpace | None: ...
    async def list_spaces(
        self,
        *,
        status: str | None = None,
    ) -> list[GlobalSpace]: ...
    async def set_space_status(self, space_id: str, status: str) -> None: ...
    async def delete_space(self, space_id: str) -> None: ...
    async def list_spaces_for_instance(
        self,
        instance_id: str,
    ) -> list[GlobalSpace]: ...

    # Subscribers
    async def add_subscriber(
        self,
        *,
        space_id: str,
        instance_id: str,
    ) -> None: ...
    async def remove_subscriber(
        self,
        *,
        space_id: str,
        instance_id: str,
    ) -> None: ...
    async def list_subscribers(
        self,
        space_id: str,
        *,
        exclude: str = "",
    ) -> list[GfsSubscriber]: ...

    # RTC transport state (spec §24.12)
    async def upsert_rtc_connection(
        self,
        instance_id: str,
        *,
        transport: str,
    ) -> None: ...
    async def get_rtc_connection(
        self,
        instance_id: str,
    ) -> RtcConnection | None: ...


class SqliteGfsFederationRepo:
    """SQLite-backed :class:`AbstractGfsFederationRepo`."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    # ── Instances ──────────────────────────────────────────────────────

    async def upsert_instance(self, instance: ClientInstance) -> None:
        await self._db.enqueue(
            """
            INSERT INTO client_instances(
                instance_id, display_name, public_key, inbox_url,
                status, auto_accept, connected_at
            ) VALUES(?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')))
            ON CONFLICT(instance_id) DO UPDATE SET
                display_name = excluded.display_name,
                public_key   = excluded.public_key,
                inbox_url = excluded.inbox_url,
                status       = excluded.status,
                auto_accept  = excluded.auto_accept
            """,
            (
                instance.instance_id,
                instance.display_name,
                instance.public_key,
                instance.inbox_url,
                instance.status,
                int(instance.auto_accept),
                instance.connected_at or None,
            ),
        )

    async def get_instance(self, instance_id: str) -> ClientInstance | None:
        row = await self._db.fetchone(
            "SELECT * FROM client_instances WHERE instance_id=?",
            (instance_id,),
        )
        return _row_to_instance(_to_dict(row))

    async def list_instances(
        self,
        *,
        status: str | None = None,
    ) -> list[ClientInstance]:
        if status is None:
            rows = await self._db.fetchall(
                "SELECT * FROM client_instances ORDER BY connected_at DESC",
            )
        else:
            rows = await self._db.fetchall(
                "SELECT * FROM client_instances WHERE status=? "
                "ORDER BY connected_at DESC",
                (status,),
            )
        return [i for i in (_row_to_instance(_to_dict(r)) for r in rows) if i]

    async def set_instance_status(
        self,
        instance_id: str,
        status: str,
    ) -> None:
        await self._db.enqueue(
            "UPDATE client_instances SET status=? WHERE instance_id=?",
            (status, instance_id),
        )

    async def delete_instance(self, instance_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM client_instances WHERE instance_id=?",
            (instance_id,),
        )

    # ── Spaces ─────────────────────────────────────────────────────────

    async def upsert_space(self, space: GlobalSpace) -> None:
        await self._db.enqueue(
            """
            INSERT INTO global_spaces(
                space_id, owning_instance, name, description, about_markdown,
                cover_url, min_age, target_audience, accent_color,
                status, subscriber_count, posts_per_week, published_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                     COALESCE(?, datetime('now')))
            ON CONFLICT(space_id) DO UPDATE SET
                name = excluded.name,
                description = excluded.description,
                about_markdown = excluded.about_markdown,
                cover_url = excluded.cover_url,
                min_age = excluded.min_age,
                target_audience = excluded.target_audience,
                accent_color = excluded.accent_color,
                status = excluded.status,
                subscriber_count = excluded.subscriber_count,
                posts_per_week = excluded.posts_per_week
            """,
            (
                space.space_id,
                space.owning_instance,
                space.name,
                space.description,
                space.about_markdown,
                space.cover_url,
                space.min_age,
                space.target_audience,
                space.accent_color,
                space.status,
                space.subscriber_count,
                space.posts_per_week,
                space.published_at or None,
            ),
        )

    async def get_space(self, space_id: str) -> GlobalSpace | None:
        row = await self._db.fetchone(
            "SELECT * FROM global_spaces WHERE space_id=?",
            (space_id,),
        )
        return _row_to_space(_to_dict(row))

    async def list_spaces(
        self,
        *,
        status: str | None = None,
    ) -> list[GlobalSpace]:
        if status is None:
            rows = await self._db.fetchall(
                "SELECT * FROM global_spaces ORDER BY published_at DESC",
            )
        else:
            rows = await self._db.fetchall(
                "SELECT * FROM global_spaces WHERE status=? ORDER BY published_at DESC",
                (status,),
            )
        return [s for s in (_row_to_space(_to_dict(r)) for r in rows) if s]

    async def set_space_status(self, space_id: str, status: str) -> None:
        await self._db.enqueue(
            "UPDATE global_spaces SET status=? WHERE space_id=?",
            (status, space_id),
        )

    async def delete_space(self, space_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM global_spaces WHERE space_id=?",
            (space_id,),
        )

    async def list_spaces_for_instance(
        self,
        instance_id: str,
    ) -> list[GlobalSpace]:
        rows = await self._db.fetchall(
            "SELECT * FROM global_spaces WHERE owning_instance=? "
            "ORDER BY published_at DESC",
            (instance_id,),
        )
        return [s for s in (_row_to_space(_to_dict(r)) for r in rows) if s]

    # ── Subscribers ────────────────────────────────────────────────────

    async def add_subscriber(
        self,
        *,
        space_id: str,
        instance_id: str,
    ) -> None:
        await self._db.enqueue(
            "INSERT OR IGNORE INTO space_subscribers(space_id, instance_id) "
            "VALUES(?, ?)",
            (space_id, instance_id),
        )
        await self._db.enqueue(
            "UPDATE global_spaces SET subscriber_count = "
            "(SELECT COUNT(*) FROM space_subscribers WHERE space_id=?) "
            "WHERE space_id=?",
            (space_id, space_id),
        )

    async def remove_subscriber(
        self,
        *,
        space_id: str,
        instance_id: str,
    ) -> None:
        await self._db.enqueue(
            "DELETE FROM space_subscribers WHERE space_id=? AND instance_id=?",
            (space_id, instance_id),
        )
        await self._db.enqueue(
            "UPDATE global_spaces SET subscriber_count = "
            "(SELECT COUNT(*) FROM space_subscribers WHERE space_id=?) "
            "WHERE space_id=?",
            (space_id, space_id),
        )

    async def list_subscribers(
        self,
        space_id: str,
        *,
        exclude: str = "",
    ) -> list[GfsSubscriber]:
        rows = await self._db.fetchall(
            """
            SELECT ci.instance_id, ci.inbox_url
            FROM space_subscribers ss
            JOIN client_instances ci USING (instance_id)
            WHERE ss.space_id = ? AND ci.instance_id != ?
              AND ci.status = 'active'
            """,
            (space_id, exclude),
        )
        return [
            GfsSubscriber(
                instance_id=r["instance_id"],
                inbox_url=r["inbox_url"],
            )
            for r in rows
        ]

    # ── RTC transport state (spec §24.12) ──────────────────────────────

    async def upsert_rtc_connection(
        self,
        instance_id: str,
        *,
        transport: str,
    ) -> None:
        await self._db.enqueue(
            """
            INSERT INTO rtc_connections(instance_id, transport)
            VALUES(?, ?)
            ON CONFLICT(instance_id) DO UPDATE SET
                transport    = excluded.transport,
                last_ping_at = datetime('now')
            """,
            (instance_id, transport),
        )

    async def get_rtc_connection(
        self,
        instance_id: str,
    ) -> RtcConnection | None:
        row = await self._db.fetchone(
            "SELECT * FROM rtc_connections WHERE instance_id=?",
            (instance_id,),
        )
        r = _to_dict(row)
        if r is None:
            return None
        return RtcConnection(
            instance_id=r["instance_id"],
            transport=r["transport"],
            connected_at=r["connected_at"],
            last_ping_at=r["last_ping_at"],
        )


# ─── Admin repo ──────────────────────────────────────────────────────────


@runtime_checkable
class AbstractGfsAdminRepo(Protocol):
    # server_config K/V
    async def get_config(self, key: str) -> str | None: ...
    async def set_config(self, key: str, value: str) -> None: ...
    async def get_configs(self, keys: list[str]) -> dict[str, str]: ...

    # Admin sessions
    async def create_session(self, token: str, expires_at: int) -> None: ...
    async def get_session(self, token: str) -> AdminSession | None: ...
    async def delete_session(self, token: str) -> None: ...
    async def purge_expired_sessions(self, now: int) -> None: ...

    # Brute-force tracking
    async def record_login_attempt(self, ip: str) -> None: ...
    async def count_failed_attempts(self, ip: str, since: int) -> int: ...

    # Audit log
    async def log_admin_action(
        self,
        *,
        action: str,
        target_type: str | None,
        target_id: str | None,
        metadata: dict,
        admin_ip: str | None,
    ) -> None: ...
    async def list_admin_actions(
        self,
        *,
        action: str | None = None,
        since: int | None = None,
        limit: int = 200,
    ) -> list[dict]: ...

    # Fraud reports
    async def save_fraud_report(self, report: GfsFraudReport) -> bool: ...
    async def get_fraud_report(self, report_id: str) -> GfsFraudReport | None: ...
    async def list_fraud_reports(
        self,
        *,
        status: str | None = None,
        limit: int = 500,
    ) -> list[GfsFraudReport]: ...
    async def count_reporters_for_target(
        self,
        target_type: str,
        target_id: str,
    ) -> int: ...
    async def set_fraud_report_status(
        self,
        report_id: str,
        status: str,
        reviewed_by: str,
    ) -> None: ...
    async def mark_pending_reports_acted(
        self,
        target_type: str,
        target_id: str,
        reviewed_by: str,
    ) -> None: ...
    async def count_reports_by_reporter(
        self,
        reporter_instance_id: str,
        since: int,
    ) -> int: ...

    # Appeals
    async def save_appeal(self, appeal: GfsAppeal) -> None: ...
    async def get_appeal(self, appeal_id: str) -> GfsAppeal | None: ...
    async def list_appeals(
        self,
        *,
        status: str | None = None,
    ) -> list[GfsAppeal]: ...
    async def set_appeal_status(
        self,
        appeal_id: str,
        status: str,
        decided_by: str,
    ) -> None: ...

    # Pair tokens
    async def save_pair_token(self, token: str, ip: str) -> None: ...
    async def consume_pair_token(self, token: str) -> bool: ...
    async def count_pair_tokens(self, ip: str, since: int) -> int: ...


class SqliteGfsAdminRepo:
    """SQLite-backed :class:`AbstractGfsAdminRepo`."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    # ── server_config ──────────────────────────────────────────────────

    async def get_config(self, key: str) -> str | None:
        row = await self._db.fetchone(
            "SELECT value FROM server_config WHERE key=?",
            (key,),
        )
        return row["value"] if row else None

    async def set_config(self, key: str, value: str) -> None:
        await self._db.enqueue(
            "INSERT INTO server_config(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    async def get_configs(self, keys: list[str]) -> dict[str, str]:
        if not keys:
            return {}
        placeholders = ",".join("?" * len(keys))
        rows = await self._db.fetchall(
            f"SELECT key, value FROM server_config WHERE key IN ({placeholders})",
            tuple(keys),
        )
        return {r["key"]: r["value"] for r in rows}

    # ── Sessions ───────────────────────────────────────────────────────

    async def create_session(self, token: str, expires_at: int) -> None:
        await self._db.enqueue(
            "INSERT INTO admin_sessions(token, expires_at) VALUES(?, ?)",
            (token, expires_at),
        )

    async def get_session(self, token: str) -> AdminSession | None:
        row = await self._db.fetchone(
            "SELECT * FROM admin_sessions WHERE token=?",
            (token,),
        )
        if row is None:
            return None
        return AdminSession(
            token=row["token"],
            expires_at=int(row["expires_at"]),
            created_at=int(row["created_at"]),
        )

    async def delete_session(self, token: str) -> None:
        await self._db.enqueue(
            "DELETE FROM admin_sessions WHERE token=?",
            (token,),
        )

    async def purge_expired_sessions(self, now: int) -> None:
        await self._db.enqueue(
            "DELETE FROM admin_sessions WHERE expires_at < ?",
            (now,),
        )

    # ── Brute-force tracking ──────────────────────────────────────────

    async def record_login_attempt(self, ip: str) -> None:
        await self._db.enqueue(
            "INSERT INTO admin_login_attempts(ip) VALUES(?)",
            (ip,),
        )

    async def count_failed_attempts(self, ip: str, since: int) -> int:
        return int(
            await self._db.fetchval(
                "SELECT COUNT(*) FROM admin_login_attempts "
                "WHERE ip=? AND attempted_at >= ?",
                (ip, since),
                default=0,
            )
        )

    # ── Audit log ─────────────────────────────────────────────────────

    async def log_admin_action(
        self,
        *,
        action: str,
        target_type: str | None,
        target_id: str | None,
        metadata: dict,
        admin_ip: str | None,
    ) -> None:
        await self._db.enqueue(
            """
            INSERT INTO admin_audit_log(
                action, target_type, target_id, metadata_json, admin_ip
            ) VALUES(?, ?, ?, ?, ?)
            """,
            (action, target_type, target_id, json.dumps(metadata or {}), admin_ip),
        )

    async def list_admin_actions(
        self,
        *,
        action: str | None = None,
        since: int | None = None,
        limit: int = 200,
    ) -> list[dict]:
        clauses = []
        params: list[Any] = []
        if action:
            clauses.append("action=?")
            params.append(action)
        if since:
            clauses.append("created_at >= ?")
            params.append(since)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(int(limit))
        rows = await self._db.fetchall(
            f"SELECT * FROM admin_audit_log{where} ORDER BY created_at DESC LIMIT ?",
            tuple(params),
        )
        return [
            {
                "id": r["id"],
                "action": r["action"],
                "target_type": r["target_type"],
                "target_id": r["target_id"],
                "metadata": _safe_json(r["metadata_json"]),
                "admin_ip": r["admin_ip"],
                "created_at": int(r["created_at"]),
            }
            for r in rows
        ]

    # ── Fraud reports ─────────────────────────────────────────────────

    async def save_fraud_report(self, report: GfsFraudReport) -> bool:
        try:
            await self._db.enqueue(
                """
                INSERT INTO gfs_fraud_reports(
                    id, target_type, target_id, category, notes,
                    reporter_instance_id, reporter_user_id, status,
                    created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report.id,
                    report.target_type,
                    report.target_id,
                    report.category,
                    report.notes,
                    report.reporter_instance_id,
                    report.reporter_user_id,
                    report.status,
                    int(report.created_at),
                ),
            )
            return True
        except Exception as exc:
            if "unique" in str(exc).lower() or "constraint" in str(exc).lower():
                return False
            raise

    async def get_fraud_report(
        self,
        report_id: str,
    ) -> GfsFraudReport | None:
        row = await self._db.fetchone(
            "SELECT * FROM gfs_fraud_reports WHERE id=?",
            (report_id,),
        )
        return _row_to_report(_to_dict(row))

    async def list_fraud_reports(
        self,
        *,
        status: str | None = None,
        limit: int = 500,
    ) -> list[GfsFraudReport]:
        if status is None:
            rows = await self._db.fetchall(
                "SELECT * FROM gfs_fraud_reports ORDER BY created_at DESC LIMIT ?",
                (int(limit),),
            )
        else:
            rows = await self._db.fetchall(
                "SELECT * FROM gfs_fraud_reports WHERE status=? "
                "ORDER BY created_at DESC LIMIT ?",
                (status, int(limit)),
            )
        return [r for r in (_row_to_report(_to_dict(row)) for row in rows) if r]

    async def count_reporters_for_target(
        self,
        target_type: str,
        target_id: str,
    ) -> int:
        return int(
            await self._db.fetchval(
                "SELECT COUNT(DISTINCT reporter_instance_id) "
                "FROM gfs_fraud_reports "
                "WHERE target_type=? AND target_id=? AND status='pending'",
                (target_type, target_id),
                default=0,
            )
        )

    async def set_fraud_report_status(
        self,
        report_id: str,
        status: str,
        reviewed_by: str,
    ) -> None:
        await self._db.enqueue(
            "UPDATE gfs_fraud_reports SET status=?, reviewed_by=?, "
            "reviewed_at=strftime('%s','now') WHERE id=?",
            (status, reviewed_by, report_id),
        )

    async def mark_pending_reports_acted(
        self,
        target_type: str,
        target_id: str,
        reviewed_by: str,
    ) -> None:
        await self._db.enqueue(
            "UPDATE gfs_fraud_reports SET status='acted', "
            "reviewed_by=?, reviewed_at=strftime('%s','now') "
            "WHERE target_type=? AND target_id=? AND status='pending'",
            (reviewed_by, target_type, target_id),
        )

    async def count_reports_by_reporter(
        self,
        reporter_instance_id: str,
        since: int,
    ) -> int:
        return int(
            await self._db.fetchval(
                "SELECT COUNT(*) FROM gfs_fraud_reports "
                "WHERE reporter_instance_id=? AND created_at >= ?",
                (reporter_instance_id, since),
                default=0,
            )
        )

    # ── Appeals ───────────────────────────────────────────────────────

    async def save_appeal(self, appeal: GfsAppeal) -> None:
        await self._db.enqueue(
            """
            INSERT INTO gfs_appeals(
                id, target_type, target_id, message, status, created_at
            ) VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                appeal.id,
                appeal.target_type,
                appeal.target_id,
                appeal.message,
                appeal.status,
                int(appeal.created_at),
            ),
        )

    async def get_appeal(self, appeal_id: str) -> GfsAppeal | None:
        row = await self._db.fetchone(
            "SELECT * FROM gfs_appeals WHERE id=?",
            (appeal_id,),
        )
        if row is None:
            return None
        return GfsAppeal(
            id=row["id"],
            target_type=row["target_type"],
            target_id=row["target_id"],
            message=row["message"] or "",
            status=row["status"],
            created_at=int(row["created_at"]),
            decided_at=int(row["decided_at"]) if row["decided_at"] else None,
            decided_by=row["decided_by"],
        )

    async def list_appeals(
        self,
        *,
        status: str | None = None,
    ) -> list[GfsAppeal]:
        if status is None:
            rows = await self._db.fetchall(
                "SELECT * FROM gfs_appeals ORDER BY created_at DESC",
            )
        else:
            rows = await self._db.fetchall(
                "SELECT * FROM gfs_appeals WHERE status=? ORDER BY created_at DESC",
                (status,),
            )
        return [
            GfsAppeal(
                id=r["id"],
                target_type=r["target_type"],
                target_id=r["target_id"],
                message=r["message"] or "",
                status=r["status"],
                created_at=int(r["created_at"]),
                decided_at=int(r["decided_at"]) if r["decided_at"] else None,
                decided_by=r["decided_by"],
            )
            for r in rows
        ]

    async def set_appeal_status(
        self,
        appeal_id: str,
        status: str,
        decided_by: str,
    ) -> None:
        await self._db.enqueue(
            "UPDATE gfs_appeals SET status=?, decided_by=?, "
            "decided_at=strftime('%s','now') WHERE id=?",
            (status, decided_by, appeal_id),
        )

    # ── Pair tokens ───────────────────────────────────────────────────

    async def save_pair_token(self, token: str, ip: str) -> None:
        await self._db.enqueue(
            "INSERT INTO gfs_pair_tokens(token, ip) VALUES(?, ?)",
            (token, ip),
        )

    async def consume_pair_token(self, token: str) -> bool:
        # Single-use + 10-min TTL: accept only if unused and < 600s old.
        row = await self._db.fetchone(
            "SELECT created_at, consumed_at FROM gfs_pair_tokens WHERE token=?",
            (token,),
        )
        if row is None or row["consumed_at"] is not None:
            return False
        import time

        if int(time.time()) - int(row["created_at"]) > 600:
            return False
        await self._db.enqueue(
            "UPDATE gfs_pair_tokens SET consumed_at=strftime('%s','now') WHERE token=?",
            (token,),
        )
        return True

    async def count_pair_tokens(self, ip: str, since: int) -> int:
        return int(
            await self._db.fetchval(
                "SELECT COUNT(*) FROM gfs_pair_tokens WHERE ip=? AND created_at >= ?",
                (ip, since),
                default=0,
            )
        )


# ─── Cluster repo (unchanged surface) ────────────────────────────────────


@runtime_checkable
class AbstractClusterRepo(Protocol):
    async def upsert_node(self, node: ClusterNode) -> None: ...
    async def list_nodes(self) -> list[ClusterNode]: ...
    async def remove_node(self, node_id: str) -> None: ...
    async def get_leader_id(self) -> str | None: ...
    async def update_active_sync_sessions(
        self,
        node_id: str,
        count: int,
    ) -> None: ...


class SqliteClusterRepo:
    """SQLite-backed :class:`AbstractClusterRepo`."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def upsert_node(self, node: ClusterNode) -> None:
        await self._db.enqueue(
            """
            INSERT INTO cluster_nodes(
                node_id, url, public_key, status, last_seen
            ) VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(node_id) DO UPDATE SET
                url=excluded.url,
                public_key=excluded.public_key,
                status=excluded.status,
                last_seen=excluded.last_seen
            """,
            (node.node_id, node.url, node.public_key, node.status, node.last_seen),
        )

    async def list_nodes(self) -> list[ClusterNode]:
        rows = await self._db.fetchall(
            "SELECT * FROM cluster_nodes ORDER BY added_at",
        )
        return [
            ClusterNode(
                node_id=r["node_id"],
                url=r["url"],
                public_key=r["public_key"] or "",
                status=r["status"],
                last_seen=r["last_seen"],
                added_at=r["added_at"],
                active_sync_sessions=int(r["active_sync_sessions"] or 0),
            )
            for r in rows
        ]

    async def remove_node(self, node_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM cluster_nodes WHERE node_id=?",
            (node_id,),
        )

    async def get_leader_id(self) -> str | None:
        """Return the first-registered node_id (v1 leader-election stub)."""
        row = await self._db.fetchone(
            "SELECT node_id FROM cluster_nodes ORDER BY added_at LIMIT 1",
        )
        return row["node_id"] if row else None

    async def update_active_sync_sessions(
        self,
        node_id: str,
        count: int,
    ) -> None:
        """Persist the per-node sync-signaling load (spec §24.10.7).

        Touches only ``active_sync_sessions`` — keeps the heartbeat /
        round-robin update path from clobbering ``status`` or
        ``last_seen``.
        """
        await self._db.enqueue(
            "UPDATE cluster_nodes SET active_sync_sessions=? WHERE node_id=?",
            (max(0, int(count)), node_id),
        )


# ─── Helpers ──────────────────────────────────────────────────────────────


def _to_dict(row) -> dict | None:
    if row is None:
        return None
    # aiosqlite rows already behave like dicts.
    if isinstance(row, dict):
        return row
    try:
        return dict(row)
    except TypeError:
        return None


def _safe_json(value) -> dict:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except ValueError, TypeError:
        return {}


def _row_to_instance(row: dict | None) -> ClientInstance | None:
    if row is None:
        return None
    return ClientInstance(
        instance_id=row["instance_id"],
        display_name=row.get("display_name", ""),
        public_key=row.get("public_key", ""),
        inbox_url=row.get("inbox_url", ""),
        status=row.get("status", "pending"),
        auto_accept=bool(row.get("auto_accept", 0)),
        connected_at=row.get("connected_at", ""),
    )


def _row_to_space(row: dict | None) -> GlobalSpace | None:
    if row is None:
        return None
    return GlobalSpace(
        space_id=row["space_id"],
        owning_instance=row.get("owning_instance", ""),
        name=row.get("name", ""),
        description=row.get("description"),
        about_markdown=row.get("about_markdown"),
        cover_url=row.get("cover_url"),
        min_age=int(row.get("min_age") or 0),
        target_audience=row.get("target_audience", "all"),
        accent_color=row.get("accent_color", "#6366f1"),
        status=row.get("status", "pending"),
        subscriber_count=int(row.get("subscriber_count") or 0),
        posts_per_week=float(row.get("posts_per_week") or 0.0),
        published_at=row.get("published_at", ""),
    )


def _row_to_report(row: dict | None) -> GfsFraudReport | None:
    if row is None:
        return None
    return GfsFraudReport(
        id=row["id"],
        target_type=row["target_type"],
        target_id=row["target_id"],
        category=row["category"],
        notes=row.get("notes"),
        reporter_instance_id=row["reporter_instance_id"],
        reporter_user_id=row.get("reporter_user_id"),
        status=row["status"],
        created_at=int(row["created_at"]),
        reviewed_by=row.get("reviewed_by"),
        reviewed_at=int(row["reviewed_at"]) if row.get("reviewed_at") else None,
    )
