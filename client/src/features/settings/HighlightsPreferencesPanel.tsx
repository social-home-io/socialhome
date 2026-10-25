/**
 * Settings › Privacy › Highlights — how long your highlights stay and who
 * sees a new one by default.
 *
 * Instant save, like a space's settings: every pick is written at once
 * (``preferences_json.highlights``, no migration) and a status line says
 * whether it took. "Who sees a new highlight" saves the kind AND the
 * chosen household / people ids, so a new day's first frame — in the full
 * composer and in quick share — starts from exactly them. The composers
 * still send the audience they show with every frame (#1159).
 */
import { useEffect } from 'preact/hooks'
import { signal, useSignal } from '@preact/signals'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { currentUser } from '@/store/auth'
import { t } from '@/i18n/i18n'
import type { HighlightAudienceKind } from '@/types'
import { getPreferences, setPreference } from '@/utils/preferences'
import {
  audienceFromFriends,
  defaultAudience,
  type ConnectedPerson,
  type FriendsPayload,
  type RemoteHousehold,
} from '@/features/highlights/todayAudience'

interface HighlightsPrefs {
  retention_days?: number
  max_count?: number
}

const RETENTION_MIN = 1
const RETENTION_MAX = 90
const COUNT_MIN = 10
const COUNT_MAX = 500

const clamp = (n: number, lo: number, hi: number, fallback: number): number =>
  Number.isFinite(n) ? Math.min(hi, Math.max(lo, Math.round(n))) : fallback

/** The picker lists, shared with nothing else: read once per mount. */
const households = signal<RemoteHousehold[]>([])
const people = signal<ConnectedPerson[]>([])
const listsFailed = signal(false)

async function loadLists(): Promise<void> {
  try {
    const payload = await api.get('/api/friends') as FriendsPayload
    const out = audienceFromFriends(payload, currentUser.value?.user_id)
    households.value = out.households
    people.value = out.people
    listsFailed.value = false
  } catch {
    listsFailed.value = true
  }
}

export function HighlightsPreferencesPanel() {
  const prefs = (getPreferences().highlights ?? {}) as HighlightsPrefs
  const start = defaultAudience()
  const retentionDays = useSignal<number>(prefs.retention_days ?? 30)
  const maxCount = useSignal<number>(prefs.max_count ?? 100)
  const kind = useSignal<HighlightAudienceKind>(start.kind)
  const ids = useSignal<string[]>(start.ids)
  const status = useSignal<'idle' | 'saved' | 'failed'>('idle')

  useEffect(() => { void loadLists() }, [])

  const save = async () => {
    try {
      await setPreference('highlights', {
        retention_days: retentionDays.value,
        max_count: maxCount.value,
        default_audience: { kind: kind.value, ids: kind.value === 'all_paired' ? [] : ids.value },
      })
      status.value = 'saved'
    } catch {
      status.value = 'failed'
    }
  }

  const pickKind = (k: HighlightAudienceKind) => {
    if (k === kind.value) return
    kind.value = k
    ids.value = []
    void save()
  }
  const toggleId = (id: string, on: boolean) => {
    ids.value = on ? [...ids.value, id] : ids.value.filter(x => x !== id)
    void save()
  }

  const choices = kind.value === 'households'
    ? households.value.map(h => ({ id: h.instance_id, label: h.display_name }))
    : people.value.map(p => ({
      id: p.user_id,
      label: p.household_name ? `${p.display_name} · ${p.household_name}` : p.display_name,
    }))

  const radio = (k: HighlightAudienceKind, labelKey: string) => (
    <label class="sh-highlight-composer-audience-row">
      <input type="radio" name="sh-hl-default-audience" value={k}
        checked={kind.value === k} onChange={() => pickKind(k)} />
      {t(labelKey)}
    </label>
  )

  return (
    <div id="highlights" class="sh-settings-highlights-panel sh-settings-subcard">
      <h3>{t('nav.highlights')}</h3>
      <p class="sh-muted">{t('settings.highlights.blurb')} {t('settings.highlights.instant')}</p>

      <fieldset class="sh-highlight-composer-audience">
        <legend>{t('settings.highlights.audience')}</legend>
        {radio('all_paired', 'settings.highlights.audience_all')}
        {radio('households', 'settings.highlights.audience_households')}
        {radio('users', 'settings.highlights.audience_users')}
        {kind.value !== 'all_paired' && (
          <div class="sh-highlight-composer-audience-list">
            {listsFailed.value ? (
              <div role="alert">
                <p class="sh-muted">{t('settings.highlights.lists_failed')}</p>
                <Button type="button" variant="secondary" onClick={() => void loadLists()}>
                  {t('common.retry')}
                </Button>
              </div>
            ) : choices.length === 0 ? (
              <p class="sh-muted">
                {t(kind.value === 'households' ? 'highlight.composer.no_households' : 'highlight.composer.no_people')}
              </p>
            ) : choices.map(c => (
              <label key={c.id} class="sh-highlight-composer-audience-row">
                <input type="checkbox" checked={ids.value.includes(c.id)}
                  onChange={e => toggleId(c.id, (e.target as HTMLInputElement).checked)} />
                {c.label}
              </label>
            ))}
            {!listsFailed.value && choices.length > 0 && ids.value.length === 0 && (
              <p class="sh-muted">{t('settings.highlights.pick_none')}</p>
            )}
          </div>
        )}
      </fieldset>

      <label class="sh-form-row">
        {t('settings.highlights.retention')}
        <input
          type="number"
          min={RETENTION_MIN}
          max={RETENTION_MAX}
          value={retentionDays.value}
          onChange={e => {
            retentionDays.value = clamp(Number((e.target as HTMLInputElement).value),
              RETENTION_MIN, RETENTION_MAX, 30)
            void save()
          }}
        />
      </label>
      <label class="sh-form-row">
        {t('settings.highlights.max_count')}
        <input
          type="number"
          min={COUNT_MIN}
          max={COUNT_MAX}
          value={maxCount.value}
          onChange={e => {
            maxCount.value = clamp(Number((e.target as HTMLInputElement).value),
              COUNT_MIN, COUNT_MAX, 100)
            void save()
          }}
        />
      </label>
      <p class={status.value === 'failed' ? 'sh-form-error' : 'sh-muted'} role="status">
        {status.value === 'saved' && t('settings.highlights.saved_inline')}
        {status.value === 'failed' && t('settings.highlights.save_failed_inline')}
      </p>
    </div>
  )
}
