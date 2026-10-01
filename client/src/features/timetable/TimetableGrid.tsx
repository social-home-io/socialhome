/**
 * TimetableGrid — the week at a glance.
 *
 * Picks the Periods table when every visible day has the same slot
 * sequence and the proportional Timeline otherwise; a small "Layout:
 * Auto / Periods / Timeline" control overrides that per timetable.
 * ``prefs.list`` (toggled from the header) swaps in the plain semantic
 * table. Columns follow the timetable's week start.
 *
 * In regular mode the grid is keyboard-editable (``useGridNav``):
 * Delete removes a lesson (with Undo), Ctrl/⌘+C / V copy a block's
 * subject onto another lesson (a PATCH with Undo). Each day heading
 * carries the day tools (Set up times…, Copy to other days…). Week
 * mode (``WeekContext``) renders the same components over the resolved
 * week and leaves the regular-plan tools out. ``TimetablePrint`` is
 * the print-only twin.
 */
import { signal } from '@preact/signals'
import { useContext } from 'preact/hooks'
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'
import type { Timetable, TimetableEntry } from '@/types'
import { isoWeekday } from '@/utils/week'
import { applyBrush, brushOn, type PaintBrush } from './brush'
import { isoDate } from './dates'
import { focusGrid, gridId } from './focus'
import { displayTitle } from './labels'
import { isPeriodsEligible, nextStartFor, prefillAt, type EntryPrefill } from './layout'
import type { MenuItem } from './OverflowMenu'
import { TimetableDayView } from './TimetableDayView'
import { TimetableList } from './TimetableList'
import { TimetablePeriods } from './TimetablePeriods'
import { TimetablePrint, printing } from './TimetablePrint'
import { TimetableTimeline } from './TimetableTimeline'
import { orderedDays, toMinutes } from './time'
import type { GridNavHandlers } from './useGridNav'
import type { LayoutChoice, ViewPrefs } from './viewPrefs'
import { WeekContext } from './weekView'
import { useTimetableScope } from './scope'

/** The block copied with Ctrl/⌘+C (in-app; the system clipboard is left alone). */
export const copiedBlock = signal<PaintBrush | null>(null)

interface Props {
  tt: Timetable
  prefs: ViewPrefs
  onPrefs: (patch: Partial<ViewPrefs>) => void
  /** ``run``: the ids of a merged double lesson the block stands for. */
  onEdit: (entry: TimetableEntry, group?: string[], run?: string[]) => void
  onAdd: (prefill: EntryPrefill) => void
  /** Phone width: one day at a time (TimetableDayView) instead of the week. */
  narrow?: boolean
  /** Day tools (regular mode). */
  onSetupDay?: (weekday: number) => void
  onCopyDay?: (weekday: number) => void
  /** Print header line: which week (week mode). */
  printWeek?: string
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

export function TimetableGrid({
  tt, prefs, onPrefs, onEdit, onAdd, narrow = false, now, onSetupDay, onCopyDay, printWeek,
}: Props) {
  const { store, editable } = useTimetableScope()
  const week = useContext(WeekContext)
  const days = orderedDays(tt.days, tt.week_start)
  const layout = resolveLayout(tt, days, prefs.layout)
  const today = week ? weekToday(week.dates, now) : todayColumn(tt, now)
  const addAt = (weekday: number, startMin: number) => onAdd(prefillAt(tt, weekday, startMin))
  const addDay = (weekday: number) => addAt(weekday, toMinutes(nextStartFor(tt, weekday)))
  const brushing = brushOn(tt.id)

  const dayMenu = week || !onSetupDay ? undefined : (d: number): MenuItem[] => [
    { label: t('timetable.day.setup'), onSelect: () => onSetupDay(d) },
    ...(onCopyDay && tt.days.length > 1
      ? [{ label: t('timetable.day.copy'), onSelect: () => onCopyDay(d) }]
      : []),
  ]
  // Keyboard editing: the regular plan, for an editor only.
  const nav: GridNavHandlers = week || !editable ? {} : {
    onDelete: (ids, focusNext) => {
      const onUndone = () => focusGrid(tt.id)
      const gone = new Set(ids)
      // A merged double lesson goes as a whole, in one PUT (one Undo).
      const call = ids.length === 1
        ? store.deleteEntry(tt.id, ids[0], { onUndone })
        : store.replaceEntries(tt.id, tt.entries.filter(e => !gone.has(e.id)), {
            undo: { message: t('timetable.entry.deleted_n', { n: String(ids.length) }), onUndone },
          })
      void call
        .then(out => { if (out) setTimeout(() => focusNext(null), 0) })
        .catch((e: unknown) => showToast((e as Error).message, 'error'))
    },
    onCopy: (entryId) => {
      const e = tt.entries.find(x => x.id === entryId)
      if (!e || e.kind !== 'lesson' || !e.title?.trim()) return
      copiedBlock.value = {
        kind: 'paint', title: e.title.trim(), icon: e.icon, color: e.color, room: e.room, teacher: e.teacher,
      }
      showToast(t('timetable.grid.copied', { title: displayTitle(e) }), 'info')
    },
    onPaste: (ids) => {
      const src = copiedBlock.value
      if (!src) return
      const undo = { message: t('timetable.grid.pasted', { title: src.title }), onUndone: () => focusGrid(tt.id) }
      const fail = (err: unknown) => showToast((err as Error).message, 'error')
      if (ids.length > 1) {
        // Every half of a double lesson, in one PUT.
        const wanted = new Set(ids)
        let changed = false
        const list = tt.entries.map(e => {
          const next = wanted.has(e.id) ? applyBrush(e, src) : null
          if (next) changed = true
          return next ?? e
        })
        if (changed) void store.replaceEntries(tt.id, list, { undo }).catch(fail)
        return
      }
      const e = tt.entries.find(x => x.id === ids[0])
      const next = e ? applyBrush(e, src) : null
      if (!e || !next) return
      void store.patchEntry(tt.id, e.id, {
        title: next.title, icon: next.icon, color: next.color, room: next.room, teacher: next.teacher,
      }, { undo }).catch(fail)
    },
  }

  return (
    <section id={gridId(tt.id)} tabIndex={-1}
             class={`sh-timetable-grid${brushing ? ' is-brushing' : ''}${week ? ' is-week' : ''}`}
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
        <TimetableList tt={tt} days={days} onEdit={editable ? onEdit : undefined} />
      ) : narrow ? (
        <TimetableDayView tt={tt} picture={prefs.picture} onEdit={onEdit} onAdd={onAdd} now={now}
                          onSetupDay={week ? undefined : onSetupDay}
                          onCopyDay={week ? undefined : onCopyDay} />
      ) : (
        <div class="sh-timetable-visual">
          {layout === 'periods' ? (
            <TimetablePeriods tt={tt} days={days} today={today} picture={prefs.picture}
                              onEdit={onEdit} onAddAt={addAt} onAddDay={addDay}
                              dayMenu={dayMenu} nav={nav} />
          ) : (
            <TimetableTimeline tt={tt} days={days} today={today} picture={prefs.picture}
                               onEdit={onEdit} onAddAt={addAt} onAddDay={addDay}
                               dayMenu={dayMenu} nav={nav}
                               onSetupDay={week ? undefined : onSetupDay} />
          )}
        </div>
      )}
      {printing.value && <TimetablePrint tt={tt} days={days} week={printWeek} />}
    </section>
  )
}

/** The shown week's column for today, if today is in it. */
function weekToday(dates: Record<number, string>, now: Date = new Date()): number | null {
  const today = isoDate(now)
  const hit = Object.entries(dates).find(([, d]) => d === today)
  return hit ? Number(hit[0]) : null
}
