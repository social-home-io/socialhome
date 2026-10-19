/**
 * LocationPicker — modal for choosing a one-shot location pin.
 *
 * Shared by the feed composer (location posts) and the DM composer
 * (``type='location'`` messages). Mirrors PollBuilder / ScheduleBuilder
 * so the Composer's "click submit on first press → open builder"
 * pattern just works.
 *
 * Flow:
 *   1. Two ways in: "Use my current location" (the browser asks for
 *      permission; we never set ``enableHighAccuracy`` — a coarse fix
 *      is all a 4-dp pin needs, and it's faster and kinder to battery)
 *      or "Pick a spot on the map" (no permission needed at all).
 *   2. The pin shows on a small map — the preview of exactly what will
 *      be shared. Tapping the map moves it.
 *   3. Optional label (cap 80 chars) + the 4-dp coordinate line.
 *   4. Submit returns a ``LocationDraft``.
 *
 * Denied / unavailable / timed-out geolocation renders inline with the
 * map-pick fallback one tap away, instead of a toast the user can miss.
 * Coordinates are rounded to 4 dp here so the preview matches what the
 * server stores; the server is the authoritative truncator.
 */
import { useEffect, useState } from 'preact/hooks'
import { t } from '@/i18n/i18n'
import { formatCoords } from '@/utils/dmLocation'
import { Button } from './Button'
import { LocationMap, type LocationMarker } from './LocationMap'
import { Modal } from './Modal'

export interface LocationDraft {
  lat: number
  lon: number
  label: string | null
  /** Accuracy of a geolocation fix in metres; absent for a map pick
   *  (or once the user moved the pin). The server buckets it. */
  accuracy_m?: number | null
}

interface LocationPickerProps {
  open: boolean
  onSubmit: (draft: LocationDraft) => void
  onClose: () => void
  /** Label of the confirm button — the DM composer says "Send
   *  location"; the feed composer keeps "Use this location". */
  submitLabel?: string
}

const LABEL_MAX = 80

/** Looked up as ``t(`location.error.${error}`)`` — i.e.
 *  t('location.error.denied'), t('location.error.unavailable'),
 *  t('location.error.timeout'), t('location.error.unsupported'). */
type GeoError = 'denied' | 'unavailable' | 'timeout' | 'unsupported'

const round4 = (v: number): number => Math.round(v * 1e4) / 1e4

export function LocationPicker({
  open, onSubmit, onClose, submitLabel,
}: LocationPickerProps) {
  const [coords, setCoords] = useState<{ lat: number; lon: number } | null>(null)
  const [accuracy, setAccuracy] = useState<number | null>(null)
  const [label, setLabel] = useState('')
  const [busy, setBusy] = useState(false)
  const [picking, setPicking] = useState(false)
  const [error, setError] = useState<GeoError | null>(null)

  // Every open starts fresh — a DM thread reuses one picker instance
  // for many shares, and last time's pin must not be re-sent by accident.
  useEffect(() => {
    if (!open) return
    setCoords(null)
    setAccuracy(null)
    setLabel('')
    setBusy(false)
    setPicking(false)
    setError(null)
  }, [open])

  const useCurrentLocation = () => {
    setError(null)
    if (!('geolocation' in navigator) || !navigator.geolocation) {
      setError('unsupported')
      return
    }
    setBusy(true)
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        setBusy(false)
        setCoords({
          lat: round4(pos.coords.latitude),
          lon: round4(pos.coords.longitude),
        })
        const acc = pos.coords.accuracy
        setAccuracy(typeof acc === 'number' && Number.isFinite(acc) ? acc : null)
      },
      (err) => {
        setBusy(false)
        setError(
          err.code === err.PERMISSION_DENIED ? 'denied'
            : err.code === err.POSITION_UNAVAILABLE ? 'unavailable'
              : 'timeout',
        )
      },
      // Deliberately no ``enableHighAccuracy`` — see the module doc.
      { timeout: 10000, maximumAge: 60000 },
    )
  }

  const pickOnMap = () => {
    setError(null)
    setPicking(true)
  }

  const onPick = (lat: number, lon: number) => {
    setCoords({ lat: round4(lat), lon: round4(lon) })
    // A hand-placed pin has no GPS accuracy to report.
    setAccuracy(null)
    setError(null)
  }

  const submit = (e: Event) => {
    e.preventDefault()
    if (!coords) return
    const draft: LocationDraft = {
      lat: coords.lat,
      lon: coords.lon,
      label: label.trim() ? label.trim() : null,
    }
    if (accuracy !== null) draft.accuracy_m = accuracy
    onSubmit(draft)
  }

  const marker: LocationMarker | null = coords
    ? {
        id: 'pick',
        lat: coords.lat,
        lon: coords.lon,
        accuracy_m: accuracy,
        label: label.trim() || formatCoords(coords),
        glyph: '📍',
      }
    : null

  const showMap = coords !== null || picking

  return (
    <Modal open={open} onClose={onClose} title={t('location.share_title')}>
      <form class="sh-form sh-location-picker" onSubmit={submit}>
        {!showMap && (
          <div class="sh-location-picker-empty">
            <Button
              type="button"
              onClick={useCurrentLocation}
              loading={busy}
              disabled={busy}
            >
              {busy ? t('location.locating') : t('location.use_current')}
            </Button>
            <Button type="button" variant="secondary" onClick={pickOnMap}>
              {t('location.pick_on_map')}
            </Button>
            <p class="sh-muted sh-location-picker-note">
              {t('location.precision_note')}
            </p>
          </div>
        )}
        {error && (
          <div class="sh-location-picker-error" role="alert">
            <span>{t(`location.error.${error}`)}</span>
          </div>
        )}
        {showMap && (
          <>
            <LocationMap
              markers={marker ? [marker] : []}
              height={220}
              onPick={onPick}
              ariaLabel={t('location.share_title')}
            />
            {coords ? (
              <p class="sh-muted sh-location-picker-coords">
                <span class="sh-location-picker-coords__value">
                  {formatCoords(coords)}
                </span>
                {' · '}
                {t('location.move_hint')}
              </p>
            ) : (
              <p class="sh-muted sh-location-picker-coords" role="status">
                {t('location.pick_hint')}
              </p>
            )}
            <button
              type="button"
              class="sh-link sh-location-picker-repin"
              onClick={useCurrentLocation}
              disabled={busy}
            >
              {busy ? t('location.locating')
                : coords ? t('location.repin') : t('location.use_current')}
            </button>
            {coords && (
              <label>
                {t('location.label')}
                <input
                  type="text"
                  maxLength={LABEL_MAX}
                  placeholder={t('location.label_placeholder')}
                  value={label}
                  onInput={(e) =>
                    setLabel((e.target as HTMLInputElement).value)
                  }
                />
              </label>
            )}
          </>
        )}
        <div class="sh-form-actions">
          <Button type="button" variant="secondary" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button type="submit" disabled={!coords}>
            {submitLabel ?? t('location.use_this')}
          </Button>
        </div>
      </form>
    </Modal>
  )
}
