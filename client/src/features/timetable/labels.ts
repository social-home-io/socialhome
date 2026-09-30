/**
 * Human text for a timetable entry — what a block shows and what a
 * screen reader hears ("Tuesday, 3rd lesson, 10:20–11:05, Maths,
 * room 204").
 */
import { t } from '@/i18n/i18n'
import type { Timetable, TimetableEntry } from '@/types'
import { presetFor } from './icons'
import { lessonNumber } from './layout'
import { formatRange, ordinal, weekdayName } from './time'

/** The visible title: the entry's own, else its icon's preset name,
 *  else "Break" for an untitled break; ``''`` for an empty lesson. */
export function displayTitle(entry: Pick<TimetableEntry, 'title' | 'icon' | 'kind'>): string {
  const title = entry.title?.trim()
  if (title) return title
  const preset = presetFor(entry.icon)
  if (preset) return t(preset.label)
  return entry.kind === 'break' ? t('timetable.kind.break') : ''
}

/** "room 204 · Frau Huber" — the muted second line. */
export function metaLine(entry: Pick<TimetableEntry, 'room' | 'teacher'>): string {
  return [entry.room, entry.teacher].filter(Boolean).join(' · ')
}

export function entryAriaLabel(tt: Timetable, entry: TimetableEntry, spanEnd?: string): string {
  const n = lessonNumber(tt, entry)
  const parts = [
    weekdayName(entry.weekday, 'long'),
    n !== null ? t('timetable.aria.nth_lesson', { ord: ordinal(n) }) : t('timetable.kind.break'),
    formatRange(entry.start, spanEnd ?? entry.end),
    displayTitle(entry) || t('timetable.aria.empty_slot'),
  ]
  if (entry.room) parts.push(t('timetable.aria.room', { room: entry.room }))
  if (entry.teacher) parts.push(t('timetable.aria.teacher', { teacher: entry.teacher }))
  return parts.join(', ')
}
