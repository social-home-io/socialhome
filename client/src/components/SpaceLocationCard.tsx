/**
 * SpaceLocationCard — map widget shown inside a space's "Map" tab
 * (§23.80). Polls :path:`GET /api/spaces/{id}/presence` and hands the
 * filtered entries to the shared :component:`LocationMap` together
 * with the per-space zone catalogue (§23.8.7).
 *
 * The space-level switch is now a single boolean (`feature_enabled`).
 * When OFF the map tab is hidden — this component renders an
 * explanatory banner so admins know why. When ON, members who opted
 * in surface as person bubbles: everyone inside one of the per-space
 * zones shares that zone's bubble, the rest sit at their GPS point. In
 * zone-only mode there are no coordinates at all — members are drawn at
 * their zone's centre (nothing beyond the zone already shared). HA-defined
 * zone names never reach this component (the server strips them at the
 * household boundary).
 *
 * Beside the map (a bottom sheet on phones) the :component:`MapOverview`
 * lists the members on the map and the space's zones with who is in each;
 * picking one there or on the map shows its detail and moves the map to it.
 *
 * The persistent **sharing chip** above the map shows the current
 * user's own opt-in state and exposes a one-tap toggle that calls
 * PATCH /api/spaces/{id}/members/me/location-sharing (§23.8.8).
 *
 * Polling cadence is 30 s — presence changes fan out via the WS
 * presence channel, but this page isn't always the active tab so we
 * keep a light pull as a fallback.
 */
import { useEffect, useState } from 'preact/hooks'
import { api } from '@/api'
import { ws } from '@/ws'
import { Spinner } from './Spinner'
import { Button } from './Button'
import { LocationMap, type LocationMarker } from './LocationMap'
import { MapOverview, type OverviewPerson } from './MapOverview'
import { Modal } from './Modal'
import { showToast } from './Toast'
import { matchZone } from '@/utils/mapMarkers'
import { presenceLabel } from '@/utils/presenceLabel'
import type { SpaceZone } from '@/types'
import { isOne, t } from '@/i18n/i18n'

interface SpacePresenceEntry {
  user_id: string
  username: string
  display_name: string
  state: string
  /** GPS-mode shape — present when ``location_mode === 'gps'``. */
  latitude?: number | null
  longitude?: number | null
  gps_accuracy_m?: number | null
  /** Zone-only-mode shape — present when ``location_mode === 'zone_only'``.
   *  The originating instance has matched the member's GPS to a
   *  space-defined zone and stripped the coordinates. */
  zone_id?: string
  zone_name?: string
  picture_url: string | null
  /** The last write of the member's location (UTC). */
  updated_at?: string | null
  /** When the member's place last changed (UTC) — "here since …".
   *  Absent on older backends (then ``updated_at``). */
  located_since?: string | null
  /** Another household's member whose household can't be reached: the
   *  pin is the last place it sent. */
  stale?: boolean
  /** Set on another household's member. Their ``state`` is a server
   *  placeholder (the pin carries no state), so it is never shown. */
  instance_id?: string | null
}

/** What to say about a member's state — nothing for another
 *  household's member, whose state the server can't know. */
function _stateLabel(p: SpacePresenceEntry): string | null {
  return p.instance_id ? null : presenceLabel(p.state)
}

interface SpacePresenceResponse {
  feature_enabled: boolean
  /** Privacy tier of the response. ``undefined`` means the feature is
   *  off; otherwise either ``"gps"`` (default) or ``"zone_only"``. */
  location_mode?: 'gps' | 'zone_only'
  entries: SpacePresenceEntry[]
  /** Full space roster size — locals from ``space_members`` plus
   *  remotes from ``space_remote_members``. Drives the
   *  "X / Y sharing" denominator. Optional on older backends. */
  total_members?: number
  /** Count of members who opted in to share with this space (locals
   *  via ``location_share_enabled=1`` + remotes via the existence of
   *  a current pin row). Optional on older backends. */
  sharing_count?: number
}

interface SpaceZonesResponse {
  zones: SpaceZone[]
}

interface SpaceMember {
  user_id: string
  location_share_enabled?: boolean
}

const POLL_INTERVAL_MS = 30_000
/** Window that folds a burst of ``space_location_updated`` frames into
 *  one presence refetch. */
export const PIN_REFRESH_COALESCE_MS = 500

function modalSeenKey(spaceId: string): string {
  return `sh.space.${spaceId}.locationModalSeen`
}

export function SpaceLocationCard({
  spaceId,
  currentUserId,
}: {
  spaceId: string
  /** Optional: when present, drives the sharing chip + onboarding
   *  modal. When absent we still render the map but skip the
   *  member-self-service surface. */
  currentUserId?: string
}) {
  const [data, setData] = useState<SpacePresenceResponse | null>(null)
  const [zones, setZones] = useState<SpaceZone[]>([])
  const [sharingMe, setSharingMe] = useState<boolean>(false)
  const [showOnboarding, setShowOnboarding] = useState<boolean>(false)
  // Person id or ``zone:<id>`` — shared by the map and the overview.
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const [presence, zonesResp] = await Promise.all([
          api.get(`/api/spaces/${spaceId}/presence`),
          api.get(`/api/spaces/${spaceId}/zones`).catch(() => ({ zones: [] })),
        ])
        if (cancelled) return
        setData(presence as SpacePresenceResponse)
        setZones((zonesResp as SpaceZonesResponse).zones || [])

        // Resolve the caller's own opt-in state from /members so the
        // chip + onboarding can render. Members endpoint is small; we
        // intentionally fetch it here to avoid threading membership
        // state through every callsite.
        if (currentUserId) {
          try {
            const members = await api.get<SpaceMember[]>(
              `/api/spaces/${spaceId}/members`,
            )
            const me = members.find((m) => m.user_id === currentUserId)
            const enabled = Boolean(me?.location_share_enabled)
            setSharingMe(enabled)
            const seen = window.localStorage.getItem(modalSeenKey(spaceId))
            if (
              !enabled
              && !seen
              && (presence as SpacePresenceResponse).feature_enabled
            ) {
              setShowOnboarding(true)
            }
          } catch {
            /* members endpoint optional for this surface */
          }
        }
      } catch (e) {
        if (!cancelled) setError((e as Error).message)
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void load()
    const t = setInterval(() => { void load() }, POLL_INTERVAL_MS)

    // §23.8.7: live zone CRUD frames from RealtimeService.
    // Apply upserts/deletes to the local zone array without a refetch.
    const offZone = ws.on('space_zone_changed', (e) => {
      const d = e.data as {
        space_id: string
        action: 'upsert' | 'delete'
        zone_id: string
        zone: SpaceZone | null
      }
      if (!d || d.space_id !== spaceId) return
      if (d.action === 'upsert' && d.zone) {
        const z = d.zone
        setZones((prev) => {
          const existing = prev.findIndex((p) => p.id === z.id)
          if (existing >= 0) {
            const out = prev.slice()
            out[existing] = z
            return out
          }
          return [...prev, z]
        })
      } else if (d.action === 'delete') {
        setZones((prev) => prev.filter((p) => p.id !== d.zone_id))
      }
    })

    // A local member's pin moved (``SpaceLocationOutbound`` fans
    // ``{type, data: {space_id, user_id, mode, …}}`` to this space's
    // local members). The frame is the federation payload, not the
    // presence-entry shape the map renders, so refetch the presence
    // view only — zones and the roster didn't change. Coalesced so a
    // burst of GPS updates costs one request. A remote member's pin
    // arrives the same way (the inbound pin is re-sent as this frame).
    let pinTimer: ReturnType<typeof setTimeout> | null = null
    const offPin = ws.on('space_location_updated', (e) => {
      const d = (e.data as { data?: { space_id?: string } }).data
      if (d?.space_id !== spaceId || pinTimer !== null) return
      pinTimer = setTimeout(() => {
        pinTimer = null
        api.get(`/api/spaces/${spaceId}/presence`)
          .then((presence) => {
            if (!cancelled) setData(presence as SpacePresenceResponse)
          })
          .catch(() => { /* keep the last map; the poll retries */ })
      }, PIN_REFRESH_COALESCE_MS)
    })

    // Someone stopped sharing their location here: drop their pin at once
    // instead of on the next poll. A remote member's removal names their
    // household (``instance_id``); a local member's names none, so it never
    // takes a remote member of the same id off the map.
    const offRemoved = ws.on('space_location_removed', (e) => {
      const d = (e.data as {
        data?: { space_id?: string, user_id?: string, instance_id?: string }
      }).data
      if (d?.space_id !== spaceId || !d.user_id) return
      const household = d.instance_id || null
      setData((prev) => {
        if (!prev) return prev
        const entries = prev.entries.filter((p) => !(
          p.user_id === d.user_id && (p.instance_id || null) === household
        ))
        const gone = prev.entries.length - entries.length
        if (gone === 0) return prev
        // The server's count still includes them until the next poll.
        const sharing = prev.sharing_count == null
          ? prev.sharing_count
          : Math.max(0, prev.sharing_count - gone)
        return { ...prev, entries, sharing_count: sharing }
      })
    })

    return () => {
      cancelled = true
      clearInterval(t)
      if (pinTimer !== null) clearTimeout(pinTimer)
      offZone()
      offPin()
      offRemoved()
    }
  }, [spaceId, currentUserId])

  const setMyOptIn = async (enabled: boolean) => {
    try {
      await api.patch(
        `/api/spaces/${spaceId}/members/me/location-sharing`,
        { enabled },
      )
      setSharingMe(enabled)
      const zone = data?.location_mode === 'zone_only'
      showToast(
        enabled
          ? t(zone ? 'location.card.now_sharing_zone' : 'location.card.now_sharing_location')
          : t(zone ? 'location.card.stopped_zone' : 'location.card.stopped_location'),
        'success',
      )
    } catch (e: any) {
      showToast(e.message || t('location.card.update_failed'), 'error')
    }
  }

  const dismissOnboarding = (decision: boolean | null) => {
    window.localStorage.setItem(modalSeenKey(spaceId), '1')
    setShowOnboarding(false)
    if (decision === true) void setMyOptIn(true)
  }

  if (loading) return <Spinner />
  if (error) return (
    <div class="sh-error-state" role="alert">
      {t('location.card.load_failed', { error })}
    </div>
  )
  if (!data) return null

  if (!data.feature_enabled) {
    return (
      <div class="sh-space-location sh-muted">
        <p>
          <strong>{t('location.card.off_title')}</strong>
        </p>
        <p>
          {t('location.card.off_body')}
        </p>
      </div>
    )
  }

  const isZoneOnly = data.location_mode === 'zone_only'

  // Everyone the map can place: zone-only members at their zone (no
  // coordinates exist), GPS members at their point — in the zone bubble
  // of the space zone they stand in.
  const placed = isZoneOnly
    ? data.entries
        .filter((p) => p.zone_id != null)
        .map((p) => ({ p, zone: zones.find((z) => z.id === p.zone_id) ?? null }))
    : data.entries
        .filter((p) => p.latitude != null && p.longitude != null)
        .map((p) => ({
          p, zone: matchZone(zones, p.latitude as number, p.longitude as number),
        }))

  const markers: LocationMarker[] = placed.map(({ p, zone }) => ({
    id: p.user_id,
    user_id: p.user_id,
    ...(isZoneOnly
      ? { zone_id: p.zone_id }
      : {
          lat: p.latitude as number,
          lon: p.longitude as number,
          accuracy_m: p.gps_accuracy_m ?? null,
        }),
    label: p.display_name,
    sub_label: isZoneOnly ? (p.zone_name ?? null) : (zone?.name ?? _stateLabel(p)),
    avatar_url: p.picture_url,
    state: p.state,
    stale: p.stale === true,
  }))

  const people: OverviewPerson[] = placed.map(({ p, zone }) => ({
    id: p.user_id,
    user_id: p.user_id,
    name: p.display_name,
    avatar_url: p.picture_url,
    status: zone?.name ?? (isZoneOnly ? (p.zone_name ?? null) : _stateLabel(p)),
    zone_id: zone?.id ?? null,
    updated_at: p.located_since ?? p.updated_at ?? null,
    stale: p.stale === true,
  }))

  // "X sharing / Y members" — Y is the full space roster (locals +
  // remote members from other households), not just the active-GPS
  // subset. Pre-server-side-count fallback: if the API didn't ship
  // total_members / sharing_count (older backend), derive from
  // entries — keeps mixed-version deployments sane during rollout.
  // Zone-only says "N in a zone": count who is in one (the server drops
  // members outside every zone), not everyone who opted in — the two
  // differ as soon as someone sharing is outside the zones.
  const sharing = isZoneOnly
    ? placed.length
    : (data.sharing_count ?? markers.length)
  const total = data.total_members ?? data.entries.length

  return (
    <div class="sh-space-location">
      {currentUserId && (
        <div class={`sh-sharing-chip ${sharingMe ? 'sh-sharing-chip--on' : 'sh-sharing-chip--off'}`}>
          <div class="sh-sharing-chip__text">
            <strong>
              {sharingMe
                ? (isZoneOnly
                    ? `📍 ${t('location.card.sharing_zone')}`
                    : `📍 ${t('location.card.sharing_location')}`)
                : `📵 ${t('location.card.private')}`}
            </strong>
            {sharingMe && (
              <span class="sh-muted">
                {isZoneOnly
                  ? t('location.card.zone_hint')
                  : t('location.card.gps_hint')}
              </span>
            )}
          </div>
          <Button
            variant={sharingMe ? 'danger' : 'primary'}
            onClick={() => setMyOptIn(!sharingMe)}
          >
            {sharingMe ? t('location.card.stop') : t('location.card.share_location')}
          </Button>
        </div>
      )}
      <MapOverview
        people={people}
        zones={zones}
        selectedId={selectedId}
        onSelect={setSelectedId}
        renderMap={(insets) => (
          <LocationMap
            markers={markers}
            zones={zones}
            height={380}
            selectedId={selectedId}
            onSelect={setSelectedId}
            fitInsets={insets}
            zoomToSelection
            emptyLabel={
              isZoneOnly
                // Members are in zones we can't place (zones failed to
                // load, or a zone was deleted): the overview says who —
                // the map stays quiet rather than claim nobody is anywhere.
                ? (data.entries.length === 0 ? t('location.card.no_one_in_zone') : '')
                : total === 0
                  ? t('location.card.no_gps_yet')
                  : t('location.card.no_gps_now')
            }
          />
        )}
      />
      <div class="sh-location-map-footer sh-muted">
        <span>
          {isZoneOnly
            ? t('location.card.in_zone_count', { n: String(sharing), total: String(total) })
            : t('location.card.sharing_count', { n: String(sharing), total: String(total) })}
        </span>
        <span>{t(isOne(zones.length) ? 'location.card.zones_configured_one' : 'location.card.zones_configured', { n: String(zones.length) })}</span>
      </div>
      <Modal
        open={showOnboarding}
        onClose={() => dismissOnboarding(null)}
        title={isZoneOnly
          ? `📍 ${t('location.card.ask_zone_title')}`
          : `📍 ${t('location.card.ask_location_title')}`}
      >
        <div class="sh-modal-body">
          {isZoneOnly ? (
            <>
              <p>{t('location.card.ask_zone_body')}</p>
              <ul>
                <li>{t('location.card.ask_zone_point_gps')}</li>
                <li>{t('location.card.ask_zone_point_none')}</li>
                <li>{t('location.card.ask_point_stop')}</li>
              </ul>
            </>
          ) : (
            <>
              <p>{t('location.card.ask_location_body')}</p>
              <ul>
                <li>{t('location.card.ask_point_stop')}</li>
                <li>{t('location.card.ask_location_point_zones')}</li>
              </ul>
            </>
          )}
          <div class="sh-modal-actions">
            <Button variant="secondary" onClick={() => dismissOnboarding(null)}>
              {t('common.not_now')}
            </Button>
            <Button variant="primary" onClick={() => dismissOnboarding(true)}>
              {isZoneOnly ? t('location.card.share_zone') : t('location.card.share_location')}
            </Button>
          </div>
        </div>
      </Modal>
    </div>
  )
}
