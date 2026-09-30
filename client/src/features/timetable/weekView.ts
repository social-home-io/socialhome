/**
 * Week mode ("This week", the Vertretungsplan) — turning the backend's
 * ResolvedWeek into something the regular grid components render.
 *
 * ``buildWeekView`` synthesises a timetable whose ``entries`` are the
 * week's effective lessons (cancelled ones included, so they can be
 * shown struck through), keyed by ``source_id`` — the entry id, or the
 * override id of an extra lesson. Colours are baked in from the
 * regular timetable, so a substitute subject never reshuffles the
 * automatic colours of the rest of the week. The status of every block
 * and the date of every column travel in ``WeekContext`` so
 * ``LessonBlock`` / ``DayHeading`` pick them up without prop drilling.
 */
import { createContext } from 'preact'
import { t } from '@/i18n/i18n'
import type { EffectiveLesson, ResolvedWeek, Timetable, TimetableEntry } from '@/types'
import { entryColor } from './colors'
import { weekdayOf } from './dates'
import { displayTitle } from './labels'
import { mergeTags } from './layout'

export interface WeekInfo {
  anchor: string
  /** weekday → date ("YYYY-MM-DD") of the shown week's days. */
  dates: Record<number, string>
  /** weekday → whether the timetable is in effect that day. */
  valid: Record<number, boolean>
  /** entry id (view) → the effective lesson behind it. */
  lessons: Map<string, EffectiveLesson>
  /** Days before this can't take overrides any more (backend: 14 days). */
  editableFrom: string
}

export const WeekContext = createContext<WeekInfo | null>(null)

/** Why nothing can be added on ``weekday`` this week — a day too far
 *  in the past, or one the timetable isn't in effect (holiday, outside
 *  the valid dates) — or ``null`` when it can. */
export function addBlockedReason(week: WeekInfo, weekday: number): string | null {
  if (isLocked(week, weekday)) return t('timetable.week.locked')
  if (!week.valid[weekday]) return t('timetable.week.day_inactive')
  return null
}

/** An add button's name with the reason it is disabled appended. */
export function blockedLabel(label: string, reason: string | null): string {
  return reason ? `${label} — ${reason}` : label
}

export function isLocked(week: WeekInfo, weekday: number): boolean {
  const date = week.dates[weekday]
  return !date || date < week.editableFrom
}

export function buildWeekView(
  tt: Timetable, week: ResolvedWeek, editableFrom: string,
): { view: Timetable; info: WeekInfo } {
  const dates: Record<number, string> = {}
  const valid: Record<number, boolean> = {}
  const lessons = new Map<string, EffectiveLesson>()
  const entries: TimetableEntry[] = []
  for (const day of week.days) {
    const wd = weekdayOf(day.date)
    dates[wd] = day.date
    valid[wd] = day.valid
    for (const l of day.lessons) {
      lessons.set(l.source_id, l)
      const e: TimetableEntry = {
        id: l.source_id,
        weekday: wd,
        start: l.start,
        end: l.end,
        kind: l.kind,
        label: l.label,
        title: l.title,
        room: l.room,
        teacher: l.teacher,
        note: l.note,
        icon: l.icon,
        color: l.color ?? (l.kind === 'lesson' ? entryColor({ color: null, title: l.title }, tt) : null),
      }
      // Changed / extra lessons keep their own block (each has its own
      // override); only two cancelled or two normal lessons merge.
      mergeTags.set(e, l.status === 'changed' || l.status === 'added' ? l.source_id : l.status)
      entries.push(e)
    }
  }
  return {
    view: { ...tt, entries },
    info: { anchor: week.anchor, dates, valid, lessons, editableFrom },
  }
}

/** "Room 204 → 112", "Mathe → Deutsch", "10:20 → 10:40" — what a
 *  changed lesson differs in from its regular slot. */
export function changesOf(l: EffectiveLesson): string[] {
  const o = l.original
  if (l.status !== 'changed' || !o) return []
  const out: string[] = []
  const was = displayTitle(o)
  const now = displayTitle(l)
  if (was !== now) out.push(`${was || '—'} → ${now || '—'}`)
  if (o.start !== l.start || o.end !== l.end) {
    out.push(o.start !== l.start ? `${o.start} → ${l.start}` : `${o.end} → ${l.end}`)
  }
  if ((o.room ?? '') !== (l.room ?? '')) {
    out.push(o.room
      ? t('timetable.week.room_change', { from: o.room, to: l.room || '—' })
      : t('timetable.aria.room', { room: l.room ?? '' }).replace(/^./, c => c.toUpperCase()))
  }
  if ((o.teacher ?? '') !== (l.teacher ?? '')) {
    out.push(`${o.teacher || '—'} → ${l.teacher || '—'}`)
  }
  if ((o.note ?? '') !== (l.note ?? '') && l.note) out.push(l.note)
  return out
}

/** ", cancelled" / ", changed: Room 204 → 112" / ", extra lesson". */
export function statusAria(l: EffectiveLesson): string {
  switch (l.status) {
    case 'cancelled': return `, ${t('timetable.week.cancelled')}`
    case 'added': return `, ${t('timetable.week.extra_aria')}`
    case 'changed': {
      const c = changesOf(l)
      return `, ${t('timetable.week.changed')}${c.length ? `: ${c.join(', ')}` : ''}`
    }
    default: return ''
  }
}
