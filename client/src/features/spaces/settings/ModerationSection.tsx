/**
 * Settings › Moderation & age — deleting old posts (retention, with the
 * kinds kept however old) and, on the household hosting the space, the
 * minimum age + discovery category (``SpaceAgeGating``). Everything saves
 * as it changes: the day count when you leave the field or press Enter.
 *
 * Retention: empty / 0 = keep forever, sent as the 0 sentinel the backend
 * normalises to ``null``. The keep-list shows only while retention is on;
 * types the UI doesn't offer are preserved.
 */
import { SpaceAgeGating } from '@/features/child-protection/SpaceAgeGating'
import { addBase } from '@/baseUrl'
import { t } from '@/i18n/i18n'
import type { Space } from '@/types'
import { patchSpace, useServerValue } from './spacePatch'
import { LinkRow } from './SettingsRows'

// Post types an admin may keep past the retention horizon. Deliberately
// absent: text / transcript (that is what retention is for) and
// highlight_share (the highlight expires on its own schedule).
const KEEPABLE: [string, string][] = [
  ['image', 'space.retention_keep_image'],
  ['video', 'space.retention_keep_video'],
  ['file', 'space.retention_keep_file'],
  ['poll', 'space.retention_keep_poll'],
  ['schedule', 'space.retention_keep_schedule'],
  ['event', 'space.retention_keep_event'],
  ['bazaar', 'space.retention_keep_bazaar'],
  ['location', 'space.retention_keep_location'],
]

/** The day count to send for the field's text: ``null`` = forever,
 *  ``undefined`` = not a number (left out). */
export function parseRetention(text: string): number | null | undefined {
  if (text.trim() === '') return null
  const n = parseInt(text, 10)
  if (!Number.isFinite(n)) return undefined
  return n > 0 ? n : null
}

export function ModerationSection({ space, isRemoteSpace, onUpdate }: {
  space: Space
  isRemoteSpace: boolean
  onUpdate: () => void
}) {
  const storedDays = space.retention_days ?? null
  const [days, setDays] = useServerValue(storedDays != null ? String(storedDays) : '')
  const [keep, setKeep] = useServerValue<string[]>([...(space.retention_exempt_types ?? [])].sort())
  const on = (parseRetention(days) ?? 0) > 0

  const saveDays = async () => {
    const value = parseRetention(days)
    if (value === undefined || value === storedDays) return
    const outcome = await patchSpace(space.id, { retention_days: value ?? 0 }, { onUpdate })
    if (outcome === 'failed') setDays(storedDays != null ? String(storedDays) : '')
  }

  const toggleKeep = async (type: string, checked: boolean) => {
    const before = keep
    const next = [...new Set(checked ? [...keep, type] : keep.filter(x => x !== type))].sort()
    setKeep(next)
    const outcome = await patchSpace(space.id, { retention_exempt_types: next }, { onUpdate })
    if (outcome === 'failed') setKeep(before)
  }

  return (
    <div class="sh-ss-card">
      <div class="sh-ss-links">
        <LinkRow href={addBase(`/spaces/${space.id}/moderation`)}
                 title={t('space.tab.moderation')}
                 meta={t('space.settings_page.moderation.queue_meta')} />
      </div>
      <fieldset class="sh-form-fieldset">
        <legend>{t('space.retention.legend')}</legend>
        <label class="sh-ss-retention">
          {t('space.retention.label')}
          <span class="sh-ss-retention__row">
            <input type="number" min={0} max={3650} inputMode="numeric"
                   value={days} placeholder={t('space.retention.placeholder')}
                   aria-describedby={`retention-hint-${space.id}`}
                   onInput={(e) => setDays((e.target as HTMLInputElement).value)}
                   onChange={() => void saveDays()} />
            <span class="sh-muted">{t('space.settings_page.moderation.days')}</span>
          </span>
        </label>
        <p class="sh-ss-hint" id={`retention-hint-${space.id}`}>
          {t('space.retention.days_hint')} {t('space.retention.applies')}
        </p>
        {on && (
          <fieldset class="sh-form-fieldset sh-retention-keep" data-testid="retention-exempt-types">
            <legend>{t('space.retention_keep_legend')}</legend>
            <p class="sh-ss-hint">{t('space.retention_keep_hint')}</p>
            <div class="sh-ss-checks">
              {KEEPABLE.map(([key, label]) => (
                <label class="sh-toggle-row" key={key}>
                  <input type="checkbox" value={key} checked={keep.includes(key)}
                         onChange={(e) => void toggleKeep(key, (e.target as HTMLInputElement).checked)} />
                  {t(label)}
                </label>
              ))}
            </div>
          </fieldset>
        )}
      </fieldset>
      {/* The age gate is enforced by the host on join — host-local. */}
      {!isRemoteSpace && (
        <fieldset class="sh-form-fieldset">
          <legend>{t('space.settings_page.tab_age')}</legend>
          <SpaceAgeGating spaceId={space.id} />
        </fieldset>
      )}
    </div>
  )
}
