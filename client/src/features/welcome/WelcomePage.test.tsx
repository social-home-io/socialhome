import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, waitFor } from '@testing-library/preact'

vi.mock('@/api', () => {
  const m = { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() }
  return { api: m, _mock: m }
})

const wsTypes: string[] = []
vi.mock('@/ws', () => ({
  ws: { on: (type: string) => { wsTypes.push(type); return () => {} } },
}))

vi.mock('@/components/SkeletonScreen', () => ({
  CardSkeleton: () => <div class="skel" />,
}))

vi.mock('@/components/Avatar', () => ({
  Avatar: ({ name }: { name: string }) => <span>{name}</span>,
}))

vi.mock('@/store/auth', () => ({
  currentUser: { value: { display_name: 'Pascal Vizeli' } },
}))

vi.mock('@/store/pageTitle', () => ({
  useTitle: () => {},
}))

import WelcomePage from './WelcomePage'
import { api } from '@/api'

const apiMock = api as unknown as { get: ReturnType<typeof vi.fn> }

interface BundleOver {
  unread_notifications?: number
  unread_conversations?: number
  upcoming_events?: unknown[]
  tasks_due_today?: unknown[]
  followed_spaces_feed?: unknown[]
  today_timetable?: unknown[]
  today_events?: unknown[]
}
function bundle(over: BundleOver = {}) {
  return {
    unread_notifications: over.unread_notifications ?? 0,
    unread_conversations: over.unread_conversations ?? 0,
    upcoming_events: over.upcoming_events ?? [],
    tasks_due_today: over.tasks_due_today ?? [],
    followed_spaces_feed: over.followed_spaces_feed ?? [],
    today_timetable: over.today_timetable ?? [],
    today_events: over.today_events ?? [],
  }
}

/** A timetable with one lesson later today (local time). */
function todayTimetable(title = 'Mathe') {
  const start = new Date()
  start.setHours(23, 0, 0, 0)
  const end = new Date(start)
  end.setMinutes(45)
  return {
    timetable_id: 'tt-emma', name: 'Emma', color: 'sky', tz: 'UTC', date: '2026-09-28',
    lessons: [{
      source_id: 'e1', date: '2026-09-28', start: '23:00', end: '23:45', kind: 'lesson',
      label: '1.', title, room: '204', teacher: null, note: null, color: null, icon: null,
      status: 'normal', override_id: null, original: null,
      start_at: start.toISOString(), end_at: end.toISOString(),
    }],
  }
}

describe('WelcomePage', () => {
  beforeEach(() => {
    apiMock.get.mockReset()
  })

  it('refreshes on the frame names the server actually emits', async () => {
    wsTypes.length = 0
    apiMock.get.mockResolvedValueOnce(bundle())
    render(<WelcomePage />)
    await waitFor(() => expect(wsTypes.length).toBeGreaterThan(0))
    // RealtimeService emits ``notification.new`` / ``notification.unread_count``
    // and ``calendar.created|updated|deleted`` — the old
    // ``notification.created`` / ``calendar.event.*`` names never fire.
    expect(wsTypes).toEqual(expect.arrayContaining([
      'notification.new', 'notification.unread_count',
      'calendar.created', 'calendar.updated', 'calendar.deleted',
    ]))
    expect(wsTypes).toEqual(expect.arrayContaining([
      'timetable.changed', 'timetable.deleted',
    ]))
    expect(wsTypes).not.toContain('notification.created')
    expect(wsTypes).not.toContain('notification.read_changed')
    expect(wsTypes.filter(t => t.startsWith('calendar.event.'))).toEqual([])
  })

  it('greets the user by first name', async () => {
    apiMock.get.mockResolvedValueOnce(bundle())
    const { container } = render(<WelcomePage />)
    await waitFor(() => {
      expect(container.querySelector('.sh-welcome-hero')).not.toBeNull()
    })
    // First-name slice — "Pascal Vizeli" → "Pascal".
    expect(container.textContent).toContain('Pascal')
    expect(container.textContent).not.toContain('Vizeli')
  })

  it('renders all-clear when nothing is on', async () => {
    apiMock.get.mockResolvedValueOnce(bundle())
    const { container } = render(<WelcomePage />)
    await waitFor(() => {
      expect(container.querySelector('.sh-welcome-allclear')).not.toBeNull()
    })
    expect(container.textContent).toContain('All clear')
  })

  it('shows today\'s events when at least one starts today', async () => {
    const now = new Date()
    now.setHours(15, 0, 0, 0)
    apiMock.get.mockResolvedValueOnce(bundle({
      upcoming_events: [{
        id: 'e1', summary: 'Tea with Lina',
        start: now.toISOString(), end: now.toISOString(),
        all_day: false,
      }],
    }))
    const { container } = render(<WelcomePage />)
    await waitFor(() => {
      expect(container.textContent).toContain('Tea with Lina')
    })
    // Today card title contains "Today" (not "Up next") when events
    // are scoped to the current day.
    const titles = [...container.querySelectorAll('.sh-welcome-card__title')]
      .map(el => el.textContent ?? '')
    expect(titles.some(t => t.includes('Today'))).toBe(true)
    expect(titles.some(t => t.includes('Up next'))).toBe(false)
  })

  it('falls back to "Up next" when nothing is today but the calendar has future events', async () => {
    const inThreeDays = new Date()
    inThreeDays.setDate(inThreeDays.getDate() + 3)
    apiMock.get.mockResolvedValueOnce(bundle({
      upcoming_events: [{
        id: 'e2', summary: 'Dinner reservation',
        start: inThreeDays.toISOString(),
        end: inThreeDays.toISOString(),
        all_day: false,
      }],
    }))
    const { container } = render(<WelcomePage />)
    await waitFor(() => {
      expect(container.textContent).toContain('Dinner reservation')
    })
    const titles = [...container.querySelectorAll('.sh-welcome-card__title')]
      .map(el => el.textContent ?? '')
    expect(titles.some(t => t.includes('Up next'))).toBe(true)
    expect(titles.some(t => t.includes('Today'))).toBe(false)
  })

  it('renders the catch-up card with chips for unread DMs and alerts', async () => {
    apiMock.get.mockResolvedValueOnce(bundle({
      unread_notifications: 5,
      unread_conversations: 2,
    }))
    const { container } = render(<WelcomePage />)
    await waitFor(() => {
      expect(container.querySelector('.sh-welcome-card--catchup')).not.toBeNull()
    })
    // Chips render with the count + the noun ("messages" / "alerts").
    expect(container.textContent).toContain('5')
    expect(container.textContent).toContain('alerts')
    expect(container.textContent).toContain('2')
    expect(container.textContent).toContain('messages')
  })

  it('renders pending tasks with overdue chip when due_date is in the past', async () => {
    const yesterday = new Date()
    yesterday.setDate(yesterday.getDate() - 1)
    const dueIso = yesterday.toISOString().slice(0, 10)
    apiMock.get.mockResolvedValueOnce(bundle({
      tasks_due_today: [
        { id: 't1', list_id: 'l1', title: 'Pay electric bill',
          status: 'todo', due_date: dueIso },
      ],
    }))
    const { container } = render(<WelcomePage />)
    await waitFor(() => {
      expect(container.textContent).toContain('Pay electric bill')
    })
    expect(container.querySelector('.sh-welcome-card__chip--overdue')).not.toBeNull()
  })

  it('replaces the Today card with the merged schedule when a timetable is on today', async () => {
    const morning = new Date()
    morning.setHours(0, 5, 0, 0)
    const morningEnd = new Date(morning)
    morningEnd.setMinutes(30)
    apiMock.get.mockResolvedValueOnce(bundle({
      today_timetable: [todayTimetable()],
      // Already over — only ``today_events`` still carries it.
      today_events: [{
        id: 'ev1', summary: 'Early swim', start: morning.toISOString(),
        end: morningEnd.toISOString(), all_day: false,
      }],
      upcoming_events: [],
    }))
    const { container } = render(<WelcomePage />)
    await waitFor(() => {
      expect(container.querySelector('.sh-welcome-card--schedule')).not.toBeNull()
    })
    expect(container.textContent).toContain('Mathe')
    expect(container.textContent).toContain('Early swim')
    // TodayCard is a link card; the merged card is not.
    expect(container.querySelector('a.sh-welcome-card')).toBeNull()
    expect(container.querySelector('.sh-welcome-allclear')).toBeNull()
    expect(container.querySelector('.sh-welcome-hero__sub')?.textContent)
      .toContain('1 lesson · 1 event')
  })

  it('keeps the Today card when no timetable is on today', async () => {
    const now = new Date()
    now.setHours(15, 0, 0, 0)
    const ev = { id: 'e1', summary: 'Tea', start: now.toISOString(), end: now.toISOString(), all_day: false }
    apiMock.get.mockResolvedValueOnce(bundle({ upcoming_events: [ev], today_events: [ev] }))
    const { container } = render(<WelcomePage />)
    await waitFor(() => expect(container.textContent).toContain('Tea'))
    expect(container.querySelector('.sh-welcome-card--schedule')).toBeNull()
    expect(container.querySelector('a.sh-welcome-card[href="/calendar"]')).not.toBeNull()
  })

  it('a timetable day is not "all clear"', async () => {
    apiMock.get.mockResolvedValueOnce(bundle({ today_timetable: [todayTimetable()] }))
    const { container } = render(<WelcomePage />)
    await waitFor(() => {
      expect(container.querySelector('.sh-welcome-card--schedule')).not.toBeNull()
    })
    expect(container.querySelector('.sh-welcome-allclear')).toBeNull()
    expect(container.querySelector('.sh-welcome-hero__sub')?.textContent).toContain('1 lesson')
  })

  it('a timetable with only untitled slots still leaves the day all clear', async () => {
    apiMock.get.mockResolvedValueOnce(bundle({ today_timetable: [todayTimetable('')] }))
    const { container } = render(<WelcomePage />)
    await waitFor(() => {
      expect(container.querySelector('.sh-welcome-allclear')).not.toBeNull()
    })
  })

  it('keeps "Up next" on a school day without calendar events today', async () => {
    const inThreeDays = new Date()
    inThreeDays.setDate(inThreeDays.getDate() + 3)
    apiMock.get.mockResolvedValueOnce(bundle({
      today_timetable: [todayTimetable()],
      today_events: [],
      upcoming_events: [{
        id: 'e2', summary: 'Dinner reservation', start: inThreeDays.toISOString(),
        end: inThreeDays.toISOString(), all_day: false,
      }],
    }))
    const { container } = render(<WelcomePage />)
    await waitFor(() => {
      expect(container.querySelector('.sh-welcome-card--schedule')).not.toBeNull()
    })
    const titles = [...container.querySelectorAll('.sh-welcome-card__title')].map(el => el.textContent ?? '')
    expect(titles.some(t => t.includes('Up next'))).toBe(true)
    expect(container.textContent).toContain('Dinner reservation')
  })
})
