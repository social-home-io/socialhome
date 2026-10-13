/**
 * ZonesAdmin — admin CRUD UI shared by every zone catalogue: a space's
 * zones (§23.8.7, :component:`SpaceZonesAdmin`) and the household's own
 * zones (Admin › Zones).
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
 * Add / edit modal: name, icon picker, color picker, radius slider (25 m – 50 km),
 * Leaflet click-to-place + drag-radius-handle picker, plus number-input
 * fallbacks for accessibility / keyboard-only users.
 *
 * Delete uses the existing :component:`ConfirmDialog`. All writes go
 * through ``{basePath}`` / ``{basePath}/{id}``; the central exception
 * map turns backend errors (radius out-of-bounds, duplicate name,
 * 50-zones cap) into translated toasts.
 *
 * ``household`` turns on the household extras: one Home zone (listed
 * first with a badge, never deleted — Home moves only by marking another
 * zone as Home) and read-only rows synced from Home Assistant.
 */
import type { ComponentChildren } from 'preact'
import { useEffect, useRef, useState } from 'preact/hooks'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'

import { api } from '@/api'
import { addTileLayer, tileErrorMessage } from '@/utils/mapTiles'
import { escapeHtml } from '@/utils/html'
import { ZONE_PALETTE, safeZoneHex, zoneColor } from '@/utils/zoneColor'
import { ZONE_ICON_PRESETS, sortZones } from '@/utils/zones'
import { Button } from './Button'
import { ConfirmDialog } from './ConfirmDialog'
import { IconPicker } from './IconPicker'
import { Modal } from './Modal'
import { showToast } from './Toast'
import { ws } from '@/ws'
import { t } from '@/i18n/i18n'

/** The fields the editor reads — a :type:`SpaceZone` or a
 *  :type:`HouseholdZone` both fit. */
export interface AdminZone {
  id: string
  name: string
  latitude: number
  longitude: number
  radius_m: number
  color: string | null
  icon: string | null
  /** Household zones only: the household's Home. */
  is_home?: boolean
  /** Household zones only: synced from Home Assistant, not editable here. */
  read_only?: boolean
}

interface ZonesResponse {
  zones: AdminZone[]
}

export interface ZonesAdminProps {
  /** Collection endpoint, e.g. ``/api/spaces/{id}/zones``. */
  basePath: string
  /** Heading above the list. */
  title: string
  /** Shown in the list when there are no zones yet. */
  emptyText: string
  /** One explanatory line under the heading. */
  intro?: string
  /** WS frame type meaning "this list changed" — refetched quietly. */
  refetchOn?: string
  /** Household zones: Home zone + read-only synced rows. */
  household?: boolean
  /** Shown instead of the editor when the load answers 403
   *  ``FEATURE_DISABLED``; without it that is a plain load error. */
  featureDisabled?: ComponentChildren
  /** Extra controls under the intro (household: the Home Assistant sync
   *  switch). Rendered only once the list has loaded. */
  toolbar?: ComponentChildren
  /** Bump to refetch the list quietly (e.g. after a sync was toggled). */
  reloadSignal?: number
}

const PALETTE = ZONE_PALETTE

const MIN_RADIUS_M = 25
const MAX_RADIUS_M = 50_000
/** Server-side cap, the same for space and household zones. */
const MAX_ZONES = 50

interface DraftZone {
  /** Id of the zone being edited; null = new zone. */
  id: string | null
  name: string
  latitude: number | null
  longitude: number | null
  radius_m: number
  color: string | null
  /** One emoji, or null for "no icon". */
  icon: string | null
  /** Household zones: this zone is (or becomes) the Home zone. */
  is_home: boolean
}

const _BLANK_DRAFT: DraftZone = {
  id: null,
  name: '',
  latitude: null,
  longitude: null,
  radius_m: 200,
  color: PALETTE[0],
  icon: null,
  is_home: false,
}

/** How the edit dialog shows the household Home toggle. */
interface HomeControl {
  /** The toggle can't change: this zone is Home already, or Home is
   *  synced from Home Assistant. */
  locked: boolean
  /** One line under the toggle saying what it does / why it's locked. */
  hint: string
}

/** The Home toggle for ``draft`` given the household's current zones. */
function _homeControl(zones: AdminZone[], draft: DraftZone): HomeControl {
  const home = zones.find((z) => z.is_home)
  if (home?.read_only) return { locked: true, hint: t('zones.home.synced_hint') }
  if (home && home.id === draft.id) return { locked: true, hint: t('zones.home.current_hint') }
  if (home) return { locked: false, hint: t('zones.home.move_hint', { name: home.name }) }
  return { locked: false, hint: t('zones.home.hint') }
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

export function ZonesAdmin({
  basePath, title, emptyText, intro, refetchOn, household = false, featureDisabled,
  toolbar, reloadSignal = 0,
}: ZonesAdminProps) {
  const [zones, setZones] = useState<AdminZone[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [featureOff, setFeatureOff] = useState(false)
  const [draft, setDraft] = useState<DraftZone | null>(null)
  const [pendingDelete, setPendingDelete] = useState<AdminZone | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    void reload()
  }, [basePath])

  // Another admin (or a Home Assistant sync) changed the list: refetch
  // without the "Loading…" swap, so an open dialog and the map stay put.
  // Read through a ref so the subscription sees the current ``basePath``
  // without re-subscribing on every render.
  const reloadRef = useRef<(quiet?: boolean) => Promise<void>>(async () => {})
  useEffect(() => {
    if (!refetchOn) return
    return ws.on(refetchOn, () => { void reloadRef.current(true) })
  }, [refetchOn])

  /** Fetch the list. ``quiet`` keeps the current list on screen (and on
   *  a failure) instead of flashing the loading / error state. */
  const reload = async (quiet = false) => {
    if (!quiet) {
      setLoading(true)
      setError(null)
      setFeatureOff(false)
    }
    try {
      const body = await api.get<ZonesResponse>(basePath)
      setZones(body.zones || [])
      // A quiet refetch that works replaces an earlier failed load.
      setError(null)
      setFeatureOff(false)
    } catch (e) {
      if (quiet) return
      // Duck-typed: an ``ApiError`` carries the server's ``code``.
      if (featureDisabled && (e as { code?: unknown }).code === 'FEATURE_DISABLED') {
        setFeatureOff(true)
      } else {
        setError((e as Error).message)
      }
    } finally {
      if (!quiet) setLoading(false)
    }
  }

  reloadRef.current = reload

  useEffect(() => {
    if (reloadSignal > 0) void reloadRef.current(true)
  }, [reloadSignal])

  const homeZone = household ? zones.find((z) => z.is_home) : undefined
  // The first zone a household adds is its Home — pre-fill it.
  const startsAsHome = household && homeZone === undefined
  const startCreate = () => setDraft(startsAsHome
    ? { ..._BLANK_DRAFT, name: t('zones.home.default_name'), icon: '🏠', is_home: true }
    : { ..._BLANK_DRAFT })
  const startEdit = (z: AdminZone) => setDraft({
    id: z.id,
    name: z.name,
    latitude: z.latitude,
    longitude: z.longitude,
    radius_m: z.radius_m,
    color: safeZoneHex(z.color) ?? PALETTE[0],
    icon: z.icon ?? null,
    is_home: z.is_home === true,
  })

  const closeDraft = () => setDraft(null)

  const saveDraft = async () => {
    if (!draft) return
    if (!draft.name.trim()) {
      showToast(t('zones.name_required'), 'error')
      return
    }
    if (draft.latitude == null || draft.longitude == null) {
      showToast(t('zones.place_required'), 'error')
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
        icon: draft.icon,
        ...(household ? { is_home: draft.is_home } : {}),
      }
      if (draft.id) {
        await api.patch(`${basePath}/${draft.id}`, body)
        showToast(t('zones.updated'), 'success')
      } else {
        await api.post(basePath, body)
        showToast(t('zones.created'), 'success')
      }
      setDraft(null)
      await reload()
    } catch (e: any) {
      showToast(e.message || t('zones.save_failed'), 'error')
    } finally {
      setBusy(false)
    }
  }

  const confirmDelete = async () => {
    if (!pendingDelete) return
    setBusy(true)
    try {
      await api.delete(`${basePath}/${pendingDelete.id}`)
      showToast(t('zones.deleted'), 'info')
      setPendingDelete(null)
      await reload()
    } catch (e: any) {
      showToast(e.message || t('zones.delete_failed'), 'error')
    } finally {
      setBusy(false)
    }
  }

  if (loading) return <div class="sh-muted">{t('zones.loading')}</div>
  if (featureOff) return <div class="sh-zones-admin__off">{featureDisabled}</div>
  if (error) {
    return (
      <div class="sh-error-state" role="alert">
        {t('zones.load_failed', { error })}
      </div>
    )
  }

  return (
    <div class="sh-zones-admin">
      <header class="sh-zones-admin__header">
        <h3>{title}</h3>
        <div class="sh-muted">
          {t('zones.used', { n: String(zones.length), max: String(MAX_ZONES) })}
        </div>
        <Button variant="primary" onClick={startCreate} disabled={zones.length >= MAX_ZONES}>
          + {startsAsHome ? t('zones.add_home') : t('zones.add')}
        </Button>
      </header>
      {intro && <p class="sh-muted sh-zones-admin__intro">{intro}</p>}
      {toolbar}

      <div class="sh-zones-admin__split">
        <ul class="sh-zones-admin__list" data-testid="zone-list">
          {zones.length === 0 && (
            <li class="sh-muted">
              {emptyText}
            </li>
          )}
          {sortZones(zones).map((z) => (
            <li key={z.id} class="sh-zones-admin__item">
              <span
                class={`sh-zones-admin__swatch${z.icon ? ' sh-zone-icon--has-icon' : ''}`}
                style={{ background: zoneColor(z) }}
                aria-hidden="true"
              >
                {z.icon}
              </span>
              <div class="sh-zones-admin__meta">
                <strong>{z.name}</strong>
                {(z.is_home || z.read_only) && (
                  <span class="sh-zones-admin__badges">
                    {z.is_home && (
                      <span class="sh-badge sh-badge--success sh-zones-admin__badge--home">
                        🏠 {t('zones.home.badge')}
                      </span>
                    )}
                    {z.read_only && <span class="sh-badge">{t('zones.synced.badge')}</span>}
                  </span>
                )}
                <span class="sh-muted">
                  {_fmtRadius(z.radius_m)} ·{' '}
                  {z.latitude.toFixed(4)}, {z.longitude.toFixed(4)}
                </span>
                {z.read_only && (
                  <span class="sh-muted sh-zones-admin__note">{t('zones.synced.hint')}</span>
                )}
              </div>
              {!z.read_only && (
                <div class="sh-zones-admin__actions">
                  <Button variant="secondary" onClick={() => startEdit(z)}>{t('common.edit')}</Button>
                  {/* Home can't be deleted — it moves when another zone is marked Home. */}
                  {!z.is_home && (
                    <Button variant="danger" onClick={() => setPendingDelete(z)}>{t('common.delete')}</Button>
                  )}
                </div>
              )}
            </li>
          ))}
        </ul>

        <ZonesPreviewMap zones={zones} draft={draft} />
      </div>

      {draft !== null && (
        <ZoneEditDialog
          draft={draft}
          existingZones={zones}
          homeControl={household ? _homeControl(zones, draft) : null}
          onChange={setDraft}
          onCancel={closeDraft}
          onSave={saveDraft}
          busy={busy}
        />
      )}

      <ConfirmDialog
        open={pendingDelete !== null}
        title={t('zones.delete_title')}
        message={
          pendingDelete
            ? t('zones.delete_body', { name: pendingDelete.name })
            : ''
        }
        confirmLabel={t('common.delete')}
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
  zones: AdminZone[]
  draft: DraftZone | null
}) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const mapRef = useRef<L.Map | null>(null)
  const layerRef = useRef<L.LayerGroup | null>(null)
  const [tileError, setTileError] = useState(false)

  useEffect(() => {
    if (!containerRef.current) return
    if (mapRef.current) return
    const map = L.map(containerRef.current, {
      center: [52.0, 5.0],
      zoom: 4,
      scrollWheelZoom: 'center',
    })
    // Tiles come from the backend proxy and land a tick later — don't
    // touch a map the effect cleanup has already removed.
    let cancelled = false
    // Two failure surfaces: the config fetch (promise rejection) and
    // the tiles themselves (``onError``, after a streak — a 401 past
    // the signature TTL, a 429, or a dead upstream). Both land in the
    // same "Map unavailable" state; a silent grey square is the bug
    // this proxy exists to kill.
    void addTileLayer(
      map,
      () => cancelled,
      () => { if (!cancelled) setTileError(true) },
    ).catch(() => {
      if (!cancelled) setTileError(true)
    })
    layerRef.current = L.layerGroup().addTo(map)
    mapRef.current = map
    const ro = new ResizeObserver(() => map.invalidateSize())
    ro.observe(containerRef.current)
    return () => {
      cancelled = true
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
      const colour = zoneColor(z)
      // Circle is the only on-map mark for the zone — the name +
      // radius + coords live in the left-pane list (the admin's
      // legend). A hover tooltip surfaces the name without
      // cluttering the map with a permanent white label box.
      L.circle([z.latitude, z.longitude], {
        radius: z.radius_m,
        color: colour,
        opacity: 0.85,
        fillColor: colour,
        // Light fill so overlapping zones don't compound into a muddy
        // blob; the coloured border is what distinguishes a zone
        // visually, the left-pane list names every one.
        fillOpacity: 0.12,
        weight: 2,
      })
        .addTo(layer)
        // Leaflet mounts string tooltip / popup content via
        // ``innerHTML``, and a zone name can come from another
        // household's admin over federation — escape both.
        .bindTooltip(escapeHtml(z.name), {
          // Desktop hover surface — touch users get the tap popup below.
          direction: 'top',
          offset: [0, -4],
          className: 'sh-zone-tooltip',
        })
        .bindPopup(
          `<strong>${escapeHtml(z.name)}</strong>`,
          { closeButton: false, autoPan: false },
        )
    }
    if (draft && draft.latitude != null && draft.longitude != null) {
      L.circle([draft.latitude, draft.longitude], {
        radius: draft.radius_m,
        color: safeZoneHex(draft.color) ?? PALETTE[0],
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
    <div class="sh-zones-admin__map sh-map-wrap">
      <div ref={containerRef} class="sh-zones-admin__canvas" data-testid="zones-map" />
      {tileError && (
        <div class="sh-map-error">
          {tileErrorMessage()}
        </div>
      )}
    </div>
  )
}


// ─── Edit modal ─────────────────────────────────────────────────────────


function ZoneEditDialog({
  draft, existingZones, homeControl, onChange, onCancel, onSave, busy,
}: {
  draft: DraftZone
  /** Every zone currently configured in this catalogue. The picker draws
   *  the OTHERS (excluding the one being edited, if any) as faint
   *  ghost circles so the admin can avoid accidental overlap. */
  existingZones: AdminZone[]
  /** The household Home toggle, or null (space zones have no Home). */
  homeControl: HomeControl | null
  onChange: (next: DraftZone) => void
  onCancel: () => void
  onSave: () => void
  busy: boolean
}) {
  const pickerRef = useRef<HTMLDivElement | null>(null)
  const mapRef = useRef<L.Map | null>(null)
  const markerRef = useRef<L.Circle | null>(null)
  const ghostLayerRef = useRef<L.LayerGroup | null>(null)
  const [tileError, setTileError] = useState(false)
  // Latch the latest draft in a ref so the click-handler closure
  // (registered once at mount) can read fresh radius / colour values.
  const draftRef = useRef(draft)
  draftRef.current = draft
  const onChangeRef = useRef(onChange)
  onChangeRef.current = onChange

  useEffect(() => {
    if (!pickerRef.current) return
    if (mapRef.current) return
    // First-paint centre. If the draft already has coordinates use
    // those; otherwise fall back to a fit-to-existing-zones rectangle
    // so the user opens the picker looking at the neighbourhood
    // they're working in, not a zoom-4 Europe view that hides
    // everything.
    const others = existingZones.filter((z) => z.id !== draft.id)
    let initial: L.LatLngExpression = [52.0, 5.0]
    let initialZoom = 4
    if (draft.latitude != null && draft.longitude != null) {
      initial = [draft.latitude, draft.longitude]
      initialZoom = 13
    } else if (others.length > 0) {
      initial = [others[0].latitude, others[0].longitude]
      initialZoom = 13
    }
    const map = L.map(pickerRef.current, {
      center: initial,
      zoom: initialZoom,
      scrollWheelZoom: 'center',
    })
    // Tiles come from the backend proxy and land a tick later — don't
    // touch a map the effect cleanup has already removed.
    let cancelled = false
    // Two failure surfaces: the config fetch (promise rejection) and
    // the tiles themselves (``onError``, after a streak — a 401 past
    // the signature TTL, a 429, or a dead upstream). Both land in the
    // same "Map unavailable" state; a silent grey square is the bug
    // this proxy exists to kill.
    void addTileLayer(
      map,
      () => cancelled,
      () => { if (!cancelled) setTileError(true) },
    ).catch(() => {
      if (!cancelled) setTileError(true)
    })
    // Layer for the OTHER zones, drawn beneath the active draft so the
    // admin can see where existing zones sit and avoid placing the new
    // one on top of them. Refreshed in a dependent effect below.
    ghostLayerRef.current = L.layerGroup().addTo(map)
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
      cancelled = true
      ro.disconnect()
      map.remove()
      mapRef.current = null
      markerRef.current = null
      ghostLayerRef.current = null
    }
  }, [])

  // Paint every existing zone (except the one being edited) as a faint
  // ghost circle on the picker so the admin can see overlap before
  // committing.
  useEffect(() => {
    const layer = ghostLayerRef.current
    if (!layer) return
    layer.clearLayers()
    for (const z of existingZones) {
      if (z.id === draft.id) continue
      const colour = zoneColor(z)
      L.circle([z.latitude, z.longitude], {
        radius: z.radius_m,
        color: colour,
        opacity: 0.45,
        fillColor: colour,
        fillOpacity: 0.06,
        weight: 1.5,
        dashArray: '3 4',
        interactive: false,
      }).addTo(layer)
    }
  }, [existingZones, draft.id])

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
          color: safeZoneHex(draft.color) ?? PALETTE[0],
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
      title={draft.id ? t('zones.edit_title') : t('zones.new_title')}
    >
      <div class="sh-zone-form">
        <label>
          {t('zones.name')}
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

        {homeControl && (
          <div class="sh-zone-form__home">
            <label class="sh-zone-form__check">
              <input
                type="checkbox"
                checked={draft.is_home}
                disabled={homeControl.locked}
                aria-describedby="zone-home-hint"
                onChange={(e) => onChange({
                  ...draft,
                  is_home: (e.target as HTMLInputElement).checked,
                })}
              />
              {t('zones.home.label')}
            </label>
            <p id="zone-home-hint" class="sh-muted">{homeControl.hint}</p>
          </div>
        )}

        <IconPicker
          value={draft.icon}
          onChange={(icon) => onChange({ ...draftRef.current, icon })}
          id={`zone-icon-${draft.id ?? 'new'}`}
          groups={[{ label: t('zones.icon.places'), presets: ZONE_ICON_PRESETS }]}
          legend={t('zones.icon.legend')}
          noneLabel={t('zones.icon.none')}
          customLabel={t('zones.icon.custom')}
          customHint={t('zones.icon.custom_hint')}
        />

        <fieldset class="sh-zone-form__palette" aria-label={t('zones.colour_aria')}>
          <legend>{t('zones.colour')}</legend>
          {PALETTE.map((c) => (
            <button
              key={c}
              type="button"
              class={`sh-zone-swatch ${draft.color === c ? 'sh-zone-swatch--selected' : ''}`}
              style={{ background: c }}
              aria-label={t('zones.use_colour', { colour: c })}
              aria-pressed={draft.color === c}
              onClick={() => onChange({ ...draft, color: c })}
            />
          ))}
        </fieldset>

        <label>
          {t('zones.radius')}
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

        <div class="sh-zone-form__picker-row">
          <p class="sh-muted">
            {t('zones.picker_hint')}
          </p>
          <Button
            variant="secondary"
            onClick={() => {
              if (!navigator.geolocation) {
                showToast(t('zones.geo_unavailable'), 'error')
                return
              }
              navigator.geolocation.getCurrentPosition(
                (pos) => {
                  onChange({
                    ...draft,
                    latitude: Math.round(pos.coords.latitude * 10_000) / 10_000,
                    longitude: Math.round(pos.coords.longitude * 10_000) / 10_000,
                  })
                  showToast(t('zones.geo_used'), 'info')
                },
                () => {
                  showToast(t('zones.geo_failed'), 'error')
                },
                { enableHighAccuracy: true, timeout: 8000 },
              )
            }}
          >
            📍 {t('zones.use_location')}
          </Button>
        </div>
        <div class="sh-map-wrap">
          <div ref={pickerRef} class="sh-zone-form__map" data-testid="zone-picker-map" />
          {tileError && (
            <div class="sh-map-error">
              {tileErrorMessage()}
            </div>
          )}
        </div>

        <div class="sh-zone-form__coords">
          <label>
            {t('zones.latitude')}
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
            {t('zones.longitude')}
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
            {t('common.cancel')}
          </Button>
          <Button variant="primary" onClick={onSave} disabled={busy}>
            {draft.id ? t('zones.save') : t('zones.create')}
          </Button>
        </div>
      </div>
    </Modal>
  )
}
