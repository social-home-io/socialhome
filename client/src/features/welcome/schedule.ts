/**
 * Today's merged schedule — the pure half of ``TodayScheduleCard``.
 *
 * Turns the corner bundle's ``today_timetable`` (lessons with UTC
 * ``start_at`` / ``end_at``) and ``today_events`` into one time-ordered
 * agenda: lesson rows, event rows and the breaks worth showing, with
 * every calendar-event ↔ lesson overlap marked on both rows, plus the
 * "Now / Next / School's out" header line.
 *
 * Everything takes ``now`` (epoch ms) so the card re-derives once a
 * minute and the tests pin a clock.
 */
import { t } from '@/i18n/i18n'
import { displayTitle } from '@/features/timetable/labels'
import type { TodayLesson, TodayTimetable, WelcomeEvent } from './cards'

export interface Overlap {
  title: string
  start: number
  end: number
}

interface RowBase {
  key: string
  start: number
  end: number
}

export interface LessonRow extends RowBase {
  type: 'lesson'
  tt: TodayTimetable
  lesson: TodayLesson
  title: string
  overlaps: Overlap[]
}

export interface EventRow extends RowBase {
  type: 'event'
  event: WelcomeEvent
  overlaps: Overlap[]
}

export interface BreakRow extends RowBase {
  type: 'break'
  tt: TodayTimetable
  lesson: TodayLesson
  title: string
}

export type AgendaRow = LessonRow | EventRow | BreakRow

export interface Agenda {
  allDay: WelcomeEvent[]
  rows: AgendaRow[]
}

const ms = (iso: string): number => new Date(iso).getTime()

/** "08:00" in the viewer's locale and zone — the same clock the
 *  calendar rows use, so lessons and events line up. */
export function clock(at: number | string): string {
  return new Date(at).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
}

export function clockRange(start: number, end: number): string {
  return `${clock(start)}–${clock(end)}`
}

/** A lesson worth a row: a titled (or iconed) lesson. Untitled slots
 *  are the template's placeholders the brush fills later — noise on
 *  the home screen. */
function isShown(ls: TodayLesson): boolean {
  return ls.kind === 'break' || displayTitle(ls) !== ''
}

/** The lessons of ``tts`` that get a row (breaks excluded). */
export function shownLessons(tts: TodayTimetable[]): TodayLesson[] {
  return tts.flatMap(tt => tt.lessons.filter(ls => ls.kind === 'lesson' && isShown(ls)))
}

/** Timetables with at least one lesson worth showing today. */
export function activeTimetables(tts: TodayTimetable[]): TodayTimetable[] {
  return tts.filter(tt => shownLessons([tt]).length > 0)
}

const overlaps = (a: RowBase, b: RowBase) => a.start < b.end && b.start < a.end

const ORDER: Record<AgendaRow['type'], number> = { lesson: 0, break: 1, event: 2 }

/**
 * The merged agenda of ``tts`` (already filtered to the visible
 * timetables) and ``events``. ``prefix`` puts the timetable name in
 * front of lesson titles ("Emma · Mathe") when several are shown.
 */
export function buildAgenda(
  tts: TodayTimetable[], events: WelcomeEvent[], { prefix = false } = {},
): Agenda {
  const lessons: LessonRow[] = []
  const breaks: BreakRow[] = []
  for (const tt of tts) {
    for (const ls of tt.lessons) {
      if (!isShown(ls)) continue
      const base = { tt, lesson: ls, start: ms(ls.start_at), end: ms(ls.end_at) }
      const title = displayTitle(ls)
      if (ls.kind === 'break') {
        breaks.push({ ...base, type: 'break', key: `b:${tt.timetable_id}:${ls.source_id}`, title })
      } else {
        lessons.push({
          ...base,
          type: 'lesson',
          key: `l:${tt.timetable_id}:${ls.source_id}`,
          title: prefix ? `${tt.name} · ${title}` : title,
          overlaps: [],
        })
      }
    }
  }
  // A break only earns a divider between two of its timetable's lessons.
  const keptBreaks = breaks.filter(b => {
    const own = lessons.filter(l => l.tt === b.tt)
    return own.some(l => l.end <= b.start) && own.some(l => l.start >= b.end)
  })
  const allDay: WelcomeEvent[] = []
  const eventRows: EventRow[] = []
  for (const e of events) {
    if (e.all_day) { allDay.push(e); continue }
    eventRows.push({
      type: 'event', key: `e:${e.id}:${e.start}`, event: e,
      start: ms(e.start), end: ms(e.end), overlaps: [],
    })
  }
  // A cancelled lesson frees the slot — it never conflicts.
  for (const ev of eventRows) {
    for (const l of lessons) {
      if (l.lesson.status === 'cancelled' || !overlaps(ev, l)) continue
      ev.overlaps.push({ title: l.title, start: l.start, end: l.end })
      l.overlaps.push({ title: ev.event.summary, start: ev.start, end: ev.end })
    }
  }
  const rows: AgendaRow[] = [...lessons, ...keptBreaks, ...eventRows]
  rows.sort((a, b) => a.start - b.start || ORDER[a.type] - ORDER[b.type] || a.end - b.end)
  return { allDay, rows }
}

/** Where the now line goes: before the first row that hasn't started
 *  (``rows.length`` when everything has). */
export function nowIndex(rows: AgendaRow[], now: number): number {
  const i = rows.findIndex(r => r.start > now)
  return i === -1 ? rows.length : i
}

/** "in 12 min" for the next hour, else ``null`` (the clock says it). */
function inMinutes(start: number, now: number): string | null {
  const min = Math.ceil((start - now) / 60_000)
  return min < 60 ? t('welcome.schedule.in_min', { n: String(Math.max(1, min)) }) : null
}

export type ScheduleState = 'now' | 'next' | 'done' | 'none'

type Timed = LessonRow | EventRow

const titleOf = (r: Timed) => (r.type === 'lesson' ? r.title : r.event.summary)

function nextText(next: Timed): string {
  const parts = [t('welcome.schedule.next', { title: titleOf(next) }), clock(next.start)]
  if (next.type === 'lesson' && next.lesson.room) {
    parts.push(t('welcome.schedule.room', { room: next.lesson.room }))
  }
  return parts.join(' · ')
}

export interface ScheduleStatus {
  state: ScheduleState
  /** The state line — only changes when the state does, so it is safe
   *  for an ``aria-live`` region. */
  text: string
  /** "in 12 min" — ticks every minute, so kept out of the live text. */
  soon: string | null
}

/**
 * The header line, over lessons that still happen and timed events:
 *
 *  * something runs → "Now: Deutsch until 10:40" (a running lesson
 *    first, then a running event: "… · Dentist until 10:30");
 *  * otherwise the earliest upcoming lesson or event → "Next: Mathe ·
 *    09:55 · Room 204 · in 12 min" (after the last lesson prefixed with
 *    "School's out · 6 lessons today");
 *  * nothing left → "School's out · 6 lessons today" / "No lessons today".
 */
export function scheduleStatus(rows: AgendaRow[], now: number): ScheduleStatus {
  const lessons = rows.filter((r): r is LessonRow =>
    r.type === 'lesson' && r.lesson.status !== 'cancelled')
  const events = rows.filter((r): r is EventRow => r.type === 'event')
  const running = (r: Timed) => r.start <= now && now < r.end
  const lessonNow = lessons.find(running)
  const eventNow = events.find(running)
  if (lessonNow || eventNow) {
    const parts: string[] = []
    if (lessonNow) parts.push(t('welcome.schedule.now', { title: lessonNow.title, time: clock(lessonNow.end) }))
    if (eventNow) {
      parts.push(lessonNow
        ? t('welcome.schedule.until', { title: eventNow.event.summary, time: clock(eventNow.end) })
        : t('welcome.schedule.now', { title: eventNow.event.summary, time: clock(eventNow.end) }))
    }
    return { state: 'now', text: parts.join(' · '), soon: null }
  }
  const upcoming = [...lessons, ...events].filter(r => r.start > now)
    .sort((a, b) => a.start - b.start || (a.type === 'lesson' ? -1 : 1))
  const done = lessons.length === 0 ? null : t(
    lessons.length === 1 ? 'welcome.schedule.done_one' : 'welcome.schedule.done',
    { n: String(lessons.length) },
  )
  const next = upcoming[0]
  if (next) {
    const schoolOver = done !== null && !lessons.some(r => r.end > now)
    const text = nextText(next)
    return {
      state: 'next',
      text: schoolOver ? `${done} · ${text}` : text,
      soon: inMinutes(next.start, now),
    }
  }
  return done === null
    ? { state: 'none', text: t('welcome.schedule.no_lessons'), soon: null }
    : { state: 'done', text: done, soon: null }
}

/**
 * Past rows to fold away: when more than two rows (breaks aside) ended
 * before ``now``, every ended row but the most recent one is hidden —
 * past breaks with them. A row still running is never hidden.
 * ``hidden`` counts the folded rows, breaks excluded.
 */
export function collapsePast(
  rows: AgendaRow[], now: number,
): { rows: AgendaRow[]; hidden: number } {
  const past = rows.filter(r => r.type !== 'break' && r.end <= now)
  if (past.length <= 2) return { rows, hidden: 0 }
  const keep = past.reduce((a, b) => (b.end >= a.end ? b : a))
  const out = rows.filter(r => r === keep || r.end > now)
  return { rows: out, hidden: past.length - 1 }
}

/** Lessons that actually happen today (cancelled ones don't count). */
export function lessonCount(tts: TodayTimetable[]): number {
  return shownLessons(tts).filter(ls => ls.status !== 'cancelled').length
}
