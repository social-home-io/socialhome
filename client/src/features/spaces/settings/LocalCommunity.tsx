/**
 * Settings › Location & zones › Local community — a public space hosted
 * here may be bound to an area (centre + radius): households join only
 * while the home they share with this household lies inside. Set, move or
 * remove the area; it saves at once, like every control outside Basics.
 *
 * Existing members stay when the area changes — the check runs when
 * someone joins. Paired households see the centre and radius before
 * joining (``PATCH /api/spaces/{id}`` ``{lat, lon, radius_km}``, 4 dp).
 */
import { useEffect, useState } from 'preact/hooks'
import { Button } from '@/components/Button'
import { LocalAreaMap } from '@/components/LocalAreaMap'
import { LocalAreaPicker } from '@/components/LocalAreaPicker'
import { t } from '@/i18n/i18n'
import { ensureHouseholdHome, householdHome } from '@/store/householdHome'
import type { LocalArea, Space } from '@/types'
import { areaLabel } from '@/utils/localArea'
import { patchSpace, useServerValue } from './spacePatch'

export function LocalCommunity({ space, onUpdate }: {
  space: Space
  onUpdate: () => void
}) {
  const [area, setArea] = useServerValue<LocalArea | null>(space.local_area ?? null)
  const [picking, setPicking] = useState(false)
  const [busy, setBusy] = useState(false)
  useEffect(() => { void ensureHouseholdHome() }, [])
  // A delegated admin's household would seat people without this
  // household's check — the server refuses the area while that is on.
  const delegated = space.features?.delegated_admin_authority === true

  const save = async (next: LocalArea | null) => {
    const before = area
    setArea(next)
    setBusy(true)
    const outcome = await patchSpace(
      space.id,
      next
        ? { lat: next.lat, lon: next.lon, radius_km: next.radius_km }
        : { lat: null, lon: null, radius_km: null },
      {
        onUpdate,
        okToast: next ? t('local_area.settings.saved') : t('local_area.settings.removed'),
      },
    )
    setBusy(false)
    if (outcome === 'failed') setArea(before)
  }

  const headingId = `local-area-h-${space.id}`
  return (
    <section class="sh-ss-local-area" aria-labelledby={headingId} data-testid="settings-local-area">
      <h3 class="sh-ss-subheading" id={headingId}>🏘️ {t('local_area.settings.title')}</h3>
      <p class="sh-ss-local-area__where" role="status">
        {area ? areaLabel(area) : t('local_area.settings.none')}
      </p>
      {area && <LocalAreaMap area={area} home={householdHome.value} height={180} />}
      <div class="sh-ss-local-area__actions">
        {area ? (
          <>
            <Button variant="secondary" onClick={() => setPicking(true)} disabled={busy || delegated}>
              {t('local_area.change')}
            </Button>
            <Button variant="ghost" onClick={() => void save(null)} disabled={busy}>
              {t('local_area.settings.remove')}
            </Button>
          </>
        ) : (
          <Button variant="secondary" onClick={() => setPicking(true)} disabled={busy || delegated}>
            {t('local_area.settings.make')}
          </Button>
        )}
      </div>
      {delegated && <p class="sh-ss-hint" role="note">{t('local_area.settings.delegated')}</p>}
      <p class="sh-ss-hint">
        {area ? t('local_area.settings.members_stay') : t('local_area.settings.intro')}
      </p>
      <p class="sh-ss-hint">{t('local_area.picker.privacy')}</p>
      <LocalAreaPicker
        open={picking}
        initial={area}
        home={householdHome.value}
        onClose={() => setPicking(false)}
        onSubmit={(next) => { setPicking(false); void save(next) }}
      />
    </section>
  )
}
