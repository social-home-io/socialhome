"""DataExportService — per-user GDPR-style data export (§25.8.7).

A user requests their own data; the service walks every table that
references their ``user_id`` (or ``username``) and returns a single
JSON blob (admins can also request another user's export for legal
process — gated at the route layer).

This is *not* the household backup (:class:`BackupService`) — that
captures the whole instance state for restore. Data export is
user-scoped and intentionally lossy: it returns only rows that
belong to or were authored by the specified user.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from ..db import AsyncDatabase

log = logging.getLogger(__name__)


# ─── What gets exported ──────────────────────────────────────────────────

#: Tables and the column linking each row to a user_id. Order matches
#: the order the user encounters surfaces in the UI — feed first,
#: then DMs, then planning, then media.
#: Fields scrubbed from every exported row. These are either cryptographic
#: secrets (useless to the subject, dangerous if leaked) or structural
#: hashes that do not reveal anything the subject didn't already know.
_SCRUB_FIELDS: frozenset[str] = frozenset(
    {
        "password_hash",
        "identity_private_key",
        "routing_secret",
        "private_key",
        "session_key",
        "auth_secret",
        "p256dh",
        "token_hash",
    }
)


def _scrub(row: dict) -> dict:
    return {k: v for k, v in row.items() if k not in _SCRUB_FIELDS}


EXPORTABLE_QUERIES: tuple[tuple[str, str], ...] = (
    ("users", "WHERE user_id = ?"),
    ("feed_posts", "WHERE author = ?"),
    ("feed_comments", "WHERE author = ?"),
    ("saved_posts", "WHERE user_id = ?"),
    ("space_post_comments", "WHERE author = ?"),
    ("space_posts", "WHERE author = ?"),
    ("conversation_messages", "WHERE sender_user_id = ?"),
    ("message_reactions", "WHERE user_id = ?"),
    ("tasks", "WHERE created_by = ?"),
    ("task_comments", "WHERE author = ?"),
    ("calendar_events", "WHERE created_by = ?"),
    ("calendar_rsvps", "WHERE user_id = ?"),
    ("pages", "WHERE author = ?"),
    ("stickies", "WHERE author = ?"),
    ("polls", "WHERE author = ?"),
    ("poll_votes", "WHERE user_id = ?"),
    ("schedule_responses", "WHERE user_id = ?"),
    ("bazaar_listings", "WHERE seller_user_id = ?"),
    ("bazaar_bids", "WHERE bidder_user_id = ?"),
    ("notifications", "WHERE user_id = ?"),
    ("shopping_list_items", "WHERE added_by = ?"),
    ("user_blocks", "WHERE blocker_user_id = ?"),
    ("gallery_albums", "WHERE owner_user_id = ?"),
    ("gallery_items", "WHERE uploaded_by = ?"),
    ("push_subscriptions", "WHERE user_id = ?"),
    ("hidden_public_spaces", "WHERE user_id = ?"),
    ("dashboard_widgets", "WHERE user_id = ?"),
    ("space_notif_prefs", "WHERE user_id = ?"),
    ("dm_contact_requests", "WHERE from_user_id = ? OR to_user_id = ?"),
    ("call_sessions", "WHERE initiator_user_id = ? OR callee_user_id = ?"),
    ("following_spaces", "WHERE user_id = ?"),
    ("drafts", "WHERE user_id = ?"),
)


@dataclass(slots=True, frozen=True)
class DataExport:
    """A user's exported data.

    ``tables`` maps table-name → list-of-rows. Empty tables are
    omitted to keep the JSON small.
    """

    user_id: str
    exported_at: str
    tables: dict[str, list[dict]]


class DataExportService:
    """Build per-user data exports."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def export_for_user(self, user_id: str) -> DataExport:
        """Walk every table linked to *user_id* and return a snapshot.

        The export intentionally includes the user's own email, phone,
        DOB, etc. — GDPR grants the subject access to *their own*
        personal data. Crypto material that is useless to the user and
        dangerous if leaked (password_hash, identity_private_key,
        routing_secret, session keys) is scrubbed via
        :data:`_SCRUB_FIELDS` before serialisation.
        """
        tables: dict[str, list[dict]] = {}
        for table, where_clause in EXPORTABLE_QUERIES:
            params = (user_id,) if where_clause.count("?") == 1 else (user_id, user_id)
            try:
                rows = await self._db.fetchall(
                    f"SELECT * FROM {table} {where_clause}",
                    params,
                )
            except Exception as exc:  # defensive
                log.debug("data_export: skipping %s: %s", table, exc)
                continue
            if not rows:
                continue
            tables[table] = [_scrub(dict(r)) for r in rows]
        return DataExport(
            user_id=user_id,
            exported_at=datetime.now(timezone.utc).isoformat(),
            tables=tables,
        )

    async def export_to_bytes(self, user_id: str) -> bytes:
        """Convenience: build the export and serialise to UTF-8 JSON."""
        export = await self.export_for_user(user_id)
        return json.dumps(
            {
                "user_id": export.user_id,
                "exported_at": export.exported_at,
                "tables": export.tables,
            },
            indent=2,
            default=str,
        ).encode("utf-8")
