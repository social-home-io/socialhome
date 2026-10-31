import { lazy } from 'preact-iso'

/** One Momentum host for its tabs (Moments, Yours, Discover). */
const MomentumPage = lazy(() => import('@/features/momentum/MomentumPage'))

export const routes: Record<string, ReturnType<typeof lazy>> = {
  '/':               lazy(() => import('@/features/feed/LandingDispatch')),
  '/feed':           lazy(() => import('@/features/feed/FeedPage')),
  // One household post with its comments — a notification's target.
  '/post/:id':       lazy(() => import('@/features/feed/PostPermalinkPage')),
  '/spaces':         lazy(() => import('@/features/spaces/SpaceListPage')),
  '/spaces/browse':  lazy(() => import('@/features/spaces/SpaceBrowseRedirect')),
  '/spaces/:id/about': lazy(() => import('@/features/spaces/SpacePublicDetailPage')),
  // One settings page; each section has its own address
  // (``/spaces/:id/settings/members`` …). One route, optional segment, so a
  // section jump re-renders instead of remounting (a dirty form survives).
  '/spaces/:id/settings/:section?': lazy(() => import('@/features/spaces/SpaceSettingsPage')),
  '/spaces/:id/zones': lazy(() => import('@/features/spaces/SpaceZonesPage')),
  // A space and its sections: ``/spaces/:id`` is the feed, ``/spaces/:id/
  // chat``, ``…/calendar`` etc. the rest. ONE route (optional segment) so a
  // section switch re-renders the page instead of remounting it. The
  // first match wins, so it sits after the fixed sub-pages above.
  '/spaces/:id/:section?': lazy(() => import('@/features/spaces/SpaceFeedPage')),
  '/join':           lazy(() => import('@/features/spaces/SpaceJoinLanding')),
  '/dms':            lazy(() => import('@/features/dms/DmInboxPage')),
  '/dms/:id':        lazy(() => import('@/features/dms/DmThreadPage')),
  '/dms/:id/calls':  lazy(() => import('@/features/dms/CallHistoryPane')),
  '/calendar':       lazy(() => import('@/features/calendar/CalendarPage')),
  // Tasks · Shopping · Stickies share a single Organize hub now —
  // each was its own routed page that crowded the AT-HOME sidebar.
  '/organize':       lazy(() => import('@/features/organize/OrganizePage')),
  '/presence':       lazy(() => import('@/features/presence/PresencePage')),
  '/settings':       lazy(() => import('@/features/settings/SettingsPage')),
  '/corner':         lazy(() => import('@/features/dashboard/DashboardPage')),
  '/dashboard':      lazy(() => import('@/features/dashboard/DashboardRedirect')),
  '/notifications':  lazy(() => import('@/features/notifications/NotificationsPage')),
  '/pages':          lazy(() => import('@/features/pages/PagesPage')),
  '/bazaar':         lazy(() => import('@/features/bazaar/BazaarPage')),
  '/apps':           lazy(() => import('@/features/apps/AppsPage')),
  '/apps/:appId':    lazy(() => import('@/features/apps/AppHost')),
  // What an app can do, and where it is added — never launches it.
  '/apps/:appId/about': lazy(() => import('@/features/apps/AppDetailPage')),
  // A group game's "Who's in" page (Join, Start, Leave…), Social Home's own.
  '/apps/:appId/tables/:sessionId': lazy(() => import('@/features/apps/groups/GameLobbyPage')),
  // Whom you give to in a gift draw — Social Home's own sheet, never the app.
  '/apps/:appId/draw/:sessionId': lazy(() => import('@/features/apps/draw/DrawResultPage')),
  '/admin':          lazy(() => import('@/features/admin/AdminPage')),
  '/connections':    lazy(() => import('@/features/connections/ConnectionsPage')),
  '/friends':        lazy(() => import('@/features/friends/FriendsPage')),
  '/gallery':        lazy(() => import('@/features/gallery/GalleryPage')),
  '/search':         lazy(() => import('@/features/search/SearchPage')),
  '/calls':          lazy(() => import('@/features/calls/CallsRedirect')),
  '/calls/:callId':  lazy(() => import('@/features/calls/InCallPage')),
  '/highlights':        lazy(() => import('@/features/highlights/HighlightsPage')),
  '/highlights/archive': lazy(() => import('@/features/highlights/HighlightsPage')),
  '/highlights/new':    lazy(() => import('@/features/highlights/HighlightComposerPage')),
  '/highlights/:highlightId': lazy(() => import('@/features/highlights/HighlightViewerPage')),
  '/momentum':              MomentumPage,
  '/momentum/archive':      lazy(() => import('@/features/momentum/MomentumArchiveRedirect')),
  // Yours is a tab with its own address — ahead of ``/momentum/:momentId``
  // (the first match wins).
  '/momentum/yours':        MomentumPage,
  '/momentum/discover':     MomentumPage,
  '/momentum/new':          lazy(() => import('@/features/momentum/MomentumComposerRedirect')),
  // Old addresses: Discover is a tab now (MomentumPage redirects), Public
  // sharing lives in Settings › Privacy.
  '/momentum/public/discover': MomentumPage,
  '/momentum/public/sharing':  lazy(() => import('@/features/momentum/PublicSharingRedirect')),
  '/momentum/:momentId':    lazy(() => import('@/features/momentum/MomentumDetailPage')),
  // The moment page with its reply field focused — the one reply entry point.
  '/momentum/:momentId/reply': lazy(() => import('@/features/momentum/MomentumDetailPage')),
  '/parent':         lazy(() => import('@/features/child-protection/ParentDashboard')),
  '/setup':          lazy(() => import('@/features/setup/SetupPage')),
}
