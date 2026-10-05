/**
 * NotificationsPage — notification centre (§23.3).
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

const notifications = signal<Notification[]>([])
const loading = signal(true)

/** Refetch the notification list. Reused by the initial load and by the
 *  ``notification.new`` WS refresh below so a freshly-arrived
 *  notification lands without a manual reload. */
function loadNotifications(): Promise<void> {
  return api.get('/api/notifications?limit=50').then(data => {
    notifications.value = data
    loading.value = false
  })
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
    try {
      await api.post(`/api/notifications/${id}/read`)
      notifications.value = notifications.value.map(n =>
        n.id === id ? { ...n, read_at: new Date().toISOString() } : n
      )
    } catch (err: unknown) {
      showToast(t('notifications.mark_failed', { error: String((err as Error)?.message ?? err) }), 'error')
    }
  }

  if (loading.value) return <NotificationListSkeleton />

  return (
    <div class="sh-notifications-page">
      <div class="sh-page-header">
        <Button variant="secondary" onClick={markAllRead}>{t('notifications.mark_all_read')}</Button>
      </div>
      {notifications.value.length === 0 && (
        <div class="sh-empty-state">
          <div aria-hidden="true">🔔</div>
          <h3>{t('notifications.empty_title')}</h3>
          <p>{t('notifications.empty_body')}</p>
        </div>
      )}
      {notifications.value.map(n => (
        <div key={n.id}
          class={`sh-notif-row ${n.read_at ? '' : 'sh-notif-row--unread'}`}
          onClick={() => !n.read_at && markRead(n.id)}>
          <div class="sh-notif-icon">{n.read_at ? '○' : '●'}</div>
          <div class="sh-notif-content">
            <div class="sh-notif-title">{n.title}</div>
            {n.body && <div class="sh-notif-body">{n.body}</div>}
            <time
              class="sh-notif-time"
              dateTime={n.created_at}
              title={new Date(n.created_at).toLocaleString(formatLocale())}
            >
              {relativeDocsTime(n.created_at)}
            </time>
          </div>
          {n.link_url && <a href={appHref(n.link_url)} class="sh-notif-link">→</a>}
        </div>
      ))}
    </div>
  )
}
