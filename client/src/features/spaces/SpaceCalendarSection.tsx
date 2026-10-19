/**
 * SpaceCalendarSection — a space's Calendar section (owner decision 9).
 *
 * One control row that fits a phone (in German too): the views
 * **Upcoming** (default) · **Month** · **Week** (wide screens only) as a
 * segmented switch of real buttons (``aria-pressed``), and "New event".
 * ‹ › and Today appear only in Month / Week — Upcoming is a rolling
 * agenda from today with no navigator, ending in "Nothing else in the
 * next 4 weeks · Subscribe from your phone" (the private ICS link).
 *
 * The views are the shared calendar components (``CalendarUpcoming``,
 * ``CalendarGrid``, ``CalendarEventRow``): a multi-day event is one row
 * ("Mon 26 – Sat 31 · 6 days") and one bar in a grid. The space
 * timetable is its own section now (``/spaces/:id/timetable``).
 *
 * A failed read shows an error with Retry, never "no events". Live: a
 * ``calendar.*`` frame for this space refetches while the section is
 * open.
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { ws } from '@/ws'
import { t } from '@/i18n/i18n'
import type { CalendarEvent } from '@/types'
import { currentUser } from '@/store/auth'
import { Button } from '@/components/Button'
import { Modal } from '@/components/Modal'
import { SegmentedSwitch } from '@/components/SegmentedSwitch'
import { SubscribeFeed } from '@/components/SubscribeFeed'
import { LoadErrorState } from '@/components/LoadErrorState'
import { PendingReviewStrip } from '@/components/PendingReviewStrip'
import { openReport } from '@/components/ReportDialog'
import { CalendarEventDialog, openSpaceEventDialog } from '@/components/CalendarEventDialog'
import { CalendarUpcoming } from '@/components/CalendarUpcoming'
import { CalendarGrid } from '@/components/CalendarGrid'
import { useMediaQuery } from '@/hooks/useMediaQuery'
import { advanceDate, formatEventBounds, formatRangeHeading } from '@/utils/calendar'
import {
  CALENDAR_WIDE_QUERY, UPCOMING_WEEKS, gridRange, gridWeeks, upcomingRange, viewOnScreen,
  type CalendarView,
} from '@/utils/calendarViews'
import { currentWeekStart } from '@/utils/week'
import { announceQueued, isQueuedWrite } from '@/utils/contentWrite'
import { pendingDeletes, undoableDelete } from '@/utils/undoableDelete'
import { AccessNote } from './AccessNote'

/** Week needs room for seven readable columns. */
export const WIDE_QUERY = CALENDAR_WIDE_QUERY

const events = signal<CalendarEvent[]>([])
/** The last read failed — an error with Retry, not the empty state. */
const loadFailed = signal(false)
const loaded = signal(false)
const view = signal<CalendarView>('upcoming')
const cursor = signal(new Date())
const subscribeOpen = signal(false)
/** The space whose calendar is on screen (``null`` when none is). */
const shownSpace = signal<string | null>(null)
/** The space the view / cursor belong to: coming back to the same
 *  space's calendar keeps them, another space starts on Upcoming. */
let viewSpace: string | null = null
/** Only the newest read may fill the section (a slow Month answer must
 *  not overwrite the Upcoming the viewer switched back to). */
let readSeq = 0

/** Back to the defaults (a new space, tests). */
export function resetSpaceCalendar(): void {
  viewSpace = null
  events.value = []
  loadFailed.value = false
  loaded.value = false
  view.value = 'upcoming'
  cursor.value = new Date()
  subscribeOpen.value = false
}

const onScreen = viewOnScreen

/** Is the screen wide enough for Week right now (outside a render)? */
function wideNow(): boolean {
  return typeof window.matchMedia === 'function' && window.matchMedia(WIDE_QUERY).matches
}

function rangeFor(v: CalendarView): { start: string; end: string } {
  if (v === 'upcoming') return upcomingRange(new Date())
  return gridRange(gridWeeks(cursor.value, v, currentWeekStart()))
}

async function load(spaceId: string, v: CalendarView) {
  const mine = ++readSeq
  const current = () => mine === readSeq && shownSpace.value === spaceId
  try {
    const rows = await api.get(`/api/spaces/${spaceId}/calendar/events`, rangeFor(v)) as CalendarEvent[]
    if (!current()) return
    events.value = rows
    loadFailed.value = false
    loaded.value = true
  } catch {
    if (!current()) return
    events.value = []
    loadFailed.value = true
    loaded.value = true
  }
}

/** A stored event id from an agenda row's id — the range query expands a
 *  recurring event into ``"{id}@{occurrence}"`` rows; edits and deletes
 *  act on the stored series. */
export function seriesEventId(id: string): string {
  const at = id.indexOf('@')
  return at === -1 ? id : id.slice(0, at)
}

/** Delete a space event with Undo (``undoableDelete``): every row of it
 *  disappears at once, the DELETE goes out when the toast closes. Held
 *  for review (somebody else's event in a "Reviewed" calendar, §4.3): the
 *  event comes back and the toast says it waits for a moderator. A
 *  recurring event goes as a whole series — the toast says so. */
export function deleteSpaceEvent(spaceId: string, ev: CalendarEvent) {
  const id = seriesEventId(ev.id)
  undoableDelete({
    ids: [id],
    message: ev.rrule
      ? t('event.deleted_series', { title: ev.summary })
      : t('event.deleted_named', { title: ev.summary }),
    commit: async ({ keepalive }) => {
      const path = `/api/spaces/${spaceId}/calendar/events/${id}`
      try {
        const res = await (keepalive
          ? api.delete<unknown>(path, { keepalive: true })
          : api.delete<unknown>(path))
        if (isQueuedWrite(res)) announceQueued({ spaceId })
      } catch (err) {
        if ((err as { status?: unknown })?.status !== 404) throw err
      }
      // The Undo window may close after the viewer left this calendar:
      // don't refill the section with this space's events then.
      if (shownSpace.value === spaceId) void load(spaceId, onScreen(view.value, wideNow()))
    },
  })
}

interface Props {
  spaceId: string
  /** The viewer may add events (access level + role). */
  canWrite: boolean
  /** A member in an admin-only calendar: the note instead of New event. */
  adminOnly: boolean
  archived: boolean
}

export function SpaceCalendarSection({ spaceId, canWrite, adminOnly, archived }: Props) {
  const wide = useMediaQuery(WIDE_QUERY)
  // Week on a phone (a rotated tablet, a narrowed window) shows Month.
  const current = onScreen(view.value, wide)

  useEffect(() => {
    if (viewSpace !== spaceId) {
      resetSpaceCalendar()
      viewSpace = spaceId
    }
    shownSpace.value = spaceId
    // Reads the view at frame time (the switch may have moved since).
    const reload = () => { void load(spaceId, onScreen(view.value, wideNow())) }
    const offCreated = ws.on('calendar.created', (e) => {
      const d = e.data as { event?: { calendar_id?: string } }
      if (d.event?.calendar_id === spaceId) reload()
    })
    const offUpdated = ws.on('calendar.updated', (e) => {
      const d = e.data as { event?: { calendar_id?: string } }
      if (d.event?.calendar_id === spaceId) reload()
    })
    const offDeleted = ws.on('calendar.deleted', (e) => {
      const d = e.data as { calendar_id?: string | null }
      if (d.calendar_id === spaceId) reload()
    })
    return () => {
      offCreated(); offUpdated(); offDeleted()
      shownSpace.value = null
    }
  }, [spaceId]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    void load(spaceId, current)
  }, [spaceId, current, cursor.value]) // eslint-disable-line react-hooks/exhaustive-deps

  const views: CalendarView[] = wide ? ['upcoming', 'month', 'week'] : ['upcoming', 'month']
  const hidden = pendingDeletes.value
  const shown = events.value.filter((e) => !hidden.has(seriesEventId(e.id)))
  const now = new Date()
  const weekStart = currentWeekStart()
  const me = currentUser.value?.user_id

  const detail = (e: CalendarEvent) => {
    const bounds = formatEventBounds(e)
    const canDelete = e.can_edit === true && !archived
    const canReport = e.created_by !== me
    return (
      <>
        {e.description && <p class="sh-cal-row__desc">{e.description}</p>}
        <div class="sh-event-times">
          <span>{t('event.starts')} {bounds.starts}</span>
          <span>{t('event.ends')} {bounds.ends}</span>
        </div>
        {(canDelete || canReport) && (
          <div class="sh-event-admin sh-row">
            {canDelete && (
              <Button variant="danger" onClick={() => deleteSpaceEvent(spaceId, e)}>
                {t('event.delete')}
              </Button>
            )}
            {canReport && (
              <Button variant="ghost" onClick={() => openReport('calendar_event', e.id, spaceId)}>
                {t('report.action')}
              </Button>
            )}
          </div>
        )}
      </>
    )
  }

  const subscribeLink = (
    <button type="button" class="sh-link-button" onClick={() => { subscribeOpen.value = true }}>
      {t('calendar.upcoming.subscribe')}
    </button>
  )

  return (
    <div class="sh-calendar sh-space-calendar">
      <div class="sh-space-cal-bar">
        <SegmentedSwitch<CalendarView>
          class="sh-space-cal-bar__views"
          options={views}
          value={current}
          labels={{
            upcoming: t('calendar.upcoming'),
            month: t('calendar.month'),
            week: t('calendar.week'),
          }}
          ariaLabel={t('calendar.views_aria')}
          onChange={(v) => { view.value = v }}
        />
        {canWrite && (
          <Button onClick={() => openSpaceEventDialog(spaceId)}>
            + {t('calendar.new_event')}
          </Button>
        )}
      </div>
      {adminOnly && <AccessNote feature="calendar" />}
      <PendingReviewStrip spaceId={spaceId} feature="calendar" />

      {current !== 'upcoming' && (
        <div class="sh-space-cal-nav">
          {/* ‹ › are bidi-mirrored characters: RTL turns them by itself. */}
          <Button variant="secondary"
                  // Keys: calendar.prev_{month,week}
                  aria-label={t(`calendar.prev_${current}`)}
                  onClick={() => { cursor.value = advanceDate(cursor.value, -1, current) }}>
            <span aria-hidden="true">&#8249;</span>
          </Button>
          <h2 class="sh-space-cal-nav__heading">
            {formatRangeHeading(cursor.value, current, weekStart)}
          </h2>
          <Button variant="secondary"
                  // Keys: calendar.next_{month,week}
                  aria-label={t(`calendar.next_${current}`)}
                  onClick={() => { cursor.value = advanceDate(cursor.value, 1, current) }}>
            <span aria-hidden="true">&#8250;</span>
          </Button>
          <Button variant="secondary" onClick={() => { cursor.value = new Date() }}>
            {t('calendar.today')}
          </Button>
        </div>
      )}

      {loadFailed.value && (
        <LoadErrorState
          headingLevel={2}
          message={t('calendar.load_failed')}
          onRetry={() => { void load(spaceId, current) }}
        />
      )}

      {!loadFailed.value && loaded.value && current === 'upcoming' && (
        <CalendarUpcoming
          events={shown}
          now={now}
          weekStart={weekStart}
          renderDetail={detail}
          empty={(
            <div class="sh-empty-state">
              <div aria-hidden="true">📅</div>
              <h2>{t('calendar.upcoming.empty', { weeks: String(UPCOMING_WEEKS) })}</h2>
              {canWrite && <p>{t('calendar.empty_space_hint')}</p>}
            </div>
          )}
          footer={(
            <>
              {shown.length > 0 && (
                <span>{t('calendar.upcoming.end', { weeks: String(UPCOMING_WEEKS) })} · </span>
              )}
              {subscribeLink}
            </>
          )}
        />
      )}

      {!loadFailed.value && loaded.value && current !== 'upcoming' && (
        <>
          <CalendarGrid
            view={current}
            cursor={cursor.value}
            events={shown}
            weekStart={weekStart}
            now={now}
            renderDetail={detail}
            maxLanes={wide ? 3 : 2}
          />
          <p class="sh-cal-upcoming__end">{subscribeLink}</p>
        </>
      )}

      <Modal
        open={subscribeOpen.value}
        onClose={() => { subscribeOpen.value = false }}
        title={t('event.subscribe.heading')}
      >
        <SubscribeFeed spaceId={spaceId} />
      </Modal>
      <CalendarEventDialog onCreated={() => { void load(spaceId, current) }} />
    </div>
  )
}
