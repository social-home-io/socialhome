/* Retry plumbing shared by the GFS public viewers (highlight + moments).
 *
 * ``RetryHost`` re-mounts its child (by ``key``) for every attempt, so a
 * retry re-runs the viewer's connect effect from a clean slate, while the
 * automatic-retry budget lives in the host and survives those re-mounts.
 *
 * ``ViewerError`` is the not-available state: announced to screen readers
 * (``role="alert"``), focus moved to *Try again*, and an automatic retry
 * after 10 s, 30 s and 60 s, then no more (the old offline page
 * refreshed itself every 10 s forever). A manual retry or an unmount
 * cancels the pending timer.
 */
import type { ComponentChildren } from 'preact'
import { useEffect, useRef, useState } from 'preact/hooks'

export const AUTO_RETRY_DELAYS_MS: readonly number[] = [10_000, 30_000, 60_000]

export interface RetryControl {
  /** User-initiated retry — does not spend the automatic budget. */
  manual: () => void
  /** Automatic retry — spends one step of the backoff. */
  auto: () => void
  /** Delay before the next automatic retry, or ``null`` once spent. */
  nextAutoDelayMs: () => number | null
}

export function RetryHost({
  render,
}: {
  render: (retry: RetryControl, attempt: number) => ComponentChildren
}) {
  const [attempt, setAttempt] = useState(0)
  const autoUsed = useRef(0)
  const retry: RetryControl = {
    manual: () => setAttempt((a) => a + 1),
    auto: () => {
      autoUsed.current += 1
      setAttempt((a) => a + 1)
    },
    nextAutoDelayMs: () => AUTO_RETRY_DELAYS_MS[autoUsed.current] ?? null,
  }
  return <>{render(retry, attempt)}</>
}

export function ViewerError({
  message,
  retry,
  className,
}: {
  message: string
  retry: RetryControl
  className?: string
}) {
  const button = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    // ``preventScroll``: the alert is already announced; don't yank the
    // page (the moments viewer sits below the profile card).
    button.current?.focus({ preventScroll: true })
  }, [])

  useEffect(() => {
    const delay = retry.nextAutoDelayMs()
    if (delay === null) return undefined
    const timer = setTimeout(retry.auto, delay)
    return () => clearTimeout(timer)
    // Once per mount: each attempt is a fresh mount (``RetryHost`` key).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div class={className} role="alert">
      <p>{message}</p>
      <button ref={button} type="button" class="viewer-retry" onClick={retry.manual}>
        Try again
      </button>
    </div>
  )
}
