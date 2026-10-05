import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

vi.mock('@/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))
vi.mock('preact-iso', () => ({ useLocation: () => ({ route: vi.fn() }) }))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))
vi.mock('@/ws', () => ({ ws: { on: () => () => undefined } }))

import { api } from '@/api'
import HighlightArchiveTab, { buildMonthGrid } from './HighlightArchiveTab'

describe('buildMonthGrid', () => {
  // October 2026 opens on a Thursday and has 31 days.
  it('Monday start: Mon-first header, three leading blanks', () => {
    const g = buildMonthGrid(2026, 9, 0)
    expect(g.weekdayLabels).toEqual(['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'])
    expect(g.cells.slice(0, 3)).toEqual([null, null, null])
    expect(g.cells[3]?.date).toBe('2026-10-01')
  })

  it('Sunday start: Sun-first header, four leading blanks', () => {
    const g = buildMonthGrid(2026, 9, 6)
    expect(g.weekdayLabels).toEqual(['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'])
    expect(g.cells.slice(0, 4)).toEqual([null, null, null, null])
    expect(g.cells[4]?.date).toBe('2026-10-01')
  })

  it('a month opening on Sunday has no lead under Sunday start and six under Monday start', () => {
    // 1 Nov 2026 is a Sunday.
    expect(buildMonthGrid(2026, 10, 6).cells[0]?.date).toBe('2026-11-01')
    const mon = buildMonthGrid(2026, 10, 0)
    expect(mon.cells.slice(0, 6).every(c => c === null)).toBe(true)
    expect(mon.cells[6]?.date).toBe('2026-11-01')
  })

  it('pads to whole weeks (min 5 rows) so a 6-row month is not clipped', () => {
    // Nov 2026 under Monday start: 6 lead + 30 days = 36 → 6 rows.
    const g = buildMonthGrid(2026, 10, 0)
    expect(g.cells.length).toBe(42)
    expect(g.cells.filter(c => c !== null).length).toBe(30)
    // Feb 2027 under Monday start: Mon 1 Feb, 28 days → pad to 35.
    expect(buildMonthGrid(2027, 1, 0).cells.length).toBe(35)
  })
})


describe('HighlightArchiveTab day keys west of UTC', () => {
  const prevTz = process.env.TZ
  afterEach(() => {
    process.env.TZ = prevTz
    vi.useRealTimers()
  })

  it('labels the month and the selected day with the day key, not the day before', async () => {
    // ``highlight_date`` is a UTC ``YYYY-MM-DD`` key. Read as UTC
    // midnight in Los Angeles that instant is still the previous
    // evening — the header must not show 3 May / April.
    process.env.TZ = 'America/Los_Angeles'
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-05-15T12:00:00Z'))
    vi.mocked(api.get).mockResolvedValue([{
      highlight: {
        id: 'h1', author_user_id: 'u1', highlight_date: '2026-05-04',
        audience_kind: 'all_paired', audience: [],
        created_at: '2026-05-04T08:00:00Z', expires_at: '2026-06-04T08:00:00Z',
      },
      frames: [],
      unseen_count: 0,
    }])
    const { container, findByRole } = render(<HighlightArchiveTab />)
    const day = await findByRole('button', { name: /May 4, 2026/ })
    expect(container.querySelector('h2')!.textContent).toBe('May 2026')
    fireEvent.click(day)
    await waitFor(() => expect(
      container.querySelector('.sh-highlight-archive-day-panel h3')!.textContent,
    ).toBe('Monday, May 4, 2026'))
  })
})
