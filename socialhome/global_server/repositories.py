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

import time
from typing import Any, Protocol, runtime_checkable

import orjson

from ..db import AsyncDatabase
from .domain import (
    AdminSession,
    ClientInstance,
    ClusterNode,
    GfsAppeal,
    GfsFraudReport,
    GfsHighlightPublication,
    GfsHighlightToken,
    GfsMomentFollow,
    GfsSubscriber,
    GfsSubscriberWithKeys,
    GfsUserPicture,
    GfsUserRegistration,
    GlobalSpace,
    RtcConnection,
)

# Retention for the brute-force counter table. ``admin_login_attempts`` is
# only ever read as a count within a short ``since`` window (the 15-minute
# BRUTE_FORCE_WINDOW_SECONDS in admin.py), so anything older is dead weight.
# 24h comfortably exceeds any rate-limit window; rows are pruned on write,
# keeping the table from growing without bound under repeated failed logins.
_ADMIN_LOGIN_ATTEMPT_RETENTION_SECONDS = 86400

# Retention for ``gfs_pair_tokens``. Tokens are single-use with a 10-min TTL
# and only counted within a short rate-limit window, so a row older than this
# is dead weight. 24h comfortably exceeds the TTL; the GFS maintenance loop
# prunes anything older to keep the table bounded.
PAIR_TOKEN_RETENTION_SECONDS = 86400


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
    async def set_instance_display_name(
        self,
        instance_id: str,
        display_name: str,
    ) -> None: ...
    async def delete_instance(self, instance_id: str) -> None: ...

    # Spaces
    async def upsert_space(self, space: GlobalSpace) -> None: ...
    async def get_space(self, space_id: str) -> GlobalSpace | None: ...
    async def list_spaces(
        self,
        *,
        status: str | None = None,
        include_withdrawn: bool = False,
    ) -> list[GlobalSpace]: ...
    async def set_space_status(self, space_id: str, status: str) -> None: ...
    async def set_space_withdrawn(
        self,
        space_id: str,
        withdrawn: bool,
    ) -> None: ...
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
    async def list_subscribers_with_keys(
        self,
        space_id: str,
    ) -> list[GfsSubscriberWithKeys]: ...

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
                status, auto_accept, connected_at,
                keywrap_public_key, kem_suite, keywrap_sig
            ) VALUES(?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')), ?, ?, ?)
            ON CONFLICT(instance_id) DO UPDATE SET
                display_name = excluded.display_name,
                public_key   = excluded.public_key,
                inbox_url = excluded.inbox_url,
                status       = excluded.status,
                auto_accept  = excluded.auto_accept,
                keywrap_public_key = excluded.keywrap_public_key,
                kem_suite          = excluded.kem_suite,
                keywrap_sig        = excluded.keywrap_sig
            """,
            (
                instance.instance_id,
                instance.display_name,
                instance.public_key,
                instance.inbox_url,
                instance.status,
                int(instance.auto_accept),
                instance.connected_at or None,
                instance.keywrap_public_key or None,
                instance.kem_suite or None,
                instance.keywrap_sig or None,
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

    async def set_instance_display_name(
        self,
        instance_id: str,
        display_name: str,
    ) -> None:
        await self._db.enqueue(
            "UPDATE client_instances SET display_name=? WHERE instance_id=?",
            (display_name, instance_id),
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
                cover_url, icon_url, min_age, category, accent_color,
                primary_color, status, subscriber_count, posts_per_week,
                published_at, identity_public_key, withdrawn
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                     COALESCE(?, datetime('now')), ?, ?)
            ON CONFLICT(space_id) DO UPDATE SET
                name = excluded.name,
                description = excluded.description,
                about_markdown = excluded.about_markdown,
                cover_url = excluded.cover_url,
                icon_url = excluded.icon_url,
                min_age = excluded.min_age,
                category = excluded.category,
                accent_color = excluded.accent_color,
                primary_color = excluded.primary_color,
                status = excluded.status,
                subscriber_count = excluded.subscriber_count,
                posts_per_week = excluded.posts_per_week,
                -- The TOFU-pinned space authority key is immutable once
                -- set (see the pin comment in ``federation.publish_space``).
                -- Enforced here rather than trusting every caller: a write
                -- carrying an empty pin (e.g. a cluster NODE_SYNC_SPACE
                -- rebuilt from a partial wire shape) must not wipe it and
                -- downgrade the space to owner-only relay.
                identity_public_key = COALESCE(
                    NULLIF(excluded.identity_public_key, ''),
                    global_spaces.identity_public_key
                ),
                withdrawn = excluded.withdrawn
            """,
            (
                space.space_id,
                space.owning_instance,
                space.name,
                space.description,
                space.about_markdown,
                space.cover_url,
                space.icon_url,
                space.min_age,
                space.category,
                space.accent_color,
                space.primary_color,
                space.status,
                space.subscriber_count,
                space.posts_per_week,
                space.published_at or None,
                space.identity_public_key or None,
                1 if space.withdrawn else 0,
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
        include_withdrawn: bool = False,
    ) -> list[GlobalSpace]:
        # ``withdrawn`` rows are excluded by default (and ONLY here + the
        # public detail/SSR reads): this is the discovery listing.
        # ``get_space`` deliberately still returns them — ``publish_space``
        # needs the row for its owner-immutability and TOFU-pin checks, and
        # hiding it would make a re-publish look like a first publish.
        #
        # ``include_withdrawn=True`` is the MODERATOR read (admin console +
        # overview counts): withdrawal is discoverability-only, so an owner
        # who delists a reported space must not thereby put it out of reach
        # of a ban while the relay keeps fanning content to subscribers.
        withdrawn_clause = "" if include_withdrawn else " AND withdrawn=0"
        if status is None:
            rows = await self._db.fetchall(
                f"SELECT * FROM global_spaces WHERE 1=1{withdrawn_clause} "
                "ORDER BY published_at DESC",
            )
        else:
            rows = await self._db.fetchall(
                f"SELECT * FROM global_spaces WHERE status=?{withdrawn_clause} "
                "ORDER BY published_at DESC",
                (status,),
            )
        return [s for s in (_row_to_space(_to_dict(r)) for r in rows) if s]

    async def set_space_status(self, space_id: str, status: str) -> None:
        await self._db.enqueue(
            "UPDATE global_spaces SET status=? WHERE space_id=?",
            (status, space_id),
        )

    async def set_space_withdrawn(self, space_id: str, withdrawn: bool) -> None:
        """Flip the OWNER-withdrawal flag, touching no other column.

        Targeted on purpose: the previous unpublish path round-tripped the
        whole row through :meth:`upsert_space` and silently dropped every
        field it forgot to copy (``icon_url`` / ``primary_color``).
        """
        await self._db.enqueue(
            "UPDATE global_spaces SET withdrawn=? WHERE space_id=?",
            (1 if withdrawn else 0, space_id),
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

    async def list_subscribers_with_keys(
        self,
        space_id: str,
    ) -> list[GfsSubscriberWithKeys]:
        """Subscribers of *space_id* joined with their registered identity +
        key-wrap pubkeys (Phase-5b-c reconcile). Only the publicly-registered
        key material is returned (no inbox URL) — a verified seed-holder uses it
        to re-seal the content key. A subscriber that registered no key-wrap key
        surfaces with empty key-wrap fields (the caller skips sealing to it)."""
        rows = await self._db.fetchall(
            """
            SELECT ci.instance_id        AS instance_id,
                   ci.public_key         AS identity_public_key,
                   ci.keywrap_public_key AS keywrap_public_key,
                   ci.keywrap_sig        AS keywrap_sig
            FROM space_subscribers ss
            JOIN client_instances ci USING (instance_id)
            WHERE ss.space_id = ?
              AND ci.status = 'active'
            """,
            (space_id,),
        )
        return [
            GfsSubscriberWithKeys(
                instance_id=r["instance_id"],
                identity_public_key=r["identity_public_key"] or "",
                keywrap_public_key=r["keywrap_public_key"] or "",
                keywrap_sig=r["keywrap_sig"] or "",
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
    async def prune_old_pair_tokens(self, cutoff: int) -> int: ...


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
        # Prune-on-write: GFS has no recurring cleanup scheduler, and this
        # table is purely a short-window rate-limit counter, so drop rows
        # older than the retention window to keep it bounded.
        now_seconds = int(time.time())
        await self._db.enqueue(
            "DELETE FROM admin_login_attempts WHERE attempted_at < ?",
            (now_seconds - _ADMIN_LOGIN_ATTEMPT_RETENTION_SECONDS,),
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
            (
                action,
                target_type,
                target_id,
                orjson.dumps(metadata or {}).decode(),
                admin_ip,
            ),
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

    async def prune_old_pair_tokens(self, cutoff: int) -> int:
        """Delete pair tokens created before ``cutoff`` (unix seconds).

        Tokens are single-use with a 10-min TTL and only counted within a
        short rate-limit window, so older rows are dead weight.
        """

        def _run(conn) -> int:
            cur = conn.execute(
                "DELETE FROM gfs_pair_tokens WHERE created_at < ?",
                (cutoff,),
            )
            return int(cur.rowcount or 0)

        return await self._db.transact(_run)


# ─── Cluster repo (unchanged surface) ────────────────────────────────────


@runtime_checkable
class AbstractClusterRepo(Protocol):
    async def upsert_node(self, node: ClusterNode) -> None: ...
    async def list_nodes(self) -> list[ClusterNode]: ...
    async def remove_node(self, node_id: str) -> None: ...
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
        parsed = orjson.loads(value)
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
        keywrap_public_key=row.get("keywrap_public_key") or "",
        kem_suite=row.get("kem_suite") or "",
        keywrap_sig=row.get("keywrap_sig") or "",
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
        icon_url=row.get("icon_url"),
        min_age=int(row.get("min_age") or 0),
        category=row.get("category", "general"),
        accent_color=row.get("accent_color", "#6366f1"),
        primary_color=row.get("primary_color") or "#6366f1",
        status=row.get("status", "pending"),
        subscriber_count=int(row.get("subscriber_count") or 0),
        posts_per_week=float(row.get("posts_per_week") or 0.0),
        published_at=row.get("published_at", ""),
        identity_public_key=row.get("identity_public_key") or "",
        withdrawn=bool(row.get("withdrawn") or 0),
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


# ─── Highlight publication repos (§highlights_public) ───────────────────────────


@runtime_checkable
class AbstractGfsHighlightPublicationRepo(Protocol):
    async def upsert(self, pub: GfsHighlightPublication) -> None: ...
    async def get(
        self,
        highlight_id: str,
        instance_id: str,
    ) -> GfsHighlightPublication | None: ...
    async def delete(self, highlight_id: str, instance_id: str) -> int: ...
    async def prune_expired(self, now: int) -> int: ...
    async def list_for_instance(
        self,
        instance_id: str,
    ) -> list[GfsHighlightPublication]: ...


class SqliteGfsHighlightPublicationRepo:
    """SQLite-backed :class:`AbstractGfsHighlightPublicationRepo`."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def upsert(self, pub: GfsHighlightPublication) -> None:
        # Re-publishing (e.g. to bump expires_at, or to swap GFSes)
        # overwrites the routing metadata in place — the GFS stores no
        # content, only the signed publish record.
        await self._db.enqueue(
            """
            INSERT INTO gfs_highlight_publications(
                highlight_id, instance_id, expires_at, published_at,
                publish_signature
            ) VALUES(?,?,?,?,?)
            ON CONFLICT(highlight_id, instance_id) DO UPDATE SET
                expires_at            = excluded.expires_at,
                published_at          = excluded.published_at,
                publish_signature     = excluded.publish_signature
            """,
            (
                pub.highlight_id,
                pub.instance_id,
                pub.expires_at,
                pub.published_at,
                pub.publish_signature,
            ),
        )

    async def get(
        self,
        highlight_id: str,
        instance_id: str,
    ) -> GfsHighlightPublication | None:
        row = await self._db.fetchone(
            "SELECT highlight_id, instance_id, expires_at, published_at, "
            "publish_signature "
            "FROM gfs_highlight_publications "
            "WHERE highlight_id=? AND instance_id=?",
            (highlight_id, instance_id),
        )
        return _row_to_highlight_pub(_as_dict(row))

    async def delete(self, highlight_id: str, instance_id: str) -> int:
        # ``rowcount`` lets the caller distinguish "no-op" from "deleted"
        # so the unpublish route can return 404 on stale tokens.
        def _run(conn) -> int:
            cur = conn.execute(
                "DELETE FROM gfs_highlight_publications WHERE highlight_id=? AND instance_id=?",
                (highlight_id, instance_id),
            )
            return int(cur.rowcount or 0)

        return await self._db.transact(_run)

    async def prune_expired(self, now: int) -> int:
        def _run(conn) -> int:
            cur = conn.execute(
                "DELETE FROM gfs_highlight_publications WHERE expires_at < ?",
                (now,),
            )
            return int(cur.rowcount or 0)

        return await self._db.transact(_run)

    async def list_for_instance(
        self,
        instance_id: str,
    ) -> list[GfsHighlightPublication]:
        rows = await self._db.fetchall(
            "SELECT highlight_id, instance_id, expires_at, published_at, "
            "publish_signature "
            "FROM gfs_highlight_publications "
            "WHERE instance_id=? ORDER BY published_at DESC",
            (instance_id,),
        )
        out: list[GfsHighlightPublication] = []
        for r in rows:
            pub = _row_to_highlight_pub(_as_dict(r))
            if pub is not None:
                out.append(pub)
        return out


@runtime_checkable
class AbstractGfsHighlightTokenRepo(Protocol):
    async def insert(self, token: GfsHighlightToken) -> None: ...
    async def lookup_active(
        self,
        token: str,
        *,
        now: int,
    ) -> tuple[GfsHighlightToken, GfsHighlightPublication] | None: ...
    async def list_for(
        self,
        highlight_id: str,
        instance_id: str,
    ) -> list[GfsHighlightToken]: ...
    async def revoke(
        self,
        token: str,
        instance_id: str,
        *,
        now: int,
    ) -> int: ...


class SqliteGfsHighlightTokenRepo:
    """SQLite-backed :class:`AbstractGfsHighlightTokenRepo`.

    ``lookup_active`` joins against ``gfs_highlight_publications`` so the
    public landing page can resolve a token in one round-trip and
    apply the parent's ``expires_at`` cap server-side.
    """

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def insert(self, token: GfsHighlightToken) -> None:
        await self._db.enqueue(
            """
            INSERT INTO gfs_highlight_tokens(
                token, highlight_id, instance_id, label, created_at, revoked_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                token.token,
                token.highlight_id,
                token.instance_id,
                token.label,
                token.created_at,
                token.revoked_at,
            ),
        )

    async def lookup_active(
        self,
        token: str,
        *,
        now: int,
    ) -> tuple[GfsHighlightToken, GfsHighlightPublication] | None:
        row = await self._db.fetchone(
            """
            SELECT t.token, t.highlight_id, t.instance_id, t.label,
                   t.created_at AS t_created_at, t.revoked_at AS t_revoked_at,
                   p.expires_at, p.published_at, p.publish_signature
              FROM gfs_highlight_tokens AS t
              JOIN gfs_highlight_publications AS p
                ON p.highlight_id = t.highlight_id AND p.instance_id = t.instance_id
             WHERE t.token = ?
               AND t.revoked_at IS NULL
               AND p.expires_at > ?
            """,
            (token, now),
        )
        if row is None:
            return None
        d = _as_dict(row)
        tok = GfsHighlightToken(
            token=d["token"],
            highlight_id=d["highlight_id"],
            instance_id=d["instance_id"],
            label=d.get("label"),
            created_at=int(d["t_created_at"]),
            revoked_at=(
                int(d["t_revoked_at"]) if d.get("t_revoked_at") is not None else None
            ),
        )
        pub = GfsHighlightPublication(
            highlight_id=d["highlight_id"],
            instance_id=d["instance_id"],
            expires_at=int(d["expires_at"]),
            published_at=int(d["published_at"]),
            publish_signature=d["publish_signature"],
        )
        return tok, pub

    async def list_for(
        self,
        highlight_id: str,
        instance_id: str,
    ) -> list[GfsHighlightToken]:
        rows = await self._db.fetchall(
            "SELECT token, highlight_id, instance_id, label, created_at, revoked_at "
            "FROM gfs_highlight_tokens "
            "WHERE highlight_id=? AND instance_id=? ORDER BY created_at DESC",
            (highlight_id, instance_id),
        )
        out: list[GfsHighlightToken] = []
        for r in rows:
            d = _as_dict(r)
            out.append(
                GfsHighlightToken(
                    token=d["token"],
                    highlight_id=d["highlight_id"],
                    instance_id=d["instance_id"],
                    label=d.get("label"),
                    created_at=int(d["created_at"]),
                    revoked_at=(
                        int(d["revoked_at"])
                        if d.get("revoked_at") is not None
                        else None
                    ),
                )
            )
        return out

    async def revoke(
        self,
        token: str,
        instance_id: str,
        *,
        now: int,
    ) -> int:
        # ``instance_id`` guard: only the owning instance can revoke its
        # own tokens, not whoever happens to know the token string.
        def _run(conn) -> int:
            cur = conn.execute(
                "UPDATE gfs_highlight_tokens SET revoked_at=? "
                "WHERE token=? AND instance_id=? AND revoked_at IS NULL",
                (now, token, instance_id),
            )
            return int(cur.rowcount or 0)

        return await self._db.transact(_run)


def _as_dict(row: Any) -> dict:
    if row is None:
        return {}
    if isinstance(row, dict):
        return row
    # sqlite3.Row supports keys() + indexing.
    try:
        return {k: row[k] for k in row.keys()}
    except Exception:
        return dict(row)


def _row_to_highlight_pub(row: dict) -> GfsHighlightPublication | None:
    if not row:
        return None
    return GfsHighlightPublication(
        highlight_id=row["highlight_id"],
        instance_id=row["instance_id"],
        expires_at=int(row["expires_at"]),
        published_at=int(row["published_at"]),
        publish_signature=row["publish_signature"],
    )


# ─── Public Momentum directory + follow graph (§Momentum-public) ─────────


@runtime_checkable
class AbstractGfsUserRegistrationRepo(Protocol):
    async def upsert(self, reg: GfsUserRegistration) -> None: ...
    async def delete(self, user_id: str) -> int: ...
    async def get(self, user_id: str) -> GfsUserRegistration | None: ...
    async def list_active(
        self, *, q: str | None = None, limit: int = 200
    ) -> list[GfsUserRegistration]: ...
    async def list_for_instance(
        self, instance_id: str
    ) -> list[GfsUserRegistration]: ...
    async def set_picture_digest(
        self, *, user_id: str, picture_digest: str | None
    ) -> int: ...


_USER_REG_COLS = (
    "user_id, instance_id, username, display_name, picture_url, bio, "
    "picture_digest, home_instance_pk, registered_at, status"
)


class SqliteGfsUserRegistrationRepo:
    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def upsert(self, reg: GfsUserRegistration) -> None:
        await self._db.enqueue(
            "INSERT INTO gfs_user_registrations("
            "user_id, instance_id, username, display_name, picture_url, bio, "
            "picture_digest, home_instance_pk, registered_at, status) "
            "VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "instance_id=excluded.instance_id, "
            "username=excluded.username, "
            "display_name=excluded.display_name, "
            "picture_url=excluded.picture_url, "
            "bio=excluded.bio, "
            "picture_digest=excluded.picture_digest, "
            "home_instance_pk=excluded.home_instance_pk, "
            "status=excluded.status",
            (
                reg.user_id,
                reg.instance_id,
                reg.username,
                reg.display_name,
                reg.picture_url,
                reg.bio,
                reg.picture_digest,
                reg.home_instance_pk,
                reg.registered_at,
                reg.status,
            ),
        )

    async def delete(self, user_id: str) -> int:
        def _run(conn) -> int:
            cur = conn.execute(
                "DELETE FROM gfs_user_registrations WHERE user_id=?",
                (user_id,),
            )
            return int(cur.rowcount or 0)

        return await self._db.transact(_run)

    async def get(self, user_id: str) -> GfsUserRegistration | None:
        row = await self._db.fetchone(
            f"SELECT {_USER_REG_COLS} FROM gfs_user_registrations WHERE user_id=?",
            (user_id,),
        )
        return _row_to_user_reg(_dict(row))

    async def list_active(
        self, *, q: str | None = None, limit: int = 200
    ) -> list[GfsUserRegistration]:
        # ``q`` is a substring match across display_name + username.
        # Caller-supplied; ``LIKE '%…%'`` is safe because the parameter
        # is bound, not interpolated.
        clamped = max(1, min(int(limit), 200))
        if q:
            like = f"%{q.strip()}%"
            rows = await self._db.fetchall(
                f"SELECT {_USER_REG_COLS} "
                "FROM gfs_user_registrations "
                "WHERE status='active' AND (display_name LIKE ? OR username LIKE ?) "
                "ORDER BY registered_at DESC LIMIT ?",
                (like, like, clamped),
            )
        else:
            rows = await self._db.fetchall(
                f"SELECT {_USER_REG_COLS} "
                "FROM gfs_user_registrations WHERE status='active' "
                "ORDER BY registered_at DESC LIMIT ?",
                (clamped,),
            )
        out = [_row_to_user_reg(_dict(r)) for r in rows]
        return [r for r in out if r is not None]

    async def list_for_instance(self, instance_id: str) -> list[GfsUserRegistration]:
        rows = await self._db.fetchall(
            f"SELECT {_USER_REG_COLS} FROM gfs_user_registrations WHERE instance_id=?",
            (instance_id,),
        )
        out = [_row_to_user_reg(_dict(r)) for r in rows]
        return [r for r in out if r is not None]

    async def set_picture_digest(
        self, *, user_id: str, picture_digest: str | None
    ) -> int:
        return await self._db.enqueue(
            "UPDATE gfs_user_registrations SET picture_digest=? WHERE user_id=?",
            (picture_digest, user_id),
        )


def _dict(row: Any) -> dict | None:
    if row is None:
        return None
    return dict(row)


def _row_to_user_reg(row: dict | None) -> GfsUserRegistration | None:
    if row is None:
        return None
    return GfsUserRegistration(
        user_id=row["user_id"],
        instance_id=row["instance_id"],
        username=row["username"],
        display_name=row["display_name"],
        picture_url=row.get("picture_url"),
        home_instance_pk=row["home_instance_pk"],
        registered_at=int(row["registered_at"]),
        status=row.get("status") or "active",
        bio=row.get("bio"),
        picture_digest=row.get("picture_digest"),
    )


@runtime_checkable
class AbstractGfsMomentFollowRepo(Protocol):
    async def upsert(self, follow: GfsMomentFollow) -> None: ...
    async def delete(self, *, follower_user_id: str, followed_user_id: str) -> int: ...
    async def followers_of(self, followed_user_id: str) -> list[GfsMomentFollow]: ...
    async def following_of(self, follower_user_id: str) -> list[GfsMomentFollow]: ...
    async def follower_count(self, followed_user_id: str) -> int: ...


class SqliteGfsMomentFollowRepo:
    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def upsert(self, follow: GfsMomentFollow) -> None:
        await self._db.enqueue(
            "INSERT INTO gfs_moment_follows("
            "follower_user_id, follower_instance_id, followed_user_id, created_at) "
            "VALUES(?,?,?,?) "
            "ON CONFLICT(follower_user_id, followed_user_id) DO UPDATE SET "
            "follower_instance_id=excluded.follower_instance_id",
            (
                follow.follower_user_id,
                follow.follower_instance_id,
                follow.followed_user_id,
                follow.created_at,
            ),
        )

    async def delete(self, *, follower_user_id: str, followed_user_id: str) -> int:
        def _run(conn) -> int:
            cur = conn.execute(
                "DELETE FROM gfs_moment_follows "
                "WHERE follower_user_id=? AND followed_user_id=?",
                (follower_user_id, followed_user_id),
            )
            return int(cur.rowcount or 0)

        return await self._db.transact(_run)

    async def followers_of(self, followed_user_id: str) -> list[GfsMomentFollow]:
        rows = await self._db.fetchall(
            "SELECT follower_user_id, follower_instance_id, followed_user_id, created_at "
            "FROM gfs_moment_follows WHERE followed_user_id=? "
            "ORDER BY created_at DESC",
            (followed_user_id,),
        )
        out = [_row_to_follow(_dict(r)) for r in rows]
        return [f for f in out if f is not None]

    async def following_of(self, follower_user_id: str) -> list[GfsMomentFollow]:
        rows = await self._db.fetchall(
            "SELECT follower_user_id, follower_instance_id, followed_user_id, created_at "
            "FROM gfs_moment_follows WHERE follower_user_id=? "
            "ORDER BY created_at DESC",
            (follower_user_id,),
        )
        out = [_row_to_follow(_dict(r)) for r in rows]
        return [f for f in out if f is not None]

    async def follower_count(self, followed_user_id: str) -> int:
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS c FROM gfs_moment_follows WHERE followed_user_id=?",
            (followed_user_id,),
        )
        d = _dict(row) or {}
        return int(d.get("c") or 0)


def _row_to_follow(row: dict | None) -> GfsMomentFollow | None:
    if row is None:
        return None
    return GfsMomentFollow(
        follower_user_id=row["follower_user_id"],
        follower_instance_id=row["follower_instance_id"],
        followed_user_id=row["followed_user_id"],
        created_at=int(row["created_at"]),
    )


# ─── Public-directory avatar mirror (§Momentum-public) ───────────────────


@runtime_checkable
class AbstractGfsUserPictureRepo(Protocol):
    async def upsert(self, picture: GfsUserPicture) -> None: ...
    async def get(self, user_id: str) -> GfsUserPicture | None: ...


class SqliteGfsUserPictureRepo:
    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def upsert(self, picture: GfsUserPicture) -> None:
        await self._db.enqueue(
            "INSERT INTO gfs_user_pictures(user_id, bytes, mime, digest, updated_at) "
            "VALUES(?,?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "bytes=excluded.bytes, mime=excluded.mime, "
            "digest=excluded.digest, updated_at=excluded.updated_at",
            (
                picture.user_id,
                picture.bytes_,
                picture.mime,
                picture.digest,
                picture.updated_at,
            ),
        )

    async def get(self, user_id: str) -> GfsUserPicture | None:
        row = await self._db.fetchone(
            "SELECT user_id, bytes, mime, digest, updated_at "
            "FROM gfs_user_pictures WHERE user_id=?",
            (user_id,),
        )
        d = _dict(row)
        if d is None:
            return None
        return GfsUserPicture(
            user_id=d["user_id"],
            bytes_=bytes(d["bytes"]),
            mime=d["mime"],
            digest=d["digest"],
            updated_at=int(d["updated_at"]),
        )
