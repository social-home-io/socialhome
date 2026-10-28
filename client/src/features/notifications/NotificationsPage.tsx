/**
 * NotificationsPage — notification centre (§23.3).
 *
 * Rows sit under day headers (Today / Yesterday / the date) and read as
 * whole sentences in the viewer's language (:mod:`./rowText`); each row is
 * ONE control (:class:`NotificationRow`). An All | Unread tab list filters
 * the list (Unread asks the server for unread rows only, so paging stays
 * honest).
 *
 * Requests that wait for the viewer sit above it all in "Needs you"
 * (:class:`NeedsYouSection`) until someone handles them; the timeline
 * leaves them out until then.
 *
 * Live arrivals never reorder the list under the reader: a "↑ N new" pill
 * appears instead, and tapping it loads them on top. One polite live region
 * outside the list says how many arrived — at most once per 10 s, counts
 * only, never the sentence.
 *
 * The newest page of rows loads first; "Show older" fetches the next page
 * with the list endpoint's ``before`` cursor (the oldest loaded row's
 * ``created_at``), and moves focus to the first row it added so a keyboard
 * or screen-reader user carries on reading where they were.
 */
import { useEffect, useRef } from 'preact/hooks'
import { signal } from '@preact/signals'
import { t } from '@/i18n/i18n'
import { useTitle } from '@/store/pageTitle'
import { api } from '@/api'
import { addBase } from '@/baseUrl'
import { ws } from '@/ws'
import { Button } from '@/components/Button'
import { NotificationListSkeleton } from '@/components/Skeleton'
import { showToast } from '@/components/Toast'
import { loadHouseholdUsers } from '@/store/householdUsers'
import { unreadCount, type NotificationLite } from '@/store/notifications'
import type { Notification } from '@/types'
import { NewPill, NotificationRow, markRowRead, rowPlainText, withRead } from './NotificationRow'
import { NeedsYouSection, pinnedIds } from './NeedsYouSection'
import { NotificationRowMenu } from './NotificationRowMenu'
import { createAnnouncer, groupByDay, type NotifFilter } from './rowList'

/** Rows per request — the list endpoint's maximum. */
export const PAGE_SIZE = 50

const notifications = signal<Notification[]>([])
const loading = signal(true)
/** A full page came back last time, so older rows may exist. */
const hasMore = signal(false)
const loadingOlder = signal(false)
export const filter = signal<NotifFilter>('all')
/** Rows that arrived (or came back) while the list was open, waiting
 *  behind the "↑ N new" pill. */
const pending = signal<ReadonlySet<string>>(new Set())
/** The polite live region's text. */
const liveText = signal('')

const TAB_IDS: Record<NotifFilter, string> = {
  all: 'sh-notif-tab-all',
  unread: 'sh-notif-tab-unread',
}

/** Put a freshly fetched newest page in front of what's loaded,
 *  keeping any older pages the person already opened. */
export function mergeNewestPage(
  first: Notification[],
  current: Notification[],
  prevHasMore: boolean,
): { list: Notification[], hasMore: boolean } {
  if (first.length < PAGE_SIZE) return { list: first, hasMore: false }
  const floor = first[first.length - 1].created_at
  const ids = new Set(first.map(n => n.id))
  const tail = current.filter(n => !ids.has(n.id) && n.created_at < floor)
  return { list: [...first, ...tail], hasMore: tail.length > 0 ? prevHasMore : true }
}

function listUrl(before?: string): string {
  let url = `/api/notifications?limit=${PAGE_SIZE}`
  if (filter.value === 'unread') url += '&unread=1'
  if (before) url += `&before=${encodeURIComponent(before)}`
  return url
}

/** Refetch the newest page (the first load, a filter switch, the pill). */
function loadNotifications(): Promise<void> {
  const want = filter.value
  return api.get(listUrl()).then((data: Notification[]) => {
    if (filter.value !== want) return // switched meanwhile: that load wins
    const merged = mergeNewestPage(data, notifications.value, hasMore.value)
    notifications.value = merged.list
    hasMore.value = merged.hasMore
    loading.value = false
  })
}

async function loadOlder(): Promise<void> {
  const list = notifications.value
  if (loadingOlder.value || list.length === 0) return
  loadingOlder.value = true
  const before = list[list.length - 1].created_at
  try {
    const data: Notification[] = await api.get(listUrl(before))
    const seen = new Set(list.map(n => n.id))
    const added = data.filter(n => !seen.has(n.id))
    notifications.value = [...list, ...added]
    hasMore.value = data.length >= PAGE_SIZE
    if (added.length > 0) {
      // The button may disappear (last page): hand focus to the first
      // new row so it never drops to <body>.
      requestAnimationFrame(() => {
        document.querySelector<HTMLElement>(`[data-notif-id="${CSS.escape(added[0].id)}"]`)
          ?.focus()
      })
    }
  } catch (err: unknown) {
    showToast(t('notifications.load_older_failed', { error: String((err as Error)?.message ?? err) }), 'error')
  } finally {
    loadingOlder.value = false
  }
}

function setFilter(next: NotifFilter): void {
  if (filter.value === next) return
  filter.value = next
  notifications.value = []
  hasMore.value = false
  pending.value = new Set()
  loading.value = true
  void loadNotifications().catch(() => { loading.value = false })
}

/** Load what's behind the pill, back to the top, focus the newest row. */
async function showPending(): Promise<void> {
  try {
    await loadNotifications()
  } catch { /* keep the pill: tapping again retries */ return }
  pending.value = new Set()
  window.scrollTo({ top: 0, behavior: 'smooth' })
  requestAnimationFrame(() => {
    document.querySelector<HTMLElement>('.sh-notifications-page .sh-notif-row')?.focus({ preventScroll: true })
  })
}

function FilterTabs() {
  const n = unreadCount.value
  const order: NotifFilter[] = ['all', 'unread']
  const onKey = (e: KeyboardEvent) => {
    if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft' && e.key !== 'Home' && e.key !== 'End') return
    e.preventDefault()
    const at = order.indexOf(filter.value)
    const next = e.key === 'Home' ? order[0]
      : e.key === 'End' ? order[order.length - 1]
      : order[(at + (e.key === 'ArrowRight' ? 1 : order.length - 1)) % order.length]
    setFilter(next)
    document.getElementById(TAB_IDS[next])?.focus()
  }
  return (
    <div class="sh-notif-filter" role="tablist" aria-label={t('notifications.filter.label')} onKeyDown={onKey}>
      {order.map(f => {
        const on = filter.value === f
        return (
          <button
            key={f}
            id={TAB_IDS[f]}
            type="button"
            role="tab"
            aria-selected={on}
            aria-controls="sh-notif-list"
            tabIndex={on ? 0 : -1}
            class={on ? 'sh-notif-filter__tab sh-notif-filter__tab--on' : 'sh-notif-filter__tab'}
            onClick={() => setFilter(f)}
          >
            {t(f === 'all' ? 'notifications.filter.all' : 'notifications.filter.unread')}
            {f === 'unread' && n > 0 && (
              <span class="sh-notif-filter__count">{n > 99 ? '99+' : n}</span>
            )}
          </button>
        )
      })}
    </div>
  )
}

export default function NotificationsPage() {
  useTitle(t('nav.notifications'))
  const announcer = useRef<ReturnType<typeof createAnnouncer> | null>(null)
  useEffect(() => {
    pending.value = new Set()
    liveText.value = ''
    void loadNotifications().catch(() => { loading.value = false })
    void loadHouseholdUsers()
    announcer.current = createAnnouncer((text) => { liveText.value = text })
    const offNew = ws.on('notification.new', (e) => {
      const f = e.data as unknown as NotificationLite
      // A repeat on a row still unread changes nothing the badge counts:
      // no announcement for it, but the pill still offers the fresh text.
      if (!f.bumped || f.reopened) announcer.current?.arrived(1)
      if (notifications.value.length === 0 && !loading.value) {
        void loadNotifications() // nothing to reorder under anyone
        return
      }
      const next = new Set(pending.value)
      next.add(f.notification_id)
      pending.value = next
    })
    return () => {
      offNew()
      announcer.current?.dispose()
      announcer.current = null
    }
  }, [])

  const markAllRead = async () => {
    try {
      await api.post('/api/notifications/read-all')
      notifications.value = notifications.value.map(n => ({
        ...n, read_at: n.read_at || new Date().toISOString(),
      }))
    } catch (err: unknown) {
      showToast(t('notifications.mark_all_failed', { error: String((err as Error)?.message ?? err) }), 'error')
    }
  }

  const open = async (n: Notification) => {
    if (n.read_at) return
    // Optimistic: the row reads as read the moment it's opened, even
    // while the link navigates away. It stays in the Unread view until
    // the next load, so nothing jumps under the finger.
    const before = notifications.value
    notifications.value = withRead(before, n)
    try {
      await markRowRead(n)
    } catch (err: unknown) {
      notifications.value = before
      showToast(t('notifications.mark_failed', { error: String((err as Error)?.message ?? err) }), 'error')
    }
  }

  const live = <p class="sr-only" role="status" aria-live="polite">{liveText.value}</p>

  if (loading.value) {
    return <>{live}<NotificationListSkeleton /></>
  }

  // A request that waits for the viewer is pinned in "Needs you" above;
  // the timeline shows it once it was handled.
  const pinned = pinnedIds()
  const list = notifications.value.filter(n => !pinned.has(n.id))
  const anyUnread = notifications.value.some(n => !n.read_at)
  const unreadView = filter.value === 'unread'

  return (
    <div class="sh-notifications-page">
      {live}
      <div class="sh-notif-toolbar">
        <FilterTabs />
        {anyUnread && (
          <Button variant="secondary" onClick={markAllRead}>{t('notifications.mark_all_read')}</Button>
        )}
        <a class="sh-btn sh-btn--secondary sh-notif-settings-link" href={addBase('/settings#notifications')}>
          {t('notifications.settings_link')}
        </a>
      </div>
      <NewPill count={pending.value.size} onShow={() => void showPending()} />
      <NeedsYouSection />
      <div id="sh-notif-list" role="tabpanel" aria-labelledby={TAB_IDS[filter.value]}>
        {list.length === 0 && (
          <div class="sh-empty-state">
            <div aria-hidden="true">🔔</div>
            <h3>{t(unreadView ? 'notifications.unread_empty_title' : 'notifications.empty_title')}</h3>
            <p>{t(unreadView ? 'notifications.unread_empty_body' : 'notifications.empty_body')}</p>
          </div>
        )}
        {groupByDay(list).map(g => (
          <section key={g.key} class="sh-notif-day" aria-labelledby={`sh-notif-day-${g.key}`}>
            <h3 class="sh-notif-day__h" id={`sh-notif-day-${g.key}`}>{g.label}</h3>
            <ul class="sh-notif-rows">
              {g.rows.map(n => (
                <li key={n.id} class="sh-notif-rows__item">
                  <NotificationRow
                    n={n}
                    onOpen={(row) => void open(row)}
                    menu={<NotificationRowMenu n={n} label={rowPlainText(n)} />}
                  />
                </li>
              ))}
            </ul>
          </section>
        ))}
      </div>
      {hasMore.value && list.length > 0 && (
        <div class="sh-notif-more">
          <Button
            variant="secondary"
            loading={loadingOlder.value}
            onClick={() => void loadOlder()}
          >
            {t('notifications.show_older')}
          </Button>
        </div>
      )}
    </div>
  )
}
