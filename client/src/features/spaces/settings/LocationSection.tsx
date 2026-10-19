/**
 * Settings › Location & zones — whether the space has a map, how precise
 * it is (live GPS rounded to about 11 m, or zone names only), and the
 * space's zones, listed here rather than behind a link inside a fieldset.
 *
 * Sharing your OWN location is personal and stays on the Map section (and
 * a member's "You in this space"); this section is the space's rules.
 * Both switches save as they change. A public space hosted here also
 * gets its local-community area (``LocalCommunity``): who may join, by
 * where they live.
 */
import { useEffect, useState } from 'preact/hooks'
import { api } from '@/api'
import { addBase } from '@/baseUrl'
import { RadioCardGroup } from '@/components/RadioCardGroup'
import { t } from '@/i18n/i18n'
import type { Space } from '@/types'
import { LocalCommunity } from './LocalCommunity'
import { patchSpace, useServerValue } from './spacePatch'
import { SwitchRow } from './SettingsRows'

type Mode = 'gps' | 'zone_only'

interface ZoneRow { id: string; name: string; icon: string | null }

export function LocationSection({ space, isRemoteSpace = false, onUpdate }: {
  space: Space
  /** Hosted by another household: the area is the host's. */
  isRemoteSpace?: boolean
  onUpdate: () => void
}) {
  const [on, setOn] = useServerValue(Boolean(space.features?.location))
  const [mode, setMode] = useServerValue<Mode>(space.features?.location_mode ?? 'gps')
  const [zones, setZones] = useState<ZoneRow[] | null>(null)

  useEffect(() => {
    if (!on) return
    let stop = false
    api.get<{ zones: ZoneRow[] }>(`/api/spaces/${space.id}/zones`)
      .then((b) => { if (!stop) setZones(Array.isArray(b?.zones) ? b.zones : []) })
      .catch(() => { if (!stop) setZones(null) })
    return () => { stop = true }
  }, [space.id, on])

  const flip = async (next: boolean) => {
    setOn(next)
    if (await patchSpace(space.id, { features: { location: next } }, { onUpdate }) === 'failed') {
      setOn(!next)
    }
  }

  const pick = async (next: Mode) => {
    if (next === mode) return
    const before = mode
    setMode(next)
    const outcome = await patchSpace(space.id, { features: { location_mode: next } }, {
      onUpdate,
      okToast: next === 'zone_only'
        ? t('space.location.zone_on_toast') : t('space.location.gps_on_toast'),
    })
    if (outcome === 'failed') setMode(before)
  }

  const zonesHref = addBase(`/spaces/${space.id}/zones`)

  return (
    <>
      {/* Only a public space hosted here can be a local community. */}
      {!isRemoteSpace && space.space_type === 'public' && (
        <div class="sh-ss-card">
          <LocalCommunity space={space} onUpdate={onUpdate} />
        </div>
      )}
      <div class="sh-ss-card">
        <SwitchRow id={`location-${space.id}`} label={t('space.location.toggle')}
                   hint={t('space.settings_page.location.precision')}
                   checked={on} onChange={(v) => void flip(v)} />
        {on && (
          <>
            <RadioCardGroup
              legend={t('space.location.mode_legend')}
              name={`location-mode-${space.id}`}
              value={mode}
              onChange={(v) => void pick(v as Mode)}
              options={[
                { value: 'gps', icon: '🛰️', title: t('space.location.gps'), subtitle: t('space.location.gps_hint') },
                {
                  value: 'zone_only', icon: '🔒',
                  title: `${t('space.location.zone')} · ${t('space.location.zone_badge')}`,
                  subtitle: t('space.location.zone_hint'),
                },
              ]}
            />
            <section class="sh-ss-zones" aria-labelledby={`zones-h-${space.id}`}
                     data-testid="settings-zones">
              <div class="sh-ss-zones__head">
                <h3 class="sh-ss-subheading" id={`zones-h-${space.id}`}>
                  {t('space.settings_page.location.zones')}
                </h3>
                <a class="sh-btn sh-btn--secondary" href={zonesHref}>
                  {t('space.location.manage_zones')}
                </a>
              </div>
              {zones !== null && zones.length === 0 && (
                <p class="sh-ss-hint">
                  {mode === 'zone_only'
                    ? t('space.settings_page.location.no_zones_needed')
                    : t('space.settings_page.location.no_zones')}
                </p>
              )}
              {zones !== null && zones.length > 0 && (
                <ul class="sh-ss-zones__list">
                  {zones.map(z => (
                    <li key={z.id}>
                      <span aria-hidden="true">{z.icon || '📍'}</span> {z.name}
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </>
        )}
        <p class="sh-ss-hint">{t('space.location.ha_zones_note')}</p>
        <p class="sh-ss-hint">
          {t('space.settings_page.location.own_sharing')}
          {on && (
            <>{' '}<a href={addBase(`/spaces/${space.id}/map`)}>{t('space.settings_page.location.open_map')}</a></>
          )}
        </p>
      </div>
    </>
  )
}
