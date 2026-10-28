/**
 * NotificationBell — top-bar bell (§23.3).
 *
 * Backed by ``store/notifications`` — the WS-driven signal store
 * whose ``unreadCount`` and ``recent`` already update live on
 * ``notification.new`` frames.
 *
 * Two shapes, one entry point with a count on every screen:
 *
 * - **Phones** (the bottom-bar breakpoint, ≤ 768 px): the bell is a
 *   link to the Notifications page. There is no Notifications tab in
 *   the bottom bar, and no dropdown (a 380 px panel can't fit beside
 *   the "You" button on a 390 px screen). On the page the bell is
 *   ``aria-current="page"``; tapping it again scrolls back to the top.
 * - **Desktop**: a button that opens a ``dialog`` panel. Focus moves
 *   to the first row; Escape closes it and hands focus back to the
 *   bell. The panel is nudged inside the viewport if it would spill
 *   past either edge, and ends with "See all notifications".
 *
 * Each row is one link (or one button when it has no target) whose
 * accessible name is the sentence; an unread row says "Unread" to
 * screen readers and is set in a heavier weight, not by colour alone.
 */
import { useEffect, useLayoutEffect, useRef } from 'preact/hooks'
import { signal } from '@preact/signals'
import { useLocation } from 'preact-iso'
import { api } from '@/api'
import { recent, unreadCount, type NotificationLite } from '@/store/notifications'
import type { Notification } from '@/types'
import { addBase } from '@/baseUrl'
import { useMediaQuery } from '@/hooks/useMediaQuery'
import { isOne, t } from '@/i18n/i18n'
import { ws } from '@/ws'
import { NewPill, NotificationRow, markRowRead, withRead } from '@/features/notifications/NotificationRow'
import { NeedsYouSection, pinnedIds } from '@/features/notifications/NeedsYouSection'

/** Same breakpoint as the bottom tab bar (``.sh-mobile-nav``). */
export const PHONE_QUERY = '(max-width: 768px)'
/** Gap kept between the panel and the viewport edge. */
const EDGE_GAP = 8
const PANEL_ID = 'sh-notif-panel'
const HEADING_ID = 'sh-notif-panel-title'

export const panelOpen = signal(false)
/** Full notifications list — populated when the panel opens.
 *  Falls back to the store's lighter ``recent`` projection while
 *  loading. */
const fullList = signal<Notification[]>([])
/** Rows that arrived while the panel was open, behind its "↑ N new" pill
 *  — the list never reorders under the reader. */
const panelPending = signal<ReadonlySet<string>>(new Set())

// 30 s polling fallback for the rare case the WS connection is
// down. Kept as a belt-and-suspenders guard — the WS path is the
// primary signal.
let pollTimer: ReturnType<typeof setInterval> | null = null
export function startNotificationPolling() {
  // Idempotent: clear any prior timer before arming a new one so a
  // double-start (or a stray render-phase call) can never leak an
  // orphaned 30 s interval that keeps fetching forever.
  stopNotificationPolling()
  const poll = async () => {
    try {
      const data = await api.get('/api/notifications/unread-count')
      // Don't overwrite the store if the WS handler is keeping it
      // honest — only correct downward when the WS feed agrees.
      if (typeof data.unread === 'number') {
        unreadCount.value = data.unread
      }
    } catch { /* transient — WS will catch up */ }
  }
  poll()
  pollTimer = setInterval(poll, 30000)
}

export function stopNotificationPolling() {
  if (pollTimer !== null) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

function bellLabel(): string {
  const n = unreadCount.value
  return n > 0
    ? t(isOne(n) ? 'notifications.bell_aria_unread_one' : 'notifications.bell_aria_unread',
      { n: String(n) })
    : t('notifications.title')
}

function Badge() {
  const n = unreadCount.value
  if (n <= 0) return null
  return (
    <span class="sh-notif-badge" aria-hidden="true">{n > 99 ? '99+' : n}</span>
  )
}

/** The phone bell: a link to the page, never a dropdown. */
function PhoneBell() {
  const loc = useLocation()
  const onPage = (loc?.path ?? '') === '/notifications'
  return (
    <div class="sh-notif-bell">
      <a
        class={`sh-notif-btn${onPage ? ' sh-active' : ''}`}
        href={addBase('/notifications')}
        aria-label={bellLabel()}
        aria-current={onPage ? 'page' : undefined}
        onClick={(e) => {
          // Already there: a second tap goes back to the newest rows
          // instead of reloading the route.
          if (!onPage) return
          e.preventDefault()
          window.scrollTo({ top: 0, behavior: 'smooth' })
        }}
      >
        <span class="sh-notif-glyph" aria-hidden="true">🔔</span>
        <Badge />
      </a>
    </div>
  )
}

function DesktopBell() {
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const btnRef = useRef<HTMLButtonElement | null>(null)
  const panelRef = useRef<HTMLDivElement | null>(null)

  // Hydrate the panel list once on mount so first-tap is instant.
  useEffect(() => {
    void api.get('/api/notifications?limit=20').then((d) => {
      fullList.value = d as Notification[]
    }).catch(() => { /* noop */ })
  }, [])

  // While the panel is open a new row waits behind the pill; closed, the
  // next open refetches anyway.
  useEffect(() => ws.on('notification.new', (e) => {
    if (!panelOpen.value) return
    const f = e.data as unknown as NotificationLite
    const next = new Set(panelPending.value)
    next.add(f.notification_id)
    panelPending.value = next
  }), [])

  const refresh = async () => {
    try {
      const data = await api.get('/api/notifications?limit=20')
      fullList.value = data as Notification[]
      panelPending.value = new Set()
    } catch { /* keep stale list visible */ }
  }

  const close = (refocus: boolean) => {
    panelOpen.value = false
    if (refocus) btnRef.current?.focus()
  }

  // Close on outside click and Escape. Escape hands focus back to the
  // bell so a keyboard user isn't dropped on <body>.
  useEffect(() => {
    if (!panelOpen.value) return
    const onPointer = (ev: MouseEvent | TouchEvent) => {
      const root = wrapRef.current
      if (!root || root.contains(ev.target as Node)) return
      close(false)
    }
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key !== 'Escape') return
      ev.preventDefault()
      close(true)
    }
    document.addEventListener('mousedown', onPointer)
    document.addEventListener('touchstart', onPointer, { passive: true })
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onPointer)
      document.removeEventListener('touchstart', onPointer)
      document.removeEventListener('keydown', onKey)
    }
  }, [panelOpen.value])

  // Keep the panel inside the viewport: anchored to the bell's right
  // edge it spilled off the left side whenever the bell sat less than
  // a panel-width from the left (narrow windows, zoomed text). Then
  // move focus into it.
  useLayoutEffect(() => {
    const panel = panelRef.current
    if (!panelOpen.value || !panel) return
    panel.style.transform = ''
    const rect = panel.getBoundingClientRect()
    const vw = document.documentElement.clientWidth || window.innerWidth
    let shift = 0
    if (rect.left < EDGE_GAP) shift = EDGE_GAP - rect.left
    else if (rect.right > vw - EDGE_GAP) shift = vw - EDGE_GAP - rect.right
    if (shift !== 0) panel.style.transform = `translateX(${Math.round(shift)}px)`
    const first = panel.querySelector<HTMLElement>('.sh-notif-row, .sh-notif-see-all')
    first?.focus({ preventScroll: true })
  }, [panelOpen.value])

  const togglePanel = async () => {
    panelOpen.value = !panelOpen.value
    if (panelOpen.value) await refresh()
  }

  const markAllRead = async () => {
    await api.post('/api/notifications/read-all')
    unreadCount.value = 0
    fullList.value = fullList.value.map(n => ({
      ...n,
      read_at: n.read_at ?? new Date().toISOString(),
    }))
  }

  const handleItemClick = async (n: Notification) => {
    // Optimistic decrement — tapping an unread item flips it (and every
    // row about the same thing) read; the server's unread-count frame
    // reconciles any divergence.
    const isUnread = !n.read_at
    if (isUnread && unreadCount.value > 0) {
      unreadCount.value = unreadCount.value - 1
    }
    if (isUnread) {
      fullList.value = withRead(fullList.value, n)
      try { await markRowRead(n) }
      catch { /* best-effort */ }
    }
    if (n.link_url) panelOpen.value = false
  }

  // The displayed list: prefer the hydrated full list (carries
  // ``read_at`` + ``created_at``); fall back to the store's recent
  // projection for instant feel right after a WS frame lands. A request
  // pinned in "Needs you" shows there, not twice.
  const pinned = pinnedIds()
  const all = fullList.value.length > 0
    ? fullList.value
    : recent.value.map(r => ({
        id:         r.notification_id,
        type:       r.notif_type,
        title:      r.title,
        body:       null,
        link_url:   r.link_url ?? null,
        read_at:    null,
        created_at: r.occurred_at ?? new Date().toISOString(),
        object_key: r.object_key ?? null,
        space_id:   r.space_id ?? null,
        params:     r.params ?? null,
      }) as Notification)
  const items = all.filter(n => !pinned.has(n.id))

  return (
    <div
      class="sh-notif-bell"
      ref={wrapRef}
      onFocusOut={(e) => {
        // Tabbing past the last row leaves the panel: close it rather
        // than leave an open popup behind the focus.
        const next = e.relatedTarget as Node | null
        if (panelOpen.value && next && !wrapRef.current?.contains(next)) close(false)
      }}
    >
      <button
        ref={btnRef}
        type="button"
        class="sh-notif-btn"
        onClick={togglePanel}
        aria-label={bellLabel()}
        aria-haspopup="dialog"
        aria-expanded={panelOpen.value}
        aria-controls={PANEL_ID}
      >
        <span class="sh-notif-glyph" aria-hidden="true">🔔</span>
        <Badge />
      </button>
      {panelOpen.value && (
        <div
          ref={panelRef}
          id={PANEL_ID}
          class="sh-notif-panel"
          role="dialog"
          aria-labelledby={HEADING_ID}
        >
          <div class="sh-notif-header">
            <h2 id={HEADING_ID} class="sh-notif-heading">{t('notifications.title')}</h2>
            {unreadCount.value > 0 && (
              <button type="button" class="sh-link" onClick={markAllRead}>
                {t('notifications.mark_all_read')}
              </button>
            )}
          </div>
          <NewPill count={panelPending.value.size} onShow={() => void refresh()} />
          <div class="sh-notif-list">
            <NeedsYouSection compact onNavigate={() => { panelOpen.value = false }} />
            {items.length === 0 && pinned.size === 0 && (
              <p class="sh-notif-empty sh-muted">{t('notifications.bell_empty')}</p>
            )}
            {items.map(n => (
              <NotificationRow key={n.id} n={n} compact onOpen={(row) => void handleItemClick(row)} />
            ))}
          </div>
          <a
            class="sh-notif-see-all"
            href={addBase('/notifications')}
            onClick={() => { panelOpen.value = false }}
          >
            {t('notifications.see_all')}
          </a>
        </div>
      )}
    </div>
  )
}

export function NotificationBell() {
  const phone = useMediaQuery(PHONE_QUERY)
  return phone ? <PhoneBell /> : <DesktopBell />
}
