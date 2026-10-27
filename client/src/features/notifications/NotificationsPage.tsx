/**
 * NotificationsPage — notification centre (§23.3).
 *
 * The newest page of rows loads first; "Show older" fetches the next
 * page with the list endpoint's ``before`` cursor (the oldest loaded
 * row's ``created_at``), and moves focus to the first row it added so
 * a keyboard or screen-reader user carries on reading where they were.
 *
 * Each row is ONE control: a link to the thing it is about (or a
 * button when there's nothing to open) whose accessible name is the
 * sentence and its time. Activating it marks the row read. Unread rows
 * say "Unread" to screen readers and use a heavier weight plus the
 * dot and edge, so read state never rests on colour alone.
 */
import { useEffect } from 'preact/hooks'
import { t, formatLocale } from '@/i18n/i18n'
import { useTitle } from '@/store/pageTitle'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { ws } from '@/ws'
import { Button } from '@/components/Button'
import { NotificationListSkeleton } from '@/components/Skeleton'
import { showToast } from '@/components/Toast'
import { relativeDocsTime } from '@/utils/relativeTime'
import type { Notification } from '@/types'
import { appHref } from '@/baseUrl'

/** Rows per request — the list endpoint's maximum. */
export const PAGE_SIZE = 50

const notifications = signal<Notification[]>([])
const loading = signal(true)
/** A full page came back last time, so older rows may exist. */
const hasMore = signal(false)
const loadingOlder = signal(false)

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

/** Refetch the newest page. Reused by the initial load and by the
 *  ``notification.new`` WS refresh below so a freshly-arrived
 *  notification lands without a manual reload. */
function loadNotifications(): Promise<void> {
  return api.get(`/api/notifications?limit=${PAGE_SIZE}`).then((data: Notification[]) => {
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
    const data: Notification[] = await api.get(
      `/api/notifications?limit=${PAGE_SIZE}&before=${encodeURIComponent(before)}`,
    )
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

export default function NotificationsPage() {
  useTitle(t('nav.notifications'))
  useEffect(() => {
    void loadNotifications()
    // Live refresh on inbound notifications — refetch-on-frame keeps the
    // page simple and lets the server own ordering / read-state.
    const offNew = ws.on('notification.new', () => { void loadNotifications() })
    return () => { offNew() }
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

  const markRead = async (id: string) => {
    // Optimistic: the row reads as read the moment it's opened, even
    // while the link navigates away.
    const before = notifications.value
    notifications.value = before.map(n =>
      n.id === id ? { ...n, read_at: new Date().toISOString() } : n,
    )
    try {
      await api.post(`/api/notifications/${id}/read`)
    } catch (err: unknown) {
      notifications.value = before
      showToast(t('notifications.mark_failed', { error: String((err as Error)?.message ?? err) }), 'error')
    }
  }

  if (loading.value) return <NotificationListSkeleton />

  const list = notifications.value
  const anyUnread = list.some(n => !n.read_at)

  return (
    <div class="sh-notifications-page">
      {anyUnread && (
        <div class="sh-page-header">
          <Button variant="secondary" onClick={markAllRead}>{t('notifications.mark_all_read')}</Button>
        </div>
      )}
      {list.length === 0 && (
        <div class="sh-empty-state">
          <div aria-hidden="true">🔔</div>
          <h3>{t('notifications.empty_title')}</h3>
          <p>{t('notifications.empty_body')}</p>
        </div>
      )}
      {list.length > 0 && (
        <ul class="sh-notif-rows">
          {list.map(n => {
            const unread = !n.read_at
            const cls = `sh-notif-row${unread ? ' sh-notif-row--unread' : ''}`
            const inner = (
              <>
                <span class="sh-notif-icon" aria-hidden="true">{unread ? '●' : '○'}</span>
                <span class="sh-notif-content">
                  {unread && <><span class="sr-only">{t('notifications.unread')},</span>{' '}</>}
                  <span class="sh-notif-title">{n.title}</span>
                  {' '}
                  {n.body && <><span class="sh-notif-body">{n.body}</span>{' '}</>}
                  <time
                    class="sh-notif-time"
                    dateTime={n.created_at}
                    title={new Date(n.created_at).toLocaleString(formatLocale())}
                  >
                    {relativeDocsTime(n.created_at)}
                  </time>
                </span>
              </>
            )
            const onActivate = () => { if (unread) void markRead(n.id) }
            return (
              <li key={n.id} class="sh-notif-rows__item">
                {n.link_url ? (
                  <a
                    class={cls}
                    href={appHref(n.link_url)}
                    data-notif-id={n.id}
                    onClick={onActivate}
                  >
                    {inner}
                  </a>
                ) : (
                  <button
                    type="button"
                    class={cls}
                    data-notif-id={n.id}
                    onClick={onActivate}
                  >
                    {inner}
                  </button>
                )}
              </li>
            )
          })}
        </ul>
      )}
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
