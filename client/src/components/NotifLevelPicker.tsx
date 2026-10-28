/**
 * NotifLevelPicker — one level vocabulary for spaces and chats (redesign D9).
 *
 *   ( ) Everything             hint
 *   ( ) Replies and mentions   hint            [Default]
 *   (•) Nothing                hint
 *       For how long?  [Until I turn it back on] [1 hour] [8 hours] [1 week]
 *   🔕 Quiet until 02:15, then back to Replies and mentions        [Undo]
 *
 * Picking Everything or Replies and mentions saves at once. Picking Nothing
 * first asks "For how long?" and saves when a length is picked; while it
 * lasts the state line says until when and what comes back, with Undo.
 *
 * The caller owns the saving (a space: ``PUT /api/spaces/{id}/notif-prefs``
 * with a duration; a chat: its level and its mute) through ``onPick``.
 */
import { useId, useState } from 'preact/hooks'
import { Button } from '@/components/Button'
import { t } from '@/i18n/i18n'
import type { NotifLevel } from '@/store/notifSettings'
import { isMuteActive, isMutedForever, muteTimeLabel, type MuteDuration } from '@/utils/mute'

/** "For how long?" — in the order D9 asks it. */
export const ASK_LENGTHS: readonly MuteDuration[] = ['forever', '1h', '8h', '1w']

/** A level's name, in the UI language. */
export function levelLabel(level: NotifLevel): string {
  switch (level) {
    case 'all': return t('notif.level.all')
    case 'mentions': return t('notif.level.mentions')
    case 'muted': return t('notif.level.muted')
  }
}

/** "Quiet until 02:15, then back to Replies and mentions" /
 *  "Quiet until you turn it back on". */
export function quietLabel(mutedUntil: string | null, then: NotifLevel | null): string {
  if (!mutedUntil || isMutedForever(mutedUntil)) return t('notif.level.quiet_forever')
  const time = muteTimeLabel(mutedUntil)
  return then
    ? t('notif.level.quiet_until_then', { time, level: levelLabel(then) })
    : t('notif.level.quiet_until', { time })
}

export interface NotifLevelPickerProps {
  /** The legend ("Posts and comments", "Chat"…). */
  legend: string
  /** Keep the legend for screen readers only (a heading above says it). */
  legendHidden?: boolean
  /** The levels offered — a 1:1 chat has no "Replies and mentions". */
  levels: readonly NotifLevel[]
  /** The level that applies now. */
  level: NotifLevel
  /** The default here (gets a "Default" tag). */
  defaultLevel?: NotifLevel | null
  /** While muted: until when (``9999-…`` = until turned back on). */
  mutedUntil: string | null
  /** While muted: the level that comes back. */
  thenLevel: NotifLevel | null
  /** One line under each level. */
  hint: (level: NotifLevel) => string
  /** Save a pick — ``duration`` with ``muted``. Resolve ``true`` on success. */
  onPick: (level: NotifLevel, duration?: MuteDuration) => Promise<boolean>
}

export function NotifLevelPicker({
  legend, legendHidden, levels, level, defaultLevel, mutedUntil, thenLevel, hint, onPick,
}: NotifLevelPickerProps) {
  const [saving, setSaving] = useState(false)
  /** Nothing is ticked, its length not picked yet. */
  const [asking, setAsking] = useState(false)
  const name = useId()
  const askId = useId()
  const muted = level === 'muted'
  const quiet = muted && (mutedUntil === null || isMuteActive(mutedUntil))
  const shown: NotifLevel = asking ? 'muted' : level

  const save = async (next: NotifLevel, duration?: MuteDuration) => {
    if (saving) return
    setSaving(true)
    try {
      if (await onPick(next, duration)) setAsking(false)
    } finally {
      setSaving(false)
    }
  }

  const choose = (next: NotifLevel) => {
    if (next === 'muted') {
      if (!muted) setAsking(true)
      return
    }
    setAsking(false)
    if (next !== level) void save(next)
  }

  const back: NotifLevel = thenLevel ?? defaultLevel ?? 'all'

  return (
    <div class="sh-level-picker">
      <fieldset class="sh-space-notif-levels" disabled={saving}>
        <legend class={legendHidden ? 'sr-only' : 'sh-space-notif-levels__legend'}>{legend}</legend>
        {levels.map(opt => (
          <label
            key={opt}
            class={`sh-space-notif-levels__option${shown === opt ? ' sh-space-notif-levels__option--on' : ''}`}
          >
            <input
              type="radio"
              name={name}
              value={opt}
              checked={shown === opt}
              onChange={() => choose(opt)}
            />
            <span class="sh-space-notif-levels__text">
              <span class="sh-space-notif-levels__title">
                {levelLabel(opt)}
                {defaultLevel === opt && (
                  <span class="sh-level-picker__default">{t('notif.level.default')}</span>
                )}
              </span>
              <span class="sh-space-notif-levels__meta">{hint(opt)}</span>
            </span>
          </label>
        ))}
      </fieldset>
      {asking && (
        <div class="sh-level-picker__ask" role="group" aria-labelledby={askId}>
          <span id={askId} class="sh-level-picker__ask-label">{t('notif.level.how_long')}</span>
          <div class="sh-level-picker__chips">
            {ASK_LENGTHS.map(d => (
              <button
                key={d}
                type="button"
                class="sh-chip"
                disabled={saving}
                onClick={() => void save('muted', d)}
              >
                {t(`notif.level.for_${d}`)}
              </button>
            ))}
          </div>
        </div>
      )}
      {quiet && !asking && (
        <div class="sh-level-picker__quiet" role="status">
          <span class="sh-level-picker__quiet-text">
            <span aria-hidden="true">🔕 </span>
            {quietLabel(mutedUntil, mutedUntil && !isMutedForever(mutedUntil) ? back : null)}
          </span>
          <Button variant="secondary" disabled={saving} onClick={() => void save(back)}>
            {t('notif.level.undo')}
          </Button>
        </div>
      )}
    </div>
  )
}
