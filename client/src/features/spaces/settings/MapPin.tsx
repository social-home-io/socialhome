/**
 * Settings › Location & zones › Map pin — where a public space sits on
 * the map (set at create with "Show on the map"). Move it (your current
 * location or a spot picked on the map) or remove it; it saves at once,
 * like every control outside Basics.
 *
 * Only for a public space hosted by this household: the pin is host-local
 * (``PATCH /api/spaces/{id}`` ``{lat, lon}``, 4 dp) and never sent to
 * another household or a GFS.
 */
import { useState } from 'preact/hooks'
import { Button } from '@/components/Button'
import { LocationPicker } from '@/components/LocationPicker'
import { t } from '@/i18n/i18n'
import { coordLabel } from '@/utils/coordLabel'
import { patchSpace, useServerValue } from './spacePatch'

export interface MapPinValue { lat: number; lon: number }

export function MapPin({ spaceId, pin: stored, onUpdate }: {
  spaceId: string
  pin: MapPinValue | null
  onUpdate: () => void
}) {
  const [pin, setPin] = useServerValue<MapPinValue | null>(stored)
  const [picking, setPicking] = useState(false)
  const [busy, setBusy] = useState(false)

  const save = async (next: MapPinValue | null) => {
    const before = pin
    setPin(next)
    setBusy(true)
    const outcome = await patchSpace(
      spaceId,
      next ? { lat: next.lat, lon: next.lon } : { lat: null, lon: null },
      {
        onUpdate,
        okToast: next ? t('space.settings_page.location.pin_saved') : t('space.settings_page.location.pin_removed'),
      },
    )
    setBusy(false)
    if (outcome === 'failed') setPin(before)
  }

  return (
    <section class="sh-ss-pin" aria-labelledby={`pin-h-${spaceId}`} data-testid="settings-map-pin">
      <h3 class="sh-ss-subheading" id={`pin-h-${spaceId}`}>
        {t('space.settings_page.location.pin_title')}
      </h3>
      <p class="sh-ss-pin__where" role="status">
        {pin
          ? t('space.create.pinned_at', { place: coordLabel(pin.lat, pin.lon) })
          : t('space.settings_page.location.pin_none')}
      </p>
      <div class="sh-ss-pin__actions">
        <Button variant="secondary" onClick={() => setPicking(true)} disabled={busy}>
          {pin ? t('space.settings_page.location.pin_move') : t('space.settings_page.location.pin_add')}
        </Button>
        {pin && (
          <Button variant="ghost" onClick={() => void save(null)} disabled={busy}>
            {t('space.settings_page.location.pin_remove')}
          </Button>
        )}
      </div>
      <p class="sh-ss-hint">{t('space.settings_page.location.pin_hint')}</p>
      <LocationPicker
        open={picking}
        initial={pin}
        noLabel
        title={t('space.settings_page.location.pin_picker_title')}
        submitLabel={t('space.settings_page.location.pin_use')}
        onClose={() => setPicking(false)}
        onSubmit={(d) => { setPicking(false); void save({ lat: d.lat, lon: d.lon }) }}
      />
    </section>
  )
}
