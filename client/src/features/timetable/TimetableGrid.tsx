/**
 * TimetableGrid — the week at a glance.
 *
 * Picks the Periods table when every visible day has the same slot
 * sequence and the proportional Timeline otherwise; a small "Layout:
 * Auto / Periods / Timeline" control overrides that per timetable.
 * ``prefs.list`` (toggled from the header) swaps in the plain semantic
 * table, which is also the print fallback. Columns follow the
 * timetable's week start.
 */
import { t } from '@/i18n/i18n'
import type { Timetable, TimetableEntry } from '@/types'
import { isoWeekday } from '@/utils/week'
import { gridId } from './focus'
import { isPeriodsEligible, nextStartFor, prefillAt, type EntryPrefill } from './layout'
import { TimetableDayView } from './TimetableDayView'
import { TimetableList } from './TimetableList'
import { TimetablePeriods } from './TimetablePeriods'
import { TimetableTimeline } from './TimetableTimeline'
import { orderedDays, toMinutes } from './time'
import type { LayoutChoice, ViewPrefs } from './viewPrefs'

interface Props {
  tt: Timetable
  prefs: ViewPrefs
  onPrefs: (patch: Partial<ViewPrefs>) => void
  onEdit: (entry: TimetableEntry, group?: string[]) => void
  onAdd: (prefill: EntryPrefill) => void
  /** Phone width: one day at a time (TimetableDayView) instead of the week. */
  narrow?: boolean
  /** Injected in tests; defaults to now. */
  now?: Date
}

/** Today's weekday when the timetable is in effect today, else null. */
export function todayColumn(tt: Timetable, now: Date = new Date()): number | null {
  const wd = isoWeekday(now)
  return tt.valid_today !== false && tt.days.includes(wd) ? wd : null
}

export function resolveLayout(tt: Timetable, days: number[], choice: LayoutChoice): 'periods' | 'timeline' {
  if (choice !== 'auto') return choice
  return isPeriodsEligible(tt, days) ? 'periods' : 'timeline'
}

const LAYOUTS: LayoutChoice[] = ['auto', 'periods', 'timeline']

export function TimetableGrid({ tt, prefs, onPrefs, onEdit, onAdd, narrow = false, now }: Props) {
  const days = orderedDays(tt.days, tt.week_start)
  const layout = resolveLayout(tt, days, prefs.layout)
  const today = todayColumn(tt, now)
  const addAt = (weekday: number, startMin: number) => onAdd(prefillAt(tt, weekday, startMin))
  const addDay = (weekday: number) => addAt(weekday, toMinutes(nextStartFor(tt, weekday)))

  return (
    <section id={gridId(tt.id)} tabIndex={-1} class="sh-timetable-grid"
             aria-label={t('timetable.grid.aria', { name: tt.name })}>
      {!prefs.list && !narrow && (
        <div class="sh-timetable-toolbar">
          <div class="sh-timetable-seg" role="group" aria-label={t('timetable.layout.label')}>
            <span class="sh-timetable-seg__label" aria-hidden="true">{t('timetable.layout.label')}</span>
            {LAYOUTS.map(l => (
              <button
                key={l}
                type="button"
                class={`sh-timetable-seg__btn${prefs.layout === l ? ' is-on' : ''}`}
                aria-pressed={prefs.layout === l}
                onClick={() => onPrefs({ layout: l })}
              >
                {t(`timetable.layout.${l}`)}
              </button>
            ))}
          </div>
        </div>
      )}

      {prefs.list ? (
        <TimetableList tt={tt} days={days} onEdit={onEdit} />
      ) : narrow ? (
        <>
          <TimetableDayView tt={tt} picture={prefs.picture} onEdit={onEdit} onAdd={onAdd} now={now} />
          <TimetableList tt={tt} days={days} printOnly />
        </>
      ) : (
        <>
          <div class="sh-timetable-visual">
            {layout === 'periods' ? (
              <TimetablePeriods tt={tt} days={days} today={today} picture={prefs.picture}
                                onEdit={onEdit} onAddAt={addAt} onAddDay={addDay} />
            ) : (
              <TimetableTimeline tt={tt} days={days} today={today} picture={prefs.picture}
                                 onEdit={onEdit} onAddAt={addAt} onAddDay={addDay} />
            )}
          </div>
          <TimetableList tt={tt} days={days} printOnly />
        </>
      )}
    </section>
  )
}
