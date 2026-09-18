"""Typed aiohttp app keys for the GFS process."""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiohttp
from aiohttp.web import AppKey

from ..db import AsyncDatabase
from .admin_service import GfsAdminService
from .cluster import ClusterService
from .config import GfsConfig
from .envelope_relay import GfsEnvelopeRelay
from .federation import GfsFederationService
from .invites import GfsInviteService
from .repositories import (
    AbstractClusterRepo,
    AbstractGfsAdminRepo,
    AbstractGfsEnvelopeQueueRepo,
    AbstractGfsFederationRepo,
    AbstractGfsHighlightPublicationRepo,
    AbstractGfsHighlightTokenRepo,
    AbstractGfsInviteRepo,
    AbstractGfsMomentFollowRepo,
    AbstractGfsUserPictureRepo,
    AbstractGfsUserRegistrationRepo,
)
from .rtc_transport import GfsRtcSession
from .relay_bridge import RelayBridge
from .highlight_publications import HighlightPublicationRegistry
from .moment_public_registry import MomentPublicRegistry
from .ws_registry import GfsWebSocketRegistry

if TYPE_CHECKING:
    # ``public`` imports this module, so the resolver type is annotation-only.
    from .public import ClientIpResolver

    # ``admin`` reads ``gfs_client_ip_key`` off the app, so it imports this
    # module — the auth type stays annotation-only to keep that one-way.
    from .admin import AdminAuth

gfs_db_key: AppKey[AsyncDatabase] = AppKey("gfs_db")
gfs_config_key: AppKey[GfsConfig] = AppKey("gfs_config")
#: Resolves a request's client IP under the configured trusted-proxy policy.
#: One parsed instance per server — never build one per request.
gfs_client_ip_key: "AppKey[ClientIpResolver]" = AppKey("gfs_client_ip")
gfs_federation_key: AppKey[GfsFederationService] = AppKey("gfs_federation")
gfs_fed_repo_key: AppKey[AbstractGfsFederationRepo] = AppKey("gfs_fed_repo")
gfs_admin_repo_key: AppKey[AbstractGfsAdminRepo] = AppKey("gfs_admin_repo")
gfs_admin_auth_key: "AppKey[AdminAuth]" = AppKey("gfs_admin_auth")
gfs_admin_service_key: AppKey[GfsAdminService] = AppKey("gfs_admin_service")
gfs_cluster_key: AppKey[ClusterService] = AppKey("gfs_cluster")
gfs_cluster_repo_key: AppKey[AbstractClusterRepo] = AppKey("gfs_cluster_repo")
gfs_rtc_key: AppKey[GfsRtcSession] = AppKey("gfs_rtc")
gfs_relay_bridge_key: AppKey[RelayBridge] = AppKey("gfs_relay_bridge")
gfs_ws_registry_key: AppKey[GfsWebSocketRegistry] = AppKey("gfs_ws_registry")
gfs_http_session_key: AppKey[aiohttp.ClientSession] = AppKey("gfs_http_session")
gfs_highlight_pub_repo_key: AppKey[AbstractGfsHighlightPublicationRepo] = AppKey(
    "gfs_highlight_pub_repo"
)
gfs_highlight_token_repo_key: AppKey[AbstractGfsHighlightTokenRepo] = AppKey(
    "gfs_highlight_token_repo"
)
gfs_highlight_pub_service_key: AppKey[HighlightPublicationRegistry] = AppKey(
    "gfs_highlight_pub_service"
)
gfs_moment_public_user_repo_key: AppKey[AbstractGfsUserRegistrationRepo] = AppKey(
    "gfs_moment_public_user_repo"
)
gfs_moment_public_follow_repo_key: AppKey[AbstractGfsMomentFollowRepo] = AppKey(
    "gfs_moment_public_follow_repo"
)
gfs_moment_public_registry_key: AppKey[MomentPublicRegistry] = AppKey(
    "gfs_moment_public_registry"
)
gfs_user_picture_repo_key: AppKey[AbstractGfsUserPictureRepo] = AppKey(
    "gfs_user_picture_repo"
)

#: Store-and-forward queue backing ``POST /gfs/envelope`` (§D2b).
gfs_envelope_queue_repo_key: AppKey[AbstractGfsEnvelopeQueueRepo] = AppKey(
    "gfs_envelope_queue_repo"
)
#: Deliver-or-queue relay for opaque household-to-household envelopes.
gfs_envelope_relay_key: AppKey[GfsEnvelopeRelay] = AppKey("gfs_envelope_relay")

#: Bulletin board of owner-minted invite links (§24.8.5).
gfs_invite_repo_key: AppKey[AbstractGfsInviteRepo] = AppKey("gfs_invite_repo")
#: Mint / revoke / look up invite links. The public ``GET /join/{token}`` page
#: reads THROUGH this (never around it into the database) so the "a fetch
#: writes nothing" rule has exactly one place it could be broken.
gfs_invite_service_key: AppKey[GfsInviteService] = AppKey("gfs_invite_service")
