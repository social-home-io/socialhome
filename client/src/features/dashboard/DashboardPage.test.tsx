import { describe, it, expect, vi } from 'vitest'

// Mock the API module before importing the page
vi.mock('@/api', () => ({
  api: {
    get: vi.fn().mockResolvedValue([]),
    post: vi.fn().mockResolvedValue({}),
    patch: vi.fn().mockResolvedValue({}),
    delete: vi.fn().mockResolvedValue(undefined),
  },
}))

// Mock auth store
vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u1', username: 'admin', display_name: 'Admin', is_admin: true, picture_url: null, bio: null, is_new_member: false } },
  token: { value: 'test-tok' },
  isAuthed: { value: true },
  setToken: vi.fn(),
  logout: vi.fn(),
}))

const wsTypes: string[] = []
vi.mock('@/ws', () => ({
  ws: { on: (type: string) => { wsTypes.push(type); return () => {} }, send: vi.fn() },
}))

describe('DashboardPage', () => {
  it('refreshes on the frame names the server actually emits', async () => {
    const { render, waitFor } = await import('@testing-library/preact')
    const { default: DashboardPage } = await import('./DashboardPage')
    const { api } = await import('@/api')
    // Keep every fetch pending — this test is about the subscriptions,
    // not the rendered bundle.
    vi.mocked(api.get).mockReturnValue(new Promise(() => {}))
    wsTypes.length = 0
    render(<DashboardPage />)
    await waitFor(() => expect(wsTypes).toContain('dm.message'))
    expect(wsTypes).toEqual(expect.arrayContaining([
      'notification.new', 'notification.unread_count',
      'calendar.created', 'calendar.updated', 'calendar.deleted',
      'timetable.changed', 'timetable.deleted',
    ]))
    expect(wsTypes).not.toContain('notification.created')
    expect(wsTypes).not.toContain('notification.read_changed')
    expect(wsTypes.filter(t => t.startsWith('calendar.event.'))).toEqual([])
  })

  it('renders the merged schedule card instead of the Today card on a timetable day', async () => {
    const { render, waitFor } = await import('@testing-library/preact')
    const { default: DashboardPage } = await import('./DashboardPage')
    const { api } = await import('@/api')
    const start = new Date()
    start.setHours(23, 0, 0, 0)
    const end = new Date(start)
    end.setMinutes(45)
    const ev = {
      id: 'ev1', summary: 'Dentist', start: start.toISOString(), end: end.toISOString(),
      all_day: false,
    }
    vi.mocked(api.get).mockImplementation(async (url: string) => (url === '/api/me/corner' ? {
      unread_notifications: 0, unread_conversations: 0, upcoming_events: [ev],
      presence: [], tasks_due_today: [],
      bazaar: { active_listings: 0, pending_offers: 0, ending_soon: 0 },
      followed_space_ids: [], followed_spaces_feed: [], today_events: [ev],
      today_timetable: [{
        timetable_id: 'tt1', name: 'Emma', color: null, tz: 'UTC', date: '2026-09-28',
        lessons: [{
          source_id: 'l1', date: '2026-09-28', start: '23:00', end: '23:45', kind: 'lesson',
          label: null, title: 'Mathe', room: null, teacher: null, note: null, color: null,
          icon: null, status: 'normal', override_id: null, original: null,
          start_at: start.toISOString(), end_at: end.toISOString(),
        }],
      }],
    } : []) as never)
    const { container } = render(<DashboardPage />)
    await waitFor(() => {
      expect(container.querySelector('.sh-welcome-card--schedule')).not.toBeNull()
    })
    expect(container.textContent).toContain('Mathe')
    expect(container.textContent).toContain('Dentist')
    expect(container.querySelector('a.sh-welcome-card[href="/calendar"]')).toBeNull()
    // The event overlaps the lesson → both rows are flagged.
    expect(container.querySelectorAll('.sh-schedule__warn')).toHaveLength(2)
  })

  it('keeps "Up next" on a school day without calendar events today', async () => {
    const { render, waitFor } = await import('@testing-library/preact')
    const { default: DashboardPage } = await import('./DashboardPage')
    const { api } = await import('@/api')
    const start = new Date()
    start.setHours(23, 0, 0, 0)
    const end = new Date(start)
    end.setMinutes(45)
    const later = new Date()
    later.setDate(later.getDate() + 3)
    vi.mocked(api.get).mockImplementation(async (url: string) => (url === '/api/me/corner' ? {
      unread_notifications: 0, unread_conversations: 0,
      upcoming_events: [{
        id: 'e9', summary: 'Dinner', start: later.toISOString(), end: later.toISOString(),
        all_day: false,
      }],
      presence: [], tasks_due_today: [],
      bazaar: { active_listings: 0, pending_offers: 0, ending_soon: 0 },
      followed_space_ids: [], followed_spaces_feed: [], today_events: [],
      today_timetable: [{
        timetable_id: 'tt1', name: 'Emma', color: null, tz: 'UTC', date: '2026-09-28',
        lessons: [{
          source_id: 'l1', date: '2026-09-28', start: '23:00', end: '23:45', kind: 'lesson',
          label: null, title: 'Mathe', room: null, teacher: null, note: null, color: null,
          icon: null, status: 'normal', override_id: null, original: null,
          start_at: start.toISOString(), end_at: end.toISOString(),
        }],
      }],
    } : []) as never)
    const { container } = render(<DashboardPage />)
    await waitFor(() => {
      expect(container.querySelector('.sh-welcome-card--schedule')).not.toBeNull()
    })
    expect(container.textContent).toContain('Up next')
    expect(container.textContent).toContain('Dinner')
  })

  it('module exports a default component', async () => {
    const mod = await import('./DashboardPage')
    expect(mod.default).toBeTruthy()
    expect(typeof mod.default).toBe('function')
  })
})
