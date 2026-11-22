import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor, act } from '@testing-library/preact'

const { apiPost, toast, handlers } = vi.hoisted(() => ({
  apiPost: vi.fn(),
  toast: vi.fn(),
  handlers: new Map<string, (e: { type: string; data: Record<string, unknown> }) => void>(),
}))
vi.mock('@/api', async () => {
  const real = await vi.importActual<typeof import('@/api')>('@/api')
  return { ...real, api: { post: apiPost } }
})
vi.mock('@/components/Toast', () => ({ showToast: toast }))
vi.mock('@/ws', () => ({
  ws: {
    on: (type: string, fn: (e: { type: string; data: Record<string, unknown> }) => void) => {
      handlers.set(type, fn)
      return () => { handlers.delete(type) }
    },
  },
}))

import { ApiError } from '@/api'
import { SyncSection } from './SyncSection'

const ago = (min: number) => new Date(Date.now() - min * 60_000).toISOString()
const base = { last_ok_at: ago(5), waiting_to_send: 0, may_be_missing: false, unsent_posts: [] }

function section(sync: unknown, opts: { hostedHere?: boolean; hostName?: string | null; isAdmin?: boolean } = {}) {
  return render(
    <SyncSection spaceId="s1" sync={sync} hostedHere={opts.hostedHere ?? false}
                 hostName={opts.hostName === undefined ? 'The Müllers' : opts.hostName}
                 isAdmin={opts.isAdmin ?? false} />,
  )
}

const heading = (c: Element) => c.querySelector('.sh-sync-card__state')?.textContent ?? ''
const body = (c: Element) => c.querySelector('.sh-sync-card__body')?.textContent ?? ''
const button = (c: Element) => c.querySelector('[data-testid="space-sync-now"]') as HTMLButtonElement | null
const why = (c: Element) => c.querySelector('[data-testid="space-sync-why"]')

beforeEach(() => { apiPost.mockReset(); toast.mockReset(); handlers.clear() })
afterEach(() => { vi.useRealTimers() })

describe('SyncSection — "Is everything here?"', () => {
  it('everything here: checked with the host household, a full date on hover', () => {
    const at = ago(5)
    const { container } = section({ ...base, state: 'up_to_date', last_ok_at: at })
    expect(heading(container)).toBe("✓Everything's here")
    expect(body(container)).toBe('Checked with The Müllers 5 min ago.')
    const time = container.querySelector('.sh-sync-card__body time') as HTMLTimeElement
    expect(time.getAttribute('datetime')).toBe(at)
    expect(time.getAttribute('title')).toMatch(/\d{4}/)
  })

  it.each([
    ['catching_up', false, '↻Getting the latest…', 'New posts from The Müllers are on their way.'],
    ['behind', false, '↻Getting the latest…', 'New posts from The Müllers are on their way.'],
    ['behind', true, '⚠Some posts may be missing', "We'll keep trying every 30 minutes. You can check now."],
    ['never', false, '⏳Getting the space ready', "The first posts from The Müllers haven't arrived yet."],
  ])('%s (may be missing: %s)', (state, missing, head, text) => {
    const { container } = section({ ...base, state, may_be_missing: missing, last_ok_at: null })
    expect(heading(container)).toBe(head)
    expect(body(container)).toBe(text)
  })

  it('an away host: neutral heading by name, what you see and when it goes out', () => {
    const { container } = section({ ...base, state: 'host_offline' })
    expect(heading(container)).toBe('☾The Müllers is away')
    expect(container.querySelector('.sh-sync-card__state--away')).not.toBeNull()
    expect(body(container)).toMatch(/^You're seeing everything up to .+\. What you post goes out when they're back\.$/)
  })

  it('without a known name it says "the host"', () => {
    const { container } = section({ ...base, state: 'host_offline' }, { hostName: null })
    expect(heading(container)).toBe('☾The host is away')
    expect(why(container)?.textContent).toBe('Checking needs the host to be online.')
    const here = section({ ...base, state: 'up_to_date', last_ok_at: null }, { hostName: null })
    expect(body(here.container)).toBe('Checked with the host.')
  })

  it('a space hosted here says so, with no Check now', () => {
    const { container } = section({ ...base, state: 'up_to_date', last_ok_at: null }, { hostedHere: true, isAdmin: true })
    expect(heading(container)).toBe("✓Everything's here")
    expect(body(container)).toBe('This household hosts the space, so it always has everything.')
    expect(button(container)).toBeNull()
    expect(container.querySelector('[data-testid="space-sync-details"]')).toBeNull()
  })

  it('renders nothing without a status (a follower)', () => {
    expect(section(null).container.innerHTML).toBe('')
  })

  it('a live frame updates the heading', () => {
    const { container } = section({ ...base, state: 'behind', may_be_missing: true })
    act(() => {
      handlers.get('space.sync_state')?.({ type: 'space.sync_state',
        data: { space_id: 's1', sync: { ...base, state: 'up_to_date' } } })
    })
    expect(heading(container)).toBe("✓Everything's here")
  })
})

describe('Check now', () => {
  it('asks the server and says "Checking…"', async () => {
    apiPost.mockResolvedValue({ space_id: 's1', targets: ['h'] })
    const { container } = section({ ...base, state: 'behind', may_be_missing: true })
    expect(button(container)?.textContent).toBe('Check now')
    expect(button(container)?.disabled).toBe(false)
    fireEvent.click(button(container)!)
    await waitFor(() => expect(toast).toHaveBeenCalledWith('Checking…', 'info'))
    expect(apiPost).toHaveBeenCalledWith('/api/spaces/s1/sync')
  })

  it('is disabled with a visible reason while the host is away', () => {
    const { container } = section({ ...base, state: 'host_offline' })
    const b = button(container)!
    expect(b.disabled).toBe(true)
    const reason = why(container)!
    expect(reason.textContent).toBe('Checking needs The Müllers to be online.')
    expect(b.getAttribute('aria-describedby')).toBe(reason.id)
  })

  it('rate-limited: disabled with "You can check again in {m} min", then back', async () => {
    apiPost.mockRejectedValue(new ApiError(429, '/api/spaces/s1/sync',
      { code: 'RATE_LIMITED', retry_after_s: 1500 } as never))
    const { container } = section({ ...base, state: 'behind', may_be_missing: true })
    fireEvent.click(button(container)!)
    await waitFor(() => expect(why(container)?.textContent).toBe('You can check again in 25 min'))
    expect(button(container)?.disabled).toBe(true)
    expect(button(container)?.getAttribute('aria-describedby')).toBe(why(container)?.id)
    expect(toast).not.toHaveBeenCalled()
  })

  it('the wait ends and the button comes back', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    apiPost.mockRejectedValue(new ApiError(429, '/api/spaces/s1/sync',
      { code: 'RATE_LIMITED', retry_after_s: 90 } as never))
    const { container } = section({ ...base, state: 'behind', may_be_missing: true })
    fireEvent.click(button(container)!)
    await waitFor(() => expect(why(container)?.textContent).toBe('You can check again in 2 min'))
    await act(async () => { vi.advanceTimersByTime(60_000) })
    expect(why(container)?.textContent).toBe('You can check again in 1 min')
    await act(async () => { vi.advanceTimersByTime(31_000) })
    expect(why(container)).toBeNull()
    expect(button(container)?.disabled).toBe(false)
  })

  it('a failure says so', async () => {
    apiPost.mockRejectedValue(new Error('offline'))
    const { container } = section({ ...base, state: 'never', last_ok_at: null })
    fireEvent.click(button(container)!)
    await waitFor(() => expect(toast).toHaveBeenCalledWith("Couldn't check. Try again.", 'error'))
  })
})

describe('Details (space admins only)', () => {
  const page = { seq: 41, at: ago(30), match: true, missing: 0, differing_buckets: 0,
    signer_role: 'owner', conflict: false }

  it('an admin gets the collapsed technical details', () => {
    const { container } = section({ ...base, state: 'up_to_date', waiting_to_send: 2, page }, { isAdmin: true })
    const details = container.querySelector('[data-testid="space-sync-details"]') as HTMLDetailsElement
    expect(details.open).toBe(false)
    expect(details.querySelector('summary')?.textContent).toBe('Details')
    const lines = [...details.querySelectorAll('li')].map(li => li.textContent)
    expect(lines).toEqual([
      'Matches page 41 · 30 min ago', 'Signed by the owner', '2 waiting to send',
      'Last successful sync 5 min ago',
    ])
  })

  it('a member does not', () => {
    const { container } = section({ ...base, state: 'up_to_date', waiting_to_send: 2, page })
    expect(container.querySelector('[data-testid="space-sync-details"]')).toBeNull()
    expect(container.textContent).not.toContain('page')
    expect(container.textContent).not.toContain('waiting')
  })
})
