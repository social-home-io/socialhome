"""Federation repository — peers, pairings, replay cache.

Covers the persistence surface the :class:`FederationService` depends on:

* CRUD for ``remote_instances`` rows — paired peers.
* Load + mark the in-memory :class:`ReplayCache` from the on-disk
  ``federation_replay_cache`` table.
* Helpers for ``pending_pairings`` (create / advance / drop) used by the
  pairing flow.
* Space instance bans (``space_instance_bans``) for the §13 moderation
  flow.

Kept separate from :mod:`outbox_repo` because the outbox has different
access patterns (a background retry loop, not the request/response path).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.federation import (
    InstanceSource,
    PairingSession,
    PairingStatus,
    RemoteInstance,
)
from .base import bool_col, row_to_dict, rows_to_dicts


@runtime_checkable
class AbstractFederationRepo(Protocol):
    # Remote instances ----------------------------------------------------
    async def get_instance(self, instance_id: str) -> RemoteInstance | None: ...
    async def get_instance_by_local_inbox_id(
        self, local_inbox_id: str
    ) -> RemoteInstance | None: ...
    async def save_instance(self, inst: RemoteInstance) -> RemoteInstance: ...
    async def set_proto_version(
        self,
        instance_id: str,
        proto_version: int,
    ) -> None: ...
    async def mark_capabilities_seen(self, instance_id: str) -> None: ...
    async def list_instances(
        self,
        *,
        source: str | None = None,
        status: str | None = None,
    ) -> list[RemoteInstance]: ...
    async def list_social_instances(self) -> list[RemoteInstance]: ...
    async def list_instances_in_space(self, space_id: str) -> list[RemoteInstance]: ...
    async def list_member_instance_ids(self, space_id: str) -> list[str]: ...
    async def delete_instance(self, instance_id: str) -> None: ...
    async def mark_reachable(self, instance_id: str) -> None: ...
    async def mark_unreachable(self, instance_id: str) -> None: ...
    async def update_inbox(self, instance_id: str, new_url: str) -> None: ...
    async def update_alias(
        self,
        instance_id: str,
        alias: str | None,
    ) -> None: ...
    async def update_display_name(self, instance_id: str, name: str) -> None: ...
    async def set_share_home(
        self,
        instance_id: str,
        *,
        value: bool,
    ) -> None: ...
    async def update_instance_home(
        self,
        instance_id: str,
        *,
        latitude: float | None,
        longitude: float | None,
    ) -> None: ...

    # Local instance identity (display_name + household coords) ----------
    async def get_local_identity(self) -> dict | None: ...
    async def set_instance_display_name(self, name: str) -> None: ...
    async def get_last_proto_version(self) -> int | None: ...
    async def set_last_proto_version(self, version: int) -> None: ...

    # Replay cache --------------------------------------------------------
    async def load_replay_cache(
        self, within_hours: int = 1
    ) -> list[tuple[str, str]]: ...
    async def insert_replay_id(self, msg_id: str) -> None: ...
    async def prune_replay_cache(self, cutoff_iso: str) -> int: ...

    # Pairings ------------------------------------------------------------
    async def create_pairing(self, session: PairingSession) -> None: ...
    async def get_pairing(self, token: str) -> PairingSession | None: ...
    async def update_pairing(self, session: PairingSession) -> None: ...
    async def delete_pairing(self, token: str) -> None: ...
    async def cleanup_expired_pairings(self) -> int: ...

    # Bans ----------------------------------------------------------------
    async def ban_instance_from_space(
        self,
        space_id: str,
        instance_id: str,
        *,
        reason: str | None = None,
    ) -> None: ...
    async def is_instance_banned_from_space(
        self,
        space_id: str,
        instance_id: str,
    ) -> bool: ...


class SqliteFederationRepo:
    """SQLite-backed :class:`AbstractFederationRepo`."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    # ── Remote instances ───────────────────────────────────────────────

    async def get_instance(self, instance_id: str) -> RemoteInstance | None:
        row = await self._db.fetchone(
            "SELECT * FROM remote_instances WHERE id=?",
            (instance_id,),
        )
        return _row_to_instance(row_to_dict(row))

    async def get_instance_by_local_inbox_id(
        self,
        local_inbox_id: str,
    ) -> RemoteInstance | None:
        row = await self._db.fetchone(
            "SELECT * FROM remote_instances WHERE local_inbox_id=? LIMIT 1",
            (local_inbox_id,),
        )
        return _row_to_instance(row_to_dict(row))

    async def save_instance(self, inst: RemoteInstance) -> RemoteInstance:
        await self._db.enqueue(
            """
            INSERT INTO remote_instances(
                id, display_name, remote_identity_pk,
                key_self_to_remote, key_remote_to_self,
                remote_inbox_url, local_inbox_id,
                status, source, proto_version,
                remote_pq_algorithm, remote_pq_identity_pk, sig_suite,
                intro_relay_enabled, relay_via,
                home_lat, home_lon, paired_at, created_at,
                last_reachable_at, unreachable_since, share_home
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,COALESCE(?, datetime('now')),?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                display_name=excluded.display_name,
                remote_identity_pk=excluded.remote_identity_pk,
                key_self_to_remote=excluded.key_self_to_remote,
                key_remote_to_self=excluded.key_remote_to_self,
                remote_inbox_url=excluded.remote_inbox_url,
                status=excluded.status,
                source=excluded.source,
                proto_version=excluded.proto_version,
                remote_pq_algorithm=excluded.remote_pq_algorithm,
                remote_pq_identity_pk=excluded.remote_pq_identity_pk,
                sig_suite=excluded.sig_suite,
                intro_relay_enabled=excluded.intro_relay_enabled,
                relay_via=excluded.relay_via,
                home_lat=excluded.home_lat,
                home_lon=excluded.home_lon,
                paired_at=excluded.paired_at,
                last_reachable_at=excluded.last_reachable_at,
                unreachable_since=excluded.unreachable_since
            """,
            (
                inst.id,
                inst.display_name,
                inst.remote_identity_pk,
                inst.key_self_to_remote,
                inst.key_remote_to_self,
                inst.remote_inbox_url,
                inst.local_inbox_id,
                inst.status.value,
                inst.source.value,
                inst.proto_version,
                inst.remote_pq_algorithm,
                inst.remote_pq_identity_pk,
                inst.sig_suite,
                int(inst.intro_relay_enabled),
                inst.relay_via,
                inst.home_lat,
                inst.home_lon,
                inst.paired_at,
                inst.created_at,
                inst.last_reachable_at,
                inst.unreachable_since,
                int(inst.share_home),
            ),
        )
        return inst

    async def set_proto_version(
        self,
        instance_id: str,
        proto_version: int,
    ) -> None:
        """Apply the peer-advertised protocol version without touching
        identity, keys, or URL.

        Called from the inbound
        :data:`FederationEventType.INSTANCE_CAPABILITIES_UPDATED`
        handler. A single targeted UPDATE so the much larger
        :meth:`save_instance` (which re-writes every column) doesn't
        accidentally clobber state set elsewhere between reads.
        """
        await self._db.enqueue(
            "UPDATE remote_instances SET proto_version=? WHERE id=?",
            (proto_version, instance_id),
        )

    async def mark_capabilities_seen(self, instance_id: str) -> None:
        """Stamp ``capabilities_seen_at`` to now.

        Called from the inbound
        :data:`FederationEventType.INSTANCE_CAPABILITIES_UPDATED` handler
        on *every* advertisement — including a re-advertisement of the same
        ``proto_version`` (where the handler short-circuits before calling
        :meth:`set_proto_version`).
        That keeps the "has this peer ever advertised" signal correct even
        when the version itself doesn't change. A targeted single-column
        UPDATE so it never clobbers state set elsewhere.
        """
        await self._db.enqueue(
            "UPDATE remote_instances SET capabilities_seen_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), instance_id),
        )

    async def list_instances(
        self,
        *,
        source: str | None = None,
        status: str | None = None,
    ) -> list[RemoteInstance]:
        clauses: list[str] = []
        params: list = []
        if source is not None:
            clauses.append("source=?")
            params.append(source)
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await self._db.fetchall(
            f"SELECT * FROM remote_instances{where} ORDER BY display_name",
            tuple(params),
        )
        return [i for i in (_row_to_instance(d) for d in rows_to_dicts(rows)) if i]

    async def list_social_instances(self) -> list[RemoteInstance]:
        """Confirmed peers this household has a **social** relationship with.

        A CONFIRMED row is not automatically a social peer. A row whose
        ``source`` is
        :data:`~socialhome.domain.federation.InstanceSource.SPACE_SESSION`
        came from a §D2b invite-link bootstrap: the two households share
        a space, nothing more. Handing it to DMs, the user roster,
        presence, the friends constellation or a peer picker would turn
        "I joined their space" into "we federate socially", which nobody
        consented to.

        Every non-space surface that fans out to "all confirmed peers"
        reads this list; space fan-outs keep using ``space_instances`` /
        :meth:`list_instances_in_space`, which are membership-scoped and
        unaffected.
        """
        rows = await self._db.fetchall(
            "SELECT * FROM remote_instances "
            "WHERE status=? AND source<>? ORDER BY display_name",
            (PairingStatus.CONFIRMED.value, InstanceSource.SPACE_SESSION.value),
        )
        return [i for i in (_row_to_instance(d) for d in rows_to_dicts(rows)) if i]

    async def list_instances_in_space(
        self,
        space_id: str,
    ) -> list[RemoteInstance]:
        """Confirmed peers that are members of ``space_id`` and not
        instance-banned from it (§24.11)."""
        rows = await self._db.fetchall(
            """
            SELECT ri.* FROM remote_instances ri
            JOIN space_instances si ON si.instance_id = ri.id
            WHERE si.space_id = ?
              AND ri.status = ?
              AND NOT EXISTS (
                  SELECT 1 FROM space_instance_bans sib
                  WHERE sib.space_id = si.space_id
                    AND sib.instance_id = ri.id
              )
            ORDER BY ri.display_name
            """,
            (space_id, PairingStatus.CONFIRMED.value),
        )
        return [i for i in (_row_to_instance(d) for d in rows_to_dicts(rows)) if i]

    async def list_member_instance_ids(self, space_id: str) -> list[str]:
        """Every instance ID in ``space_instances`` for this space,
        minus bans. Unlike :meth:`list_instances_in_space` this does
        NOT filter on ``remote_instances.status = CONFIRMED`` — so a
        mesh-only member (private invite accepted via SPACE_ROUTED,
        no direct pair) is included. The caller is expected to route
        each ID through :meth:`FederationService.send_with_mesh_fallback`
        which decides direct vs mesh based on the per-peer pairing
        state. This is what makes the space-content fanout
        mesh-capable in steady state.
        """
        rows = await self._db.fetchall(
            """
            SELECT si.instance_id FROM space_instances si
            WHERE si.space_id = ?
              AND NOT EXISTS (
                  SELECT 1 FROM space_instance_bans sib
                  WHERE sib.space_id = si.space_id
                    AND sib.instance_id = si.instance_id
              )
            ORDER BY si.instance_id
            """,
            (space_id,),
        )
        return [str(r[0]) for r in rows]

    async def delete_instance(self, instance_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM remote_instances WHERE id=?",
            (instance_id,),
        )

    async def mark_reachable(self, instance_id: str) -> None:
        await self._db.enqueue(
            "UPDATE remote_instances SET last_reachable_at=datetime('now'), "
            "unreachable_since=NULL WHERE id=?",
            (instance_id,),
        )

    async def mark_unreachable(self, instance_id: str) -> None:
        await self._db.enqueue(
            "UPDATE remote_instances "
            "SET unreachable_since=COALESCE(unreachable_since, datetime('now')) "
            "WHERE id=?",
            (instance_id,),
        )

    async def update_inbox(self, instance_id: str, new_url: str) -> None:
        await self._db.enqueue(
            "UPDATE remote_instances SET remote_inbox_url=? WHERE id=?",
            (new_url, instance_id),
        )

    async def update_alias(
        self,
        instance_id: str,
        alias: str | None,
    ) -> None:
        """Set or clear the local alias for a paired peer.

        ``alias`` is the local-only display string the admin set in
        the UI ("Brother's house"). ``None`` clears it so the SPA
        falls back to the peer's federated display_name. Never
        federated — purely local UX state.
        """
        await self._db.enqueue(
            "UPDATE remote_instances SET local_alias=? WHERE id=?",
            (alias, instance_id),
        )

    async def update_display_name(self, instance_id: str, name: str) -> None:
        """Update the peer's *advertised* federated ``display_name``.

        Called from the inbound
        :data:`FederationEventType.INSTANCE_CAPABILITIES_UPDATED` handler when
        a peer re-broadcasts a new household name. Deliberately does NOT touch
        ``local_alias`` — an admin-set alias keeps winning in
        :attr:`RemoteInstance.effective_display_name`. A targeted single-column
        UPDATE so it never clobbers state set elsewhere.
        """
        await self._db.enqueue(
            "UPDATE remote_instances SET display_name=? WHERE id=?",
            (name, instance_id),
        )

    async def set_share_home(
        self,
        instance_id: str,
        *,
        value: bool,
    ) -> None:
        """Enable or disable home-location sharing with a specific peer.

        A targeted single-column UPDATE so the much larger
        :meth:`save_instance` doesn't accidentally clobber state set
        separately. Local-only flag — never federated.
        """
        await self._db.enqueue(
            "UPDATE remote_instances SET share_home=? WHERE id=?",
            (int(value), instance_id),
        )

    async def update_instance_home(
        self,
        instance_id: str,
        *,
        latitude: float | None,
        longitude: float | None,
    ) -> None:
        """Update the peer's ``home_lat`` / ``home_lon`` columns only.

        Used by the inbound
        :data:`FederationEventType.LOCAL_HOME_LOCATION_CHANGED`
        handler. Values are 4dp-truncated on write per §25 — the
        inbound caller has already validated the body but we defend-
        in-depth here. Pass ``None`` for both to clear the columns
        (revoke signal). Missing rows are silent no-ops (the §24.11
        inbound pipeline has already verified the sender is known).
        """
        lat_db = round(float(latitude), 4) if latitude is not None else None
        lon_db = round(float(longitude), 4) if longitude is not None else None
        await self._db.enqueue(
            "UPDATE remote_instances SET home_lat = ?, home_lon = ? WHERE id = ?",
            (lat_db, lon_db, instance_id),
        )

    async def get_local_identity(self) -> dict | None:
        """Return the local instance's display_name + household coords.

        Used by routes that need a friendly name + map pin for "us" —
        the keys + secrets in this row stay private. Returns ``None``
        before bootstrap creates the row (only possible during a very
        early test fixture).
        """
        row = await self._db.fetchone(
            "SELECT instance_id, display_name, home_lat, home_lon "
            "FROM instance_identity WHERE id='self'",
        )
        if row is None:
            return None
        return {
            "instance_id": row["instance_id"],
            "display_name": row["display_name"],
            "home_lat": row["home_lat"],
            "home_lon": row["home_lon"],
        }

    async def set_instance_display_name(self, name: str) -> None:
        """Persist the household's federated display name on the self-row.

        This is the field that federates — it's carried in the pairing QR
        and shown to peers. Overwrites in place on the singleton self-row.
        """
        await self._db.enqueue(
            "UPDATE instance_identity SET display_name=? WHERE id='self'",
            (name,),
        )

    async def get_last_proto_version(self) -> int | None:
        """Return the build's ``OURS`` as of the last successful boot.

        ``None`` until the upgrade trigger first records it (NULL column
        default — see migration ``0024``). A ``None`` is treated as an
        upgrade on the first boot after the migration ships.
        """
        row = await self._db.fetchone(
            "SELECT last_proto_version FROM instance_identity WHERE id='self'",
        )
        if row is None:
            return None
        return row["last_proto_version"]

    async def set_last_proto_version(self, version: int) -> None:
        """Persist ``version`` (this build's ``OURS``) on the self-row.

        Overwrites in place — the singleton self-row is the only row.
        """
        await self._db.enqueue(
            "UPDATE instance_identity SET last_proto_version=? WHERE id='self'",
            (version,),
        )

    # ── Replay cache ───────────────────────────────────────────────────

    async def load_replay_cache(
        self,
        within_hours: int = 1,
    ) -> list[tuple[str, str]]:
        rows = await self._db.fetchall(
            "SELECT msg_id, received_at FROM federation_replay_cache "
            "WHERE received_at > datetime('now', ?)",
            (f"-{within_hours} hours",),
        )
        return [(r["msg_id"], r["received_at"]) for r in rows]

    async def insert_replay_id(self, msg_id: str) -> None:
        await self._db.enqueue(
            "INSERT OR IGNORE INTO federation_replay_cache(msg_id) VALUES(?)",
            (msg_id,),
        )

    async def prune_replay_cache(self, cutoff_iso: str) -> int:
        """Delete replay entries older than ``cutoff_iso``.

        Returns the count purged. Callers typically run this from an hourly
        scheduler.
        """
        before = await self._db.fetchval(
            "SELECT COUNT(*) FROM federation_replay_cache WHERE received_at < ?",
            (cutoff_iso,),
            default=0,
        )
        await self._db.enqueue(
            "DELETE FROM federation_replay_cache WHERE received_at < ?",
            (cutoff_iso,),
        )
        return int(before)

    # ── Pairings ───────────────────────────────────────────────────────

    async def create_pairing(self, session: PairingSession) -> None:
        await self._db.enqueue(
            """
            INSERT INTO pending_pairings(
                token, own_identity_pk, own_dh_pk, own_dh_sk,
                peer_identity_pk, peer_dh_pk, peer_inbox_url, inbox_url,
                own_local_inbox_id,
                verification_code, intro_note, relay_via,
                status, issued_at, expires_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                session.token,
                session.own_identity_pk,
                session.own_dh_pk,
                session.own_dh_sk,
                session.peer_identity_pk,
                session.peer_dh_pk,
                session.peer_inbox_url,
                session.inbox_url,
                session.own_local_inbox_id,
                session.verification_code,
                session.intro_note,
                session.relay_via,
                session.status.value,
                session.issued_at,
                session.expires_at,
            ),
        )

    async def get_pairing(self, token: str) -> PairingSession | None:
        row = await self._db.fetchone(
            "SELECT * FROM pending_pairings WHERE token=?",
            (token,),
        )
        d = row_to_dict(row)
        if d is None:
            return None
        return PairingSession(
            token=d["token"],
            own_identity_pk=d["own_identity_pk"],
            own_dh_pk=d["own_dh_pk"],
            own_dh_sk=d["own_dh_sk"],
            peer_identity_pk=d.get("peer_identity_pk"),
            peer_dh_pk=d.get("peer_dh_pk"),
            peer_inbox_url=d.get("peer_inbox_url"),
            inbox_url=d["inbox_url"],
            own_local_inbox_id=d["own_local_inbox_id"],
            verification_code=d.get("verification_code"),
            intro_note=d.get("intro_note"),
            relay_via=d.get("relay_via"),
            status=PairingStatus(d.get("status", "pending_sent")),
            issued_at=d.get("issued_at"),
            expires_at=d.get("expires_at"),
        )

    async def update_pairing(self, session: PairingSession) -> None:
        await self._db.enqueue(
            """
            UPDATE pending_pairings SET
                peer_identity_pk=?,
                peer_dh_pk=?,
                peer_inbox_url=?,
                verification_code=?,
                intro_note=?,
                relay_via=?,
                status=?
            WHERE token=?
            """,
            (
                session.peer_identity_pk,
                session.peer_dh_pk,
                session.peer_inbox_url,
                session.verification_code,
                session.intro_note,
                session.relay_via,
                session.status.value,
                session.token,
            ),
        )

    async def delete_pairing(self, token: str) -> None:
        await self._db.enqueue(
            "DELETE FROM pending_pairings WHERE token=?",
            (token,),
        )

    async def cleanup_expired_pairings(self) -> int:
        """Delete ``pending_pairings`` past their ``expires_at`` plus the
        orphan ``remote_instances`` rows (in PENDING_SENT /
        PENDING_RECEIVED status) whose ``local_inbox_id`` matched one
        of those sessions. Returns the count of pairing rows pruned.

        SQLite's ``datetime()`` function normalises both the
        ``"YYYY-MM-DDTHH:MM:SS+00:00"`` ISO 8601 shape Python's
        :meth:`datetime.isoformat` produces and the
        ``"YYYY-MM-DD HH:MM:SS"`` shape SQLite's ``datetime('now')``
        emits, so a single comparison works regardless of which path
        wrote the row.

        Already-confirmed pairs are safe: :meth:`confirm` deletes the
        ``pending_pairings`` row before flipping the
        ``remote_instances`` row to ``CONFIRMED``, so no expired
        session ever points at a confirmed instance.
        """
        expired_count = await self._db.fetchval(
            "SELECT COUNT(*) FROM pending_pairings "
            "WHERE datetime(expires_at) < datetime('now')",
            default=0,
        )
        if not expired_count:
            return 0
        await self._db.enqueue(
            """
            DELETE FROM remote_instances
            WHERE status IN ('pending_sent', 'pending_received')
              AND local_inbox_id IN (
                SELECT own_local_inbox_id FROM pending_pairings
                WHERE datetime(expires_at) < datetime('now')
              )
            """,
        )
        await self._db.enqueue(
            "DELETE FROM pending_pairings WHERE datetime(expires_at) < datetime('now')",
        )
        return int(expired_count)

    # ── Instance bans ──────────────────────────────────────────────────

    async def ban_instance_from_space(
        self,
        space_id: str,
        instance_id: str,
        *,
        reason: str | None = None,
    ) -> None:
        await self._db.enqueue(
            """
            INSERT INTO space_instance_bans(space_id, instance_id, reason)
            VALUES(?, ?, ?)
            ON CONFLICT(space_id, instance_id) DO UPDATE SET
                reason=excluded.reason
            """,
            (space_id, instance_id, reason),
        )

    async def is_instance_banned_from_space(
        self,
        space_id: str,
        instance_id: str,
    ) -> bool:
        row = await self._db.fetchone(
            "SELECT 1 FROM space_instance_bans WHERE space_id=? AND instance_id=?",
            (space_id, instance_id),
        )
        return row is not None


# ─── Row → domain helper ──────────────────────────────────────────────────


def _row_to_instance(row: dict | None) -> RemoteInstance | None:
    if row is None:
        return None
    return RemoteInstance(
        id=row["id"],
        display_name=row["display_name"],
        remote_identity_pk=row["remote_identity_pk"],
        key_self_to_remote=row["key_self_to_remote"],
        key_remote_to_self=row["key_remote_to_self"],
        remote_inbox_url=row["remote_inbox_url"],
        local_inbox_id=row["local_inbox_id"],
        status=PairingStatus(row.get("status", "confirmed")),
        source=InstanceSource(row.get("source", "manual")),
        proto_version=int(row.get("proto_version") or 1),
        remote_pq_algorithm=row.get("remote_pq_algorithm"),
        remote_pq_identity_pk=row.get("remote_pq_identity_pk"),
        sig_suite=str(row.get("sig_suite") or "ed25519"),
        intro_relay_enabled=bool_col(row.get("intro_relay_enabled", 1)),
        relay_via=row.get("relay_via"),
        home_lat=row.get("home_lat"),
        home_lon=row.get("home_lon"),
        paired_at=row.get("paired_at"),
        created_at=row.get("created_at"),
        last_reachable_at=row.get("last_reachable_at"),
        unreachable_since=row.get("unreachable_since"),
        local_alias=row.get("local_alias"),
        share_home=bool_col(row.get("share_home", 1)),
        capabilities_seen_at=row.get("capabilities_seen_at"),
    )
