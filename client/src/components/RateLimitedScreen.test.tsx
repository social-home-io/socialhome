import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, cleanup, act } from '@testing-library/preact'
import { RateLimitedScreen } from './RateLimitedScreen'

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('RateLimitedScreen — a rate-limited cold start', () => {
  it('says what happened and counts down to the automatic retry', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(50_000)
    const { getByRole, getByText, queryByRole } = render(<RateLimitedScreen until={62_000} />)
    expect(getByRole('heading', { name: 'Too many requests' })).toBeTruthy()
    expect(getByText('Trying again in 12 s…')).toBeTruthy()
    // Never a login form or a password field.
    expect(queryByRole('textbox')).toBeNull()
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000) })
    expect(getByText('Trying again in 7 s…')).toBeTruthy()
    await act(async () => { await vi.advanceTimersByTimeAsync(8_000) })
    expect(getByText('Trying again now…')).toBeTruthy()
  })

  it('the countdown is not announced every second (live region on the title only)', () => {
    vi.useFakeTimers()
    vi.setSystemTime(0)
    const { container } = render(<RateLimitedScreen until={3_000} />)
    const live = container.querySelectorAll('[aria-live]')
    expect(live.length).toBe(1)
    expect(live[0]!.textContent).not.toMatch(/\d+ s/)
  })
})
