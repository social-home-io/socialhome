import { useEffect } from 'preact/hooks'
import { t } from '@/i18n/i18n'
import { useTitle } from '@/store/pageTitle'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { ws } from '@/ws'
import { Avatar } from '@/components/Avatar'
import { StatusEditor, formatClearsAt } from '@/components/StatusEditor'
import { Spinner } from '@/components/Spinner'
import { Button } from '@/components/Button'
import { LocationMap, type LocationMarker } from '@/components/LocationMap'
import { currentUser } from '@/store/auth'
import type { UserStatus } from '@/types'

interface PresenceEntry {
  username: string
  display_name: string
  picture_url: string | null
  state: string
  zone_name: string | null
  latitude?: number | null
  longitude?: number | null
  gps_accuracy_m?: number | null
  last_seen_at?: string | null
  is_online?: boolean
  is_idle?: boolean
  dnd?: boolean
  /** Emoji + text status; null when unset or expired. */
  status?: UserStatus | null
}

/** Compact "5 min ago" / "2 h ago" / "3 d ago" rendering for the
 *  ``last_seen_at`` line. Returns ``null`` when the input is missing
 *  or in the future (clock skew). */
function humanizeAgo(iso: string | null | undefined): string | null {
  if (!iso) return null
  const ts = Date.parse(iso)
  if (Number.isNaN(ts)) return null
  const sec = Math.max(0, Math.round((Date.now() - ts) / 1000))
  if (sec < 60)        return t('time.just_now')
  if (sec < 60 * 60)   return t('time.minutes_ago', { n: String(Math.floor(sec / 60)) })
  if (sec < 86400)     return t('time.hours_ago', { n: String(Math.floor(sec / 3600)) })
  return t('time.days_ago', { n: String(Math.floor(sec / 86400)) })
}

const presenceList = signal<PresenceEntry[]>([])
const loading = signal(true)
const showStatusEditor = signal(false)

/** "🌴 On leave" — null when there is nothing to show. */
function statusLine(s: UserStatus | null | undefined): string | null {
  if (!s || (!s.emoji && !s.text)) return null
  if (s.expires_at && Date.parse(s.expires_at) <= Date.now()) return null
  return [s.emoji, s.text].filter(Boolean).join(' ')
}

function presenceDot(state: string): string {
  switch (state) {
    case 'home': return 'sh-dot sh-dot--home'
    case 'away': return 'sh-dot sh-dot--away'
    case 'not_home': return 'sh-dot sh-dot--not-home'
    default: return 'sh-dot sh-dot--unknown'
  }
}

function presenceLabel(state: string): string {
  switch (state) {
    case 'home': return t('presence.state.home')
    case 'away': return t('presence.state.away')
    case 'not_home': return t('presence.state.not_home')
    default: return state
  }
}

/** Refetch the household presence list. Reused by the initial load and
 *  by the presence-WS refresh subscriptions below — keeping a single
 *  loader means a `user.online` frame and the first paint go through the
 *  exact same path (no drift between the two). */
function loadPresenceList(): Promise<void> {
  return api.get('/api/presence').then(data => {
    presenceList.value = data
    loading.value = false
  })
}

export default function PresencePage() {
  useTitle(t('nav.presence'))
  useEffect(() => {
    void loadPresenceList()
    // Live refresh: presence frames (physical state + GPS) and the
    // session-presence frames (online / idle / offline) both move the
    // roster, so refetch on any of them. Refetch-on-frame keeps this
    // page dead simple — the list is small and the endpoint cached.
    const refresh = () => { void loadPresenceList() }
    const offUpdated = ws.on('presence.updated', refresh)
    const offOnline = ws.on('user.online', refresh)
    const offIdle = ws.on('user.idle', refresh)
    const offOffline = ws.on('user.offline', refresh)
    // A member's status (set, cleared, or expired by the server's sweep).
    // Our own status also lands on ``currentUser`` so the "Your status"
    // line follows a change made from another tab.
    const offStatus = ws.on('user.status_changed', (e) => {
      const me = currentUser.value
      if (me && e.data.user_id === me.user_id) {
        currentUser.value = {
          ...me,
          status: (e.data.status as UserStatus | null)
            ?? { emoji: null, text: null, expires_at: null },
        }
      }
      refresh()
    })
    return () => { offUpdated(); offOnline(); offIdle(); offOffline(); offStatus() }
  }, [])

  if (loading.value) return <Spinner />

  const myStatus = currentUser.value?.status
  const myLine = statusLine(myStatus)

  return (
    <div class="sh-presence">

      <div class="sh-presence-controls">
        {showStatusEditor.value ? (
          <StatusEditor
            onSave={() => { showStatusEditor.value = false; void loadPresenceList() }}
            onCancel={() => { showStatusEditor.value = false }}
          />
        ) : (
          <div class="sh-my-status">
            <span class="sh-my-status__label">{t('presence.your_status')}</span>
            <span class={myLine ? 'sh-my-status__value' : 'sh-my-status__value sh-muted'}>
              {myLine ?? t('presence.no_status')}
              {myLine && myStatus?.expires_at && (
                <span class="sh-muted">
                  {` · ${t('presence.clears', { time: formatClearsAt(myStatus.expires_at) })}`}
                </span>
              )}
            </span>
            <Button variant="secondary" onClick={() => { showStatusEditor.value = true }}>
              {myLine ? t('presence.edit_status') : t('presence.set_status')}
            </Button>
          </div>
        )}
      </div>

      <div class="sh-presence-map">
        <h2>{t('presence.who_is_where')}</h2>
        <LocationMap
          markers={presenceList.value
            .filter((p) =>
              typeof p.latitude === 'number' && typeof p.longitude === 'number',
            )
            .map<LocationMarker>((p) => ({
              id: p.username,
              lat: p.latitude as number,
              lon: p.longitude as number,
              accuracy_m: p.gps_accuracy_m ?? null,
              label: p.display_name,
              sub_label: p.zone_name || presenceLabel(p.state),
              avatar_url: p.picture_url,
              state: p.state,
            }))}
          height={360}
          emptyLabel={t('presence.no_location')}
        />
        <div class="sh-location-map-footer sh-muted">
          <span>
            {t('presence.sharing_count', {
              n: String(presenceList.value.filter((p) =>
                typeof p.latitude === 'number' && typeof p.longitude === 'number',
              ).length),
              total: String(presenceList.value.length),
            })}
          </span>
          <span>{t('presence.gps_rounded')}</span>
        </div>
      </div>

      <h2>{t('presence.members')}</h2>
      {presenceList.value.length === 0 && (
        <div class="sh-empty-state">
          <div aria-hidden="true">🏠</div>
          <h3>{t('presence.empty_title')}</h3>
          <p>{t('presence.empty_body')}</p>
        </div>
      )}
      <div class="sh-presence-list">
      {presenceList.value.map(p => {
        const online = p.is_online ? (p.is_idle ? 'idle' : 'online') : null
        const lastSeen = humanizeAgo(p.last_seen_at)
        return (
          <div key={p.username}
               class={`sh-presence-card sh-presence-card--${p.state}`}>
            <span class={presenceDot(p.state)} />
            <Avatar
              name={p.display_name}
              src={p.picture_url}
              online={online}
            />
            <div>
              <strong>{p.display_name}</strong>
              {p.dnd && <span class="sh-badge sh-badge--dnd">{t('presence.dnd_badge')}</span>}
              <span class={`sh-presence-state sh-presence-state--${p.state}`}>
                {p.zone_name || presenceLabel(p.state)}
              </span>
              <span class="sh-presence-online sh-muted">
                {online === 'online' && `· ${t('dms.status.online')}`}
                {online === 'idle'   && `· ${t('dms.status.idle')}`}
                {!online && lastSeen && `· ${t('dms.status.last_seen', { ago: lastSeen })}`}
                {!online && !lastSeen && `· ${t('dms.status.offline')}`}
              </span>
              {statusLine(p.status) && (
                <span class="sh-presence-status">{statusLine(p.status)}</span>
              )}
            </div>
          </div>
        )
      })}
      </div>
    </div>
  )
}
