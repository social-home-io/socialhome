/**
 * SpaceLocationCard — map widget shown inside a space's "Map" tab
 * (§23.80). Polls :path:`GET /api/spaces/{id}/presence` and hands the
 * filtered entries to the shared :component:`LocationMap` together
 * with the per-space zone catalogue (§23.8.7).
 *
 * The space-level switch is now a single boolean (`feature_enabled`).
 * When OFF the map tab is hidden — this component renders an
 * explanatory banner so admins know why. When ON, members who opted
 * in surface as GPS pins; the per-space zones are drawn as labelled
 * overlay circles. HA-defined zone names never reach this component
 * (the server strips them at the household boundary).
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
import { Modal } from './Modal'
import { showToast } from './Toast'
import type { SpaceZone } from '@/types'

interface SpacePresenceEntry {
  user_id: string
  username: string
  display_name: string
  state: string
  latitude: number | null
  longitude: number | null
  gps_accuracy_m: number | null
  picture_url: string | null
}

interface SpacePresenceResponse {
  feature_enabled: boolean
  entries: SpacePresenceEntry[]
}

interface SpaceZonesResponse {
  zones: SpaceZone[]
}

interface SpaceMember {
  user_id: string
  location_share_enabled?: boolean
}

const POLL_INTERVAL_MS = 30_000

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

    return () => {
      cancelled = true
      clearInterval(t)
      offZone()
    }
  }, [spaceId, currentUserId])

  const setMyOptIn = async (enabled: boolean) => {
    try {
      await api.patch(
        `/api/spaces/${spaceId}/members/me/location-sharing`,
        { enabled },
      )
      setSharingMe(enabled)
      showToast(
        enabled
          ? 'Now sharing your location with this space'
          : 'You stopped sharing your location with this space',
        'success',
      )
    } catch (e: any) {
      showToast(e.message || 'Failed to update', 'error')
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
      Could not load space presence: {error}
    </div>
  )
  if (!data) return null

  if (!data.feature_enabled) {
    return (
      <div class="sh-space-location sh-muted">
        <p>
          <strong>Location sharing is off for this space.</strong>
        </p>
        <p>
          An admin can turn it on in Space Settings → Location sharing.
          When enabled, each member also opts in individually so their
          GPS reaches the space.
        </p>
      </div>
    )
  }

  const markers: LocationMarker[] = data.entries
    .filter((p) => p.latitude != null && p.longitude != null)
    .map((p) => ({
      id: p.user_id,
      lat: p.latitude as number,
      lon: p.longitude as number,
      accuracy_m: p.gps_accuracy_m,
      label: p.display_name,
      sub_label: matchZoneName(zones, p.latitude as number, p.longitude as number) || p.state,
      avatar_url: p.picture_url,
      state: p.state,
    }))

  const sharing = markers.length
  const total = data.entries.length

  return (
    <div class="sh-space-location">
      {currentUserId && (
        <div class={`sh-sharing-chip ${sharingMe ? 'sh-sharing-chip--on' : 'sh-sharing-chip--off'}`}>
          <span>
            {sharingMe
              ? '📍 You are sharing your location with this space'
              : '📵 Your location is private here'}
          </span>
          <Button
            variant={sharingMe ? 'danger' : 'primary'}
            onClick={() => setMyOptIn(!sharingMe)}
          >
            {sharingMe ? 'Stop sharing' : 'Share my location'}
          </Button>
        </div>
      )}
      <LocationMap
        markers={markers}
        zones={zones}
        height={380}
        emptyLabel={
          total === 0
            ? 'No one in this space is sharing GPS yet.'
            : 'No one in this space is sharing GPS right now.'
        }
      />
      <div class="sh-location-map-footer sh-muted">
        <span>{sharing} of {total} sharing GPS</span>
        <span>{zones.length} zone{zones.length === 1 ? '' : 's'} configured</span>
      </div>
      <Modal
        open={showOnboarding}
        onClose={() => dismissOnboarding(null)}
        title="📍 Share your location with this space?"
      >
        <div class="sh-modal-body">
          <p>
            This space shows members on a map. If you opt in, your GPS
            coordinates will be visible to other members of this space —
            but never to other spaces or other households outside this
            space.
          </p>
          <ul>
            <li>You can stop sharing at any time from the map tab.</li>
            <li>Your home assistant zones never reach this space.</li>
          </ul>
          <div class="sh-modal-actions">
            <Button variant="secondary" onClick={() => dismissOnboarding(null)}>
              Not now
            </Button>
            <Button variant="primary" onClick={() => dismissOnboarding(true)}>
              Share my location
            </Button>
          </div>
        </div>
      </Modal>
    </div>
  )
}

/** Client-side match of a marker's GPS to the closest zone whose
 *  haversine distance is ≤ radius_m. Returns the zone name, or null
 *  when the pin is outside every zone. The server never sends
 *  preprocessed labels — see §23.8.7. */
function matchZoneName(
  zones: SpaceZone[],
  lat: number,
  lon: number,
): string | null {
  let best: { name: string; d: number } | null = null
  for (const z of zones) {
    const d = haversineMeters(lat, lon, z.latitude, z.longitude)
    if (d <= z.radius_m && (best === null || d < best.d)) {
      best = { name: z.name, d }
    }
  }
  return best?.name ?? null
}

function haversineMeters(
  lat1: number, lon1: number, lat2: number, lon2: number,
): number {
  const R = 6_371_000
  const toRad = (deg: number) => (deg * Math.PI) / 180
  const dLat = toRad(lat2 - lat1)
  const dLon = toRad(lon2 - lon1)
  const a =
    Math.sin(dLat / 2) ** 2
    + Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2
  return 2 * R * Math.asin(Math.sqrt(a))
}
