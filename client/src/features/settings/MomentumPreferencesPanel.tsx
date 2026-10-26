/**
 * Settings › Privacy › Momentum — how long people who follow you keep your
 * moments, and how far away a moment may come from to show up for you.
 *
 * Instant save, like the Highlights panel beside it: every pick writes the
 * whole ``preferences_json.moments`` block at once (the server merges
 * top-level keys only, so both fields always travel together) and a status
 * line says whether it took. The retention sets the signed ``expires_at``
 * of every moment posted from then on (decision 6d, v_65); the author's own
 * archive keeps their moments regardless (#1174).
 *
 * A protected account sees moments from its household and directly paired
 * households only (#1185), whatever ``max_hops`` says — so it gets that
 * fixed circle in words instead of a 3-hop picker it doesn't have.
 */
import { useSignal } from '@preact/signals'
import { t } from '@/i18n/i18n'
import { currentUser } from '@/store/auth'
import {
  getMomentsPreferences,
  MOMENT_RETENTION_CHOICES,
  MOMENT_RETENTION_DEFAULT,
  momentRetentionLabel,
  setPreference,
  type MomentsHops,
} from '@/utils/preferences'

export function MomentumPreferencesPanel() {
  const start = getMomentsPreferences()
  const retention = useSignal<number>(start.follower_retention_days)
  const maxHops = useSignal<MomentsHops>(start.max_hops)
  const status = useSignal<'idle' | 'saved' | 'failed'>('idle')

  const save = async () => {
    try {
      await setPreference('moments', {
        max_hops: maxHops.value,
        follower_retention_days: retention.value,
      })
      status.value = 'saved'
    } catch {
      status.value = 'failed'
    }
  }

  // A value set elsewhere (another device, the API) that isn't a preset
  // still shows as itself rather than snapping to a preset on render.
  const choices = MOMENT_RETENTION_CHOICES.includes(retention.value)
    ? MOMENT_RETENTION_CHOICES
    : [...MOMENT_RETENTION_CHOICES, retention.value].sort((a, b) => a - b)

  return (
    <div id="momentum" class="sh-settings-momentum-panel sh-settings-subcard">
      <h3>{t('nav.momentum')}</h3>
      <p class="sh-muted">{t('settings.momentum.instant')}</p>

      <label class="sh-form-row">
        {t('settings.momentum.retention')}
        <select
          value={String(retention.value)}
          aria-describedby="sh-momentum-retention-hint"
          onChange={e => {
            retention.value = Number((e.target as HTMLSelectElement).value)
            void save()
          }}
        >
          {choices.map(d => (
            <option key={d} value={String(d)}>
              {d === MOMENT_RETENTION_DEFAULT
                ? t('settings.momentum.days_default', { n: String(d) })
                : momentRetentionLabel(d)}
            </option>
          ))}
        </select>
      </label>
      <p id="sh-momentum-retention-hint" class="sh-muted">
        {t('settings.momentum.retention_hint')}
      </p>

      {currentUser.value?.protected ? (
        <p class="sh-muted sh-settings-momentum-circle">{t('settings.momentum.circle_protected')}</p>
      ) : (
        <>
          <p class="sh-muted">{t('settings.momentum.blurb')}</p>
          <label class="sh-form-row">
            {t('settings.momentum.show_up_to')}
            <select
              value={String(maxHops.value)}
              onChange={e => {
                maxHops.value = Number((e.target as HTMLSelectElement).value) as MomentsHops
                void save()
              }}
            >
              <option value="1">{t('settings.momentum.hop_1')}</option>
              <option value="2">{t('settings.momentum.hop_2')}</option>
              <option value="3">{t('settings.momentum.hop_3')}</option>
            </select>
          </label>
        </>
      )}
      <p class={status.value === 'failed' ? 'sh-form-error' : 'sh-muted'} role="status">
        {status.value === 'saved' && t('settings.momentum.saved_inline')}
        {status.value === 'failed' && t('settings.momentum.save_failed_inline')}
      </p>
    </div>
  )
}
