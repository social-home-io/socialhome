/* Media cache card on the GFS admin Overview — reads the ``space_media``
   block of ``GET /admin/api/overview``: used of max (with a bar), blobs, the
   TTL, this node's evictions and refusals, and a clear turned-off state. */
import { afterEach, describe, expect, it, vi, beforeEach } from 'vitest'
import { render, waitFor } from '@testing-library/preact'
import { MediaCacheCard, formatBytes, formatTtl, type SpaceMediaStats } from './MediaCache'
import { OverviewPanel } from './Overview'

const GIB = 1024 ** 3
const MIB = 1024 ** 2

const STATS: SpaceMediaStats = {
  enabled: true,
  stored_blobs: 1234,
  stored_bytes: Math.round(1.5 * GIB),
  max_bytes: 5 * GIB,
  ttl_seconds: 3 * 86_400,
  evicted_blobs: 17,
  evicted_bytes: 40 * MIB,
  refused_quota: 3,
  refused_too_large: 1,
}

describe('formatBytes', () => {
  it('uses binary units with one decimal where it helps', () => {
    expect(formatBytes(0)).toBe('0 B')
    expect(formatBytes(512)).toBe('512 B')
    expect(formatBytes(64 * 1024)).toBe('64 KiB')
    expect(formatBytes(1.5 * MIB)).toBe('1.5 MiB')
    expect(formatBytes(256 * MIB)).toBe('256 MiB')
    expect(formatBytes(5 * GIB)).toBe('5 GiB')
    expect(formatBytes(1.25 * GIB)).toBe('1.3 GiB')
  })

  it('rounds before it picks the unit and the decimals', () => {
    // 1023.6 KiB rounds to 1024 KiB: that is the next unit.
    expect(formatBytes(Math.round(1023.6 * 1024))).toBe('1.0 MiB')
    // 9.96 MiB rounds to 10.0: no decimal at 10 and above.
    expect(formatBytes(Math.round(9.96 * MIB))).toBe('10 MiB')
    expect(formatBytes(1023.6)).toBe('1.0 KiB')
    expect(formatBytes(1023 * 1024)).toBe('1023 KiB')
  })
})

describe('formatTtl', () => {
  it('names the largest whole unit', () => {
    expect(formatTtl(3 * 86_400)).toBe('3 days')
    expect(formatTtl(86_400)).toBe('1 day')
    expect(formatTtl(7_200)).toBe('2 hours')
    expect(formatTtl(3_600)).toBe('1 hour')
    expect(formatTtl(600)).toBe('10 minutes')
    expect(formatTtl(90 * 60)).toBe('90 minutes')
  })
})

describe('MediaCacheCard', () => {
  it('shows used of max with a bar, blobs, TTL and node counters', () => {
    const { container, getByRole } = render(<MediaCacheCard stats={STATS} />)
    const text = container.textContent ?? ''
    expect(text).toContain('Media cache')
    expect(text).toContain('1.5 GiB of 5 GiB used')
    expect(text).toContain('1,234')
    expect(text).toContain('3 days')
    expect(text).toMatch(/since this node started/i)
    expect(text).toContain('17')
    expect(text).toContain('40 MiB')
    expect(text).toMatch(/3\b.*over a space's upload rate/)
    expect(text).toMatch(/1\b.*too large/)
    const bar = getByRole('progressbar')
    expect(bar.getAttribute('aria-valuenow')).toBe('30')
    expect(bar.getAttribute('aria-valuemin')).toBe('0')
    expect(bar.getAttribute('aria-valuemax')).toBe('100')
    expect(bar.getAttribute('aria-valuetext')).toBe('1.5 GiB of 5 GiB used')
  })

  it('caps the bar at 100 % when the cache is briefly over its cap', () => {
    const { getByRole } = render(
      <MediaCacheCard stats={{ ...STATS, stored_bytes: 6 * GIB }} />,
    )
    expect(getByRole('progressbar').getAttribute('aria-valuenow')).toBe('100')
  })

  it('shows a sliver for a cache in use that rounds to 0 %, none when empty', () => {
    const fill = (stored_bytes: number) => {
      const { container, unmount } = render(
        <MediaCacheCard stats={{ ...STATS, stored_bytes, max_bytes: 5 * 1024 * GIB }} />,
      )
      const width = (container.querySelector('.media-cache-bar span') as HTMLElement).style.width
      const now = container.querySelector('[role="progressbar"]')?.getAttribute('aria-valuenow')
      unmount()
      return { width, now }
    }
    // 5 MiB of 5 TiB: the value stays honest (0), the fill stays visible.
    expect(fill(5 * MIB)).toEqual({ width: '1%', now: '0' })
    expect(fill(0)).toEqual({ width: '0%', now: '0' })
  })

  it('is a region named by its heading, on and off', () => {
    for (const stats of [STATS, { ...STATS, enabled: false, max_bytes: 0 }]) {
      const { getByRole, unmount } = render(<MediaCacheCard stats={stats} />)
      expect(getByRole('region', { name: 'Media cache' })).toBeTruthy()
      unmount()
    }
  })

  it('keeps each refusal reason on one line', () => {
    const { container } = render(<MediaCacheCard stats={STATS} />)
    const parts = [...container.querySelectorAll('.media-cache-phrase')].map(
      el => el.textContent,
    )
    expect(parts).toEqual(["3 over a space's upload rate", '1 too large'])
  })

  it('shows the on-demand counts of this node (v_83)', () => {
    const { container } = render(
      <MediaCacheCard
        stats={{
          ...STATS,
          on_demand: true,
          holder_requests: 42,
          streams_served: 30,
          streamed_bytes: 12 * MIB,
          reuploads: 9,
          declined: 2,
          on_demand_misses: 5,
          live_sessions: 1,
        }}
      />,
    )
    const text = container.textContent ?? ''
    expect(text).toMatch(/missed pictures on request/i)
    expect(text).toMatch(/Holder requests\s*42/)
    expect(text).toMatch(/Streams served\s*30 \(12 MiB\)/)
    expect(text).toMatch(/Re-uploads\s*9/)
    expect(text).toMatch(/Declined\s*2/)
    expect(text).toMatch(/Not found\s*5/)
  })

  it('leaves the on-demand part out for an older server without the keys', () => {
    const { container } = render(<MediaCacheCard stats={STATS} />)
    const text = container.textContent ?? ''
    expect(text).not.toMatch(/on request/i)
    expect(text).not.toContain('Streams served')
    expect(text).not.toContain('Re-uploads')
  })

  it('says the on-demand part is off when the operator turned it off', () => {
    const { container } = render(
      <MediaCacheCard stats={{ ...STATS, on_demand: false, streams_served: 0 }} />,
    )
    const text = container.textContent ?? ''
    expect(text).toContain('turned off')
    expect(text).toContain('[space_media] on_demand')
    expect(text).not.toContain('Streams served')
  })

  it('says so clearly when the operator turned the cache off', () => {
    const { container, queryByRole } = render(
      <MediaCacheCard stats={{ ...STATS, enabled: false, max_bytes: 0 }} />,
    )
    const text = container.textContent ?? ''
    expect(text).toContain('Turned off')
    expect(text).toContain('[space_media] max_bytes')
    expect(text).toContain('GFS_SPACE_MEDIA_MAX_BYTES')
    expect(queryByRole('progressbar')).toBeNull()
  })
})

function stubFetch(body: unknown) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () =>
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    ),
  )
}

const BASE = {
  clients: { active: 3, pending: 1, banned: 0 },
  spaces: { active: 2, pending: 5, banned: 1 },
  open_reports: 7,
}

beforeEach(() => {
  vi.restoreAllMocks()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('OverviewPanel media cache', () => {
  it('renders the card from the overview', async () => {
    stubFetch({ ...BASE, space_media: STATS })
    const { container } = render(<OverviewPanel />)
    await waitFor(() => expect(container.textContent).toContain('Media cache'))
  })

  it('hides the card for an older server without the block', async () => {
    stubFetch(BASE)
    const { container } = render(<OverviewPanel />)
    await waitFor(() => expect(container.textContent).toContain('Open reports'))
    expect(container.textContent).not.toContain('Media cache')
  })

  it('hides the card when the block is null', async () => {
    stubFetch({ ...BASE, space_media: null })
    const { container } = render(<OverviewPanel />)
    await waitFor(() => expect(container.textContent).toContain('Open reports'))
    expect(container.textContent).not.toContain('Media cache')
  })
})
