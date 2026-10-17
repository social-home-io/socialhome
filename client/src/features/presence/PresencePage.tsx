/**
 * PresencePage — the household's "who is where": the map
 * with the household zones (people in a zone share its bubble) and the
 * :component:`MapOverview` beside it (People / Zones; a bottom sheet on
 * phones), then a card per member.
 *
 * A member is placed in a household zone by the zone the server stored
 * for them (``zone_name``; ``state: home`` is the Home zone), else by the
 * zone their point is in — the same zone on the map and in the overview's
 * counts (both use ``personZone``).
 */
import { useEffect, useState } from 'preact/hooks'
import { t } from '@/i18n/i18n'
import { useTitle } from '@/store/pageTitle'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { ws } from '@/ws'
import { Avatar } from '@/components/Avatar'
import { PersonButton } from '@/components/PersonButton'
import { Spinner } from '@/components/Spinner'
import { LocationMap, type LocationMarker } from '@/components/LocationMap'
import { MapOverview, type OverviewPerson } from '@/components/MapOverview'
import { presenceLabel } from '@/utils/presenceLabel'
import { statusLine } from '@/utils/statusLine'
import { personZone } from '@/utils/mapMarkers'
import type { HouseholdZone, UserStatus } from '@/types'

interface PresenceEntry {
  username: string
  user_id: string
  display_name: string
  picture_url: string | null
  state: string
  zone_name: string | null
  latitude?: number | null
  longitude?: number | null
  gps_accuracy_m?: number | null
  /** When the presence row last changed (UTC). */
  updated_at?: string | null
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
const householdZones = signal<HouseholdZone[]>([])
const loading = signal(true)

function presenceDot(state: string): string {
  switch (state) {
    case 'home': return 'sh-dot sh-dot--home'
    case 'away': return 'sh-dot sh-dot--away'
    case 'not_home': return 'sh-dot sh-dot--not-home'
    default: return 'sh-dot sh-dot--unknown'
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

/** Refetch the household zones (drawn on the map, listed in the
 *  overview). The endpoint answers 403 while presence is off; the map
 *  then simply shows no zones. */
function loadHouseholdZones(): Promise<void> {
  return api.get<{ zones: HouseholdZone[] }>('/api/household/zones')
    .then((body) => { householdZones.value = body.zones || [] })
    .catch(() => { /* keep the last zones */ })
}

/** The household zone a member is in: the Home zone for ``home``, else
 *  the zone whose name matches their ``zone_name`` (any case). */
function memberZone(
  p: Pick<PresenceEntry, 'state' | 'zone_name'>, zones: readonly HouseholdZone[],
): HouseholdZone | null {
  if (p.state === 'home') return zones.find((z) => z.is_home) ?? null
  const name = p.zone_name?.trim().toLocaleLowerCase()
  if (!name) return null
  return zones.find((z) => !z.is_home && z.name.trim().toLocaleLowerCase() === name) ?? null
}

function _hasPosition(p: PresenceEntry): boolean {
  return typeof p.latitude === 'number' && typeof p.longitude === 'number'
}

export default function PresencePage() {
  useTitle(t('nav.presence'))
  // Person id or ``zone:<id>`` — shared by the map and the overview.
  const [selectedId, setSelectedId] = useState<string | null>(null)
  useEffect(() => {
    void loadPresenceList()
    void loadHouseholdZones()
    const offZones = ws.on('household_zones_changed', () => { void loadHouseholdZones() })
    // Live refresh: presence frames (physical state + GPS) and the
    // session-presence frames (online / idle / offline) both move the
    // roster, so refetch on any of them. Refetch-on-frame keeps this
    // page dead simple — the list is small and the endpoint cached.
    const refresh = () => { void loadPresenceList() }
    const offUpdated = ws.on('presence.updated', refresh)
    const offOnline = ws.on('user.online', refresh)
    const offIdle = ws.on('user.idle', refresh)
    const offOffline = ws.on('user.offline', refresh)
    // A member's status (set, cleared, or expired by the server's sweep)
    // moves their roster row. Our own ``currentUser`` is kept live
    // app-wide by ``MeButton``.
    const offStatus = ws.on('user.status_changed', refresh)
    return () => { offUpdated(); offOnline(); offIdle(); offOffline(); offStatus(); offZones() }
  }, [])

  if (loading.value) return <Spinner />

  const zones = householdZones.value

  // Everyone the map can place: a GPS point, or (no point) a known zone.
  const placed = presenceList.value
    .map((p) => ({ p, zone: memberZone(p, zones) }))
    .filter(({ p, zone }) => _hasPosition(p) || zone != null)
  const markers = placed.map<LocationMarker>(({ p, zone }) => ({
    id: p.user_id,
    user_id: p.user_id,
    lat: _hasPosition(p) ? p.latitude : null,
    lon: _hasPosition(p) ? p.longitude : null,
    // The stored zone wins over the point, so the bubble and the
    // overview's count agree.
    zone_id: zone?.id ?? null,
    accuracy_m: p.gps_accuracy_m ?? null,
    label: p.display_name,
    sub_label: presenceLabel(p.state, p.zone_name),
    avatar_url: p.picture_url,
    state: p.state,
  }))
  // Counted in the zone the map draws them in: the stored zone, else
  // the zone their point is in (Home Assistant can say not_home while
  // the point sits inside a household zone).
  const people = placed.map<OverviewPerson>(({ p }, i) => ({
    id: p.user_id,
    user_id: p.user_id,
    name: p.display_name,
    avatar_url: p.picture_url,
    status: presenceLabel(p.state, p.zone_name),
    zone_id: personZone(markers[i], zones)?.id ?? null,
    updated_at: p.updated_at ?? null,
    online: p.is_online ? (p.is_idle ? 'idle' : 'online') : 'offline',
    last_seen_at: p.last_seen_at ?? null,
  }))

  return (
    <div class="sh-presence">

      <div class="sh-presence-map">
        <h2>{t('presence.who_is_where')}</h2>
        <MapOverview
          people={people}
          zones={zones}
          selectedId={selectedId}
          onSelect={setSelectedId}
          renderMap={(insets) => (
            <LocationMap
              markers={markers}
              zones={zones}
              height={360}
              selectedId={selectedId}
              onSelect={setSelectedId}
              fitInsets={insets}
              zoomToSelection
              emptyLabel={t('presence.no_location')}
            />
          )}
        />
        <div class="sh-location-map-footer sh-muted">
          <span>
            {t('presence.sharing_count', {
              n: String(presenceList.value.filter(_hasPosition).length),
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
          // The whole card opens the person's sheet (Message, Call,
          // their status in full).
          <PersonButton
            key={p.username}
            userId={p.user_id}
            hint={() => ({
              display_name: p.display_name,
              picture_url: p.picture_url,
              username: p.username,
              is_local: true,
              is_online: p.is_online,
              is_idle: p.is_idle,
              status: p.status ?? null,
            })}
            class={`sh-presence-card sh-presence-card--${p.state}`}
            data-testid="presence-card"
          >
            <span class={presenceDot(p.state)} />
            <Avatar
              name={p.display_name}
              src={p.picture_url}
              online={online}
            />
            <span class="sh-presence-card-body">
              <strong>{p.display_name}</strong>
              {p.dnd && <span class="sh-badge sh-badge--dnd">{t('presence.dnd_badge')}</span>}
              <span class={`sh-presence-state sh-presence-state--${p.state}`}>
                {presenceLabel(p.state, p.zone_name)}
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
            </span>
          </PersonButton>
        )
      })}
      </div>
    </div>
  )
}
