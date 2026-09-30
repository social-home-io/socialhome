/**
 * HighlightArchiveTab — month-grid browser for retention-window highlights.
 *
 * The Highlights inbox tab shows today's rings + recent items, but
 * highlights live up to the author's retention setting (default 30
 * days) — there's a wide window of past posts the inbox never
 * surfaces. This tab renders a calendar where each day with a
 * visible highlight is clickable; the day panel below the grid lists
 * every author + first-frame thumb for that date so the user can
 * replay it via the existing viewer.
 *
 * Data path — reuse the existing ``GET /api/highlights`` endpoint. The
 * server's ``list_visible_to`` already returns every highlight whose
 * ``expires_at`` is in the future, i.e. the full retention window.
 * Group + sort client-side; no new endpoint, no schema change.
 */
import { useEffect, useMemo, useState } from 'preact/hooks'
import { signal, computed } from '@preact/signals'
import { useLocation } from 'preact-iso'
import { api } from '@/api'
import { Avatar } from '@/components/Avatar'
import { Button } from '@/components/Button'
import { CalendarSkeleton } from '@/components/Skeleton'
import { showToast } from '@/components/Toast'
import { resolveDisplayName } from '@/utils/avatar'
import { currentWeekStart, weekdayOrder, type WeekStart } from '@/utils/week'
import { ws } from '@/ws'
import type { HighlightInboxItem } from '@/types'


type DayKey = string  // 'YYYY-MM-DD'

const inbox = signal<HighlightInboxItem[]>([])
const loading = signal<boolean>(true)
const loadError = signal<string | null>(null)

/** Group every loaded highlight by ``highlight_date`` so the grid can ask
 *  ``byDay.get('2026-05-04')`` in O(1). Recomputed when inbox flips. */
const byDay = computed<Map<DayKey, HighlightInboxItem[]>>(() => {
  const m = new Map<DayKey, HighlightInboxItem[]>()
  for (const s of inbox.value) {
    const k = s.highlight.highlight_date
    if (!m.has(k)) m.set(k, [])
    m.get(k)!.push(s)
  }
  return m
})


function ymd(d: Date): DayKey {
  return d.toISOString().slice(0, 10)
}

function startOfMonth(year: number, month0: number): Date {
  return new Date(Date.UTC(year, month0, 1))
}

/** Localised short weekday label for ISO weekday ``iso`` (Mon = 0).
 *  2024-01-01 was a Monday. */
function dowLabel(iso: number): string {
  return new Date(Date.UTC(2024, 0, 1 + iso)).toLocaleDateString(
    undefined, { weekday: 'short', timeZone: 'UTC' },
  )
}

type GridCell = { date: DayKey; inMonth: boolean } | null

/** Header labels + day cells for the month grid, ordered to open on
 *  ``ws``. Leading blanks align the 1st under its weekday column;
 *  trailing blanks pad to whole weeks (at least 5 rows) so the layout
 *  stays stable and a 6-row month is never clipped. */
export function buildMonthGrid(
  year: number, month0: number, weekStart: WeekStart,
): { weekdayLabels: string[]; cells: GridCell[] } {
  const order = weekdayOrder(weekStart)
  // ``getUTCDay()`` is 0 (Sun) ‥ 6 (Sat); shift to ISO Mon = 0.
  const firstIso = (startOfMonth(year, month0).getUTCDay() + 6) % 7
  const lead = order.indexOf(firstIso)
  const daysInMonth = new Date(Date.UTC(year, month0 + 1, 0)).getUTCDate()
  const cells: GridCell[] = []
  for (let i = 0; i < lead; i++) cells.push(null)
  for (let d = 1; d <= daysInMonth; d++) {
    cells.push({
      date: ymd(new Date(Date.UTC(year, month0, d))),
      inMonth: true,
    })
  }
  while (cells.length < 35 || cells.length % 7 !== 0) cells.push(null)
  return { weekdayLabels: order.map(dowLabel), cells }
}


export default function HighlightArchiveTab() {
  const loc = useLocation()
  const today = useMemo(() => new Date(), [])
  const [year, setYear] = useState(today.getUTCFullYear())
  const [month, setMonth] = useState(today.getUTCMonth())  // 0-11
  const [selectedDay, setSelectedDay] = useState<DayKey | null>(null)

  useEffect(() => {
    loading.value = true
    loadError.value = null
    const fetchInbox = (initial: boolean) =>
      api.get('/api/highlights')
        .then((rows: HighlightInboxItem[]) => {
          inbox.value = rows ?? []
          if (initial) loading.value = false
        })
        .catch((err: unknown) => {
          loadError.value = (err as Error)?.message ?? 'Could not load'
          if (initial) loading.value = false
          showToast(`Failed to load highlight archive: ${loadError.value}`, 'error')
        })
    void fetchInbox(true)
    const dispose = [
      ws.on('highlight.frame_added',   () => { void fetchInbox(false) }),
      ws.on('highlight.frame_removed', () => { void fetchInbox(false) }),
      ws.on('highlight.removed',       () => { void fetchInbox(false) }),
    ]
    return () => { dispose.forEach(d => d()) }
  }, [])

  if (loading.value) return <CalendarSkeleton />

  // Month grid opening on the user's first day of the week (the same
  // ``week_start`` preference /calendar honours).
  const monthStart = startOfMonth(year, month)
  const { weekdayLabels, cells } = buildMonthGrid(year, month, currentWeekStart())

  const monthLabel = monthStart.toLocaleDateString(undefined, {
    year:  'numeric',
    month: 'long',
  })

  const goPrev = () => {
    setSelectedDay(null)
    if (month === 0) { setYear(year - 1); setMonth(11) }
    else setMonth(month - 1)
  }
  const goNext = () => {
    setSelectedDay(null)
    if (month === 11) { setYear(year + 1); setMonth(0) }
    else setMonth(month + 1)
  }

  // Earliest highlight still in the retention window — used to mute the
  // header when the rendered month falls entirely below it.
  const oldestHighlightDate = inbox.value.length > 0
    ? inbox.value
        .map(s => s.highlight.highlight_date)
        .reduce((a, b) => (a < b ? a : b))
    : null
  const monthEndKey = ymd(new Date(Date.UTC(year, month + 1, 0)))
  const monthEntirelyBeforeRetention = oldestHighlightDate !== null
    && monthEndKey < oldestHighlightDate

  const selectedHighlights = selectedDay
    ? (byDay.value.get(selectedDay) ?? [])
    : []

  return (
    <div class="sh-highlight-archive">
      <header class="sh-highlight-archive-header">
        <Button variant="secondary" onClick={goPrev}>‹ Prev</Button>
        <h2 style={{ margin: 0 }}>{monthLabel}</h2>
        <Button variant="secondary" onClick={goNext}>Next ›</Button>
      </header>

      {monthEntirelyBeforeRetention && (
        <p class="sh-muted" style={{ marginTop: 0 }}>
          No highlights before {oldestHighlightDate} — older highlights are pruned by the
          author's retention setting.
        </p>
      )}

      <div class="sh-highlight-archive-grid" role="grid" aria-label={monthLabel}>
        {weekdayLabels.map((d, i) => (
          <div key={i} class="sh-highlight-archive-dow" aria-hidden="true">{d}</div>
        ))}
        {cells.map((cell, i) => {
          if (cell === null) {
            return <div key={`pad-${i}`} class="sh-highlight-archive-day sh-highlight-archive-day--pad" />
          }
          const highlights = byDay.value.get(cell.date) ?? []
          const hasHighlights = highlights.length > 0
          const dayNum = Number(cell.date.slice(8, 10))
          const isToday = cell.date === ymd(today)
          const isSelected = cell.date === selectedDay
          const cls = [
            'sh-highlight-archive-day',
            hasHighlights && 'sh-highlight-archive-day--has',
            isToday && 'sh-highlight-archive-day--today',
            isSelected && 'sh-highlight-archive-day--selected',
          ].filter(Boolean).join(' ')

          if (!hasHighlights) {
            return (
              <div key={cell.date} class={cls}>
                <span class="sh-highlight-archive-day-num">{dayNum}</span>
              </div>
            )
          }

          // Pick the most-recent highlight's first frame as the cell
          // thumbnail, with a small chip when the day has more than
          // one highlight.
          const firstFrame = highlights[0].frames[0]
          const extraCount = highlights.length - 1
          return (
            <button
              key={cell.date}
              type="button"
              class={cls}
              onClick={() => setSelectedDay(cell.date)}
              aria-label={`${highlights.length} ${highlights.length === 1 ? 'highlight' : 'highlights'} on ${cell.date}`}
            >
              <span class="sh-highlight-archive-day-num">{dayNum}</span>
              {firstFrame && firstFrame.frame_type === 'image' && (
                <img
                  src={firstFrame.media_url}
                  alt=""
                  loading="lazy"
                  class="sh-highlight-archive-day-thumb"
                />
              )}
              {firstFrame && firstFrame.frame_type === 'video' && (
                <span class="sh-highlight-archive-day-thumb sh-highlight-archive-day-thumb--video">
                  🎬
                </span>
              )}
              {extraCount > 0 && (
                <span class="sh-highlight-archive-day-more">+{extraCount}</span>
              )}
            </button>
          )
        })}
      </div>

      {selectedDay && (
        <section
          class="sh-highlight-archive-day-panel"
          aria-label={`Highlights from ${selectedDay}`}
        >
          <h3 style={{ margin: 0 }}>
            {new Date(selectedDay + 'T00:00:00Z').toLocaleDateString(undefined, {
              weekday: 'long', month: 'short', day: 'numeric', year: 'numeric',
            })}
          </h3>
          <ul class="sh-highlight-archive-day-list">
            {selectedHighlights.map(s => {
              const author = s.highlight.author_user_id
              const name = resolveDisplayName(null, author, author)
              const first = s.frames[0]
              return (
                <li key={s.highlight.id}>
                  <a
                    href={`/highlights/${s.highlight.id}`}
                    class="sh-highlight-archive-tile"
                    onClick={() => loc.route(`/highlights/${s.highlight.id}`)}
                  >
                    {first && first.frame_type === 'image' && (
                      <img
                        src={first.media_url}
                        alt=""
                        loading="lazy"
                        class="sh-highlight-archive-tile-thumb"
                      />
                    )}
                    {first && first.frame_type === 'video' && (
                      <span class="sh-highlight-archive-tile-thumb sh-highlight-archive-tile-thumb--video">
                        🎬
                      </span>
                    )}
                    <div class="sh-highlight-archive-tile-meta">
                      <Avatar name={name} size={32} />
                      <strong>{name}</strong>
                      <span class="sh-muted">
                        {s.frames.length} frame{s.frames.length === 1 ? '' : 's'}
                      </span>
                    </div>
                  </a>
                </li>
              )
            })}
          </ul>
        </section>
      )}

      {inbox.value.length === 0 && !loading.value && (
        <div class="sh-empty-state">
          <div aria-hidden="true">🌅</div>
          <h3>No highlights in the archive yet</h3>
          <p>
            Once you or anyone in your household / connected peers post a
            highlight, it'll show up here for as long as the author's retention
            setting keeps it.
          </p>
        </div>
      )}
    </div>
  )
}
