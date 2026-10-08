import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/preact'
import { AUTO_RETRY_DELAYS_MS, RetryHost, ViewerError } from './viewer_retry'

/* A viewer stand-in that fails on every attempt and counts its mounts. */
function makeFailingViewer() {
  const mounts = { count: 0 }
  function Failing({ retry }: { retry: Parameters<Parameters<typeof RetryHost>[0]['render']>[0] }) {
    mounts.count += 1
    return <ViewerError message="This isn’t available right now." retry={retry} />
  }
  return { mounts, Failing }
}

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('ViewerError', () => {
  it('announces the error and moves focus to Try again', () => {
    const { Failing } = makeFailingViewer()
    render(<RetryHost render={(retry, attempt) => <Failing key={attempt} retry={retry} />} />)
    const alert = screen.getByRole('alert')
    expect(alert.textContent).toContain('This isn’t available right now.')
    const button = screen.getByRole('button', { name: 'Try again' })
    expect(document.activeElement).toBe(button)
  })
})

describe('RetryHost auto-retry', () => {
  it('retries after 10 s, 30 s and 60 s, then stops', () => {
    expect(AUTO_RETRY_DELAYS_MS).toEqual([10_000, 30_000, 60_000])
    const { mounts, Failing } = makeFailingViewer()
    render(<RetryHost render={(retry, attempt) => <Failing key={attempt} retry={retry} />} />)
    const start = mounts.count

    act(() => { vi.advanceTimersByTime(9_999) })
    expect(mounts.count).toBe(start)
    act(() => { vi.advanceTimersByTime(1) })
    const afterFirst = mounts.count
    expect(afterFirst).toBeGreaterThan(start)

    act(() => { vi.advanceTimersByTime(29_999) })
    expect(mounts.count).toBe(afterFirst)
    act(() => { vi.advanceTimersByTime(1) })
    const afterSecond = mounts.count
    expect(afterSecond).toBeGreaterThan(afterFirst)

    act(() => { vi.advanceTimersByTime(60_000) })
    const afterThird = mounts.count
    expect(afterThird).toBeGreaterThan(afterSecond)

    // Budget spent — no further automatic attempts, however long we wait.
    act(() => { vi.advanceTimersByTime(10 * 60_000) })
    expect(mounts.count).toBe(afterThird)
    // The manual control still works.
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(mounts.count).toBeGreaterThan(afterThird)
  })

  it('a manual retry cancels the pending automatic one', () => {
    const { mounts, Failing } = makeFailingViewer()
    render(<RetryHost render={(retry, attempt) => <Failing key={attempt} retry={retry} />} />)
    act(() => { vi.advanceTimersByTime(5_000) })
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    const afterManual = mounts.count
    // The 10 s timer from the first attempt must not fire on top: the
    // next automatic attempt is the FIRST delay again from the new mount.
    act(() => { vi.advanceTimersByTime(5_000) })
    expect(mounts.count).toBe(afterManual)
  })

  it('unmounting cancels the pending retry', () => {
    const { mounts, Failing } = makeFailingViewer()
    const { unmount } = render(
      <RetryHost render={(retry, attempt) => <Failing key={attempt} retry={retry} />} />,
    )
    const before = mounts.count
    unmount()
    act(() => { vi.advanceTimersByTime(120_000) })
    expect(mounts.count).toBe(before)
  })
})
