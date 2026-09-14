import { describe, it, expect, vi, beforeEach } from 'vitest'

// Mock the API module before importing the page. Per-test mocks
// override the default no-op shape via ``vi.mocked(api.get).mockImplementation``.
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

  it('renders day-group headings in chronological order regardless of creation order', async () => {
    // Regression for the bug where three events scheduled for
    // 2026-05-14, 2026-05-19 and 2026-05-21 surfaced as 14 → 21 →
    // 19 in the agenda. The root cause was a locale-fragile
    // ``new Date(toLocaleDateString())`` round-trip in the day-key
    // sort; this test pins the rendered order at the SPA boundary.
    const { api } = await import('@/api')
    vi.mocked(api.get).mockImplementation(async (url: string) => {
      if (url === '/api/calendars') {
        return [{
          id: 'cal-1',
          name: 'Family',
          owner_username: 'admin',
          color: null,
        }]
      }
      if (url.startsWith('/api/calendars/cal-1/events')) {
        // Order intentionally NOT chronological to mimic the
        // multi-calendar ``responses.flat()`` shape in the bug
        // report. The page must surface them in event-date order
        // anyway.
        return [
          {
            id: 'e14', calendar_id: 'cal-1', summary: 'On the 14th',
            description: null,
            start: '2026-05-14T10:00:00Z', end: '2026-05-14T11:00:00Z',
            all_day: false, rrule: null, capacity: null,
            created_by: 'u1', attendees: ['u1'],
            rsvp_enabled: false, location: null, cover_url: null,
          },
          {
            id: 'e21', calendar_id: 'cal-1', summary: 'On the 21st',
            description: null,
            start: '2026-05-21T10:00:00Z', end: '2026-05-21T11:00:00Z',
            all_day: false, rrule: null, capacity: null,
            created_by: 'u1', attendees: ['u1'],
            rsvp_enabled: false, location: null, cover_url: null,
          },
          {
            id: 'e19', calendar_id: 'cal-1', summary: 'On the 19th',
            description: null,
            start: '2026-05-19T10:00:00Z', end: '2026-05-19T11:00:00Z',
            all_day: false, rrule: null, capacity: null,
            created_by: 'u1', attendees: ['u1'],
            rsvp_enabled: false, location: null, cover_url: null,
          },
        ]
      }
      return []
    })

    const { render, waitFor } = await import('@testing-library/preact')
    const mod = await import('./CalendarPage')
    const { container } = render(<mod.default />)

    // Wait for the async load to settle and all three day headings
    // to be rendered.
    await waitFor(() => {
      const titles = container.querySelectorAll('.sh-event strong')
      expect(titles.length).toBe(3)
    }, { timeout: 2000 })

    const eventTitles = Array.from(
      container.querySelectorAll('.sh-event strong'),
    ).map(el => el.textContent)
    expect(eventTitles).toEqual([
      'On the 14th', 'On the 19th', 'On the 21st',
    ])
  })

  it('spreads a multi-day event across one day card per day, expanding only the clicked row', async () => {
    // Regression for the bug where a Fri–Sun event was filed only
    // under Friday (invisible when you looked at Saturday). Dates are
    // built relative to ``new Date()`` because the page fetches — and
    // now CLAMPS the day expansion to — the month range around today;
    // hard-coded 2026 dates would only survive because the mock
    // ignores the query string.
    const now = new Date()
    const iso = (day: number, hour: number) =>
      new Date(now.getFullYear(), now.getMonth(), day, hour, 0, 0).toISOString()

    const { api } = await import('@/api')
    vi.mocked(api.get).mockImplementation(async (url: string) => {
      if (url === '/api/calendars') {
        return [{
          id: 'cal-1',
          name: 'Family',
          owner_username: 'admin',
          color: null,
        }]
      }
      if (url.startsWith('/api/calendars/cal-1/events')) {
        return [{
          id: 'trip', calendar_id: 'cal-1', summary: 'Weekend trip',
          description: null,
          start: iso(5, 16), end: iso(7, 10),
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
      expect(container.querySelectorAll('.sh-event strong').length).toBe(3)
    }, { timeout: 2000 })
    expect(container.querySelectorAll('.sh-calendar-day-group').length).toBe(3)

    // Activating the middle day's row header expands exactly that row —
    // the expansion is keyed by ``dayKey:eventId``, not by event id, so
    // the same event on the other two day cards stays collapsed. (The
    // toggle lives on the header <button>, not the wrapper div, so the
    // detail's Edit / Delete / RSVP controls aren't nested in it.)
    const rows = container.querySelectorAll('.sh-event')
    fireEvent.click(rows[1].querySelector('.sh-event-header') as HTMLElement)
    await waitFor(() => {
      expect(container.querySelectorAll('.sh-event-detail').length).toBe(1)
    }, { timeout: 2000 })
    expect(rows[1].querySelector('.sh-event-detail')).toBeTruthy()
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
      new Date(now.getFullYear(), now.getMonth(), day, hour, 0, 0).toISOString()

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
      expect(container.querySelectorAll('.sh-event-header').length).toBe(1)
    }, { timeout: 2000 })

    const header = container.querySelector('.sh-event-header') as HTMLButtonElement
    expect(header.tagName).toBe('BUTTON')
    expect(header.type).toBe('button')

    // Collapsed disclosure announced as such, then expanded on activation.
    expect(header.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(header)
    await waitFor(() => {
      expect(container.querySelectorAll('.sh-event-detail').length).toBe(1)
    }, { timeout: 2000 })
    expect(header.getAttribute('aria-expanded')).toBe('true')

    // ``aria-controls`` has to point at the panel that actually appeared.
    const detail = container.querySelector('.sh-event-detail') as HTMLElement
    expect(detail.id).toBeTruthy()
    expect(header.getAttribute('aria-controls')).toBe(detail.id)
  })

  it('does not open the detail from a click on the row wrapper outside the header button', async () => {
    // The wrapper ``.sh-event`` div must no longer own the toggle —
    // otherwise the detail (which holds Edit / Delete / RSVP /
    // ReminderPicker) would sit inside the click target and the
    // disclosure semantics would be a lie.
    //
    // NOTE the distinct event id + day: ``selectedRow`` is a
    // module-level signal and the module is imported once per file, so
    // reusing the previous test's ``dayKey:eventId`` would start this
    // test already expanded.
    const now = new Date()
    const iso = (day: number, hour: number) =>
      new Date(now.getFullYear(), now.getMonth(), day, hour, 0, 0).toISOString()

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
      expect(container.querySelectorAll('.sh-event').length).toBe(1)
    }, { timeout: 2000 })

    const row = container.querySelector('.sh-event') as HTMLElement
    fireEvent.click(row)
    // Nothing async should have been kicked off, but give the signal a
    // microtask-flush window so a regression can't hide behind timing.
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(container.querySelectorAll('.sh-event-detail').length).toBe(0)
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
      new Date(now.getFullYear(), now.getMonth(), day, hour, 0, 0).toISOString()

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
      expect(container.querySelectorAll('.sh-event strong').length).toBe(1)
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
      new Date(now.getFullYear(), now.getMonth(), day, hour, 0, 0).toISOString()

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
      expect(container.querySelectorAll('.sh-event strong').length).toBe(1)
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
      new Date(now.getFullYear(), now.getMonth(), day, hour, 0, 0).toISOString()

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
      expect(container.querySelectorAll('.sh-event strong').length).toBe(1)
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
      new Date(now.getFullYear(), now.getMonth(), day, hour, 0, 0).toISOString()
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
      expect(container.querySelectorAll('.sh-event strong').length).toBe(1)
    }, { timeout: 2000 })

    const chips = Array.from(container.querySelectorAll('.sh-event-owner'))
      .map(el => el.textContent)
    expect(chips).toEqual(['You', 'emely'])
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
})
