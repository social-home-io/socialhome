/**
 * SpaceZonesAdmin — admin CRUD UI for the per-space zone catalogue (§23.8.7).
 *
 * Two-pane layout:
 *
 *   ┌──────────────┬────────────────────────────────┐
 *   │  Zone list   │  Leaflet map showing all zones │
 *   │  (left)      │  (right). Click on a zone in   │
 *   │              │  the list to focus, "Add zone" │
 *   │              │  to start the picker.          │
 *   └──────────────┴────────────────────────────────┘
 *
 * Add / edit modal: name, color picker, radius slider (25 m – 50 km),
 * Leaflet click-to-place + drag-radius-handle picker, plus number-input
 * fallbacks for accessibility / keyboard-only users.
 *
 * Delete uses the existing :component:`ConfirmDialog`. All writes go
 * through the :path:`/api/spaces/{id}/zones` endpoints; the central
 * exception map maps backend errors (radius out-of-bounds, duplicate
 * name, 50-zones cap) to toasts.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'

import { api } from '@/api'
import { Button } from './Button'
import { ConfirmDialog } from './ConfirmDialog'
import { Modal } from './Modal'
import { showToast } from './Toast'
import type { SpaceZone } from '@/types'

interface ZonesResponse {
  zones: SpaceZone[]
}

const PALETTE = [
  '#3b82f6', '#f97316', '#10b981', '#a855f7', '#ec4899',
  '#facc15', '#14b8a6', '#ef4444', '#6366f1', '#84cc16',
]

const MIN_RADIUS_M = 25
const MAX_RADIUS_M = 50_000

interface DraftZone {
  /** Id of the zone being edited; null = new zone. */
  id: string | null
  name: string
  latitude: number | null
  longitude: number | null
  radius_m: number
  color: string | null
}

const _BLANK_DRAFT: DraftZone = {
  id: null,
  name: '',
  latitude: null,
  longitude: null,
  radius_m: 200,
  color: PALETTE[0],
}

/** Convert a slider value in [0, 1000] → radius in metres on a log
 *  scale. The slider always maps cleanly onto the 25 m – 50 km range
 *  so the cursor moves at a perceptually-uniform speed across the
 *  three orders of magnitude. */
function _sliderToRadius(slider: number): number {
  const t = Math.max(0, Math.min(1000, slider)) / 1000
  const min = Math.log(MIN_RADIUS_M)
  const max = Math.log(MAX_RADIUS_M)
  return Math.round(Math.exp(min + t * (max - min)))
}
function _radiusToSlider(radius_m: number): number {
  const min = Math.log(MIN_RADIUS_M)
  const max = Math.log(MAX_RADIUS_M)
  const t = (Math.log(Math.max(MIN_RADIUS_M, Math.min(MAX_RADIUS_M, radius_m))) - min) / (max - min)
  return Math.round(t * 1000)
}

/** Format metres for display: 25 m, 1.2 km, 50 km. */
function _fmtRadius(m: number): string {
  if (m < 1000) return `${m} m`
  return `${(m / 1000).toFixed(m < 10_000 ? 1 : 0)} km`
}

export function SpaceZonesAdmin({ spaceId }: { spaceId: string }) {
  const [zones, setZones] = useState<SpaceZone[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [draft, setDraft] = useState<DraftZone | null>(null)
  const [pendingDelete, setPendingDelete] = useState<SpaceZone | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    void reload()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [spaceId])

  const reload = async () => {
    setLoading(true)
    setError(null)
    try {
      const body = await api.get<ZonesResponse>(`/api/spaces/${spaceId}/zones`)
      setZones(body.zones || [])
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }

  const startCreate = () => setDraft({ ..._BLANK_DRAFT })
  const startEdit = (z: SpaceZone) => setDraft({
    id: z.id,
    name: z.name,
    latitude: z.latitude,
    longitude: z.longitude,
    radius_m: z.radius_m,
    color: z.color ?? PALETTE[0],
  })

  const closeDraft = () => setDraft(null)

  const saveDraft = async () => {
    if (!draft) return
    if (!draft.name.trim()) {
      showToast('Zone name is required', 'error')
      return
    }
    if (draft.latitude == null || draft.longitude == null) {
      showToast('Click on the map to place the zone centre', 'error')
      return
    }
    setBusy(true)
    try {
      const body = {
        name: draft.name.trim(),
        latitude: draft.latitude,
        longitude: draft.longitude,
        radius_m: draft.radius_m,
        color: draft.color,
      }
      if (draft.id) {
        await api.patch(`/api/spaces/${spaceId}/zones/${draft.id}`, body)
        showToast('Zone updated', 'success')
      } else {
        await api.post(`/api/spaces/${spaceId}/zones`, body)
        showToast('Zone created', 'success')
      }
      setDraft(null)
      await reload()
    } catch (e: any) {
      showToast(e.message || 'Failed to save zone', 'error')
    } finally {
      setBusy(false)
    }
  }

  const confirmDelete = async () => {
    if (!pendingDelete) return
    setBusy(true)
    try {
      await api.delete(`/api/spaces/${spaceId}/zones/${pendingDelete.id}`)
      showToast('Zone deleted', 'info')
      setPendingDelete(null)
      await reload()
    } catch (e: any) {
      showToast(e.message || 'Failed to delete zone', 'error')
    } finally {
      setBusy(false)
    }
  }

  if (loading) return <div class="sh-muted">Loading zones…</div>
  if (error) {
    return (
      <div class="sh-error-state" role="alert">
        Could not load zones: {error}
      </div>
    )
  }

  return (
    <div class="sh-zones-admin">
      <header class="sh-zones-admin__header">
        <h3>Space zones</h3>
        <div class="sh-muted">
          {zones.length} of 50 zones used
        </div>
        <Button variant="primary" onClick={startCreate} disabled={zones.length >= 50}>
          + Add zone
        </Button>
      </header>

      <div class="sh-zones-admin__split">
        <ul class="sh-zones-admin__list" data-testid="zone-list">
          {zones.length === 0 && (
            <li class="sh-muted">
              No zones yet. Add a zone to label members on the map
              (e.g. "The Workshop", "Coffee Shop").
            </li>
          )}
          {zones.map((z) => (
            <li key={z.id} class="sh-zones-admin__item">
              <span
                class="sh-zones-admin__swatch"
                style={`background: ${z.color || PALETTE[0]}`}
                aria-hidden="true"
              />
              <div class="sh-zones-admin__meta">
                <strong>{z.name}</strong>
                <span class="sh-muted">
                  {_fmtRadius(z.radius_m)} ·{' '}
                  {z.latitude.toFixed(4)}, {z.longitude.toFixed(4)}
                </span>
              </div>
              <div class="sh-zones-admin__actions">
                <Button variant="secondary" onClick={() => startEdit(z)}>Edit</Button>
                <Button variant="danger" onClick={() => setPendingDelete(z)}>Delete</Button>
              </div>
            </li>
          ))}
        </ul>

        <ZonesPreviewMap zones={zones} draft={draft} />
      </div>

      {draft !== null && (
        <ZoneEditDialog
          draft={draft}
          onChange={setDraft}
          onCancel={closeDraft}
          onSave={saveDraft}
          busy={busy}
        />
      )}

      <ConfirmDialog
        open={pendingDelete !== null}
        title="Delete zone?"
        message={
          pendingDelete
            ? `Delete "${pendingDelete.name}"? Members' GPS pins will no longer carry this label.`
            : ''
        }
        confirmLabel="Delete"
        destructive
        onConfirm={() => void confirmDelete()}
        onCancel={() => setPendingDelete(null)}
      />
    </div>
  )
}

// ─── Preview map (right pane) ───────────────────────────────────────────


function ZonesPreviewMap({
  zones, draft,
}: {
  zones: SpaceZone[]
  draft: DraftZone | null
}) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const mapRef = useRef<L.Map | null>(null)
  const layerRef = useRef<L.LayerGroup | null>(null)

  useEffect(() => {
    if (!containerRef.current) return
    if (mapRef.current) return
    const map = L.map(containerRef.current, {
      center: [52.0, 5.0],
      zoom: 4,
      scrollWheelZoom: 'center',
    })
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      attribution:
        '&copy; <a href="https://www.openstreetmap.org/copyright">'
        + 'OpenStreetMap</a> contributors',
    }).addTo(map)
    layerRef.current = L.layerGroup().addTo(map)
    mapRef.current = map
    const ro = new ResizeObserver(() => map.invalidateSize())
    ro.observe(containerRef.current)
    return () => {
      ro.disconnect()
      map.remove()
      mapRef.current = null
      layerRef.current = null
    }
  }, [])

  useEffect(() => {
    const map = mapRef.current
    const layer = layerRef.current
    if (!map || !layer) return
    layer.clearLayers()
    for (const z of zones) {
      const colour = z.color || PALETTE[0]
      L.circle([z.latitude, z.longitude], {
        radius: z.radius_m,
        color: colour,
        opacity: 0.6,
        fillOpacity: 0.10,
        weight: 1.5,
      }).addTo(layer).bindTooltip(z.name, {
        permanent: true,
        direction: 'center',
        className: 'sh-zone-label',
      })
    }
    if (draft && draft.latitude != null && draft.longitude != null) {
      L.circle([draft.latitude, draft.longitude], {
        radius: draft.radius_m,
        color: draft.color || PALETTE[0],
        opacity: 0.9,
        fillOpacity: 0.15,
        weight: 2,
        dashArray: '4 6',
      }).addTo(layer)
    }
    const all: L.LatLngExpression[] = zones.map(
      (z) => [z.latitude, z.longitude] as [number, number],
    )
    if (draft && draft.latitude != null && draft.longitude != null) {
      all.push([draft.latitude, draft.longitude])
    }
    if (all.length === 1) {
      map.setView(all[0], 13)
    } else if (all.length > 1) {
      map.fitBounds(L.latLngBounds(all).pad(0.3), { maxZoom: 14 })
    }
  }, [zones, draft])

  return (
    <div class="sh-zones-admin__map">
      <div ref={containerRef} class="sh-zones-admin__canvas" data-testid="zones-map" />
    </div>
  )
}


// ─── Edit modal ─────────────────────────────────────────────────────────


function ZoneEditDialog({
  draft, onChange, onCancel, onSave, busy,
}: {
  draft: DraftZone
  onChange: (next: DraftZone) => void
  onCancel: () => void
  onSave: () => void
  busy: boolean
}) {
  const pickerRef = useRef<HTMLDivElement | null>(null)
  const mapRef = useRef<L.Map | null>(null)
  const markerRef = useRef<L.Circle | null>(null)
  // Latch the latest draft in a ref so the click-handler closure
  // (registered once at mount) can read fresh radius / colour values.
  const draftRef = useRef(draft)
  draftRef.current = draft
  const onChangeRef = useRef(onChange)
  onChangeRef.current = onChange

  useEffect(() => {
    if (!pickerRef.current) return
    if (mapRef.current) return
    const initial: L.LatLngExpression =
      draft.latitude != null && draft.longitude != null
        ? [draft.latitude, draft.longitude]
        : [52.0, 5.0]
    const map = L.map(pickerRef.current, {
      center: initial,
      zoom: draft.latitude != null ? 13 : 4,
      scrollWheelZoom: 'center',
    })
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      attribution:
        '&copy; <a href="https://www.openstreetmap.org/copyright">'
        + 'OpenStreetMap</a> contributors',
    }).addTo(map)
    map.on('click', (ev: L.LeafletMouseEvent) => {
      const lat = Math.round(ev.latlng.lat * 10_000) / 10_000
      const lon = Math.round(ev.latlng.lng * 10_000) / 10_000
      onChangeRef.current({
        ...draftRef.current,
        latitude: lat,
        longitude: lon,
      })
    })
    mapRef.current = map
    const ro = new ResizeObserver(() => map.invalidateSize())
    ro.observe(pickerRef.current)
    return () => {
      ro.disconnect()
      map.remove()
      mapRef.current = null
      markerRef.current = null
    }
  }, [])

  useEffect(() => {
    const map = mapRef.current
    if (!map) return
    if (markerRef.current) {
      markerRef.current.remove()
      markerRef.current = null
    }
    if (draft.latitude != null && draft.longitude != null) {
      markerRef.current = L.circle(
        [draft.latitude, draft.longitude],
        {
          radius: draft.radius_m,
          color: draft.color || PALETTE[0],
          opacity: 0.9,
          fillOpacity: 0.18,
          weight: 2,
        },
      ).addTo(map)
    }
  }, [draft])

  return (
    <Modal
      open
      onClose={onCancel}
      title={draft.id ? 'Edit zone' : 'New zone'}
    >
      <div class="sh-zone-form">
        <label>
          Name
          <input
            type="text"
            value={draft.name}
            maxLength={64}
            onInput={(e) => onChange({
              ...draft,
              name: (e.target as HTMLInputElement).value,
            })}
          />
        </label>

        <fieldset class="sh-zone-form__palette" aria-label="Zone colour">
          <legend>Colour</legend>
          {PALETTE.map((c) => (
            <button
              key={c}
              type="button"
              class={`sh-zone-swatch ${draft.color === c ? 'sh-zone-swatch--selected' : ''}`}
              style={`background: ${c}`}
              aria-label={`Use colour ${c}`}
              aria-pressed={draft.color === c}
              onClick={() => onChange({ ...draft, color: c })}
            />
          ))}
        </fieldset>

        <label>
          Radius
          <input
            type="range"
            min={0}
            max={1000}
            value={_radiusToSlider(draft.radius_m)}
            onInput={(e) => onChange({
              ...draft,
              radius_m: _sliderToRadius(
                Number((e.target as HTMLInputElement).value),
              ),
            })}
          />
          <span class="sh-muted">{_fmtRadius(draft.radius_m)}</span>
        </label>

        <p class="sh-muted">
          Click on the map to place the zone centre, or type the
          coordinates directly below for keyboard-only access.
        </p>
        <div ref={pickerRef} class="sh-zone-form__map" data-testid="zone-picker-map" />

        <div class="sh-zone-form__coords">
          <label>
            Latitude
            <input
              type="number"
              step="0.0001"
              value={draft.latitude ?? ''}
              onInput={(e) => onChange({
                ...draft,
                latitude: parseFloat(
                  (e.target as HTMLInputElement).value,
                ) || null,
              })}
            />
          </label>
          <label>
            Longitude
            <input
              type="number"
              step="0.0001"
              value={draft.longitude ?? ''}
              onInput={(e) => onChange({
                ...draft,
                longitude: parseFloat(
                  (e.target as HTMLInputElement).value,
                ) || null,
              })}
            />
          </label>
        </div>

        <div class="sh-zone-form__actions">
          <Button variant="secondary" onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
          <Button variant="primary" onClick={onSave} disabled={busy}>
            {draft.id ? 'Save changes' : 'Create zone'}
          </Button>
        </div>
      </div>
    </Modal>
  )
}
