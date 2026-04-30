/**
 * SideNav — left sidebar navigation, organised into four IA groups:
 * **At home** (the household's own surfaces — feed, plan, share),
 * **Talk** (synchronous human comms), **Browse** (cross-cutting
 * context-switchers — Spaces / Bazaar / Corner) and **You** (personal
 * settings / admin / Parent Control). Empty groups (after feature-flag
 * gating) suppress their header entirely so a minimal household
 * configuration doesn't show empty section labels.
 *
 * The data lives in the GROUPS arrays below — purely declarative; the
 * render path filters items through the live state snapshot pulled
 * from auth / household-features / guardian signals.
 */
import { useLocation } from 'preact-iso'
import { useComputed } from '@preact/signals'
import { currentUser } from '@/store/auth'
import { isGuardian } from '@/store/guardian'
import { toggles } from '@/components/HouseholdToggles'
import { Avatar } from '@/components/Avatar'
import { Wordmark } from '@/components/Wordmark'
import { SideNavIcon, type SideNavIconName } from '@/components/SideNavIcon'

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
}

interface SideNavGroup {
  key: string
  label: string
  items: SideNavItem[]
}

interface SideNavState {
  isAdmin: boolean
  isGuardian: boolean
  feat_feed: boolean
  feat_calendar: boolean
  feat_tasks: boolean
  feat_pages: boolean
  feat_stickies: boolean
}

const ALL_ON: Omit<SideNavState, 'isAdmin' | 'isGuardian'> = {
  feat_feed: true,
  feat_calendar: true,
  feat_tasks: true,
  feat_pages: true,
  feat_stickies: true,
}

const HOME_GROUP: SideNavGroup = {
  key: 'home',
  label: 'At home',
  items: [
    { key: 'feed',     label: 'Feed',     href: '/',         icon: 'feed',
      gate: s => s.feat_feed },
    { key: 'calendar', label: 'Calendar', href: '/calendar', icon: 'calendar',
      gate: s => s.feat_calendar },
    { key: 'tasks',    label: 'Tasks',    href: '/tasks',    icon: 'tasks',
      gate: s => s.feat_tasks },
    { key: 'shopping', label: 'Shopping', href: '/shopping', icon: 'shopping' },
    { key: 'presence', label: 'Presence', href: '/presence', icon: 'presence' },
    { key: 'gallery',  label: 'Gallery',  href: '/gallery',  icon: 'gallery' },
    { key: 'pages',    label: 'Pages',    href: '/pages',    icon: 'pages',
      gate: s => s.feat_pages },
    { key: 'stickies', label: 'Stickies', href: '/stickies', icon: 'stickies',
      gate: s => s.feat_stickies },
  ],
}

const TALK_GROUP: SideNavGroup = {
  key: 'talk',
  label: 'Talk',
  items: [
    { key: 'messages', label: 'Messages', href: '/dms',   icon: 'messages' },
    { key: 'calls',    label: 'Calls',    href: '/calls', icon: 'calls' },
  ],
}

const BROWSE_GROUP: SideNavGroup = {
  key: 'browse',
  label: 'Browse',
  items: [
    { key: 'spaces', label: 'Spaces', href: '/spaces',    icon: 'spaces' },
    { key: 'bazaar', label: 'Bazaar', href: '/bazaar',    icon: 'bazaar' },
    { key: 'corner', label: 'Corner', href: '/dashboard', icon: 'corner' },
  ],
}

const YOU_GROUP: SideNavGroup = {
  key: 'you',
  label: 'You',
  items: [
    { key: 'parent-control', label: 'Parent Control', href: '/parent', icon: 'parent-control',
      gate: s => s.isGuardian },
    { key: 'settings',    label: 'Settings',    href: '/settings',    icon: 'settings' },
    { key: 'connections', label: 'Connections', href: '/connections', icon: 'connections',
      gate: s => s.isAdmin },
    { key: 'admin',       label: 'Admin',       href: '/admin',       icon: 'admin',
      gate: s => s.isAdmin },
  ],
}

const MAIN_GROUPS: readonly SideNavGroup[] = [HOME_GROUP, TALK_GROUP, BROWSE_GROUP]

export function SideNav() {
  const loc = useLocation()
  const view = useComputed(() => {
    const user = currentUser.value
    const t = toggles.value
    const state: SideNavState = {
      isAdmin: !!user?.is_admin,
      // null = still loading; treat as "not a guardian" so the link
      // doesn't flash on then off if loadGuardian resolves false.
      isGuardian: isGuardian.value === true,
      // Toggles haven't loaded yet → assume everything visible. Avoids
      // a "feature appears" flash once the API responds.
      ...(t
        ? {
            feat_feed: t.feat_feed,
            feat_calendar: t.feat_calendar,
            feat_tasks: t.feat_tasks,
            feat_pages: t.feat_pages,
            feat_stickies: t.feat_stickies,
          }
        : ALL_ON),
    }
    const filter = (g: SideNavGroup) => g.items.filter(i => i.gate ? i.gate(state) : true)
    return {
      main: MAIN_GROUPS
        .map(g => ({ group: g, items: filter(g) }))
        .filter(({ items }) => items.length > 0),
      you: { group: YOU_GROUP, items: filter(YOU_GROUP) },
      user,
    }
  })

  const { main, you, user } = view.value
  const currentPath = loc.path

  const renderGroup = (group: SideNavGroup, items: SideNavItem[]) => {
    const isActive = items.some(i => i.href === currentPath)
    const headerId = `sidenav-group-${group.key}`
    return (
      <nav
        key={group.key}
        class={`sh-sidenav-group${isActive ? ' sh-sidenav-group--active' : ''}`}
        aria-labelledby={headerId}
      >
        <h2 id={headerId} class="sh-sidenav-group-header">{group.label}</h2>
        {items.map(i => (
          <a
            key={i.key}
            href={i.href}
            aria-current={i.href === currentPath ? 'page' : undefined}
          >
            <SideNavIcon name={i.icon} />
            <span class="sh-sidenav-link-label">{i.label}</span>
          </a>
        ))}
      </nav>
    )
  }

  return (
    <aside class="sh-sidenav" aria-label="Sidebar">
      <Wordmark as="a" href="/" size={28} className="sh-sidenav-brand" />
      {main.map(({ group, items }) => renderGroup(group, items))}
      {you.items.length > 0 && (
        <>
          <hr class="sh-sidenav-divider" />
          {renderGroup(you.group, you.items)}
        </>
      )}
      {user && (
        <div class="sh-sidenav-identity" aria-label="Signed in user">
          <Avatar src={user.picture_url} name={user.display_name} size={32} />
          <span class="sh-sidenav-identity__name">{user.display_name}</span>
        </div>
      )}
    </aside>
  )
}
