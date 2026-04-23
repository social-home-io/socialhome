"""Typed application keys for the aiohttp app dict.

Using :class:`aiohttp.web.AppKey` instead of bare string keys eliminates
``NotAppKeyWarning`` and gives type checkers visibility into the app dict.
"""

from __future__ import annotations

import aiohttp
from aiohttp.web import AppKey

from .config import Config
from .db import AsyncDatabase

# ── Core ─────────────────────────────────────────────────────────────────
config_key: AppKey[Config] = AppKey("config")
db_key: AppKey[AsyncDatabase] = AppKey("db")
#: Shared aiohttp.ClientSession used by every HTTP caller in the app
#: (HA adapter, Supervisor client, federation, GFS, standalone push).
#: Created during app startup, closed on cleanup.
http_session_key: AppKey[aiohttp.ClientSession] = AppKey("http_session")

# ── Federation infrastructure ────────────────────────────────────────────
key_manager_key: AppKey = AppKey("key_manager")
event_bus_key: AppKey = AppKey("event_bus")
federation_service_key: AppKey = AppKey("federation_service")
outbox_processor_key: AppKey = AppKey("outbox_processor")
sync_session_manager_key: AppKey = AppKey("sync_session_manager")
call_signaling_service_key: AppKey = AppKey("call_signaling_service")
call_repo_key: AppKey = AppKey("call_repo")
ws_manager_key: AppKey = AppKey("ws_manager")
push_service_key: AppKey = AppKey("push_service")
push_subscription_repo_key: AppKey = AppKey("push_subscription_repo")
search_service_key: AppKey = AppKey("search_service")
theme_service_key: AppKey = AppKey("theme_service")
space_crypto_service_key: AppKey = AppKey("space_crypto_service")
calendar_import_service_key: AppKey = AppKey("calendar_import_service")
storage_quota_service_key: AppKey = AppKey("storage_quota_service")
backup_service_key: AppKey = AppKey("backup_service")
i18n_key: AppKey = AppKey("i18n")
idempotency_cache_key: AppKey = AppKey("idempotency_cache")
reconnect_queue_key: AppKey = AppKey("reconnect_queue")
public_space_discovery_key: AppKey = AppKey("public_space_discovery")
peer_space_directory_repo_key: AppKey = AppKey("peer_space_directory_repo")
peer_directory_service_key: AppKey = AppKey("peer_directory_service")
platform_adapter_key: AppKey = AppKey("platform_adapter")
gallery_service_key: AppKey = AppKey("gallery_service")
gallery_repo_key: AppKey = AppKey("gallery_repo")
child_protection_service_key: AppKey = AppKey("child_protection_service")
typing_service_key: AppKey = AppKey("typing_service")
household_features_service_key: AppKey = AppKey("household_features_service")
data_export_service_key: AppKey = AppKey("data_export_service")
dm_routing_service_key: AppKey = AppKey("dm_routing_service")
federation_inbound_service_key: AppKey = AppKey("federation_inbound_service")
pairing_relay_queue_key: AppKey = AppKey("pairing_relay_queue")
space_sync_service_key: AppKey = AppKey("space_sync_service")
space_sync_receiver_key: AppKey = AppKey("space_sync_receiver")
space_sync_scheduler_key: AppKey = AppKey("space_sync_scheduler")
dm_history_provider_key: AppKey = AppKey("dm_history_provider")
dm_history_receiver_key: AppKey = AppKey("dm_history_receiver")
dm_history_scheduler_key: AppKey = AppKey("dm_history_scheduler")
report_repo_key: AppKey = AppKey("report_repo")
report_service_key: AppKey = AppKey("report_service")
page_conflict_service_key: AppKey = AppKey("page_conflict_service")
rate_limiter_key: AppKey = AppKey("rate_limiter")
presence_service_key: AppKey = AppKey("presence_service")
poll_service_key: AppKey = AppKey("poll_service")
space_poll_service_key: AppKey = AppKey("space_poll_service")
bazaar_service_key: AppKey = AppKey("bazaar_service")
instance_id_key: AppKey[str] = AppKey("instance_id")
instance_signing_key_key: AppKey[bytes] = AppKey("instance_signing_key")
ha_bridge_service_key: AppKey = AppKey("ha_bridge_service")
bot_bridge_service_key: AppKey = AppKey("bot_bridge_service")
space_bot_service_key: AppKey = AppKey("space_bot_service")
space_bot_repo_key: AppKey = AppKey("space_bot_repo")
stt_service_key: AppKey = AppKey("stt_service")

gfs_connection_service_key: AppKey = AppKey("gfs_connection_service")
gfs_connection_repo_key: AppKey = AppKey("gfs_connection_repo")

# ── Services ─────────────────────────────────────────────────────────────
user_service_key: AppKey = AppKey("user_service")
feed_service_key: AppKey = AppKey("feed_service")
space_service_key: AppKey = AppKey("space_service")
notification_service_key: AppKey = AppKey("notification_service")
dm_service_key: AppKey = AppKey("dm_service")
corner_service_key: AppKey = AppKey("corner_service")
auto_pair_coordinator_key: AppKey = AppKey("auto_pair_coordinator")
auto_pair_inbox_key: AppKey = AppKey("auto_pair_inbox")
task_service_key: AppKey = AppKey("task_service")
space_task_service_key: AppKey = AppKey("space_task_service")
calendar_service_key: AppKey = AppKey("calendar_service")
space_cal_service_key: AppKey = AppKey("space_cal_service")
shopping_service_key: AppKey = AppKey("shopping_service")

# ── Repos (exposed for routes / federation / webhooks) ───────────────────
user_repo_key: AppKey = AppKey("user_repo")
profile_picture_repo_key: AppKey = AppKey("profile_picture_repo")
post_repo_key: AppKey = AppKey("post_repo")
space_repo_key: AppKey = AppKey("space_repo")
space_cover_repo_key: AppKey = AppKey("space_cover_repo")
notification_repo_key: AppKey = AppKey("notification_repo")
conversation_repo_key: AppKey = AppKey("conversation_repo")
outbox_repo_key: AppKey = AppKey("outbox_repo")
federation_repo_key: AppKey = AppKey("federation_repo")
page_repo_key: AppKey = AppKey("page_repo")
sticky_repo_key: AppKey = AppKey("sticky_repo")
bazaar_repo_key: AppKey = AppKey("bazaar_repo")
shopping_repo_key: AppKey = AppKey("shopping_repo")
