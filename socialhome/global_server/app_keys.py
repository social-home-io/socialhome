"""Typed aiohttp app keys for the GFS process."""

from __future__ import annotations

import aiohttp
from aiohttp.web import AppKey

from ..db import AsyncDatabase
from .admin import AdminAuth
from .admin_service import GfsAdminService
from .cluster import ClusterService
from .config import GfsConfig
from .federation import GfsFederationService
from .repositories import (
    AbstractClusterRepo,
    AbstractGfsAdminRepo,
    AbstractGfsFederationRepo,
)
from .rtc_transport import GfsRtcSession

gfs_db_key: AppKey[AsyncDatabase] = AppKey("gfs_db")
gfs_config_key: AppKey[GfsConfig] = AppKey("gfs_config")
gfs_federation_key: AppKey[GfsFederationService] = AppKey("gfs_federation")
gfs_fed_repo_key: AppKey[AbstractGfsFederationRepo] = AppKey("gfs_fed_repo")
gfs_admin_repo_key: AppKey[AbstractGfsAdminRepo] = AppKey("gfs_admin_repo")
gfs_admin_auth_key: AppKey[AdminAuth] = AppKey("gfs_admin_auth")
gfs_admin_service_key: AppKey[GfsAdminService] = AppKey("gfs_admin_service")
gfs_cluster_key: AppKey[ClusterService] = AppKey("gfs_cluster")
gfs_cluster_repo_key: AppKey[AbstractClusterRepo] = AppKey("gfs_cluster_repo")
gfs_rtc_key: AppKey[GfsRtcSession] = AppKey("gfs_rtc")
gfs_http_session_key: AppKey[aiohttp.ClientSession] = AppKey("gfs_http_session")
