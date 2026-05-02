import { useEffect } from 'preact/hooks'
import { useTitle } from '@/store/pageTitle'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { Avatar } from '@/components/Avatar'
import { StatusEditor } from '@/components/StatusEditor'
import { Spinner } from '@/components/Spinner'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'
import { LocationMap, type LocationMarker } from '@/components/LocationMap'

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
}

/** Compact "5 min ago" / "2 h ago" / "3 d ago" rendering for the
 *  ``last_seen_at`` line. Returns ``null`` when the input is missing
 *  or in the future (clock skew). */
function humanizeAgo(iso: string | null | undefined): string | null {
  if (!iso) return null
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return null
  const sec = Math.max(0, Math.round((Date.now() - t) / 1000))
  if (sec < 60)        return 'just now'
  if (sec < 60 * 60)   return `${Math.floor(sec / 60)} min ago`
  if (sec < 86400)     return `${Math.floor(sec / 3600)} h ago`
  return `${Math.floor(sec / 86400)} d ago`
}

type ExpiryDuration = '30m' | '1h' | '4h' | 'today' | 'none'

const presenceList = signal<PresenceEntry[]>([])
const loading = signal(true)
const dndEnabled = signal(false)
const statusExpiry = signal<ExpiryDuration>('none')
const showStatusEditor = signal(false)
const locationSharing = signal(true)

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
    case 'home': return 'Home'
    case 'away': return 'Away'
    case 'not_home': return 'Not home'
    default: return state
  }
}

export default function PresencePage() {
  useTitle('Presence')
  useEffect(() => {
    api.get('/api/presence').then(data => {
      presenceList.value = data
      loading.value = false
    })
    api.get('/api/me/preferences').then((data: { dnd?: boolean; location_sharing?: boolean }) => {
      if (typeof data.dnd === 'boolean') dndEnabled.value = data.dnd
      if (typeof data.location_sharing === 'boolean') locationSharing.value = data.location_sharing
    }).catch(() => {})
  }, [])

  const toggleDnd = async () => {
    dndEnabled.value = !dndEnabled.value
    try {
      await api.patch('/api/me/preferences', { dnd: dndEnabled.value })
      showToast(dndEnabled.value ? 'Do Not Disturb enabled' : 'Do Not Disturb disabled', 'info')
    } catch {
      dndEnabled.value = !dndEnabled.value
      showToast('Failed to update DND', 'error')
    }
  }

  const setExpiry = async (duration: ExpiryDuration) => {
    statusExpiry.value = duration
    let clear_after: string | null = null
    if (duration === '30m') clear_after = '30m'
    else if (duration === '1h') clear_after = '1h'
    else if (duration === '4h') clear_after = '4h'
    else if (duration === 'today') clear_after = 'today'
    try {
      await api.patch('/api/me', { status_clear_after: clear_after })
      showToast(`Status will clear after ${duration === 'none' ? 'never' : duration}`, 'info')
    } catch {
      showToast('Failed to set expiry', 'error')
    }
  }

  const toggleLocationSharing = async () => {
    locationSharing.value = !locationSharing.value
    try {
      await api.patch('/api/me/preferences', { location_sharing: locationSharing.value })
      showToast(locationSharing.value ? 'Location sharing enabled' : 'Location sharing disabled', 'info')
    } catch {
      locationSharing.value = !locationSharing.value
      showToast('Failed to update', 'error')
    }
  }

  if (loading.value) return <Spinner />

  return (
    <div class="sh-presence">

      <div class="sh-presence-controls">
        <label class="sh-toggle-row">
          <input type="checkbox" checked={dndEnabled.value} onChange={toggleDnd} />
          Do Not Disturb
        </label>

        <label class="sh-toggle-row">
          <input type="checkbox" checked={locationSharing.value} onChange={toggleLocationSharing} />
          Share my location
        </label>

        <div class="sh-status-expiry">
          <span>Clear status after:</span>
          <div class="sh-expiry-options">
            {(['none', '30m', '1h', '4h', 'today'] as ExpiryDuration[]).map(d => (
              <button
                key={d}
                type="button"
                class={statusExpiry.value === d ? 'sh-chip sh-chip--active' : 'sh-chip'}
                onClick={() => setExpiry(d)}
              >
                {d === 'none' ? 'Never' : d}
              </button>
            ))}
          </div>
        </div>

        <Button variant="secondary" onClick={() => { showStatusEditor.value = !showStatusEditor.value }}>
          {showStatusEditor.value ? 'Hide status editor' : 'Set status'}
        </Button>
        {showStatusEditor.value && <StatusEditor onSave={() => { showStatusEditor.value = false }} />}
      </div>

      <div class="sh-presence-map">
        <h2>Who is where</h2>
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
          emptyLabel={
            locationSharing.value
              ? 'No one is sharing their location right now.'
              : 'Turn on location sharing above to see the map.'
          }
        />
        <div class="sh-location-map-footer sh-muted">
          <span>
            {presenceList.value.filter((p) =>
              typeof p.latitude === 'number' && typeof p.longitude === 'number',
            ).length}
            {' '}of {presenceList.value.length} sharing their location
          </span>
          <span>GPS rounded to ~10 m (§GPS truncation)</span>
        </div>
      </div>

      <h2>Members</h2>
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
              {p.dnd && <span class="sh-badge sh-badge--dnd">DND</span>}
              <span class={`sh-presence-state sh-presence-state--${p.state}`}>
                {p.zone_name || presenceLabel(p.state)}
              </span>
              <span class="sh-presence-online sh-muted">
                {online === 'online' && '· Online'}
                {online === 'idle'   && '· Idle'}
                {!online && lastSeen && `· Last seen ${lastSeen}`}
                {!online && !lastSeen && '· Offline'}
              </span>
            </div>
          </div>
        )
      })}
    </div>
  )
}
