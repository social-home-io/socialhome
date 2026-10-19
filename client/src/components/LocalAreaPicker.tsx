/**
 * LocalAreaPicker — choose a local community's area: a centre and a
 * radius. Households join the space only while their home lies inside.
 *
 * ``mode="pin"`` picks a GLOBAL space's place pin instead: a point with an
 * optional radius HINT ("best for people within 10 km", or "No radius" —
 * the default), and NO "Use our home" shortcut — a pin is shown to everyone
 * who can find the space, so it is never the household's home by default.
 * The hint is never enforced.
 *
 * ``mode="home"`` picks the HOUSEHOLD's own home (Admin › Settings › Our
 * home location, standalone): a point only — no radius, no hint, no
 * shortcut — drawn as a 🏠.
 *
 * The centre is tapped on the map, typed as latitude / longitude (the
 * keyboard way in — there is no place search, which would be a third-party
 * fetch), or "Use our home" takes the HOUSEHOLD's home location (never this device's position — the space is
 * about where people live, not where the phone happens to be). The radius
 * is one of a few presets. Coordinates are rounded to 4 dp here so the
 * preview matches what the server stores (it truncates again).
 */
import { useEffect, useState } from 'preact/hooks'
import { t } from '@/i18n/i18n'
import type { LocalArea } from '@/types'
import { coordLabel } from '@/utils/coordLabel'
import {
  DEFAULT_RADIUS_KM, RADIUS_PRESETS_KM, radiusLabel, round4, type Home,
} from '@/utils/localArea'
import { Button } from './Button'
import { ChipRadioGroup } from './ChipRadioGroup'
import { LocalAreaMap } from './LocalAreaMap'
import { Modal } from './Modal'

/** A typed coordinate: a number (``,`` or ``.`` decimal) within ±limit,
 *  rounded to 4 dp — else ``null``. */
export function parseCoord(raw: string, limit: number): number | null {
  const text = raw.trim().replace(',', '.')
  if (!/^-?\d+(\.\d+)?$/.test(text)) return null
  const v = Number(text)
  if (!Number.isFinite(v) || Math.abs(v) > limit) return null
  return round4(v) + 0
}

const fmt4 = (v: number): string => v.toFixed(4)

/** How big the dot is that marks a place pin on the map (km). */
export const PIN_DOT_KM = 0.15

/** The words each mode uses (``t`` keys). */
const COPY = {
  area: {
    title: 'local_area.picker.title', intro: 'local_area.picker.intro',
    mapLabel: 'local_area.picker.map_label', at: 'local_area.picker.centre',
    tapHint: 'local_area.picker.tap_hint', coords: 'local_area.picker.coords_legend',
    privacy: 'local_area.picker.privacy', submit: 'local_area.picker.submit',
  },
  pin: {
    title: 'place_pin.picker.title', intro: 'place_pin.picker.intro',
    mapLabel: 'place_pin.picker.map_label', at: 'place_pin.picker.at',
    tapHint: 'place_pin.picker.tap_hint', coords: 'place_pin.picker.coords_legend',
    privacy: 'place_pin.privacy', submit: 'place_pin.picker.submit',
  },
  home: {
    title: 'home_location.title', intro: 'home_location.picker.intro',
    mapLabel: 'home_location.picker.map_label', at: 'home_location.picker.at',
    tapHint: 'home_location.picker.tap_hint', coords: 'home_location.picker.coords_legend',
    privacy: 'home_location.stays_home', submit: 'home_location.picker.submit',
  },
} as const

export function LocalAreaPicker({
  open, initial, home, onClose, onSubmit, mode = 'area', initialHintKm = null,
}: {
  open: boolean
  /** The current area (editing), or ``null`` for a new one. */
  initial: LocalArea | null
  /** This household's home: the "Use our home" shortcut and a 🏠 pin
   *  (area mode only). */
  home: Home | null | undefined
  onClose: () => void
  /** In pin mode read ``lat`` / ``lon`` and the second argument: the
   *  radius hint in km, or ``null`` for none. */
  onSubmit: (area: LocalArea, hintKm: number | null) => void
  mode?: 'area' | 'pin' | 'home'
  /** Pin mode: the stored radius hint (``null`` = none). */
  initialHintKm?: number | null
}) {
  const pin = mode === 'pin'
  /** A point with no radius to pick: a place pin, or our own home. */
  const point = mode !== 'area'
  const copy = COPY[mode]
  // A pin never starts from — or offers — the household's home (and our
  // home is what is being picked).
  const ownHome = point ? null : home
  const [centre, setCentre] = useState<Home | null>(null)
  const [radius, setRadius] = useState<number>(DEFAULT_RADIUS_KM)
  /** Pin mode: the suggested radius, or ``null`` ("No radius"). */
  const [hint, setHint] = useState<number | null>(null)
  // The typed fields mirror the centre; a map tap or "Use our home"
  // rewrites them, typing a valid pair moves the centre.
  const [latText, setLatText] = useState('')
  const [lonText, setLonText] = useState('')

  const place = (next: Home | null) => {
    setCentre(next)
    setLatText(next ? fmt4(next.lat) : '')
    setLonText(next ? fmt4(next.lon) : '')
  }

  const typed = (nextLat: string, nextLon: string) => {
    setLatText(nextLat)
    setLonText(nextLon)
    const lat = parseCoord(nextLat, 90)
    const lon = parseCoord(nextLon, 180)
    if (lat !== null && lon !== null) setCentre({ lat, lon })
  }
  const latBad = latText.trim() !== '' && parseCoord(latText, 90) === null
  const lonBad = lonText.trim() !== '' && parseCoord(lonText, 180) === null

  // Every open starts from the stored area (or nothing).
  useEffect(() => {
    if (!open) return
    place(initial ? { lat: initial.lat, lon: initial.lon } : null)
    setRadius(initial && RADIUS_PRESETS_KM.includes(initial.radius_km)
      ? initial.radius_km
      : initial?.radius_km ?? DEFAULT_RADIUS_KM)
    setHint(initialHintKm)
  }, [open]) // eslint-disable-line react-hooks/exhaustive-deps -- read on open only

  const area: LocalArea | null = centre
    ? { ...centre, radius_km: pin ? hint ?? PIN_DOT_KM : point ? PIN_DOT_KM : radius }
    : null
  const radiusOptions = (RADIUS_PRESETS_KM.includes(radius)
    ? RADIUS_PRESETS_KM : [...RADIUS_PRESETS_KM, radius].sort((a, b) => a - b))
    .map((km) => ({ value: String(km), label: radiusLabel(km) }))
  const hintOptions = [
    { value: 'none', label: t('place_pin.hint.none') },
    ...(hint === null || RADIUS_PRESETS_KM.includes(hint)
      ? RADIUS_PRESETS_KM : [...RADIUS_PRESETS_KM, hint].sort((a, b) => a - b))
      .map((km) => ({ value: String(km), label: radiusLabel(km) })),
  ]

  const submit = (e: Event) => {
    e.preventDefault()
    if (area) onSubmit(area, pin ? hint : null)
  }

  return (
    <Modal open={open} onClose={onClose} title={t(copy.title)}>
      <form class="sh-form sh-local-area-picker" onSubmit={submit}>
        <p class="sh-muted">{t(copy.intro)}</p>
        <LocalAreaMap
          area={area}
          home={ownHome}
          height={240}
          onPick={(lat, lon) => place({ lat: round4(lat), lon: round4(lon) })}
          ariaLabel={t(copy.mapLabel)}
          zoneName={pin ? t('place_pin.map_zone') : mode === 'home' ? t('local_area.your_home') : undefined}
          zoneIcon={mode === 'home' ? '🏠' : undefined}
        />
        <p class="sh-muted sh-local-area-picker__where" role="status">
          {centre
            ? t(copy.at, { place: coordLabel(centre.lat, centre.lon) })
            : t(copy.tapHint)}
        </p>
        {ownHome && (
          <button type="button" class="sh-link sh-local-area-picker__home"
                  onClick={() => place({ lat: round4(ownHome.lat), lon: round4(ownHome.lon) })}>
            🏠 {t('local_area.picker.use_home')}
          </button>
        )}
        <fieldset class="sh-local-area-picker__coords">
          <legend>{t(copy.coords)}</legend>
          <label>
            {t('local_area.picker.lat')}
            <input type="text" inputMode="decimal" autoComplete="off" spellcheck={false}
                   name="local-area-lat" value={latText} placeholder="47.3769"
                   aria-invalid={latBad ? 'true' : undefined}
                   aria-describedby={latBad ? 'local-area-lat-err' : undefined}
                   onInput={(e) => typed((e.target as HTMLInputElement).value, lonText)} />
            {latBad && <span id="local-area-lat-err" class="sh-form-error">{t('local_area.picker.lat_invalid')}</span>}
          </label>
          <label>
            {t('local_area.picker.lon')}
            <input type="text" inputMode="decimal" autoComplete="off" spellcheck={false}
                   name="local-area-lon" value={lonText} placeholder="8.5417"
                   aria-invalid={lonBad ? 'true' : undefined}
                   aria-describedby={lonBad ? 'local-area-lon-err' : undefined}
                   onInput={(e) => typed(latText, (e.target as HTMLInputElement).value)} />
            {lonBad && <span id="local-area-lon-err" class="sh-form-error">{t('local_area.picker.lon_invalid')}</span>}
          </label>
        </fieldset>
        {!point && <div class="sh-local-area-picker__radius">
          <span id="local-area-radius-label" class="sh-local-area-picker__radius-label">
            {t('local_area.picker.radius')}
          </span>
          <ChipRadioGroup
            options={radiusOptions}
            value={String(radius)}
            onChange={(v) => setRadius(Number(v))}
            labelledBy="local-area-radius-label"
          />
        </div>}
        {pin && <div class="sh-local-area-picker__radius">
          <span id="place-pin-hint-label" class="sh-local-area-picker__radius-label">
            {t('place_pin.hint.legend')}
          </span>
          <ChipRadioGroup
            options={hintOptions}
            value={hint === null ? 'none' : String(hint)}
            onChange={(v) => setHint(v === 'none' ? null : Number(v))}
            labelledBy="place-pin-hint-label"
          />
          <p class="sh-ss-hint">{t('place_pin.hint.only_a_hint')}</p>
        </div>}
        <p class="sh-ss-hint">{t(copy.privacy)}</p>
        <div class="sh-form-actions">
          <Button type="button" variant="secondary" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button type="submit" disabled={!area || latBad || lonBad}>
            {t(copy.submit)}
          </Button>
        </div>
      </form>
    </Modal>
  )
}
