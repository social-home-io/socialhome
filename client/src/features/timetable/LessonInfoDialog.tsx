/**
 * LessonInfoDialog — a lesson's details, read-only.
 *
 * What a block opens for a viewer who can't edit the timetable (a space
 * member): the title with its icon, the day and time, room, teacher and
 * note — the parts a narrow block truncates or leaves out — and, in
 * week mode, this week's status ("Cancelled", "Extra", or what
 * changed). One Close button; nothing here writes.
 */
import { signal } from '@preact/signals'
import { Modal } from '@/components/Modal'
import { Button } from '@/components/Button'
import { t } from '@/i18n/i18n'
import type { EffectiveLesson, TimetableEntry } from '@/types'
import { colorClass, entryColor } from './colors'
import { weekdayDate } from './dates'
import { displayTitle } from './labels'
import { useTimetableScope } from './scope'
import { formatRange, weekdayName } from './time'
import { changesOf } from './weekView'

export interface LessonInfo {
  timetableId: string
  entry: TimetableEntry
  /** A merged double lesson: shown up to here. */
  spanEnd?: string
  /** Week mode: the date and what this week does to the lesson. */
  date?: string
  lesson?: EffectiveLesson
}

export const lessonInfo = signal<LessonInfo | null>(null)

export function openLessonInfo(info: LessonInfo): void {
  lessonInfo.value = info
}

export function closeLessonInfo(): void {
  lessonInfo.value = null
}

export function LessonInfoDialog() {
  const { store } = useTimetableScope()
  const info = lessonInfo.value
  const tt = info ? store.timetables.value.find(x => x.id === info.timetableId) : undefined
  if (!info || !tt) return null
  const { entry: e, lesson } = info
  const title = displayTitle(e) || t('timetable.aria.empty_slot')
  const status = lesson?.status ?? 'normal'
  const changes = lesson ? changesOf(lesson) : []
  const badge = status === 'cancelled' ? t('timetable.week.cancelled_badge')
    : status === 'added' ? t('timetable.week.extra')
      : status === 'changed' ? t('timetable.info.changed') : null
  const rows: [string, string][] = [
    [t('timetable.info.when'), `${info.date ? weekdayDate(info.date) : weekdayName(e.weekday, 'long')} · ${
      formatRange(e.start, info.spanEnd ?? e.end)}`],
    ...(e.room ? [[t('timetable.entry.room'), e.room] as [string, string]] : []),
    ...(e.teacher ? [[t('timetable.entry.teacher'), e.teacher] as [string, string]] : []),
    ...(e.note ? [[t('timetable.entry.note'), e.note] as [string, string]] : []),
  ]
  return (
    <Modal open onClose={closeLessonInfo} title={title}>
      <div class={`sh-timetable-info ${e.kind === 'break' ? '' : colorClass(entryColor(e, tt))}`}>
        {(e.icon || badge) && (
          <p class="sh-timetable-info__lead">
            {e.icon && <span class="sh-timetable-info__icon" aria-hidden="true">{e.icon}</span>}
            {badge && <span class={`sh-timetable-badge sh-timetable-badge--${status}`}>{badge}</span>}
          </p>
        )}
        {changes.length > 0 && (
          <ul class="sh-timetable-info__changes">
            {changes.map(c => <li key={c}>{c}</li>)}
          </ul>
        )}
        <dl class="sh-timetable-info__rows">
          {rows.map(([k, v]) => (
            <div key={k} class="sh-timetable-info__row">
              <dt>{k}</dt>
              <dd>{v}</dd>
            </div>
          ))}
        </dl>
        <div class="sh-form-actions">
          <Button type="button" variant="secondary" onClick={closeLessonInfo}>
            {t('timetable.close')}
          </Button>
        </div>
      </div>
    </Modal>
  )
}
