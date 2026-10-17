/**
 * MobileNav — bottom tab bar + sidebar drawer (§23.20).
 *
 * Two surfaces in one component:
 *
 * 1. A persistent 5-tab bar pinned to the bottom of the viewport on
 *    touch viewports (≤768px). Covers the most-trafficked routes
 *    (Feed / Spaces / DMs / Notifs) plus a "More" tab.
 *
 * 2. The "More" tab is a button (not a link) that toggles a slide-
 *    in drawer containing the full :class:`SideNav` — so every
 *    surface the desktop sidebar reaches (Calendar, Tasks, Shopping,
 *    Pages, Stickies, Friends, etc.) stays one tap away on mobile.
 *    Without this drawer the 4 routes covered by the bar would be
 *    the only thing reachable from a phone.
 *
 * Marks the active tab with both ``aria-current="page"`` (assistive
 * tech) and the ``sh-active`` class (visual). Without the active state
 * the bar gives no orientation cue, so a mobile user couldn't tell at
 * a glance which tab they were on.
 *
 * While the on-screen keyboard is up (``useKeyboardOpen``) the bar
 * slides away so the field being typed into keeps the room; it is
 * ``inert`` + ``aria-hidden`` meanwhile so neither focus nor a screen
 * reader lands on controls nobody can see, and it returns as soon as
 * the keyboard closes (blur, rotation, keyboard dismissed).
 */
import { useEffect, useRef } from 'preact/hooks'
import { signal } from '@preact/signals'
import { useLocation } from 'preact-iso'
import { SideNav } from '@/components/SideNav'
import { dmUnreadTotal } from '@/store/dms'
import { shortcutsHelpOpen } from '@/lib/shortcuts'
import { addBase } from '@/baseUrl'
import { useKeyboardOpen } from '@/hooks/useKeyboardOpen'
import { t } from '@/i18n/i18n'

interface Tab {
  href:   string
  emoji:  string
  /** Called at render so the label follows the UI language. */
  label:  () => string
  /** Match function — handles "is the user on this tab or a descendant?".
   *  Default is exact match against the path. */
  matches?: (path: string) => boolean
  /** Optional unread-count badge rendered on the tab icon. Read lazily
   *  (a signal accessor) so the count stays live. ``0`` hides it. */
  badge?: () => number
}

const TABS: readonly Tab[] = [
  // First tab is the "Welcome" landing (corner-light) at ``/`` —
  // that's the surface a returning user actually opens the app to.
  // Power users who want the household feed instead can pick it from
  // the drawer or change the landing-page preference in Settings.
  { href: '/',              emoji: '🏠', label: () => t('nav.home'),
    matches: (p) => p === '/' },
  { href: '/spaces',        emoji: '💬', label: () => t('nav.spaces'),
    matches: (p) => p === '/spaces' || p.startsWith('/spaces/') },
  { href: '/dms',           emoji: '✉️', label: () => t('nav.chats'),
    matches: (p) => p === '/dms' || p.startsWith('/dms/'),
    badge: () => dmUnreadTotal.value },
  { href: '/notifications', emoji: '🔔', label: () => t('nav.notifications_short'),
    matches: (p) => p.startsWith('/notifications') },
]

/** Drawer-open signal — exported so other surfaces (the topbar burger
 *  button, Esc handlers in dialogs) can close it without prop-drilling. */
export const mobileSidebarOpen = signal(false)

export function MobileNav() {
  const loc = useLocation()
  const path = loc.path
  const keyboardUp = useKeyboardOpen()
  const drawerRef = useRef<HTMLDivElement | null>(null)
  const moreRef = useRef<HTMLButtonElement | null>(null)

  // Auto-close the drawer on route change so tapping a sidebar link
  // dismisses the overlay without a second tap on the scrim. Esc also
  // closes via the keydown handler below.
  useEffect(() => {
    // Closing the drawer on path change — `path` is the only dep,
    // touching the signal inside is intentional.
    if (mobileSidebarOpen.value) mobileSidebarOpen.value = false
  }, [path])

  // The drawer's SideNav hosts the "Keyboard shortcuts" entry: close
  // the drawer when the help dialog opens so the dialog isn't stacked
  // on a second modal surface (one Esc would otherwise close both).
  useEffect(() => {
    if (shortcutsHelpOpen.value && mobileSidebarOpen.value) {
      mobileSidebarOpen.value = false
    }
  }, [shortcutsHelpOpen.value])

  // Esc closes the drawer. Opening moves focus onto the drawer (as
  // Modal does on touch) so Tab walks its links; the closed drawer is
  // inert, so Esc from inside it hands focus back to the More button
  // instead of dropping it on <body>.
  useEffect(() => {
    if (!mobileSidebarOpen.value) return
    drawerRef.current?.focus({ preventScroll: true })
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        const hadFocus = drawerRef.current?.contains(document.activeElement)
        mobileSidebarOpen.value = false
        if (hadFocus) moreRef.current?.focus()
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [mobileSidebarOpen.value])

  // Lock background scroll while the drawer is open so the sidebar's
  // own scroll doesn't drag the underlying page along with it.
  useEffect(() => {
    const html = document.documentElement
    if (mobileSidebarOpen.value) html.classList.add('sh-no-scroll')
    else html.classList.remove('sh-no-scroll')
    return () => html.classList.remove('sh-no-scroll')
  }, [mobileSidebarOpen.value])

  const moreActive = mobileSidebarOpen.value
  return (
    <>
      {mobileSidebarOpen.value && (
        <div
          class="sh-mobile-drawer-scrim"
          aria-hidden="true"
          onClick={() => (mobileSidebarOpen.value = false)}
        />
      )}
      {/* Closed, the drawer only slides off-screen (it stays mounted for
          the transition), so it is inert + aria-hidden: otherwise its
          SideNav links sit in the Tab order and screen readers still
          find a "Navigation" dialog nobody can see. */}
      <div
        ref={drawerRef}
        id="sh-mobile-drawer"
        tabIndex={-1}
        class={
          mobileSidebarOpen.value
            ? 'sh-mobile-drawer sh-mobile-drawer--open'
            : 'sh-mobile-drawer'
        }
        role="dialog"
        aria-modal={mobileSidebarOpen.value}
        aria-label={t('nav.drawer')}
        inert={!mobileSidebarOpen.value}
        aria-hidden={mobileSidebarOpen.value ? undefined : 'true'}
      >
        <SideNav />
      </div>
      <nav
        class={keyboardUp ? 'sh-mobile-nav sh-mobile-nav--kbd' : 'sh-mobile-nav'}
        role="navigation"
        aria-label={t('nav.mobile')}
        inert={keyboardUp}
        aria-hidden={keyboardUp ? 'true' : undefined}
      >
        {TABS.map((tab) => {
          const active = tab.matches ? tab.matches(path) : path === tab.href
          const count = tab.badge ? tab.badge() : 0
          return (
            <a
              key={tab.href}
              href={addBase(tab.href)}
              class={`sh-mobile-tab${active ? ' sh-active' : ''}`}
              aria-current={active ? 'page' : undefined}
            >
              <span class="sh-mobile-tab__icon" aria-hidden="true">
                {tab.emoji}
                {count > 0 && (
                  <span class="sh-mobile-tab__badge">
                    {count > 99 ? '99+' : count}
                  </span>
                )}
              </span>
              <span class="sh-mobile-tab__label">
                {tab.label()}
                {count > 0 && (
                  <span class="sr-only">{` (${t('nav.unread', { count: String(count) })})`}</span>
                )}
              </span>
            </a>
          )
        })}
        <button
          ref={moreRef}
          type="button"
          class={`sh-mobile-tab sh-mobile-tab--more${moreActive ? ' sh-active' : ''}`}
          aria-expanded={moreActive}
          aria-controls="sh-mobile-drawer"
          onClick={() => (mobileSidebarOpen.value = !mobileSidebarOpen.value)}
        >
          <span class="sh-mobile-tab__icon" aria-hidden="true">
            {moreActive ? '✕' : '☰'}
          </span>
          <span class="sh-mobile-tab__label">{moreActive ? t('common.close') : t('nav.more')}</span>
        </button>
      </nav>
    </>
  )
}
