/**
 * LocationPicker — composer modal for the location post type.
 *
 * Mirrors PollBuilder / ScheduleBuilder shape so the Composer's
 * "click submit on first press → open builder" pattern just works.
 *
 * Flow:
 *   1. User opens the picker (Composer.handleSubmit detects `type=location`
 *      with no draft yet).
 *   2. Picker shows "📍 Use my current location" — the first click
 *      runs `navigator.geolocation.getCurrentPosition` and pins the
 *      result on a small LocationMap preview.
 *   3. Optional label input (cap 80 chars, hint shows the 4dp coords).
 *   4. "Use this location" returns a `LocationDraft` to the Composer,
 *      which then submits the post on the user's second click.
 */
import { useState } from 'preact/hooks'
import { Button } from './Button'
import { LocationMap, type LocationMarker } from './LocationMap'
import { showToast } from './Toast'

export interface LocationDraft {
  lat: number
  lon: number
  label: string | null
}

interface LocationPickerProps {
  open: boolean
  onSubmit: (draft: LocationDraft) => void
  onClose: () => void
}

const LABEL_MAX = 80

export function LocationPicker({ open, onSubmit, onClose }: LocationPickerProps) {
  const [coords, setCoords] = useState<{ lat: number; lon: number } | null>(null)
  const [label, setLabel] = useState('')
  const [busy, setBusy] = useState(false)

  if (!open) return null

  const useCurrentLocation = () => {
    if (!('geolocation' in navigator)) {
      showToast(
        "This browser doesn't support geolocation. Try a different browser or paste coords manually.",
        'error',
      )
      return
    }
    setBusy(true)
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        setBusy(false)
        // Round at the client too so the preview matches what the
        // server will store (4dp). The server is the authoritative
        // truncator — this is just a visual nicety.
        setCoords({
          lat: Math.round(pos.coords.latitude  * 1e4) / 1e4,
          lon: Math.round(pos.coords.longitude * 1e4) / 1e4,
        })
      },
      (err) => {
        setBusy(false)
        const msg =
          err.code === err.PERMISSION_DENIED
            ? 'Location permission denied. Allow it in the address bar.'
            : err.code === err.POSITION_UNAVAILABLE
              ? "Couldn't pinpoint your position right now."
              : 'Location request timed out — try again.'
        showToast(msg, 'error')
      },
      { enableHighAccuracy: true, timeout: 10000 },
    )
  }

  const submit = (e: Event) => {
    e.preventDefault()
    if (!coords) return
    onSubmit({
      lat: coords.lat,
      lon: coords.lon,
      label: label.trim() ? label.trim() : null,
    })
  }

  const marker: LocationMarker | null = coords
    ? {
        id: 'pick',
        lat: coords.lat,
        lon: coords.lon,
        label: label || `${coords.lat.toFixed(4)}, ${coords.lon.toFixed(4)}`,
      }
    : null

  return (
    <div class="sh-modal-overlay" role="presentation" onClick={onClose}>
      <div
        class="sh-modal"
        role="dialog"
        aria-modal="true"
        aria-label="Share location"
        onClick={(e) => e.stopPropagation()}
      >
        <div class="sh-modal-header">
          <h3 style={{ margin: 0 }}>Share a location</h3>
          <button
            type="button"
            class="sh-modal-close"
            aria-label="Close dialog"
            onClick={onClose}
          >×</button>
        </div>
        <form class="sh-modal-body sh-form sh-location-picker" onSubmit={submit}>
          {!coords && (
            <div class="sh-location-picker-empty">
              <Button
                type="button"
                onClick={useCurrentLocation}
                loading={busy}
                disabled={busy}
              >
                📍 Use my current location
              </Button>
              <p class="sh-muted" style={{ margin: 0, fontSize: 'var(--sh-font-size-sm)' }}>
                We'll round the coordinates to ~11 m precision before
                anyone sees them.
              </p>
            </div>
          )}
          {marker && (
            <>
              <LocationMap markers={[marker]} height={220} />
              <p class="sh-muted" style={{ margin: 0, fontSize: 'var(--sh-font-size-xs)' }}>
                {coords!.lat.toFixed(4)}, {coords!.lon.toFixed(4)} —{' '}
                <button
                  type="button"
                  class="sh-link"
                  onClick={useCurrentLocation}
                >
                  re-pin
                </button>
              </p>
              <label>
                Label (optional)
                <input
                  type="text"
                  maxLength={LABEL_MAX}
                  placeholder="e.g. Marina, Beach Park…"
                  value={label}
                  onInput={(e) =>
                    setLabel((e.target as HTMLInputElement).value)
                  }
                />
              </label>
            </>
          )}
          <div class="sh-form-actions">
            <Button type="button" variant="secondary" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={!coords}>
              Use this location
            </Button>
          </div>
        </form>
      </div>
    </div>
  )
}
