/**
 * LocalAreaPicker — choose a local community's area: a centre and a
 * radius. Households join the space only while their home lies inside.
 *
 * The centre is tapped on the map, or "Use our home" takes the
 * HOUSEHOLD's home location (never this device's position — the space is
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

export function LocalAreaPicker({
  open, initial, home, onClose, onSubmit,
}: {
  open: boolean
  /** The current area (editing), or ``null`` for a new one. */
  initial: LocalArea | null
  /** This household's home: the "Use our home" shortcut and a 🏠 pin. */
  home: Home | null | undefined
  onClose: () => void
  onSubmit: (area: LocalArea) => void
}) {
  const [centre, setCentre] = useState<Home | null>(null)
  const [radius, setRadius] = useState<number>(DEFAULT_RADIUS_KM)

  // Every open starts from the stored area (or nothing).
  useEffect(() => {
    if (!open) return
    setCentre(initial ? { lat: initial.lat, lon: initial.lon } : null)
    setRadius(initial && RADIUS_PRESETS_KM.includes(initial.radius_km)
      ? initial.radius_km
      : initial?.radius_km ?? DEFAULT_RADIUS_KM)
  }, [open]) // eslint-disable-line react-hooks/exhaustive-deps -- read on open only

  const area: LocalArea | null = centre ? { ...centre, radius_km: radius } : null
  const radiusOptions = (RADIUS_PRESETS_KM.includes(radius)
    ? RADIUS_PRESETS_KM : [...RADIUS_PRESETS_KM, radius].sort((a, b) => a - b))
    .map((km) => ({ value: String(km), label: radiusLabel(km) }))

  const submit = (e: Event) => {
    e.preventDefault()
    if (area) onSubmit(area)
  }

  return (
    <Modal open={open} onClose={onClose} title={t('local_area.picker.title')}>
      <form class="sh-form sh-local-area-picker" onSubmit={submit}>
        <p class="sh-muted">{t('local_area.picker.intro')}</p>
        <LocalAreaMap
          area={area}
          home={home}
          height={240}
          onPick={(lat, lon) => setCentre({ lat: round4(lat), lon: round4(lon) })}
          ariaLabel={t('local_area.picker.map_label')}
        />
        <p class="sh-muted sh-local-area-picker__where" role="status">
          {centre
            ? t('local_area.picker.centre', { place: coordLabel(centre.lat, centre.lon) })
            : t('local_area.picker.tap_hint')}
        </p>
        {home && (
          <button type="button" class="sh-link sh-local-area-picker__home"
                  onClick={() => setCentre({ lat: round4(home.lat), lon: round4(home.lon) })}>
            🏠 {t('local_area.picker.use_home')}
          </button>
        )}
        <div class="sh-local-area-picker__radius">
          <span id="local-area-radius-label" class="sh-local-area-picker__radius-label">
            {t('local_area.picker.radius')}
          </span>
          <ChipRadioGroup
            options={radiusOptions}
            value={String(radius)}
            onChange={(v) => setRadius(Number(v))}
            labelledBy="local-area-radius-label"
          />
        </div>
        <p class="sh-ss-hint">{t('local_area.picker.privacy')}</p>
        <div class="sh-form-actions">
          <Button type="button" variant="secondary" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button type="submit" disabled={!area}>
            {t('local_area.picker.submit')}
          </Button>
        </div>
      </form>
    </Modal>
  )
}
