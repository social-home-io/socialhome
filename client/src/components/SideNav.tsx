/**
 * SideNav — left sidebar navigation, organised into four IA groups:
 * **At home** (the household's own surfaces — feed, plan, share),
 * **Talk** (synchronous human comms), **Browse** (cross-cutting
 * context-switchers — Spaces / Bazaar / Corner) and **Local** (admin /
 * Federation / Parent Control — gated entry points for the people
 * who run this household). Personal settings are reached by clicking
 * the user identity strip at the bottom of the sidebar. Empty groups
 * (after feature-flag gating) suppress their header entirely so a
 * minimal household configuration doesn't show empty section labels.
 *
 * The data lives in the GROUPS arrays below — purely declarative; the
 * render path filters items through the live state snapshot pulled
 * from auth / household-features / guardian signals.
 */
import type { ComponentChildren } from 'preact'
import { useEffect } from 'preact/hooks'
import { useLocation } from 'preact-iso'
import { useComputed } from '@preact/signals'
import { currentUser } from '@/store/auth'
import { isGuardian } from '@/store/guardian'
import { isSupervisorAddon } from '@/platform'
import { active as activeCalls } from '@/store/calls'
import { dmUnreadTotal } from '@/store/dms'
import { spaces, loadSpaces } from '@/store/spaces'
import { toggles } from '@/components/HouseholdToggles'
import { userPreferences } from '@/store/userPreferences'
import { Avatar } from '@/components/Avatar'
import { Wordmark } from '@/components/Wordmark'
import { SideNavIcon, type SideNavIconName } from '@/components/SideNavIcon'
import { openShortcutsHelp } from '@/lib/shortcuts'
import { t } from '@/i18n/i18n'

interface SideNavItem {
  key: string
  label: string
  href: string
  icon: SideNavIconName
  /**
   * Visibility predicate. Receives the live state snapshot the
   * sidebar already pulled from signals; returns true when the link
   * should render. Default = always visible.
   */
  gate?: (s: SideNavState) => boolean
  /**
   * Optional unread badge — rendered as a count chip on the link
   * when the resolved value is positive. Cap rendering at 99+ is
   * applied at the badge component, not here.
   */
  badge?: (s: SideNavState) => number
}

interface SideNavGroup {
  key: string
  label: string
  items: SideNavItem[]
}

interface SideNavState {
  isAdmin: boolean
  isGuardian: boolean
  hasActiveCall: boolean
  dmUnread: number
  feat_feed: boolean
  feat_calendar: boolean
  feat_timetable: boolean
  feat_tasks: boolean
  feat_pages: boolean
  feat_stickies: boolean
  feat_presence: boolean
  feat_gallery: boolean
  hide_highlights: boolean
  hide_momentum: boolean
  hide_bazaar: boolean
}

const ALL_ON: Omit<SideNavState, 'isAdmin' | 'isGuardian' | 'hasActiveCall' | 'dmUnread'> = {
  feat_feed: true,
  feat_calendar: true,
  feat_timetable: true,
  feat_tasks: true,
  feat_pages: true,
  feat_stickies: true,
  feat_presence: true,
  feat_gallery: true,
  hide_highlights: false,
  hide_momentum: false,
  hide_bazaar: false,
}

const HOME_GROUP: SideNavGroup = {
  key: 'home',
  label: 'At home',
  items: [
    // ``/`` belongs to the Welcome surface (corner-light) — clicking
    // "Feed" should land on the actual feed, not the welcome card.
    // Both routes already resolve to FeedPage; the welcome surface
    // sits at ``/`` via LandingDispatch.
    { key: 'feed',     label: 'Feed',     href: '/feed',     icon: 'feed',
      gate: s => s.feat_feed },
    // The Calendar page also hosts the Timetable tab, so it stays in
    // the nav while either feature is on.
    { key: 'calendar', label: 'Calendar', href: '/calendar', icon: 'calendar',
      gate: s => s.feat_calendar || s.feat_timetable },
    // Tasks · Shopping · Stickies share a single hub at /organize —
    // individually low-traffic, collectively crowded the sidebar; the
    // hub renders them as tabs with live count chips ("Tasks · 3 ·
    // Shopping · 4 · Stickies"). Hidden iff every underlying feature
    // is disabled, which keeps minimal household configurations from
    // showing a dead nav row.
    { key: 'organize', label: 'Organize', href: '/organize', icon: 'tasks',
      gate: s => s.feat_tasks || s.feat_stickies },
    { key: 'presence', label: 'Presence', href: '/presence', icon: 'presence',
      gate: s => s.feat_presence },
    { key: 'gallery',  label: 'Gallery',  href: '/gallery',  icon: 'gallery',
      gate: s => s.feat_gallery },
    { key: 'pages',    label: 'Pages',    href: '/pages',    icon: 'pages',
      gate: s => s.feat_pages },
  ],
}

const TALK_GROUP: SideNavGroup = {
  key: 'talk',
  label: 'Talk',
  items: [
    { key: 'messages', label: 'Chats',    href: '/dms',     icon: 'messages',
      badge: s => s.dmUnread },
    // Time-critical fast lane: only renders while a call is live so
    // the user can hop back in one click. The Chats panel's Calls
    // tab stays the canonical surface (history, hang-up controls).
    { key: 'calls',    label: 'Calls',    href: '/dms?tab=calls', icon: 'calls',
      gate: s => s.hasActiveCall },
    // Highlights + Momentum are user-level features — gated by the user's own
    // hide_* preferences (from /api/me/preferences), not household toggles.
    { key: 'highlights',  label: 'Highlights',  href: '/highlights', icon: 'highlights',
      gate: s => !s.hide_highlights },
    { key: 'momentum', label: 'Momentum', href: '/momentum', icon: 'momentum',
      gate: s => !s.hide_momentum },
  ],
}

const BROWSE_GROUP: SideNavGroup = {
  key: 'browse',
  label: 'Browse',
  items: [
    { key: 'spaces',  label: 'Spaces', href: '/spaces',  icon: 'spaces' },
    { key: 'friends', label: 'Friends', href: '/friends', icon: 'connections' },
    { key: 'bazaar',  label: 'Bazaar', href: '/bazaar',  icon: 'bazaar',
      gate: s => !s.hide_bazaar },
    { key: 'corner',  label: 'Corner', href: '/corner',  icon: 'corner' },
    { key: 'apps',    label: 'Apps',   href: '/apps',    icon: 'apps' },
  ],
}

const LOCAL_GROUP: SideNavGroup = {
  key: 'local',
  label: 'Settings',
  items: [
    // First-class entry to personal settings for every authenticated
    // user (no ``gate``). Previously the only path was the identity
    // strip at the bottom of the sidebar — visually unobvious enough
    // that non-admins reported "I can't find where to change my
    // profile". Lifting it into the sidebar group makes the path the
    // same regardless of role.
    { key: 'personal', label: 'Personal', href: '/settings', icon: 'person' },
    { key: 'parent-control', label: 'Parent Control', href: '/parent', icon: 'parent-control',
      gate: s => s.isGuardian },
    // Labelled "Federation" in the sidebar to disambiguate from the
    // Browse-group "Friends" link (people-you-know vs federated
    // households). Route + key stay ``/connections`` so URL bookmarks
    // and i18n keys keep working.
    { key: 'connections', label: 'Federation', href: '/connections', icon: 'connections',
      gate: s => s.isAdmin },
    { key: 'admin',       label: 'Admin',       href: '/admin',       icon: 'admin',
      gate: s => s.isAdmin },
  ],
}

const MAIN_GROUPS: readonly SideNavGroup[] = [HOME_GROUP, TALK_GROUP, BROWSE_GROUP]

export function SideNav() {
  const loc = useLocation()

  // Lazy-load the spaces list so a deep-link into a space (or any
  // first-render that doesn't pass through ``/spaces``) can still
  // resolve the active space's name + emoji for the sidebar
  // sub-row. Guard against an undefined signal value (test mocks of
  // ``@/api`` resolve ``api.get`` to ``undefined``, which the store
  // assigns straight through).
  useEffect(() => {
    if (!spaces.value || spaces.value.length === 0) void loadSpaces()
  }, [])

  // Resolve the active space when the URL points at a specific space
  // page (``/spaces/{id}`` and its sub-routes), excluding ``/spaces``
  // (the listing page) and ``/spaces/browse`` (the public catalogue).
  const activeSpaceFromUrl = useComputed(() => {
    const path = loc.path ?? ''
    const m = path.match(/^\/spaces\/([^/]+)/)
    if (!m) return null
    const id = m[1]
    if (id === 'browse') return null
    const list = spaces.value
    if (!Array.isArray(list)) return null
    return list.find(s => s.id === id) ?? null
  })

  const view = useComputed(() => {
    const user = currentUser.value
    const t = toggles.value
    // The identity strip only renders outside haos mode. In haos mode
    // the SH SPA is iframed under HA Core's left sidebar, which already
    // shows the signed-in user — a second avatar in our sidebar
    // doubles up. ha and standalone modes keep the strip; it's the
    // user's only "who am I signed in as" cue when SH is the primary
    // UI surface.
    const isHaos = isSupervisorAddon()
    const up = userPreferences.value
    const state: SideNavState = {
      isAdmin: !!user?.is_admin,
      // null = still loading; treat as "not a guardian" so the link
      // doesn't flash on then off if loadGuardian resolves false.
      isGuardian: isGuardian.value === true,
      hasActiveCall: activeCalls.value.length > 0,
      dmUnread: dmUnreadTotal.value,
      // Toggles haven't loaded yet → assume everything visible. Avoids
      // a "feature appears" flash once the API responds.
      ...(t
        ? {
            feat_feed: t.feat_feed,
            feat_calendar: t.feat_calendar,
            feat_timetable: t.feat_timetable,
            feat_tasks: t.feat_tasks,
            feat_pages: t.feat_pages,
            feat_stickies: t.feat_stickies,
            feat_presence: t.feat_presence,
            feat_gallery: t.feat_gallery,
          }
        : ALL_ON),
      // User-level hide_* prefs — default to false (show) while prefs
      // haven't loaded yet so links don't flash off then on.
      hide_highlights: up.hide_highlights,
      hide_momentum: up.hide_momentum,
      hide_bazaar: up.hide_bazaar,
    }
    const filter = (g: SideNavGroup) => g.items.filter(i => i.gate ? i.gate(state) : true)
    return {
      main: MAIN_GROUPS
        .map(g => ({ group: g, items: filter(g) }))
        .filter(({ items }) => items.length > 0),
      local: { group: LOCAL_GROUP, items: filter(LOCAL_GROUP) },
      user,
      state,
      isHaos,
    }
  })

  const { main, local, user, isHaos } = view.value
  const currentPath = loc.path

  const state = view.value.state
  const activeSpace = activeSpaceFromUrl.value
  const renderGroup = (
    group: SideNavGroup,
    items: SideNavItem[],
    extra?: ComponentChildren,
  ) => {
    const isActive = items.some(i => i.href === currentPath)
      || (group.key === 'browse' && activeSpace !== null)
    const headerId = `sidenav-group-${group.key}`
    return (
      <nav
        key={group.key}
        class={`sh-sidenav-group${isActive ? ' sh-sidenav-group--active' : ''}`}
        aria-labelledby={headerId}
      >
        <h2 id={headerId} class="sh-sidenav-group-header">{group.label}</h2>
        {items.map(i => {
          const count = i.badge ? i.badge(state) : 0
          // The "Spaces" link is the parent of the active-space row;
          // it lights up when the URL is ``/spaces*`` (any sub-route)
          // even though the deep URL doesn't equal ``/spaces`` exactly.
          const ariaCurrent =
            i.href === currentPath ||
            (i.key === 'spaces' && activeSpace !== null)
              ? 'page'
              : undefined
          return (
            <>
              <a
                key={i.key}
                href={i.href}
                aria-current={ariaCurrent}
              >
                <SideNavIcon name={i.icon} />
                <span class="sh-sidenav-link-label">{i.label}</span>
                {count > 0 && (
                  <span class="sh-sidenav-badge" aria-label={`${count} unread`}>
                    {count > 99 ? '99+' : count}
                  </span>
                )}
              </a>
              {/* Active-space sub-row anchors "you are here" inside
               *  Spaces. Renders right under the parent "Spaces" link,
               *  one indent deeper, and only when the URL points at a
               *  specific space (so the sub-row doesn't ghost in on
               *  the ``/spaces`` listing page). */}
              {i.key === 'spaces' && activeSpace && (
                <a
                  key={`spaces-active-${activeSpace.id}`}
                  href={`/spaces/${activeSpace.id}`}
                  class="sh-sidenav-subitem sh-sidenav-subitem--active"
                  aria-current="page"
                >
                  <span class="sh-sidenav-subitem__emoji" aria-hidden="true">
                    {activeSpace.emoji || '🌐'}
                  </span>
                  <span class="sh-sidenav-link-label">
                    {activeSpace.name}
                  </span>
                </a>
              )}
            </>
          )
        })}
        {extra}
      </nav>
    )
  }

  return (
    <aside class="sh-sidenav" aria-label="Sidebar">
      <Wordmark as="a" href="/" size={28} className="sh-sidenav-brand" />
      {main.map(({ group, items }) => renderGroup(group, items))}
      {local.items.length > 0 && (
        <>
          <hr class="sh-sidenav-divider" />
          {renderGroup(
            local.group,
            local.items,
            // Discoverability for the ``?`` help sheet — a button, not
            // a link: it opens a dialog, it doesn't navigate.
            <button
              type="button"
              class="sh-sidenav-action"
              aria-haspopup="dialog"
              onClick={openShortcutsHelp}
            >
              <SideNavIcon name="keyboard" />
              <span class="sh-sidenav-link-label">{t('shortcuts.menu_entry')}</span>
            </button>,
          )}
        </>
      )}
      {user && !isHaos && (
        <a
          href="/settings"
          class="sh-sidenav-identity"
          aria-label={`Signed in as ${user.display_name} — open settings`}
          aria-current={currentPath === '/settings' ? 'page' : undefined}
        >
          <Avatar src={user.picture_url} name={user.display_name} size={32} />
          <span class="sh-sidenav-identity__name">{user.display_name}</span>
        </a>
      )}
    </aside>
  )
}
