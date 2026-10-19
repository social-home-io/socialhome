/**
 * SpaceCalendarSection (owner decision 9): Upcoming by default, Month,
 * Week only on a wide screen — real toggle buttons, never tabs; ‹ › and
 * Today only in Month / Week; the end line with "Subscribe from your
 * phone"; New event only for writers.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const apiGet = vi.fn()
vi.mock('@/api', () => ({
  api: {
    get: (...a: unknown[]) => apiGet(...a),
    post: vi.fn().mockResolvedValue({}),
    delete: vi.fn().mockResolvedValue({}),
  },
}))
vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u1' } },
  token: { value: 'tok' },
}))
vi.mock('@/components/SubscribeFeed', () => ({
  SubscribeFeed: (p: { spaceId: string }) => <div data-testid="subscribe-feed">{p.spaceId}</div>,
}))

function wide(on: boolean) {
  window.matchMedia = ((q: string) => ({
    matches: on && q.includes('min-width'), media: q,
    addEventListener() {}, removeEventListener() {},
  })) as unknown as typeof window.matchMedia
}

const tomorrow = (h: number) => {
  const d = new Date()
  d.setDate(d.getDate() + 1)
  d.setHours(h, 0, 0, 0)
  return d.toISOString()
}

async function mount(props: Partial<{ canWrite: boolean; adminOnly: boolean }> = {}) {
  const tl = await import('@testing-library/preact')
  const { SpaceCalendarSection } = await import('./SpaceCalendarSection')
  const r = tl.render(
    <SpaceCalendarSection spaceId="s1" canWrite={props.canWrite ?? true}
                          adminOnly={props.adminOnly ?? false} archived={false} />,
  )
  return { tl, r }
}

beforeEach(() => {
  vi.resetModules()
  apiGet.mockReset()
  apiGet.mockResolvedValue([{
    id: 'e1', calendar_id: 's1', summary: 'Swimming lesson', description: null,
    start: tomorrow(17), end: tomorrow(18), all_day: false, created_by: 'u2',
  }])
  wide(true)
})

afterEach(async () => {
  const { cleanup } = await import('@testing-library/preact')
  cleanup()
})

describe('SpaceCalendarSection', () => {
  it('opens on Upcoming: a toggle group of buttons, no tabs, no navigator', async () => {
    const { tl, r } = await mount()
    await tl.waitFor(() => expect(r.getByText('Swimming lesson')).toBeTruthy())
    const views = r.getByRole('group', { name: 'Calendar view' })
    const buttons = tl.within(views).getAllByRole('button')
    expect(buttons.map((b) => b.textContent)).toEqual(['Upcoming', 'Month', 'Week'])
    expect(buttons[0].getAttribute('aria-pressed')).toBe('true')
    expect(r.queryAllByRole('tab')).toEqual([])
    expect(r.queryByRole('button', { name: 'Today' })).toBeNull()
    expect(r.queryByRole('button', { name: 'Previous month' })).toBeNull()
    // The rolling range: today through four weeks.
    const [, range] = apiGet.mock.calls[0] as [string, { start: string; end: string }]
    expect(apiGet.mock.calls[0][0]).toBe('/api/spaces/s1/calendar/events')
    const days = (Date.parse(range.end) - Date.parse(range.start)) / 86_400_000
    expect(Math.round(days)).toBe(28)
    expect(r.getByText(/Nothing else in the next 4 weeks/)).toBeTruthy()
  })

  it('"Subscribe from your phone" opens the private calendar link', async () => {
    const { tl, r } = await mount()
    tl.fireEvent.click(await tl.waitFor(() => r.getByRole('button', { name: 'Subscribe from your phone' })))
    await tl.waitFor(() => expect(r.getByTestId('subscribe-feed').textContent).toBe('s1'))
  })

  it('Month brings ‹ › and Today and fetches the whole grid', async () => {
    const { tl, r } = await mount()
    await tl.waitFor(() => expect(r.getByText('Swimming lesson')).toBeTruthy())
    tl.fireEvent.click(r.getByRole('button', { name: 'Month' }))
    await tl.waitFor(() => expect(r.getByRole('button', { name: 'Today' })).toBeTruthy())
    expect(r.getByRole('button', { name: 'Month' }).getAttribute('aria-pressed')).toBe('true')
    expect(r.container.querySelector('.sh-cal-grid')).not.toBeNull()
    const before = apiGet.mock.calls.length
    tl.fireEvent.click(r.getByRole('button', { name: 'Next month' }))
    await tl.waitFor(() => expect(apiGet.mock.calls.length).toBeGreaterThan(before))
  })

  it('a phone gets no Week', async () => {
    wide(false)
    const { tl, r } = await mount()
    const views = await tl.waitFor(() => r.getByRole('group', { name: 'Calendar view' }))
    expect(tl.within(views).getAllByRole('button').map((b) => b.textContent)).toEqual(['Upcoming', 'Month'])
  })

  it('New event for writers only; the admin-only note for the rest', async () => {
    let m = await mount()
    expect(m.r.getByRole('button', { name: /New event/ })).toBeTruthy()
    m.tl.cleanup()
    m = await mount({ canWrite: false, adminOnly: true })
    expect(m.r.queryByRole('button', { name: /New event/ })).toBeNull()
    expect(m.r.getByTestId('access-note-calendar')).toBeTruthy()
  })

  it('nothing coming up: the empty state, still with Subscribe', async () => {
    apiGet.mockResolvedValue([])
    const { tl, r } = await mount()
    await tl.waitFor(() => expect(r.getByRole('heading', { name: 'Nothing planned in the next 4 weeks' })).toBeTruthy())
    expect(r.getByRole('button', { name: 'Subscribe from your phone' })).toBeTruthy()
    expect(r.queryByText(/Nothing else/)).toBeNull()
  })
})
