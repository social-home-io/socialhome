import { describe, it, expect, vi, beforeEach } from 'vitest'

// Mock the API module before importing the page. Per-test mocks
// override the default no-op shape via ``vi.mocked(api.get).mockImplementation``.
vi.mock('@/api', () => ({
  api: {
    get: vi.fn().mockResolvedValue([]),
    post: vi.fn().mockResolvedValue({}),
    patch: vi.fn().mockResolvedValue({}),
    delete: vi.fn().mockResolvedValue(undefined),
    postRaw: vi.fn().mockResolvedValue({ events: [] }),
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

// Title hook + household users + WS are out-of-scope for these tests.
vi.mock('@/store/pageTitle', () => ({ useTitle: vi.fn() }))
vi.mock('@/store/householdUsers', () => ({
  householdUsers: { value: new Map() },
  loadHouseholdUsers: vi.fn().mockResolvedValue(undefined),
}))

// The event dialog is a module-level singleton driven by imperative
// ``open*`` helpers; these tests only need to reach its ``onCreated``
// callback, so stub the module and stash the prop the page passed.
const dialogSpy = vi.hoisted(() => ({
  onCreated: null as ((ids: string[] | null) => void) | null,
}))
vi.mock('@/components/CalendarEventDialog', () => ({
  CalendarEventDialog: (props: { onCreated?: (ids: string[] | null) => void }) => {
    dialogSpy.onCreated = props.onCreated ?? null
    return null
  },
  openEventDialog: vi.fn(),
  openEditEventDialog: vi.fn(),
}))

// The Timetable tab has its own tests; here it only needs to show up.
vi.mock('@/features/timetable/TimetablePage', () => ({
  default: () => <div data-testid="timetable-page" />,
}))

const VIS_KEY = 'sh-cal-visible:u1'

describe('CalendarPage', () => {
  beforeEach(() => {
    // The visibility preference is a per-user localStorage key and the
    // page module (with its signals) is imported once per file — clear
    // it so one test's persisted overlay can't seed the next.
    localStorage.clear()
    dialogSpy.onCreated = null
  })

  it('module exports a default component', async () => {
    const mod = await import('./CalendarPage')
    expect(mod.default).toBeTruthy()
    expect(typeof mod.default).toBe('function')
  })

  it('lists Upcoming in date order regardless of creation order', async () => {
    // Regression for the bug where three events surfaced as 14 → 21 →
    // 19 (a locale-fragile day-key sort). Upcoming opens by default and
    // must list them in date order whatever order the calendars answer.
    const now = new Date()
    const iso = (day: number, hour: number) =>
      new Date(now.getFullYear(), now.getMonth(), now.getDate() + day, hour, 0, 0).toISOString()
    const row = (id: string, summary: string, day: number) => ({
      id, calendar_id: 'cal-1', summary, description: null,
      start: iso(day, 10), end: iso(day, 11),
      all_day: false, rrule: null, capacity: null,
      created_by: 'u1', attendees: ['u1'],
      rsvp_enabled: false, location: null, cover_url: null,
    })
    const { api } = await import('@/api')
    vi.mocked(api.get).mockImplementation(async (url: string) => {
      if (url === '/api/calendars') {
        return [{ id: 'cal-1', name: 'Family', owner_username: 'admin', color: null }]
      }
      if (url.startsWith('/api/calendars/cal-1/events')) {
        return [row('e14', 'First', 2), row('e21', 'Third', 9), row('e19', 'Second', 6)]
      }
      return []
    })

    const { render, waitFor } = await import('@testing-library/preact')
    const mod = await import('./CalendarPage')
    const { container } = render(<mod.default />)

    await waitFor(() => {
      expect(container.querySelectorAll('.sh-cal-row__title').length).toBe(3)
    }, { timeout: 2000 })
    const titles = Array.from(container.querySelectorAll('.sh-cal-row__title')).map(el => el.textContent)
    expect(titles).toEqual(['First', 'Second', 'Third'])
    // Week headings sit one level under the page title (the TopBar h1).
    const headings = container.querySelectorAll('.sh-cal-week__heading')
    expect(headings.length).toBeGreaterThan(0)
    expect([...headings].every(h => h.tagName === 'H2')).toBe(true)
  })

  it('shows a multi-day event as ONE row with its span, like a space calendar', async () => {
    // The household agenda used to repeat a Fri–Sun trip as a card per
    // day. Every calendar now shares one row anatomy: "Fri 5 – / Sun 7 /
    // 3 days" in the date column, one row, one detail.
    const now = new Date()
    const iso = (day: number, hour: number) =>
      new Date(now.getFullYear(), now.getMonth(), now.getDate() + day, hour, 0, 0).toISOString()

    const { api } = await import('@/api')
    vi.mocked(api.get).mockImplementation(async (url: string) => {
      if (url === '/api/calendars') {
        return [{ id: 'cal-1', name: 'Family', owner_username: 'admin', color: null }]
      }
      if (url.startsWith('/api/calendars/cal-1/events')) {
        return [{
          id: 'trip', calendar_id: 'cal-1', summary: 'Weekend trip',
          description: null,
          start: iso(2, 16), end: iso(4, 10),
          all_day: false, rrule: null, capacity: null,
          created_by: 'u1', attendees: ['u1'],
          rsvp_enabled: false, location: null, cover_url: null,
        }]
      }
      return []
    })

    const { render, waitFor, fireEvent } = await import('@testing-library/preact')
    const mod = await import('./CalendarPage')
    const { container } = render(<mod.default />)

    await waitFor(() => {
      expect(container.querySelectorAll('.sh-cal-row__title').length).toBe(1)
    }, { timeout: 2000 })
    const when = container.querySelector('.sh-cal-row__when')!.textContent ?? ''
    expect(when).toContain('–')
    expect(when).toContain('3 days')
    fireEvent.click(container.querySelector('.sh-cal-row__head') as HTMLElement)
    await waitFor(() => {
      expect(container.querySelectorAll('.sh-cal-row__detail').length).toBe(1)
    }, { timeout: 2000 })
  })

  it('exposes each agenda row header as a native keyboard-operable button', async () => {
    // Regression for the mouse-only agenda: the row was a bare
    // ``<div onClick>`` with no role, no tabindex and no key handler,
    // so a keyboard / screen-reader user could not open an event —
    // and therefore could not reach Edit, Delete, RSVP or reminders.
    // Asserting the tag + type is what actually buys operability: a
    // native button gets Enter AND Space activation, focus and the
    // right role from the platform. (Faking a ``keyDown`` would pass
    // for the wrong reason — jsdom does not synthesize activation
    // from keydown on a button.)
    const now = new Date()
    const iso = (day: number, hour: number) =>
      new Date(now.getFullYear(), now.getMonth(), now.getDate() + day, hour, 0, 0).toISOString()

    const { api } = await import('@/api')
    vi.mocked(api.get).mockImplementation(async (url: string) => {
      if (url === '/api/calendars') {
        return [{
          id: 'cal-1', name: 'Family', owner_username: 'admin', color: null,
        }]
      }
      if (url.startsWith('/api/calendars/cal-1/events')) {
        return [{
          id: 'ev1', calendar_id: 'cal-1', summary: 'Dentist',
          description: null,
          start: iso(9, 9), end: iso(9, 10),
          all_day: false, rrule: null, capacity: null,
          created_by: 'u1', attendees: ['u1'],
          rsvp_enabled: false, location: null, cover_url: null,
        }]
      }
      return []
    })

    const { render, waitFor, fireEvent } = await import('@testing-library/preact')
    const mod = await import('./CalendarPage')
    const { container } = render(<mod.default />)

    await waitFor(() => {
      expect(container.querySelectorAll('.sh-cal-row__head').length).toBe(1)
    }, { timeout: 2000 })

    const header = container.querySelector('.sh-cal-row__head') as HTMLButtonElement
    expect(header.tagName).toBe('BUTTON')
    expect(header.type).toBe('button')

    // Collapsed disclosure announced as such, then expanded on activation.
    expect(header.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(header)
    await waitFor(() => {
      expect(container.querySelectorAll('.sh-cal-row__detail').length).toBe(1)
    }, { timeout: 2000 })
    expect(header.getAttribute('aria-expanded')).toBe('true')

    // ``aria-controls`` has to point at the panel that actually appeared.
    const detail = container.querySelector('.sh-cal-row__detail') as HTMLElement
    expect(detail.id).toBeTruthy()
    expect(header.getAttribute('aria-controls')).toBe(detail.id)
  })

  it('does not open the detail from a click on the row wrapper outside the header button', async () => {
    // The wrapper ``.sh-cal-row`` div must not own the toggle —
    // otherwise the detail (which holds Edit / Delete / RSVP /
    // ReminderPicker) would sit inside the click target and the
    // disclosure semantics would be a lie.
    //
    const now = new Date()
    const iso = (day: number, hour: number) =>
      new Date(now.getFullYear(), now.getMonth(), now.getDate() + day, hour, 0, 0).toISOString()

    const { api } = await import('@/api')
    vi.mocked(api.get).mockImplementation(async (url: string) => {
      if (url === '/api/calendars') {
        return [{
          id: 'cal-1', name: 'Family', owner_username: 'admin', color: null,
        }]
      }
      if (url.startsWith('/api/calendars/cal-1/events')) {
        return [{
          id: 'ev-wrapper', calendar_id: 'cal-1', summary: 'Dentist',
          description: null,
          start: iso(11, 9), end: iso(11, 10),
          all_day: false, rrule: null, capacity: null,
          created_by: 'u1', attendees: ['u1'],
          rsvp_enabled: false, location: null, cover_url: null,
        }]
      }
      return []
    })

    const { render, waitFor, fireEvent } = await import('@testing-library/preact')
    const mod = await import('./CalendarPage')
    const { container } = render(<mod.default />)

    await waitFor(() => {
      expect(container.querySelectorAll('.sh-cal-row').length).toBe(1)
    }, { timeout: 2000 })

    const row = container.querySelector('.sh-cal-row') as HTMLElement
    fireEvent.click(row)
    // Nothing async should have been kicked off, but give the signal a
    // microtask-flush window so a regression can't hide behind timing.
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(container.querySelectorAll('.sh-cal-row__detail').length).toBe(0)
  })
  it('shows every owner chip for a shared event while only one calendar is visible', async () => {
    // Regression for "I tick the other members and nothing happens":
    // a household event fanned out to three calendars rendered with NO
    // owner chips (and opened the edit dialog with those members
    // unticked, so re-ticking POSTed duplicate rows) whenever the
    // viewer only had their own calendar switched on. The authoritative
    // ``copies`` array is visibility-independent, so the chips must
    // render from it even with a single calendar overlaid.
    const now = new Date()
    const iso = (day: number, hour: number) =>
      new Date(now.getFullYear(), now.getMonth(), now.getDate() + day, hour, 0, 0).toISOString()

    const { api } = await import('@/api')
    vi.mocked(api.get).mockImplementation(async (url: string) => {
      if (url === '/api/calendars') {
        return [
          { id: 'cal-1', name: 'Admin', owner_username: 'admin', color: null },
          { id: 'cal-2', name: 'Emely', owner_username: 'emely', color: null },
          { id: 'cal-3', name: 'Yoshua', owner_username: 'yoshua', color: null },
        ]
      }
      if (url.startsWith('/api/calendars/cal-1/events')) {
        return [{
          id: 'shared-1', calendar_id: 'cal-1', summary: 'Household dinner',
          description: null,
          start: iso(3, 18), end: iso(3, 20),
          all_day: false, rrule: null, capacity: null,
          created_by: 'u1', attendees: ['u1'],
          rsvp_enabled: false, location: null, cover_url: null,
          client_event_uuid: 'uuid-dinner',
          copies: [
            { event_id: 'shared-1', calendar_id: 'cal-1', owner_username: 'admin' },
            { event_id: 'shared-2', calendar_id: 'cal-2', owner_username: 'emely' },
            { event_id: 'shared-3', calendar_id: 'cal-3', owner_username: 'yoshua' },
          ],
        }]
      }
      return []
    })

    const { render, waitFor } = await import('@testing-library/preact')
    const mod = await import('./CalendarPage')
    const { container } = render(<mod.default />)

    await waitFor(() => {
      expect(container.querySelectorAll('.sh-cal-row__title').length).toBe(1)
    }, { timeout: 2000 })

    const chips = Array.from(container.querySelectorAll('.sh-event-owner'))
      .map(el => el.textContent)
    expect(chips).toEqual(['You', 'emely', 'yoshua'])
  })

  it('renders no owner chips for a single-owner event in the single-calendar view', async () => {
    // Only your own calendar on screen and only you holding a copy: the
    // owner is implied, so a lone "You" chip on every agenda row would
    // be pure noise. (Contrast the multi-calendar view below, where the
    // same event MUST keep its byline.)
    const now = new Date()
    const iso = (day: number, hour: number) =>
      new Date(now.getFullYear(), now.getMonth(), now.getDate() + day, hour, 0, 0).toISOString()

    const { api } = await import('@/api')
    vi.mocked(api.get).mockImplementation(async (url: string) => {
      if (url === '/api/calendars') {
        return [
          { id: 'cal-1', name: 'Admin', owner_username: 'admin', color: null },
          { id: 'cal-2', name: 'Emely', owner_username: 'emely', color: null },
        ]
      }
      if (url.startsWith('/api/calendars/cal-1/events')) {
        return [{
          id: 'solo-1', calendar_id: 'cal-1', summary: 'Dentist',
          description: null,
          start: iso(4, 9), end: iso(4, 10),
          all_day: false, rrule: null, capacity: null,
          created_by: 'u1', attendees: ['u1'],
          rsvp_enabled: false, location: null, cover_url: null,
          client_event_uuid: 'uuid-solo',
          copies: [
            { event_id: 'solo-1', calendar_id: 'cal-1', owner_username: 'admin' },
          ],
        }]
      }
      return []
    })

    const { render, waitFor } = await import('@testing-library/preact')
    const mod = await import('./CalendarPage')
    const { container } = render(<mod.default />)

    await waitFor(() => {
      expect(container.querySelectorAll('.sh-cal-row__title').length).toBe(1)
    }, { timeout: 2000 })
    expect(container.querySelectorAll('.sh-event-owner').length).toBe(0)
  })

  it('keeps the owner chip on a single-owner event while several calendars are overlaid', async () => {
    // The byline is what says WHOSE row this is once two members'
    // calendars are overlaid — calendar hue alone is a thin accent, two
    // household calendars can sit close together, and it's inaccessible
    // to anyone who can't discriminate them. Suppressing the chip for
    // every single-owner event (a plausible over-correction of the
    // "no lone You chip" rule) makes Maria's and Pascal's rows read
    // identically in exactly the view the overlay exists for.
    localStorage.setItem(VIS_KEY, JSON.stringify(['cal-1', 'cal-2']))
    const now = new Date()
    const iso = (day: number, hour: number) =>
      new Date(now.getFullYear(), now.getMonth(), now.getDate() + day, hour, 0, 0).toISOString()

    const { api } = await import('@/api')
    vi.mocked(api.get).mockImplementation(async (url: string) => {
      if (url === '/api/calendars') {
        return [
          { id: 'cal-1', name: 'Admin', owner_username: 'admin', color: null },
          { id: 'cal-2', name: 'Emely', owner_username: 'emely', color: null },
        ]
      }
      if (url.startsWith('/api/calendars/cal-1/events')) {
        return [{
          id: 'solo-overlay', calendar_id: 'cal-1', summary: 'Physio',
          description: null,
          start: iso(5, 9), end: iso(5, 10),
          all_day: false, rrule: null, capacity: null,
          created_by: 'u1', attendees: ['u1'],
          rsvp_enabled: false, location: null, cover_url: null,
          client_event_uuid: 'uuid-solo-overlay',
          copies: [
            { event_id: 'solo-overlay', calendar_id: 'cal-1', owner_username: 'admin' },
          ],
        }]
      }
      return []
    })

    const { render, waitFor } = await import('@testing-library/preact')
    const mod = await import('./CalendarPage')
    const { container } = render(<mod.default />)

    await waitFor(() => {
      expect(container.querySelectorAll('.sh-cal-row__title').length).toBe(1)
    }, { timeout: 2000 })

    const chips = Array.from(container.querySelectorAll('.sh-event-owner'))
      .map(el => el.textContent)
    expect(chips).toEqual(['You'])
  })

  it('falls back to render-time grouping when the server reports no copies', async () => {
    // uuid-less legacy / ICS rows come back with ``copies: []``; the
    // agenda's content-key grouping is still the only signal there.
    localStorage.setItem(VIS_KEY, JSON.stringify(['cal-1', 'cal-2']))
    const now = new Date()
    const iso = (day: number, hour: number) =>
      new Date(now.getFullYear(), now.getMonth(), now.getDate() + day, hour, 0, 0).toISOString()
    const start = iso(6, 18)
    const end = iso(6, 20)

    const { api } = await import('@/api')
    vi.mocked(api.get).mockImplementation(async (url: string) => {
      if (url === '/api/calendars') {
        return [
          { id: 'cal-1', name: 'Admin', owner_username: 'admin', color: null },
          { id: 'cal-2', name: 'Emely', owner_username: 'emely', color: null },
        ]
      }
      if (url.startsWith('/api/calendars/cal-1/events')) {
        return [{
          id: 'legacy-1', calendar_id: 'cal-1', summary: 'School run',
          description: null, start, end,
          all_day: false, rrule: null, capacity: null,
          created_by: 'u1', attendees: ['u1'],
          rsvp_enabled: false, location: null, cover_url: null,
          copies: [],
        }]
      }
      if (url.startsWith('/api/calendars/cal-2/events')) {
        return [{
          id: 'legacy-2', calendar_id: 'cal-2', summary: 'School run',
          description: null, start, end,
          all_day: false, rrule: null, capacity: null,
          created_by: 'u1', attendees: ['u1'],
          rsvp_enabled: false, location: null, cover_url: null,
          copies: [],
        }]
      }
      return []
    })

    const { render, waitFor } = await import('@testing-library/preact')
    const mod = await import('./CalendarPage')
    const { container } = render(<mod.default />)

    await waitFor(() => {
      expect(container.querySelectorAll('.sh-cal-row__title').length).toBe(1)
    }, { timeout: 2000 })

    const chips = Array.from(container.querySelectorAll('.sh-event-owner'))
      .map(el => el.textContent)
    expect(chips).toEqual(['You', 'emely'])
  })

  it('offers Upcoming and Month as pressed buttons (no Day); Month brings ‹ › and Today', async () => {
    const { api } = await import('@/api')
    vi.mocked(api.get).mockImplementation(async (url: string) => {
      if (url === '/api/calendars') {
        return [{ id: 'cal-1', name: 'Family', owner_username: 'admin', color: null }]
      }
      return []
    })
    const { render, fireEvent, waitFor } = await import('@testing-library/preact')
    const mod = await import('./CalendarPage')
    const utils = render(<mod.default />)
    const views = await utils.findByRole('group', { name: 'Calendar view' })
    const buttons = [...views.querySelectorAll('button')]
    // jsdom has no wide screen: Week stays out, as on a phone.
    expect(buttons.map(b => b.textContent)).toEqual(['Upcoming', 'Month'])
    expect(buttons.map(b => b.getAttribute('aria-pressed'))).toEqual(['true', 'false'])
    expect(utils.container.querySelector('.sh-calendar [role="tablist"]')).toBeNull()
    expect(utils.queryByRole('button', { name: 'Day' })).toBeNull()
    expect(utils.queryByRole('button', { name: 'Today' })).toBeNull()
    // Nothing coming up: the empty state still offers New event + Import.
    expect(await utils.findByText('Nothing planned in the next 4 weeks')).toBeTruthy()
    fireEvent.click(buttons[1])
    await waitFor(() => expect(utils.getByRole('button', { name: 'Today' })).toBeTruthy())
    expect(utils.getByRole('button', { name: 'Previous month' })).toBeTruthy()
    expect(utils.container.querySelector('.sh-cal-grid')).not.toBeNull()
    // Back to Upcoming for the tests after this one (module-level view).
    fireEvent.click(utils.getByRole('button', { name: 'Upcoming' }))
  })

  it('reveals and persists every calendar an event was created on', async () => {
    // Creating "for Pascal and Emely" while only your own chip is on
    // used to leave their copies invisible — and the auto-reveal that
    // did happen was never persisted, so it reverted on reload.
    const { api } = await import('@/api')
    vi.mocked(api.get).mockImplementation(async (url: string) => {
      if (url === '/api/calendars') {
        return [
          { id: 'cal-1', name: 'Admin', owner_username: 'admin', color: null },
          { id: 'cal-2', name: 'Emely', owner_username: 'emely', color: null },
          { id: 'cal-3', name: 'Yoshua', owner_username: 'yoshua', color: null },
        ]
      }
      return []
    })

    const { render, waitFor } = await import('@testing-library/preact')
    const mod = await import('./CalendarPage')
    render(<mod.default />)

    await waitFor(() => {
      expect(dialogSpy.onCreated).toBeTruthy()
    }, { timeout: 2000 })

    dialogSpy.onCreated!(['cal-1', 'cal-2', 'cal-3'])

    await waitFor(() => {
      expect(localStorage.getItem(VIS_KEY)).toBeTruthy()
    }, { timeout: 2000 })
    const persisted = JSON.parse(localStorage.getItem(VIS_KEY)!) as string[]
    expect(new Set(persisted)).toEqual(new Set(['cal-1', 'cal-2', 'cal-3']))

    // …and the freshly-visible calendars are actually fetched.
    await waitFor(() => {
      const urls = vi.mocked(api.get).mock.calls.map(c => c[0] as string)
      expect(urls.some(u => u.startsWith('/api/calendars/cal-2/events'))).toBe(true)
      expect(urls.some(u => u.startsWith('/api/calendars/cal-3/events'))).toBe(true)
    }, { timeout: 2000 })
  })

  it('leaves visibility untouched when a space event reports no target calendars', async () => {
    const { api } = await import('@/api')
    vi.mocked(api.get).mockImplementation(async (url: string) => {
      if (url === '/api/calendars') {
        return [
          { id: 'cal-1', name: 'Admin', owner_username: 'admin', color: null },
          { id: 'cal-2', name: 'Emely', owner_username: 'emely', color: null },
        ]
      }
      return []
    })

    const { render, waitFor } = await import('@testing-library/preact')
    const mod = await import('./CalendarPage')
    render(<mod.default />)

    await waitFor(() => {
      expect(dialogSpy.onCreated).toBeTruthy()
    }, { timeout: 2000 })

    vi.mocked(api.get).mockClear()
    expect(() => dialogSpy.onCreated!(null)).not.toThrow()
    await new Promise(resolve => setTimeout(resolve, 0))

    // Nothing persisted, and only the already-visible calendar re-fetched.
    expect(localStorage.getItem(VIS_KEY)).toBeNull()
    const urls = vi.mocked(api.get).mock.calls.map(c => c[0] as string)
    expect(urls.some(u => u.startsWith('/api/calendars/cal-2/events'))).toBe(false)
  })

  it('leaves visibility untouched when an edit added no new copy', async () => {
    // A user who deliberately hid Emely's calendar must not get it
    // switched back on — and SAVED — merely because they opened and
    // re-saved a shared event that happens to include her. An empty
    // report means "nothing new was written", so nothing to reveal.
    const { api } = await import('@/api')
    vi.mocked(api.get).mockImplementation(async (url: string) => {
      if (url === '/api/calendars') {
        return [
          { id: 'cal-1', name: 'Admin', owner_username: 'admin', color: null },
          { id: 'cal-2', name: 'Emely', owner_username: 'emely', color: null },
        ]
      }
      return []
    })

    const { render, waitFor } = await import('@testing-library/preact')
    const mod = await import('./CalendarPage')
    render(<mod.default />)

    await waitFor(() => {
      expect(dialogSpy.onCreated).toBeTruthy()
    }, { timeout: 2000 })

    vi.mocked(api.get).mockClear()
    dialogSpy.onCreated!([])
    await new Promise(resolve => setTimeout(resolve, 0))

    expect(localStorage.getItem(VIS_KEY)).toBeNull()
    const urls = vi.mocked(api.get).mock.calls.map(c => c[0] as string)
    expect(urls.some(u => u.startsWith('/api/calendars/cal-2/events'))).toBe(false)
  })

  it('imports a calendar file into a hidden calendar and switches it on so the events show', async () => {
    const { api } = await import('@/api')
    vi.mocked(api.get).mockImplementation(async (url: string) => {
      if (url === '/api/calendars') {
        return [
          { id: 'cal-1', name: 'Admin', owner_username: 'admin', color: null },
          { id: 'cal-2', name: 'Emely', owner_username: 'emely', color: null },
        ]
      }
      return []
    })
    vi.mocked(api.postRaw).mockResolvedValueOnce({
      events: [{ id: 'e1', summary: 'Swim', start: '2026-10-01T10:00:00+00:00' }],
    })
    const { render, waitFor, fireEvent } = await import('@testing-library/preact')
    const mod = await import('./CalendarPage')
    const utils = render(<mod.default />)
    const trigger = await utils.findByRole('button', { name: 'Import' })
    fireEvent.click(trigger)
    fireEvent.change(utils.getByLabelText('Add to'), { target: { value: 'cal-2' } })
    const input = document.querySelector('form.sh-cal-import input[type="file"]') as HTMLInputElement
    const file = new File(['BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n'], 'swim.ics', { type: 'text/calendar' })
    Object.defineProperty(input, 'files', { value: [file], configurable: true })
    fireEvent.change(input)
    await utils.findByText(/swim\.ics/)
    vi.mocked(api.get).mockClear()
    fireEvent.submit(document.querySelector('form.sh-cal-import')!)
    await utils.findByText('Added 1 event to emely\'s calendar.')
    expect(api.postRaw).toHaveBeenCalledWith('/api/calendars/cal-2/import_ics', file, 'text/calendar')
    await waitFor(() => {
      const urls = vi.mocked(api.get).mock.calls.map(c => c[0] as string)
      expect(urls.some(u => u.startsWith('/api/calendars/cal-2/events'))).toBe(true)
    })
    expect(JSON.parse(localStorage.getItem(VIS_KEY) ?? '[]')).toContain('cal-2')
    // "Show in calendar" opens Month at the imported event's month.
    fireEvent.click(utils.getByRole('button', { name: 'Show in calendar' }))
    await utils.findByText('October 2026')
    expect(utils.getByRole('button', { name: 'Month' }).getAttribute('aria-pressed')).toBe('true')
    fireEvent.click(utils.getByRole('button', { name: 'Upcoming' }))
  })
})

describe('CalendarPage — Calendar ⇄ Timetable links', () => {
  const ON = {
    feat_feed: true, feat_pages: true, feat_tasks: true, feat_stickies: true,
    feat_calendar: true, feat_presence: true, feat_gallery: true, feat_timetable: true, feat_household_chat: true,
    allow_text: true, allow_image: true, allow_video: true, allow_file: true,
    allow_poll: true, allow_schedule: true, allow_highlight_share: true,
    allow_link_preview: true, share_home_default: true, household_name: 'Home',
  }

  async function renderAt(url: string, toggleState: Partial<typeof ON> | null) {
    const { toggles } = await import('@/components/HouseholdToggles')
    toggles.value = toggleState === null ? null : { ...ON, ...toggleState }
    window.history.replaceState(null, '', url)
    const { render } = await import('@testing-library/preact')
    const { LocationProvider } = await import('preact-iso')
    const mod = await import('./CalendarPage')
    return render(<LocationProvider><mod.default /></LocationProvider>)
  }

  beforeEach(async () => {
    const { api } = await import('@/api')
    vi.mocked(api.get).mockResolvedValue([])
  })

  const link = (utils: { queryByRole: (r: string, o: object) => HTMLElement | null }, name: string) =>
    utils.queryByRole('link', { name }) as HTMLAnchorElement | null

  it('links to the Timetable from the control row — a real link, no tab strip', async () => {
    const utils = await renderAt('/calendar', ON)
    const tt = await utils.findByRole('link', { name: 'Timetable' }) as HTMLAnchorElement
    expect(tt?.getAttribute('href')).toBe('/calendar?tab=timetable')
    // It sits in the one control row with the view switch and actions.
    const row = tt.closest('.sh-cal-controls')!
    expect(row.querySelector('[role="group"][aria-label="Calendar view"]')).toBeTruthy()
    expect([...row.children].map(el => el.textContent))
      .toEqual(['UpcomingMonth', 'Timetable', 'Import+ New event'])
    expect(utils.queryByRole('tablist')).toBeNull()
    expect(utils.queryByRole('tab')).toBeNull()
    expect(link(utils, 'Calendar')).toBeNull()
    expect(utils.queryByTestId('timetable-page')).toBeNull()
  })

  it('?tab=timetable opens the Timetable, which links back to the Calendar', async () => {
    const { fireEvent, waitFor } = await import('@testing-library/preact')
    const utils = await renderAt('/calendar?tab=timetable', ON)
    expect(utils.getByTestId('timetable-page')).toBeTruthy()
    expect(link(utils, 'Timetable')).toBeNull()
    const cal = link(utils, 'Calendar')!
    expect(cal.getAttribute('href')).toBe('/calendar')
    fireEvent.click(cal)
    await waitFor(() => expect(utils.queryByTestId('timetable-page')).toBeNull())
    expect(window.location.search).toBe('')
    fireEvent.click(await utils.findByRole('link', { name: 'Timetable' }))
    await waitFor(() => expect(utils.getByTestId('timetable-page')).toBeTruthy())
    expect(window.location.search).toBe('?tab=timetable')
  })

  it('treats not-yet-loaded toggles as all on', async () => {
    const utils = await renderAt('/calendar', null)
    expect(await utils.findByRole('link', { name: 'Timetable' })).toBeTruthy()
  })

  it('feat_timetable off: no Timetable link, and ?tab=timetable falls back to the calendar', async () => {
    const utils = await renderAt('/calendar?tab=timetable', { feat_timetable: false })
    await utils.findByRole('group', { name: 'Calendar view' })
    expect(link(utils, 'Timetable')).toBeNull()
    expect(utils.queryByTestId('timetable-page')).toBeNull()
  })

  it('feat_calendar off + feat_timetable on: the Timetable alone, no Calendar link', async () => {
    const utils = await renderAt('/calendar', { feat_calendar: false })
    expect(utils.getByTestId('timetable-page')).toBeTruthy()
    expect(link(utils, 'Calendar')).toBeNull()
  })
})
