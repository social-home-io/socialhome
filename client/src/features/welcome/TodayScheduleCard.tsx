/**
 * TodayScheduleCard — today's timetable lessons merged with today's
 * calendar events, on the home screen and ``/corner``.
 *
 * Replaces ``TodayCard`` whenever the caller has a timetable in effect
 * today (the merged list covers today's events too). One time-ordered
 * list: lesson rows (timetable colour bar, icon, title, room, label
 * chip), event rows (calendar dot, linking to the calendar) and
 * breaks as thin dividers between two lessons. All-day events sit in
 * their own labelled strip above the list (links, not chips). A "now"
 * divider moves once a minute; past rows fade, and with more than two
 * of them all but the latest fold behind "Show N earlier" (remembered
 * for the day in ``sessionStorage``).
 * An event overlapping a lesson that still happens marks both rows ⚠ —
 * the reason this card exists ("Dentist 10:30 overlaps Deutsch").
 *
 * With several timetables (one per child), a "Show:" group of toggle
 * buttons "All · Emma · Leo" filters the lessons (events always show); the choice is a per-viewer
 * convenience in ``localStorage``. Picture view (a per-timetable
 * choice from the timetable page) shows the lesson icons large.
 *
 * The pure agenda logic lives in :mod:`./schedule`; status styling and
 * wording reuse the timetable feature (``changesOf`` / ``statusAria`` /
 * ``.sh-timetable-badge`` / colour tokens).
 */
import { useEffect, useId, useState } from 'preact/hooks'
import { locale, t } from '@/i18n/i18n'
import { colorClass } from '@/features/timetable/colors'
import { loadViewPrefs } from '@/features/timetable/viewPrefs'
import { changesOf, statusAria } from '@/features/timetable/weekView'
import type { TodayTimetable, WelcomeEvent } from './cards'
import {
  activeTimetables, buildAgenda, clock, clockRange, collapsePast, nowIndex, scheduleStatus,
  type AgendaRow, type BreakRow, type EventRow, type LessonRow, type Overlap,
} from './schedule'
import { addBase } from '@/baseUrl'

export const FILTER_KEY = 'sh-welcome-schedule:filter'
/** sessionStorage: the local date on which "Show earlier" was opened. */
export const EARLIER_KEY = 'sh-welcome-schedule:earlier'
const ALL = 'all'

function loadFilter(): string {
  try {
    return localStorage.getItem(FILTER_KEY) || ALL
  } catch {
    return ALL
  }
}

function saveFilter(value: string): void {
  try {
    localStorage.setItem(FILTER_KEY, value)
  } catch {
    // Storage blocked — the choice lasts for this visit.
  }
}

/** The viewer-local date of ``now`` — "YYYY-MM-DD". */
function dayKey(now: number): string {
  const d = new Date(now)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

function loadEarlier(now: number): boolean {
  try {
    return sessionStorage.getItem(EARLIER_KEY) === dayKey(now)
  } catch {
    return false
  }
}

function saveEarlier(open: boolean, now: number): void {
  try {
    if (open) sessionStorage.setItem(EARLIER_KEY, dayKey(now))
    else sessionStorage.removeItem(EARLIER_KEY)
  } catch {
    // Storage blocked — the toggle lasts for this render only.
  }
}

/** True when the viewer's locale writes times with AM / PM — the time
 *  column then needs room for "07:45 AM". */
const hourCycles = new Map<string, boolean>()

function uses12h(): boolean {
  // Once per UI locale — ``Intl`` resolution isn't free on every render.
  const key = locale.value
  let hit = hourCycles.get(key)
  if (hit === undefined) {
    try {
      const cycle = new Intl.DateTimeFormat(undefined, { hour: 'numeric' }).resolvedOptions().hourCycle
      hit = cycle === 'h11' || cycle === 'h12'
    } catch {
      hit = false
    }
    hourCycles.set(key, hit)
  }
  return hit
}

/** Local midnight of the day ``now`` falls on. */
function startOfDay(now: number): number {
  const d = new Date(now)
  d.setHours(0, 0, 0, 0)
  return d.getTime()
}

/** ``Date.now()``, re-read at every minute boundary. */
function useMinuteClock(): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    let interval: ReturnType<typeof setInterval> | null = null
    const first = setTimeout(() => {
      setNow(Date.now())
      interval = setInterval(() => setNow(Date.now()), 60_000)
    }, 60_000 - (Date.now() % 60_000))
    return () => {
      clearTimeout(first)
      if (interval) clearInterval(interval)
    }
  }, [])
  return now
}

function overlapLabel(o: Overlap): string {
  return t('welcome.schedule.overlaps', { title: o.title, range: clockRange(o.start, o.end) })
}

function Warn({ overlaps }: { overlaps: Overlap[] }) {
  if (overlaps.length === 0) return null
  const label = overlaps.map(overlapLabel).join('; ')
  return (
    <span class="sh-schedule__warn" role="img" aria-label={label} title={label}>⚠</span>
  )
}

function rowClass(r: AgendaRow, now: number, extra: string[] = []): string {
  return [
    'sh-schedule__row',
    `sh-schedule__row--${r.type}`,
    r.end <= now ? 'is-past' : '',
    r.start <= now && now < r.end ? 'is-current' : '',
    ...extra,
  ].filter(Boolean).join(' ')
}

function LessonItem({ row, now, picture }: { row: LessonRow; now: number; picture: boolean }) {
  const ls = row.lesson
  const changes = changesOf(ls)
  // A changed room already reads "Room 204 → 112" on the change line.
  const roomChanged = ls.status === 'changed' && (ls.original?.room ?? '') !== (ls.room ?? '')
  const room = ls.room && ls.status !== 'cancelled' && !roomChanged ? ls.room : null
  const badge = ls.status === 'cancelled' ? 'cancelled' : ls.status === 'added' ? 'added' : null
  const initial = picture && !ls.icon && ls.title?.trim()
    ? Array.from(ls.title.trim())[0].toUpperCase()
    : null
  let sr = `${ls.label ? `${ls.label}, ` : ''}${clockRange(row.start, row.end)}, ${row.title}`
  if (ls.room) sr += `, ${t('timetable.aria.room', { room: ls.room })}`
  sr += statusAria(ls)
  return (
    <li
      class={rowClass(row, now, [
        colorClass(row.tt.color),
        ls.status !== 'normal' ? `is-${ls.status}` : '',
        row.overlaps.length > 0 ? 'has-overlap' : '',
        picture ? 'is-picture' : '',
      ])}
    >
      <time class="sh-schedule__time" dateTime={ls.start_at} aria-hidden="true">
        {clock(row.start)}
      </time>
      <span class="sh-schedule__bar" aria-hidden="true" />
      {(ls.icon || initial) && (
        <span class="sh-schedule__icon" aria-hidden="true">{ls.icon || initial}</span>
      )}
      <span class="sh-schedule__body" aria-hidden="true">
        <span class="sh-schedule__title">
          {badge && (
            <span class={`sh-timetable-badge sh-timetable-badge--${badge}`}>
              {t(badge === 'cancelled' ? 'timetable.week.cancelled_badge' : 'timetable.week.extra')}
            </span>
          )}
          <span class="sh-schedule__name">{row.title}</span>
        </span>
        {changes.length > 0 && (
          <span class="sh-schedule__meta sh-schedule__meta--change">{changes.join(' · ')}</span>
        )}
        {room && !picture && (
          <span class="sh-schedule__meta">{t('welcome.schedule.room', { room })}</span>
        )}
      </span>
      {ls.label && <span class="sh-schedule__label" aria-hidden="true">{ls.label}</span>}
      <Warn overlaps={row.overlaps} />
      <span class="sr-only">{sr}</span>
    </li>
  )
}

function EventItem({ row, now }: { row: EventRow; now: number }) {
  // An event that began on an earlier day (a party past midnight) says
  // which day, so "21:00" isn't read as tonight.
  const day = row.start < startOfDay(now)
    ? new Date(row.start).toLocaleDateString(undefined, { weekday: 'short' })
    : null
  const sr = `${day ? `${day} ` : ''}${clockRange(row.start, row.end)}, ${row.event.summary}`
  return (
    <li
      class={rowClass(row, now, [row.overlaps.length > 0 ? 'has-overlap' : ''])}
    >
      <a class="sh-schedule__link" href={addBase('/calendar')}>
        <time class="sh-schedule__time" dateTime={row.event.start} aria-hidden="true">
          {day && <span class="sh-schedule__day">{day} </span>}
          {clock(row.start)}
        </time>
        <span class="sh-schedule__dot" aria-hidden="true" />
        <span class="sh-schedule__body" aria-hidden="true">
          <span class="sh-schedule__title">
            <span class="sh-schedule__name">{row.event.summary}</span>
          </span>
        </span>
        <Warn overlaps={row.overlaps} />
        <span class="sr-only">{sr}</span>
      </a>
    </li>
  )
}

function BreakItem({ row, now }: { row: BreakRow; now: number }) {
  const minutes = Math.round((row.end - row.start) / 60_000)
  return (
    <li class={rowClass(row, now)}>
      <span class="sh-schedule__break">
        {t('welcome.schedule.break_len', { title: row.title, n: String(minutes) })}
      </span>
    </li>
  )
}

export function TodayScheduleCard({
  timetables, events,
}: {
  timetables: TodayTimetable[]
  events: WelcomeEvent[]
}) {
  const now = useMinuteClock()
  const [filter, setFilter] = useState(loadFilter)
  const [earlier, setEarlier] = useState(() => loadEarlier(now))
  const uid = useId()
  const listId = `${uid}-list`
  const filterLabelId = `${uid}-filter`
  const allDayLabelId = `${uid}-allday`
  const active = activeTimetables(timetables)
  const chosen = active.find(tt => tt.timetable_id === filter)
  const visible = chosen ? [chosen] : active
  const multi = active.length > 1
  const { allDay, rows: allRows } = buildAgenda(visible, events, { prefix: visible.length > 1 })
  const status = scheduleStatus(allRows, now)
  const folded = collapsePast(allRows, now)
  const showEarlier = earlier
  const rows = showEarlier ? allRows : folded.rows
  const at = nowIndex(rows, now)
  const pictureIds = new Set(
    visible.filter(tt => loadViewPrefs(tt.timetable_id).picture).map(tt => tt.timetable_id),
  )
  const target = chosen ?? active[0]
  const timetableHref = target
    ? `/calendar?tab=timetable&tt=${encodeURIComponent(target.timetable_id)}`
    : '/calendar?tab=timetable'

  const toggleEarlier = () => {
    const next = !showEarlier
    setEarlier(next)
    saveEarlier(next, now)
  }

  const choose = (value: string) => {
    setFilter(value)
    saveFilter(value)
  }

  const items = rows.map(r => {
    switch (r.type) {
      case 'lesson':
        return <LessonItem key={r.key} row={r} now={now} picture={pictureIds.has(r.tt.timetable_id)} />
      case 'event':
        return <EventItem key={r.key} row={r} now={now} />
      default:
        return <BreakItem key={r.key} row={r} now={now} />
    }
  })
  items.splice(at, 0, (
    <li key="now" class="sh-schedule__now" aria-hidden="true">
      <span class="sh-schedule__now-time">{clock(now)}</span>
    </li>
  ))

  return (
    <section class={`sh-welcome-card sh-welcome-card--schedule${uses12h() ? ' sh-schedule--12h' : ''}`}>
      <h2 class="sh-welcome-card__title">
        <span aria-hidden="true">📅</span> {t('welcome.schedule.title')}
      </h2>
      <p class={`sh-schedule__status sh-schedule__status--${status.state}`}>
        <span aria-live="polite">{status.text}</span>
        {status.soon && (
          <span class="sh-schedule__soon" aria-hidden="true">{` · ${status.soon}`}</span>
        )}
      </p>
      {multi && (
        <div class="sh-schedule__filter" role="group" aria-labelledby={filterLabelId}>
          <span id={filterLabelId} class="sh-schedule__filter-label">
            {t('welcome.schedule.filter')}
          </span>
          {[{ id: ALL, name: t('welcome.schedule.all'), color: null }, ...active.map(tt => ({
            id: tt.timetable_id, name: tt.name, color: tt.color,
          }))].map(c => {
            const on = c.id === ALL ? !chosen : chosen?.timetable_id === c.id
            return (
              <button
                key={c.id}
                type="button"
                class={`sh-schedule__chip ${c.id === ALL ? '' : colorClass(c.color)}`}
                aria-pressed={on ? 'true' : 'false'}
                onClick={() => choose(c.id)}
              >
                {c.id !== ALL && <span class="sh-schedule__chip-dot" aria-hidden="true" />}
                {c.name}
              </button>
            )
          })}
        </div>
      )}
      {allDay.length > 0 && (
        <div class="sh-schedule__allday">
          <span id={allDayLabelId} class="sh-schedule__allday-label">
            {t('welcome.schedule.all_day')}
          </span>
          <ul class="sh-schedule__allday-list" aria-labelledby={allDayLabelId}>
            {allDay.map(e => (
              <li key={e.id}>
                <a class="sh-schedule__allday-item" href={addBase('/calendar')}>
                  <span class="sh-schedule__dot" aria-hidden="true" />
                  <span class="sh-schedule__name">{e.summary}</span>
                </a>
              </li>
            ))}
          </ul>
        </div>
      )}
      {(folded.hidden > 0) && (
        <button
          type="button"
          class="sh-schedule__earlier"
          aria-expanded={showEarlier ? 'true' : 'false'}
          aria-controls={listId}
          onClick={toggleEarlier}
        >
          {showEarlier
            ? t('welcome.schedule.hide_earlier')
            : t('welcome.schedule.show_earlier', { n: String(folded.hidden) })}
        </button>
      )}
      <ul id={listId} class="sh-schedule__list">{items}</ul>
      <div class="sh-schedule__links">
        <a class="sh-welcome-card__more" href={addBase('/calendar')}>
          {t('welcome.schedule.open_calendar')}<span class="sh-schedule__arrow" aria-hidden="true">→</span>
        </a>
        <a class="sh-welcome-card__more" href={addBase(timetableHref)}>
          {t('welcome.schedule.view_timetable')}<span class="sh-schedule__arrow" aria-hidden="true">→</span>
        </a>
      </div>
    </section>
  )
}
