import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'

const { apiMock } = vi.hoisted(() => ({
  apiMock: { get: vi.fn(), post: vi.fn() },
}))

vi.mock('@/api', () => ({ api: apiMock }))

vi.mock('@/components/Toast', () => ({
  showToast: vi.fn(),
}))

vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'me', is_admin: false } },
}))

import { EventPostCard } from './EventPostCard'
import { rsvpCounts, myRsvpStatus } from '@/store/calendar'

beforeEach(() => {
  apiMock.get.mockReset()
  apiMock.post.mockReset()
  rsvpCounts.value = {}
  myRsvpStatus.value = {}
})

const futureEvent = {
  id: 'ev-1',
  calendar_id: 'sp-1',
  summary: 'Friday party',
  description: null,
  start: new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString(),
  end: new Date(Date.now() + 26 * 60 * 60 * 1000).toISOString(),
  all_day: false,
  created_by: 'someone-else',
  capacity: null as number | null,
}

describe('EventPostCard', () => {
  it('renders an orphan card when eventId is null', () => {
    const { container } = render(<EventPostCard eventId={null} />)
    // Copy softened from "Event removed" to "This event isn't here
    // anymore" — the bare "removed" word read like an error state.
    expect(container.textContent).toContain("isn't here")
  })

  it('renders the summary + RSVP buttons after fetching', async () => {
    apiMock.get.mockResolvedValueOnce(futureEvent)
    const { container, findByText } = render(<EventPostCard eventId="ev-1" />)
    await findByText('Going')
    expect(apiMock.get).toHaveBeenCalledWith('/api/calendars/events/ev-1')
    expect(container.textContent).toContain('Going')
    expect(container.textContent).toContain('Maybe')
    expect(container.textContent).toContain("Can't make it")
  })

  it('switches the going button copy to "Request to join" when capacity is set', async () => {
    apiMock.get.mockResolvedValueOnce({
      ...futureEvent,
      id: 'ev-cap',
      capacity: 5,
    })
    const { container, findByText } = render(<EventPostCard eventId="ev-cap" />)
    // Wait for any RSVP button to render before checking for the
    // request-to-join copy (which is also a button label, just split
    // across an emoji span + the label text).
    await findByText('Maybe')
    expect(container.textContent).toContain('Request to join')
  })

  it('disables RSVP buttons when the event has ended', async () => {
    const past = {
      ...futureEvent,
      start: new Date(Date.now() - 2 * 3600_000).toISOString(),
      end: new Date(Date.now() - 1 * 3600_000).toISOString(),
    }
    apiMock.get.mockResolvedValueOnce({ ...past, id: 'ev-past' })
    const { container, findByText } = render(<EventPostCard eventId="ev-past" />)
    await findByText('Maybe')
    expect(container.textContent).toContain('This event has ended')
    const buttons = container.querySelectorAll('button')
    let disabledCount = 0
    buttons.forEach((b) => {
      if (b.disabled) disabledCount++
    })
    expect(disabledCount).toBeGreaterThan(0)
  })

  it('renders the event title as an in-card headline', async () => {
    apiMock.get.mockResolvedValueOnce({
      ...futureEvent,
      id: 'ev-title',
      summary: 'Annual block party',
    })
    const { container, findByText } = render(<EventPostCard eventId="ev-title" />)
    await findByText('Going')
    const title = container.querySelector('.sh-event-card-title')
    expect(title?.textContent).toBe('Annual block party')
  })

  it('renders the description through markdown rendering', async () => {
    apiMock.get.mockResolvedValueOnce({
      ...futureEvent,
      id: 'ev-desc',
      description: 'Bring a **side dish** or drinks.',
    })
    const { container, findByText } = render(<EventPostCard eventId="ev-desc" />)
    await findByText('Going')
    const desc = container.querySelector('.sh-event-card-description')
    // Markdown rendered into innerHTML — the strong wrapper proves
    // PostBody-equivalent rendering ran (vs plain text fallback).
    expect(desc?.innerHTML).toContain('<strong>side dish</strong>')
  })

  it('shows the end time on the dateline (range, not just start)', async () => {
    apiMock.get.mockResolvedValueOnce(futureEvent)
    const { container, findByText } = render(<EventPostCard eventId="ev-1" />)
    await findByText('Going')
    const when = container.querySelector('.sh-event-card-when-text')
    // Range separator (en-dash with surrounding spaces) is what
    // ``EventWhen`` joins start + end with.
    expect(when?.textContent).toContain(' – ')
  })

  it('qualifies the end time with its date for an overnight event', async () => {
    // Start/end offsets picked so the span always crosses a calendar
    // day boundary regardless of what hour the test runs at (48h out,
    // 26h long) — mirrors the "different offsets = different day"
    // guard the all-day branch already has.
    const start = new Date(Date.now() + 48 * 60 * 60 * 1000)
    const end = new Date(start.getTime() + 26 * 60 * 60 * 1000)
    const overnight = {
      ...futureEvent,
      id: 'ev-overnight',
      tz: 'UTC',
      start: start.toISOString(),
      end: end.toISOString(),
    }
    apiMock.get.mockResolvedValueOnce(overnight)
    const { container, findByText } = render(
      <EventPostCard eventId="ev-overnight" />
    )
    await findByText('Going')
    const when = container.querySelector('.sh-event-card-when-text')
    const dateOpts: Intl.DateTimeFormatOptions = {
      timeZone: 'UTC',
      weekday: 'short',
      month: 'short',
      day: 'numeric',
    }
    const startDateStr = start.toLocaleDateString(undefined, dateOpts)
    const endDateStr = end.toLocaleDateString(undefined, dateOpts)
    expect(startDateStr).not.toBe(endDateStr)
    expect(when?.textContent).toContain(startDateStr)
    expect(when?.textContent).toContain(endDateStr)
  })

  it('anchors an all-day date on the event tz, not UTC', async () => {
    // Regression: an all-day event is stored as 00:00 / 23:59 in the
    // event's OWN tz (CalendarEventDialog), so a Zurich household's
    // "1 May" lands at 2026-04-30T22:00Z. Formatting that instant in
    // UTC printed the day before AND made the single day look like a
    // two-day range ("Apr 30 – May 1").
    const zurich = 'Europe/Zurich'
    // 22:00Z is 00:00 the next day in Zurich only under CEST (UTC+2),
    // so pin the fixture to 1 July of next year — always in the future
    // and never across a DST switch, whatever day the suite runs.
    const start = new Date(
      Date.UTC(new Date().getUTCFullYear() + 1, 6, 1, 22, 0, 0, 0),
    )
    const end = new Date(start.getTime() + (23 * 60 + 59) * 60 * 1000)
    const dateOpts: Intl.DateTimeFormatOptions = {
      timeZone: zurich, weekday: 'short', month: 'short', day: 'numeric',
    }
    const authoredDay = start.toLocaleDateString(undefined, dateOpts)
    // Guard the fixture's premise rather than trusting it silently.
    expect(end.toLocaleDateString(undefined, dateOpts)).toBe(authoredDay)
    const utcDay = start.toLocaleDateString(undefined, {
      ...dateOpts, timeZone: 'UTC',
    })
    expect(utcDay).not.toBe(authoredDay)

    apiMock.get.mockResolvedValueOnce({
      ...futureEvent,
      id: 'ev-allday',
      all_day: true,
      tz: zurich,
      start: start.toISOString(),
      end: end.toISOString(),
    })
    const { container, findByText } = render(
      <EventPostCard eventId="ev-allday" />,
    )
    await findByText('Going')
    const when = container.querySelector('.sh-event-card-when-text')
    expect(when?.textContent).toBe(authoredDay)
  })

  it('treats an exclusive 00:00 all-day end as the previous day', async () => {
    // ICS-imported all-day rows carry an EXCLUSIVE midnight end bound,
    // so a 10–12 September event arrives as ``…-13T00:00:00Z``. The
    // agenda row and the expanded detail already subtract the last
    // millisecond; the card printed "Sep 10 – Sep 13".
    const start = new Date(Date.now() + 30 * 24 * 60 * 60 * 1000)
    start.setUTCHours(0, 0, 0, 0)
    const end = new Date(start.getTime() + 3 * 24 * 60 * 60 * 1000)
    const dateOpts: Intl.DateTimeFormatOptions = {
      timeZone: 'UTC', weekday: 'short', month: 'short', day: 'numeric',
    }
    const firstDay = start.toLocaleDateString(undefined, dateOpts)
    const lastDay = new Date(end.getTime() - 1)
      .toLocaleDateString(undefined, dateOpts)
    const exclusiveDay = end.toLocaleDateString(undefined, dateOpts)
    // Guard the fixture's premise rather than trusting it silently.
    expect(lastDay).not.toBe(exclusiveDay)

    apiMock.get.mockResolvedValueOnce({
      ...futureEvent,
      id: 'ev-ics-allday',
      all_day: true,
      tz: 'UTC',
      start: start.toISOString(),
      end: end.toISOString(),
    })
    const { container, findByText } = render(
      <EventPostCard eventId="ev-ics-allday" />,
    )
    await findByText('Going')
    const when = container.querySelector('.sh-event-card-when-text')
    expect(when?.textContent).toBe(`${firstDay} – ${lastDay}`)
  })

  it('keeps rendering when the event carries a malformed tz', async () => {
    // A peer / ICS import can put anything in ``event.tz``; Intl throws
    // ``RangeError`` on an unknown zone, which used to blank the card.
    apiMock.get.mockResolvedValueOnce({
      ...futureEvent,
      id: 'ev-bad-tz',
      tz: 'Foo/Bar',
    })
    const { container, findByText } = render(
      <EventPostCard eventId="ev-bad-tz" />,
    )
    await findByText('Going')
    const when = container.querySelector('.sh-event-card-when-text')
    expect(when?.textContent).toBeTruthy()
    expect(when?.textContent).not.toContain('Invalid')
  })

  it('renders attendance summary for uncapped events with responses', async () => {
    apiMock.get.mockResolvedValueOnce(futureEvent)
    rsvpCounts.value = { 'ev-1': { going: 7, maybe: 2, declined: 0 } }
    const { container, findByText } = render(<EventPostCard eventId="ev-1" />)
    await findByText('Going')
    const att = container.querySelector('.sh-event-card-attendance')
    expect(att?.textContent).toBe('7 going · 2 maybe')
  })

  it('suppresses the redundant "You\'re going" pill', async () => {
    apiMock.get.mockResolvedValueOnce({ ...futureEvent, id: 'ev-going' })
    myRsvpStatus.value = { 'ev-going': 'going' }
    const { container, findByText } = render(<EventPostCard eventId="ev-going" />)
    await findByText('Going')
    expect(container.querySelector('.sh-event-card-pill')).toBeNull()
  })

  it('keeps the pill for the informative waitlist case', async () => {
    apiMock.get.mockResolvedValueOnce({
      ...futureEvent,
      id: 'ev-waitlist',
      capacity: 4,
    })
    rsvpCounts.value = {
      'ev-waitlist': { going: 4, maybe: 0, declined: 0, waitlist: 3 },
    }
    myRsvpStatus.value = { 'ev-waitlist': 'waitlist' }
    const { container, findByText } = render(<EventPostCard eventId="ev-waitlist" />)
    await findByText('Maybe')
    expect(container.querySelector('.sh-event-card-pill')).not.toBeNull()
  })

  it('promotes "Add to my calendar" onto the RSVP row (out of the kebab)', async () => {
    apiMock.get.mockResolvedValueOnce(futureEvent)
    const { container, findByText } = render(<EventPostCard eventId="ev-1" />)
    await findByText('Going')
    const ics = container.querySelector('a.sh-event-card-ics')
    expect(ics).not.toBeNull()
    expect(ics?.getAttribute('href')).toContain(
      'api/calendars/events/ev-1/export.ics',
    )
  })

  it('POSTs to the rsvp endpoint when a button is clicked', async () => {
    apiMock.get.mockResolvedValueOnce(futureEvent)
    apiMock.post.mockResolvedValueOnce({ ok: true })
    const { findByText } = render(<EventPostCard eventId="ev-1" />)
    const goingBtn = await findByText('Going')
    fireEvent.click(goingBtn.closest('button')!)
    // microtask boundary
    await Promise.resolve()
    expect(apiMock.post).toHaveBeenCalledWith(
      '/api/calendars/events/ev-1/rsvp',
      expect.objectContaining({ status: 'going' }),
    )
  })
})
