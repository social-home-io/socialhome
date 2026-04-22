"""Push subscription repository — stores Web Push subscriptions per user.

A user's browser registers via :class:`~socialhome.routes.push` ; the
returned ``endpoint`` + ``p256dh`` + ``auth`` triplet from the Web Push
API is persisted here. The push service later looks up subscriptions
to dispatch notifications.

All three secret fields are listed in :data:`SENSITIVE_FIELDS` so the
generic API sanitiser strips them from any accidental serialisation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from .base import rows_to_dicts


# Domain dataclass lives in ``socialhome/domain/push_subscription.py``;
# re-exported here so existing repo-level imports keep working.
from ..domain.push_subscription import PushSubscription  # noqa: F401,E402


@runtime_checkable
class AbstractPushSubscriptionRepo(Protocol):
    async def save(self, sub: PushSubscription) -> None: ...
    async def get(self, sub_id: str) -> PushSubscription | None: ...
    async def list_for_user(self, user_id: str) -> list[PushSubscription]: ...
    async def delete(self, sub_id: str, *, user_id: str) -> bool: ...
    async def delete_by_endpoint(self, endpoint: str) -> int: ...


class SqlitePushSubscriptionRepo:
    """SQLite-backed :class:`AbstractPushSubscriptionRepo`."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def save(self, sub: PushSubscription) -> None:
        created = sub.created_at or datetime.now(timezone.utc).isoformat()
        # Upsert on endpoint so a re-register doesn't multiply rows.
        await self._db.enqueue(
            """
            INSERT INTO push_subscriptions(
                id, user_id, endpoint, p256dh, auth_secret, device_label, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                endpoint=excluded.endpoint,
                p256dh=excluded.p256dh,
                auth_secret=excluded.auth_secret,
                device_label=excluded.device_label
            """,
            (
                sub.id,
                sub.user_id,
                sub.endpoint,
                sub.p256dh,
                sub.auth_secret,
                sub.device_label,
                created,
            ),
        )

    async def get(self, sub_id: str) -> PushSubscription | None:
        row = await self._db.fetchone(
            "SELECT * FROM push_subscriptions WHERE id=?",
            (sub_id,),
        )
        return _row_to_sub(row) if row else None

    async def list_for_user(self, user_id: str) -> list[PushSubscription]:
        rows = await self._db.fetchall(
            "SELECT * FROM push_subscriptions WHERE user_id=? ORDER BY created_at",
            (user_id,),
        )
        return [_row_to_sub(r) for r in rows_to_dicts(rows)]

    async def delete(self, sub_id: str, *, user_id: str) -> bool:
        row = await self._db.fetchone(
            "SELECT id FROM push_subscriptions WHERE id=? AND user_id=?",
            (sub_id, user_id),
        )
        if row is None:
            return False
        await self._db.enqueue(
            "DELETE FROM push_subscriptions WHERE id=? AND user_id=?",
            (sub_id, user_id),
        )
        return True

    async def delete_by_endpoint(self, endpoint: str) -> int:
        """Drop every row matching *endpoint* (e.g. after a 404/410 from the push service)."""
        rows = await self._db.fetchall(
            "SELECT id FROM push_subscriptions WHERE endpoint=?",
            (endpoint,),
        )
        if not rows:
            return 0
        await self._db.enqueue(
            "DELETE FROM push_subscriptions WHERE endpoint=?",
            (endpoint,),
        )
        return len(rows)


def _row_to_sub(row) -> PushSubscription:
    return PushSubscription(
        id=row["id"],
        user_id=row["user_id"],
        endpoint=row["endpoint"],
        p256dh=row["p256dh"],
        auth_secret=row["auth_secret"],
        device_label=row["device_label"],
        created_at=row["created_at"],
    )
