/** Builders for timetable tests (not imported by production code). */
import type { Timetable, TimetableEntry } from '@/types'

let seq = 0

export function entry(
  weekday: number, start: string, end: string, extra: Partial<TimetableEntry> = {},
): TimetableEntry {
  seq += 1
  return {
    id: `e${seq}`, weekday, start, end, kind: 'lesson', label: null, title: null,
    room: null, teacher: null, note: null, color: null, icon: null, ...extra,
  }
}

export function timetable(extra: Partial<Timetable> = {}): Timetable {
  return {
    schema: 1, id: 'tt1', name: "Emma's timetable", created_by: 'u1',
    created_at: '2026-09-01T08:00:00+00:00', updated_at: '2026-09-01T08:00:00+00:00',
    updated_by: null, version: 1, week_start: 0, tz: 'UTC', color: null,
    days: [0, 1, 2, 3, 4],
    defaults: { lesson_minutes: 45, gap_minutes: 5, day_start: '08:00' },
    entries: [], overrides: [],
    validity: { valid_from: null, valid_until: null, excluded_weeks: [] },
    assignees: ['u1'], active_this_week: true, valid_today: true,
    ...extra,
  }
}

/** The same school day on every weekday in ``days``. */
export function schoolWeek(days: number[] = [0, 1, 2, 3, 4], titles: string[][] = []): TimetableEntry[] {
  const slots: [string, string, 'lesson' | 'break'][] = [
    ['08:00', '08:45', 'lesson'], ['08:50', '09:35', 'lesson'], ['09:35', '09:55', 'break'],
    ['09:55', '10:40', 'lesson'], ['10:45', '11:30', 'lesson'],
  ]
  return days.flatMap((d, di) => {
    let n = 0
    return slots.map(([s, e, kind]) => kind === 'break'
      ? entry(d, s, e, { kind, title: 'Pause' })
      : entry(d, s, e, { label: `${++n}.`, title: titles[di]?.[n - 1] ?? null }))
  })
}
