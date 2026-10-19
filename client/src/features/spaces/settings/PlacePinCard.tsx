/**
 * Settings › Location & zones › Place — a global space hosted here may be
 * pinned to a place (a town, a club house). It is listed with the space on
 * its GFSs, so people browsing Discover › Global see nearby spaces first —
 * the distance is worked out in THEIR browser against their own home.
 * Sorting only: joining never checks where anyone lives.
 *
 * Never the household's home by default: the picker has no "use our home"
 * shortcut, and the card says the place is shown to everyone who can find
 * the space. Saves at once (``PATCH /api/spaces/{id}`` ``{place}``).
 */
import { useRef, useState } from 'preact/hooks'
import { Button } from '@/components/Button'
import { LocalAreaMap } from '@/components/LocalAreaMap'
import { LocalAreaPicker, PIN_DOT_KM } from '@/components/LocalAreaPicker'
import { t } from '@/i18n/i18n'
import type { Space } from '@/types'
import { coordLabel } from '@/utils/coordLabel'
import { patchSpace, useServerValue } from './spacePatch'

type Pin = { lat: number; lon: number }

export function PlacePinCard({ space, onUpdate }: {
  space: Space
  onUpdate: () => void
}) {
  const [pin, setPin] = useServerValue<Pin | null>(space.place ?? null)
  const [picking, setPicking] = useState(false)
  const [busy, setBusy] = useState(false)
  const sectionRef = useRef<HTMLElement | null>(null)

  /** Remove swaps the buttons: put focus back on the card's first action. */
  const keepFocus = () => requestAnimationFrame(() => {
    const active = document.activeElement
    if (active && active !== document.body && active.isConnected) return
    sectionRef.current?.querySelector<HTMLElement>('.sh-ss-local-area__actions button')?.focus()
  })

  const save = async (next: Pin | null) => {
    if (busy) return
    const before = pin
    setPin(next)
    setBusy(true)
    const outcome = await patchSpace(space.id, { place: next }, {
      onUpdate,
      okToast: next ? t('place_pin.settings.saved') : t('place_pin.settings.removed'),
    })
    setBusy(false)
    if (outcome === 'failed') setPin(before)
    keepFocus()
  }

  const headingId = `place-pin-h-${space.id}`
  const open = () => { if (!busy) setPicking(true) }
  return (
    <section ref={sectionRef} class="sh-ss-local-area" aria-labelledby={headingId}
             aria-busy={busy ? 'true' : undefined} data-testid="settings-place-pin">
      <h3 class="sh-ss-subheading" id={headingId}>📍 {t('place_pin.settings.title')}</h3>
      <p class="sh-ss-local-area__where" role="status">
        {pin ? t('place_pin.settings.where', { place: coordLabel(pin.lat, pin.lon) })
          : t('place_pin.settings.none')}
      </p>
      {pin && (
        <LocalAreaMap area={{ ...pin, radius_km: PIN_DOT_KM }} height={180}
                      zoneName={t('place_pin.map_zone')} ariaLabel={t('place_pin.map_label')} />
      )}
      <div class="sh-ss-local-area__actions">
        {pin ? (
          <>
            <Button variant="secondary" onClick={open}>{t('place_pin.settings.change')}</Button>
            <Button variant="ghost" onClick={() => void save(null)}>
              {t('place_pin.settings.remove')}
            </Button>
          </>
        ) : (
          <Button variant="secondary" onClick={open}>{t('place_pin.settings.make')}</Button>
        )}
      </div>
      <p class="sh-ss-hint">{t('place_pin.settings.sort_only')}</p>
      <p class="sh-ss-hint">{t('place_pin.privacy')}</p>
      <LocalAreaPicker
        mode="pin"
        open={picking}
        initial={pin ? { ...pin, radius_km: PIN_DOT_KM } : null}
        home={null}
        onClose={() => setPicking(false)}
        onSubmit={(next) => { setPicking(false); void save({ lat: next.lat, lon: next.lon }) }}
      />
    </section>
  )
}
