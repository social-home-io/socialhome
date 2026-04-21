"""Route views for the GFS aiohttp application.

Each module groups views by URL concern:

* :mod:`.relay`   — ``/gfs/*`` relay endpoints (register / publish /
  subscribe / report / appeal / spaces / healthz).
* :mod:`.cluster` — ``/cluster/*`` + admin cluster tab.
* :mod:`.rtc`     — ``/gfs/rtc/*`` WebRTC signalling.
* :mod:`.admin`   — ``/admin/api/*`` admin portal JSON routes + static
  index + ``/admin/login`` / ``/admin/logout``.

:func:`register_routes` is the single entry point the GFS app uses to
wire every view onto an :class:`aiohttp.web.Application`.
"""

from __future__ import annotations

from pathlib import Path

from aiohttp import web

from .. import admin as admin_mod
from ..public import handle_invite_page, handle_landing, handle_space_page
from .admin import (
    AdminAppealCollectionView,
    AdminAppealDecideView,
    AdminAuditView,
    AdminBrandingHeaderImageView,
    AdminBrandingView,
    AdminClientActionView,
    AdminClientCollectionView,
    AdminClusterCollectionView,
    AdminClusterPeerCollectionView,
    AdminClusterPeerDetailView,
    AdminClusterPeerPingView,
    AdminOverviewView,
    AdminPolicyView,
    AdminReportCollectionView,
    AdminReportReviewView,
    AdminSpaceActionView,
    AdminSpaceCollectionView,
    AdminUiIndexView,
)
from .cluster import ClusterHealthView, ClusterSyncView
from .relay import (
    AppealView,
    HealthzView,
    PublishView,
    RegisterView,
    ReportView,
    SpacesListView,
    SubscribeView,
)
from .rtc import (
    RtcAnswerView,
    RtcIceView,
    RtcOfferView,
    RtcPingView,
    RtcSessionView,
)


def register_routes(
    app: web.Application, *, admin_ui_dir: Path, media_dir: str
) -> None:
    """Mount every GFS view onto *app*.

    Accepts ``admin_ui_dir`` for the admin-static mount and ``media_dir``
    (absolute) for the public ``/media/`` file mount so deployment layout
    stays in the caller's hands.
    """
    # Relay + public GFS wire endpoints.
    app.router.add_view("/gfs/register", RegisterView)
    app.router.add_view("/gfs/publish", PublishView)
    app.router.add_view("/gfs/subscribe", SubscribeView)
    app.router.add_view("/gfs/report", ReportView)
    app.router.add_view("/gfs/appeal", AppealView)
    app.router.add_view("/gfs/spaces", SpacesListView)
    app.router.add_view("/healthz", HealthzView)

    # Public SSR pages (spec §24.7 / §24.8) — staying procedural since
    # they only implement GET and need no auth / body parsing.
    app.router.add_get("/", handle_landing)
    app.router.add_get("/spaces/{slug}", handle_space_page)
    app.router.add_get("/join/{gfs_token}", handle_invite_page)

    # Cluster (spec §24.10).
    app.router.add_view("/cluster/sync", ClusterSyncView)
    app.router.add_view("/cluster/health", ClusterHealthView)

    # WebRTC DataChannel signalling (spec §24.12).
    app.router.add_view("/gfs/rtc/offer", RtcOfferView)
    app.router.add_view("/gfs/rtc/answer", RtcAnswerView)
    app.router.add_view("/gfs/rtc/ice", RtcIceView)
    app.router.add_view("/gfs/rtc/ping", RtcPingView)
    app.router.add_view("/gfs/rtc/session/{session_id}", RtcSessionView)

    # Admin portal — login/logout stay as module-level functions in
    # ``global_server.admin`` since they wire cookie lifecycle.
    app.router.add_post("/admin/login", admin_mod.handle_login)
    app.router.add_post("/admin/logout", admin_mod.handle_logout)

    # Admin static UI.
    app.router.add_view("/admin", AdminUiIndexView)
    if admin_ui_dir.is_dir():
        app.router.add_static("/admin/static/", admin_ui_dir)
    if Path(media_dir).is_dir():
        app.router.add_static("/media/", media_dir)

    # Admin JSON API (all behind admin_auth_middleware).
    app.router.add_view("/admin/api/overview", AdminOverviewView)
    app.router.add_view("/admin/api/clients", AdminClientCollectionView)
    app.router.add_view(
        "/admin/api/clients/{instance_id}/{action}",
        AdminClientActionView,
    )
    app.router.add_view("/admin/api/spaces", AdminSpaceCollectionView)
    app.router.add_view(
        "/admin/api/spaces/{space_id}/{action}",
        AdminSpaceActionView,
    )
    app.router.add_view("/admin/api/policy", AdminPolicyView)
    app.router.add_view("/admin/api/branding", AdminBrandingView)
    app.router.add_view(
        "/admin/api/branding/header-image",
        AdminBrandingHeaderImageView,
    )
    app.router.add_view("/admin/api/reports", AdminReportCollectionView)
    app.router.add_view(
        "/admin/api/reports/{report_id}/review",
        AdminReportReviewView,
    )
    app.router.add_view("/admin/api/appeals", AdminAppealCollectionView)
    app.router.add_view(
        "/admin/api/appeals/{appeal_id}/decide",
        AdminAppealDecideView,
    )
    app.router.add_view("/admin/api/audit", AdminAuditView)

    app.router.add_view("/admin/api/cluster", AdminClusterCollectionView)
    app.router.add_view(
        "/admin/api/cluster/peers",
        AdminClusterPeerCollectionView,
    )
    app.router.add_view(
        "/admin/api/cluster/peers/{node_id}",
        AdminClusterPeerDetailView,
    )
    app.router.add_view(
        "/admin/api/cluster/peers/{node_id}/ping",
        AdminClusterPeerPingView,
    )


__all__ = ["register_routes"]
