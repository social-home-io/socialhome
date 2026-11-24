/**
 * Settings › Privacy › Momentum — how long people who follow you keep your
 * moments, how far away a moment may come from to show up for you, and how
 * far your own moments go (v_96 reach).
 *
 * Instant save, like the Highlights panel beside it: every pick writes the
 * whole ``preferences_json.moments`` block at once (the server merges
 * top-level keys only, so ``max_hops``, ``follower_retention_days`` and
 * ``reach`` always travel together) and a status line says whether it
 * took. The retention sets the signed ``expires_at`` of every moment posted
 * from then on (decision 6d, v_65); the author's own archive keeps their
 * moments regardless (#1174).
 *
 * "How far my moments go" offers 1–5 hops; picks above the household
 * maximum (``reach_max`` from ``GET /api/moments/limits``) are disabled
 * with a line saying so. When the limits can't be read all five stay on
 * offer — the server clamps. Admins also get the household maximum itself
 * (``PUT /api/admin/moments/reach-max``, instant save, put back on a
 * failure).
 *
 * A protected account sees moments from its household and directly paired
 * households only (#1185), and its own go to the same households, whatever
 * ``max_hops`` / ``reach`` say — so it gets that fixed circle in words
 * instead of pickers it doesn't have.
 */
import { useEffect } from 'preact/hooks'
import { useSignal } from '@preact/signals'
import { api } from '@/api'
import { t } from '@/i18n/i18n'
import { currentUser } from '@/store/auth'
import {
  loadMomentLimits,
  MOMENT_MAX_REACH,
  momentReachMax,
  myMomentReach,
  reachKey,
} from '@/store/momentLimits'
import {
  getMomentsPreferences,
  MOMENT_RETENTION_CHOICES,
  MOMENT_RETENTION_DEFAULT,
  momentRetentionLabel,
  setPreference,
  type MomentsHops,
} from '@/utils/preferences'

const HOPS: readonly MomentsHops[] = [1, 2, 3, 4, 5]

export function MomentumPreferencesPanel() {
  const start = getMomentsPreferences()
  const retention = useSignal<number>(start.follower_retention_days)
  const maxHops = useSignal<MomentsHops>(start.max_hops)
  const reach = useSignal<MomentsHops>(start.reach)
  const status = useSignal<'idle' | 'saved' | 'failed' | 'max_failed'>('idle')
  const savingMax = useSignal(false)
  const isProtected = currentUser.value?.protected === true
  const isAdmin = currentUser.value?.is_admin === true

  useEffect(() => { void loadMomentLimits() }, [])

  const save = async () => {
    try {
      await setPreference('moments', {
        max_hops: maxHops.value,
        follower_retention_days: retention.value,
        reach: reach.value,
      })
      status.value = 'saved'
      if (!isProtected) {
        myMomentReach.value = Math.min(reach.value, momentReachMax.value ?? MOMENT_MAX_REACH)
      }
    } catch {
      status.value = 'failed'
    }
  }

  const saveMax = async (next: number) => {
    const prev = momentReachMax.value
    momentReachMax.value = next
    savingMax.value = true
    try {
      await api.put('/api/admin/moments/reach-max', { reach_max: next })
      status.value = 'saved'
      if (!isProtected) myMomentReach.value = Math.min(reach.value, next)
    } catch {
      momentReachMax.value = prev
      status.value = 'max_failed'
    } finally {
      savingMax.value = false
    }
  }

  // A value set elsewhere (another device, the API) that isn't a preset
  // still shows as itself rather than snapping to a preset on render.
  const choices = MOMENT_RETENTION_CHOICES.includes(retention.value)
    ? MOMENT_RETENTION_CHOICES
    : [...MOMENT_RETENTION_CHOICES, retention.value].sort((a, b) => a - b)

  const householdMax = momentReachMax.value
  const hopLabel = (n: number) => t(`settings.momentum.hop_${n}`)

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

      {isProtected ? (
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
              {HOPS.map(n => <option key={n} value={String(n)}>{hopLabel(n)}</option>)}
            </select>
          </label>

          <label class="sh-form-row">
            {t('settings.momentum.reach_label')}
            <select
              id="sh-momentum-reach"
              value={String(reach.value)}
              aria-describedby="sh-momentum-reach-hint"
              onChange={e => {
                reach.value = Number((e.target as HTMLSelectElement).value) as MomentsHops
                void save()
              }}
            >
              {HOPS.map(n => (
                <option key={n} value={String(n)}
                  disabled={householdMax !== null && n > householdMax}>
                  {hopLabel(n)}
                </option>
              ))}
            </select>
          </label>
          <div id="sh-momentum-reach-hint" class="sh-muted">
            {householdMax !== null && householdMax < MOMENT_MAX_REACH && (
              <p class="sh-settings-momentum-household-max">
                {t(reachKey('settings.momentum.reach_household_max', householdMax),
                  { n: String(householdMax) })}
              </p>
            )}
            <p>{t('settings.momentum.reach_hint')}</p>
          </div>
        </>
      )}

      {isAdmin && householdMax !== null && (
        <>
          <label class="sh-form-row">
            {t('settings.momentum.reach_max_label')}
            <select
              id="sh-momentum-reach-max"
              value={String(householdMax)}
              aria-describedby="sh-momentum-reach-max-hint"
              disabled={savingMax.value}
              onChange={e => void saveMax(Number((e.target as HTMLSelectElement).value))}
            >
              {HOPS.map(n => (
                <option key={n} value={String(n)}>
                  {n === MOMENT_MAX_REACH
                    ? t('settings.momentum.reach_max_none')
                    : n === 1 ? hopLabel(1) : t('settings.momentum.steps', { n: String(n) })}
                </option>
              ))}
            </select>
          </label>
          <p id="sh-momentum-reach-max-hint" class="sh-muted">
            {t('settings.momentum.reach_max_hint')}
          </p>
        </>
      )}

      <p class={status.value === 'failed' || status.value === 'max_failed' ? 'sh-form-error' : 'sh-muted'}
        role="status">
        {status.value === 'saved' && t('settings.momentum.saved_inline')}
        {status.value === 'failed' && t('settings.momentum.save_failed_inline')}
        {status.value === 'max_failed' && t('settings.momentum.reach_max_failed')}
      </p>
    </div>
  )
}
