/**
 * WeekBar — the mode switch above the grid: **Regular | This week**.
 *
 * Regular shows the plan every week follows, plus a "Changes this
 * week: 2" link when the current week has overrides. This week (the
 * Vertretungsplan) adds the week navigator — "‹ W41 · Oct 5 – 11 ›
 * [This week]" (a date range only for Sunday-start timetables, which
 * have no ISO week number).
 */
import { t } from '@/i18n/i18n'
import type { Timetable } from '@/types'
import { addDays, todayIn, weekAnchor, weekLabel } from './dates'

interface Props {
  tt: Timetable
  /** Anchor of the week shown, ``null`` in regular mode. */
  anchor: string | null
  onWeek: (date: string | null) => void
  /** Regular mode: the current week's changed / cancelled / extra
   *  lessons (resolved — the same count the week banner shows). */
  pending?: number
  now?: Date
}

export function WeekBar({ tt, anchor, onWeek, pending = 0, now }: Props) {
  const today = todayIn(tt.tz, now)
  const current = weekAnchor(today, tt.week_start)
  return (
    <div class="sh-timetable-modebar">
      <div class="sh-timetable-seg" role="group" aria-label={t('timetable.week.mode_label')}>
        <button type="button" class={`sh-timetable-seg__btn${anchor === null ? ' is-on' : ''}`}
                aria-pressed={anchor === null} onClick={() => onWeek(null)}>
          {t('timetable.week.mode_regular')}
        </button>
        <button type="button" class={`sh-timetable-seg__btn${anchor !== null ? ' is-on' : ''}`}
                aria-pressed={anchor !== null} onClick={() => { if (anchor === null) onWeek(current) }}>
          {t('timetable.week.mode_week')}
        </button>
      </div>
      {anchor === null ? (
        anchor === null && pending > 0 && (
          <button type="button" class="sh-link sh-timetable-modebar__changes" onClick={() => onWeek(current)}>
            {t('timetable.week.changes_link', { n: String(pending) })}
          </button>
        )
      ) : (
        <nav class="sh-timetable-weeknav" aria-label={t('timetable.week.nav_aria')}>
          <button type="button" class="sh-timetable-weeknav__step" onClick={() => onWeek(addDays(anchor, -7))}
                  aria-label={t('timetable.week.prev')} title={t('timetable.week.prev')}>
            <span aria-hidden="true">‹</span>
          </button>
          <span class="sh-timetable-weeknav__label" aria-live="polite">{weekLabel(anchor, tt.week_start)}</span>
          <button type="button" class="sh-timetable-weeknav__step" onClick={() => onWeek(addDays(anchor, 7))}
                  aria-label={t('timetable.week.next')} title={t('timetable.week.next')}>
            <span aria-hidden="true">›</span>
          </button>
          <button type="button" class="sh-chip sh-timetable-chip sh-timetable-weeknav__today"
                  title={t('timetable.week.jump_today')}
                  disabled={anchor === current} onClick={() => onWeek(current)}>
            {t('timetable.week.today')}
          </button>
        </nav>
      )}
    </div>
  )
}
