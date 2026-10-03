"""Infrastructure — event bus, key manager, outbox, idempotency, ws, reconnect."""

from .event_bus import EventBus, Handler
from .idempotency import IdempotencyCache
from .key_manager import KeyManager, KeyManagerError
from .outbox_processor import (
    BACKOFF_SECONDS,
    MAX_ATTEMPTS,
    MAX_RETRY_AFTER_S,
    PAIR_WINDOW_404_ATTEMPTS,
    NEVER_DROP,
    DeliveryOutcome,
    OutboxProcessor,
    RetryAfter,
)
from .reconnect_queue import (
    P1_SECURITY,
    P2_STRUCTURAL,
    P3_MEMBERSHIP,
    P4_DM,
    P5_CONTENT,
    P6_PRODUCTIVITY,
    P7_BULK,
    SYNC_CONCURRENCY,
    ReconnectSyncQueue,
)
from .ws_manager import WebSocketManager

__all__ = [
    "BACKOFF_SECONDS",
    "DeliveryOutcome",
    "EventBus",
    "Handler",
    "IdempotencyCache",
    "KeyManager",
    "KeyManagerError",
    "MAX_ATTEMPTS",
    "MAX_RETRY_AFTER_S",
    "PAIR_WINDOW_404_ATTEMPTS",
    "NEVER_DROP",
    "OutboxProcessor",
    "RetryAfter",
    "P1_SECURITY",
    "P2_STRUCTURAL",
    "P3_MEMBERSHIP",
    "P4_DM",
    "P5_CONTENT",
    "P6_PRODUCTIVITY",
    "P7_BULK",
    "ReconnectSyncQueue",
    "SYNC_CONCURRENCY",
    "WebSocketManager",
]
