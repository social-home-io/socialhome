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
import { setLocale } from '@/i18n/i18n'
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
    ['behind', false, '⏳Getting the space ready', "The first posts from The Müllers haven't arrived yet."],
    ['behind', true, '⚠Some posts may be missing', "We'll keep trying every 30 minutes. You can check now."],
    ['never', false, '⏳Getting the space ready', "The first posts from The Müllers haven't arrived yet."],
  ])('%s (may be missing: %s)', (state, missing, head, text) => {
    const { container } = section({ ...base, state, may_be_missing: missing, last_ok_at: null })
    expect(heading(container)).toBe(head)
    expect(body(container)).toBe(text)
  })

  it('one failed round after an ok, no stream: "Checking again soon", not "on their way"', () => {
    const { container } = section({ ...base, state: 'behind' })
    expect(heading(container)).toBe('↻Checking again soon')
    expect(body(container)).toBe("The last check didn't bring everything. It tries again every 30 minutes.")
  })

  it('an away host: neutral, plural-safe, with a full date (not a bare time)', () => {
    const at = '2026-11-21T07:03:00Z'
    const { container } = section({ ...base, state: 'host_offline', last_ok_at: at })
    expect(heading(container)).toBe("☾Can't reach The Müllers right now")
    expect(container.querySelector('.sh-sync-card__state--away')).not.toBeNull()
    expect(body(container)).toMatch(/^You're seeing everything up to .+\. What you post goes out once the connection is back\.$/)
    const shown = container.querySelector('.sh-sync-card__body time')!.textContent!
    expect(shown).toMatch(/21/)
    expect(shown).toMatch(/Nov/)
  })

  it('without a known name it says "the host"', () => {
    const { container } = section({ ...base, state: 'host_offline' }, { hostName: null })
    expect(heading(container)).toBe("☾Can't reach the host right now")
    const here = section({ ...base, state: 'up_to_date', last_ok_at: null }, { hostName: null })
    expect(body(here.container)).toBe('Checked with the host.')
  })

  it('a space hosted here catching up says so — no warning, no Check now', () => {
    const { container } = section(
      { ...base, state: 'catching_up', may_be_missing: false },
      { hostedHere: true },
    )
    expect(heading(container)).toBe('↻Getting the latest…')
    expect(body(container)).toBe(
      'Getting what an admin household received while this one was away.',
    )
    expect(button(container)).toBeNull()
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

  it('stays enabled while the host is away: a check asks it again, and finds it back', () => {
    const { container } = section({ ...base, state: 'host_offline' })
    expect(button(container)!.disabled).toBe(false)
    expect(why(container)).toBeNull()
  })

  it('a sentence that starts with its time starts with a capital (de)', async () => {
    await setLocale('de')
    try {
      const { container } = section({
        ...base, state: 'up_to_date',
        page: { seq: 4, at: ago(20), match: true, missing: 0, differing_buckets: 0, signer_role: 'owner', conflict: false },
      })
      expect(body(container)).toMatch(/^Vor 20 Min\. geprüft: Es fehlt nichts\.$/)
    } finally {
      await setLocale('en')
    }
  })

  it('after a match with the signed page: its time, and no host named', () => {
    const at = ago(20)
    const { container } = section({
      ...base, last_ok_at: ago(200), state: 'up_to_date',
      page: { seq: 4, at, match: true, missing: 0, differing_buckets: 0, signer_role: 'owner', conflict: false },
    })
    expect(body(container)).not.toContain('The Müllers')
    expect(body(container)).toContain('nothing is missing')
    expect(container.querySelector('.sh-sync-card__body time')?.getAttribute('datetime')).toBe(at)
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

  it('a reload already knows the wait (next_check_at) and counts down', () => {
    const next = new Date(Date.now() + 25 * 60_000 - 1000).toISOString()
    const { container } = section({ ...base, state: 'behind', may_be_missing: true, next_check_at: next })
    expect(button(container)?.disabled).toBe(true)
    expect(why(container)?.textContent).toBe('You can check again in 25 min')
  })

  it('a next_check_at in the past leaves Check now on', () => {
    const past = new Date(Date.now() - 1000).toISOString()
    const { container } = section({ ...base, state: 'behind', may_be_missing: true, next_check_at: past })
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
