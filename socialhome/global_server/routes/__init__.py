"""Route views for the GFS aiohttp application.

Each module groups views by URL concern:

* :mod:`.relay`   — ``/gfs/*`` HTTPS relay endpoints (register, plus
  HTTPS-fallback variants of publish / subscribe / report / appeal +
  spaces / healthz).
* :mod:`.ws`      — ``/gfs/ws`` SH↔GFS WebSocket transport (spec §24.12,
  primary path).
* :mod:`.cluster` — ``/cluster/*`` + admin cluster tab.
* :mod:`.rtc`     — ``/gfs/rtc/*`` SH↔SH WebRTC signalling rendezvous
  for §4.2.3 direct sync (the GFS holds no PeerConnection here).
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
from .envelope import EnvelopeRelayView
from .invites import SpaceInviteTokenView, SpaceInviteView
from .cluster import (
    ClusterHealthView,
    ClusterSignalingBeginView,
    ClusterSignalingEndView,
    ClusterSyncView,
)
from .relay import (
    AppealView,
    GfsInfoView,
    HealthzView,
    InstanceUpdateView,
    PublishView,
    RegisterView,
    ReportView,
    SpaceDetailView,
    SpacePublishView,
    SpacesListView,
    SpaceSubscribersView,
    SpaceUnpublishView,
    SubscribeView,
)
from .rtc import (
    RtcAnswerView,
    RtcIceView,
    RtcOfferView,
    RtcPingView,
    RtcSessionView,
)
from .highlights import (
    HighlightPublicLandingView,
    HighlightPublishView,
    HighlightTokenMintView,
    HighlightTokenRevokeView,
    HighlightUnpublishView,
)
from .highlight_rtc import (
    HighlightIceServersView,
    HighlightRelayStreamView,
    HighlightRelayUploadView,
    HighlightRtcAnswerView,
    HighlightRtcAuthorIceView,
    HighlightRtcOfferView,
    HighlightRtcSessionView,
    HighlightRtcViewerIceView,
)
from .moment_rtc import (
    MomentRelayStreamView,
    MomentRelayUploadView,
    MomentRtcAnswerView,
    MomentRtcAuthorIceView,
    MomentRtcOfferView,
    MomentRtcSessionView,
    MomentRtcViewerIceView,
)
from .moments_public import (
    GfsMomentPublicDeleteView,
    GfsMomentPublicPublishView,
    GfsUserDeregisterView,
    GfsUserDetailHtmlView,
    GfsUserDetailView,
    GfsUserDirectoryHtmlView,
    GfsUserDirectoryView,
    GfsUserFollowView,
    GfsUserPictureView,
    GfsUserRegisterView,
    GfsUserUnfollowView,
)
from .ws import GfsWebSocketView


def register_routes(
    app: web.Application,
    *,
    admin_ui_dir: Path,
    media_dir: str,
    public_static_dir: Path | None = None,
) -> None:
    """Mount every GFS view onto *app*.

    Accepts ``admin_ui_dir`` for the admin-static mount, ``media_dir``
    (absolute) for the public ``/media/`` file mount, and the optional
    ``public_static_dir`` that holds the §highlights_public viewer
    bundle. Tests pass ``None`` for the static dir when they don't
    care about the public landing JS.
    """
    # Pairing handshake (spec §24.12 — the only HTTPS hop in the
    # primary flow; everything afterwards rides the WebSocket below).
    # ``/gfs/info`` is the public-key descriptor HFS clients fetch after
    # scanning the QR (which only ships ``{base_url, token}``); they
    # then POST to ``/gfs/register`` with ``{token, instance_id,
    # public_key, inbox_url}``.
    app.router.add_view("/gfs/info", GfsInfoView)
    app.router.add_view("/gfs/register", RegisterView)
    # Signed self-service rename for an already-registered instance — the
    # pairing token is single-use, so re-registering to change the name
    # isn't possible (verify-then-mutate against the registered pubkey).
    app.router.add_view("/gfs/instance", InstanceUpdateView)

    # Persistent SH↔GFS WebSocket — primary transport (spec §24.12).
    app.router.add_view("/gfs/ws", GfsWebSocketView)

    # HTTPS-fallback wire endpoints — used when the WebSocket cannot
    # stay open. The protocol is otherwise identical to the WS frames.
    app.router.add_view("/gfs/publish", PublishView)
    app.router.add_view("/gfs/subscribe", SubscribeView)
    # Opaque household-to-household relay (§D2b invite bootstrap). Anonymous
    # by design: the body names only the recipient, the payload is sealed
    # end-to-end, and every well-formed request gets the same 202.
    app.router.add_view("/gfs/envelope", EnvelopeRelayView)
    app.router.add_view("/gfs/report", ReportView)
    app.router.add_view("/gfs/appeal", AppealView)
    app.router.add_view("/gfs/spaces", SpacesListView)
    app.router.add_view("/gfs/spaces/{space_id}", SpaceDetailView)
    app.router.add_view("/gfs/spaces/{space_id}/subscribers", SpaceSubscribersView)
    app.router.add_view("/gfs/spaces/{space_id}/publish", SpacePublishView)
    app.router.add_view("/gfs/spaces/{space_id}/unpublish", SpaceUnpublishView)
    # Owner-minted invite links (§24.8.5). The mint/revoke pair is signed by
    # the owning household; the public half is ``GET /join/{gfs_token}``
    # below.
    app.router.add_view("/gfs/spaces/{space_id}/invite", SpaceInviteView)
    app.router.add_view(
        "/gfs/spaces/{space_id}/invite/{gfs_token}",
        SpaceInviteTokenView,
    )
    app.router.add_view("/healthz", HealthzView)

    # Public SSR pages (spec §24.7 / §24.8) — staying procedural since
    # they only implement GET and need no auth / body parsing.
    app.router.add_get("/", handle_landing)
    app.router.add_get("/spaces/{slug}", handle_space_page)
    app.router.add_get("/join/{gfs_token}", handle_invite_page)

    # Cluster (spec §24.10).
    app.router.add_view("/cluster/sync", ClusterSyncView)
    app.router.add_view("/cluster/health", ClusterHealthView)
    # Spec §24.10.7 — round-robin sync-signaling node selection.
    app.router.add_view(
        "/cluster/signaling-session",
        ClusterSignalingBeginView,
    )
    app.router.add_view(
        "/cluster/signaling-session/release",
        ClusterSignalingEndView,
    )

    # SH↔SH WebRTC signalling rendezvous for §4.2.3 direct DataChannel
    # sync. The GFS is a public bulletin board for SDP offers / answers /
    # ICE candidates so two NATted households can find each other; the
    # GFS itself holds no PeerConnection.
    app.router.add_view("/gfs/rtc/offer", RtcOfferView)
    app.router.add_view("/gfs/rtc/answer", RtcAnswerView)
    app.router.add_view("/gfs/rtc/ice", RtcIceView)
    app.router.add_view("/gfs/rtc/ping", RtcPingView)
    app.router.add_view("/gfs/rtc/session/{session_id}", RtcSessionView)

    # Public highlight publications (§highlights_public). Signed wire endpoints
    # for author SH instances; the public landing is anonymous.
    app.router.add_view(
        "/gfs/highlights/{highlight_id}/publish",
        HighlightPublishView,
    )
    app.router.add_view(
        "/gfs/highlights/{highlight_id}/tokens",
        HighlightTokenMintView,
    )
    app.router.add_view(
        "/gfs/highlight_tokens/{token}/revoke",
        HighlightTokenRevokeView,
    )
    app.router.add_view(
        "/gfs/highlights/{highlight_id}/unpublish",
        HighlightUnpublishView,
    )
    app.router.add_view(
        "/highlight/{instance_id}/{highlight_id}/{token}",
        HighlightPublicLandingView,
    )

    # Public-viewer signalling for the public-highlight flow. The offer
    # endpoint is anonymous (token-gated); the answer endpoints are
    # Ed25519-signed by the author SH like the rest of /gfs/rtc/*.
    app.router.add_view("/gfs/highlights/ice-servers", HighlightIceServersView)
    app.router.add_view("/gfs/highlight_rtc/offer", HighlightRtcOfferView)
    app.router.add_view(
        "/gfs/highlight_rtc/session/{session_id}",
        HighlightRtcSessionView,
    )
    app.router.add_view("/gfs/highlight_rtc/ice/viewer", HighlightRtcViewerIceView)
    app.router.add_view("/gfs/highlight_rtc/answer", HighlightRtcAnswerView)
    app.router.add_view("/gfs/highlight_rtc/ice/author", HighlightRtcAuthorIceView)
    # GFS-relay fallback: anon guest stream (token-gated) + signed author upload.
    app.router.add_view(
        "/gfs/highlight_rtc/relay/{instance_id}/{highlight_id}",
        HighlightRelayStreamView,
    )
    app.router.add_view(
        "/gfs/highlight_rtc/relay-stream/{relay_id}",
        HighlightRelayUploadView,
    )

    # Public Momentum (§Momentum-public). Signed wire endpoints for
    # registration / follow + unsigned discovery for households. The
    # whole surface lives under ``/gfs/moments/*`` so the public
    # Momentum feature is one namespace on the wire.
    app.router.add_view("/gfs/moments/users/register", GfsUserRegisterView)
    app.router.add_view(
        "/gfs/moments/users/{user_id}/deregister", GfsUserDeregisterView
    )
    app.router.add_view("/gfs/moments/users/{user_id}/follow", GfsUserFollowView)
    app.router.add_view("/gfs/moments/users/{user_id}/unfollow", GfsUserUnfollowView)
    app.router.add_view("/gfs/moments/publish", GfsMomentPublicPublishView)
    app.router.add_view("/gfs/moments/delete", GfsMomentPublicDeleteView)
    app.router.add_view("/gfs/moments/users/{user_id}/picture", GfsUserPictureView)
    app.router.add_view("/gfs/moments/users", GfsUserDirectoryView)
    app.router.add_view("/gfs/moments/users/{user_id}", GfsUserDetailView)
    app.router.add_view("/moments", GfsUserDirectoryHtmlView)
    app.router.add_view("/moments/{user_id}", GfsUserDetailHtmlView)

    # Public-viewer signalling for the public-moments live index
    # (§Momentum-public). The offer endpoint is anonymous (gated by the
    # user's live directory registration); the answer endpoints are
    # Ed25519-signed by the author SH like the rest of /gfs/rtc/*. The
    # GFS stores zero moment bytes — it brokers signalling and, on the
    # fallback path, pipes the framed bytes straight through.
    app.router.add_view("/gfs/moment_rtc/offer", MomentRtcOfferView)
    app.router.add_view(
        "/gfs/moment_rtc/session/{session_id}",
        MomentRtcSessionView,
    )
    app.router.add_view("/gfs/moment_rtc/ice/viewer", MomentRtcViewerIceView)
    app.router.add_view("/gfs/moment_rtc/answer", MomentRtcAnswerView)
    app.router.add_view("/gfs/moment_rtc/ice/author", MomentRtcAuthorIceView)
    # GFS-relay fallback: anon guest stream (registration-gated) + signed
    # author upload.
    app.router.add_view(
        "/gfs/moment_rtc/relay/{user_id}",
        MomentRelayStreamView,
    )
    app.router.add_view(
        "/gfs/moment_rtc/relay-stream/{relay_id}",
        MomentRelayUploadView,
    )

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
    if public_static_dir is not None and public_static_dir.is_dir():
        # Hosts the vanilla-JS public-viewer bundle for §highlights_public.
        # The landing page references ``/static/highlight_public_viewer.js``.
        app.router.add_static("/static/", str(public_static_dir))

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
