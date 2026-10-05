import { signal } from '@preact/signals'
import { t as tr } from '@/i18n/i18n'

/** One inline action on a toast — typically "Undo". */
export interface ToastAction {
  label: string
  onClick: () => void
}

export interface ToastOptions {
  /** Renders a button in the toast; clicking it runs ``onClick`` and
   *  dismisses the toast. */
  action?: ToastAction
  /** Auto-dismiss delay in ms. Defaults to 4 s, or 8 s for a toast
   *  with an action so there is time to reach the button. */
  duration?: number
  /** Runs once when the toast leaves WITHOUT its action being used —
   *  its auto-dismiss timer ran out, or a newer toast pushed it past
   *  the visible cap. Never runs when the action button is clicked.
   *  The deferred-commit half of "delete with Undo"
   *  (``utils/undoableDelete.ts``). */
  onExpire?: () => void
}

interface ToastItem {
  id: number
  message: string
  type: 'info' | 'success' | 'error'
  /** How many identical (message+type) toasts have been collapsed
   *  into this row. Rendered as "× N" when > 1. */
  count: number
  /** Active auto-dismiss timer id — kept on the row so a duplicate
   *  call can clear it before scheduling a fresh one. */
  timeoutId: ReturnType<typeof setTimeout>
  action?: ToastAction
  duration: number
  onExpire?: () => void
}

/** Cap on the number of toast rows visible at once. WS bursts (a
 *  flurry of ``post.created`` while the user scrolls a busy feed)
 *  could otherwise stack a tall queue the user has to wait through. */
const MAX_VISIBLE = 3

/** How long a single toast stays on screen before auto-dismissal. */
const DISMISS_AFTER_MS = 4000
/** …and one that carries an action button (WCAG 2.2.1 — enough time
 *  to reach "Undo" by keyboard). */
const ACTION_DISMISS_AFTER_MS = 8000

let nextId = 0
export const toasts = signal<ToastItem[]>([])

function dropById(id: number) {
  toasts.value = toasts.value.filter(t => t.id !== id)
}

/** Run a leaving toast's ``onExpire`` — isolated so a throwing
 *  callback can't break the toast stack. */
function fireExpire(row: ToastItem | undefined) {
  if (!row?.onExpire) return
  try {
    row.onExpire()
  } catch (err) {
    console.error('toast onExpire failed', err)
  }
}

/** Auto-dismiss: the toast ran out of time, so it EXPIRED. */
function expireById(id: number) {
  const row = toasts.value.find(t => t.id === id)
  if (!row) return
  dropById(id)
  fireExpire(row)
}

function scheduleDismiss(
  id: number, ms: number = DISMISS_AFTER_MS,
): ReturnType<typeof setTimeout> {
  return setTimeout(() => expireById(id), ms)
}

/** Stop a toast's auto-dismiss (hovered, or its action button has focus). */
function pauseDismiss(id: number) {
  const row = toasts.value.find(t => t.id === id)
  if (row) clearTimeout(row.timeoutId)
}

/** Restart a paused toast's auto-dismiss with its full duration. */
function resumeDismiss(id: number) {
  const row = toasts.value.find(t => t.id === id)
  if (!row) return
  clearTimeout(row.timeoutId)
  const timeoutId = scheduleDismiss(id, row.duration)
  toasts.value = toasts.value.map(t => t.id === id ? { ...t, timeoutId } : t)
}

/** Remove a toast now — no ``onExpire``, no action. For callers that
 *  settled the toast's business some other way (e.g. a flushed
 *  deferred delete). Unknown / already-gone ids are ignored. */
export function dismissToast(id: number): void {
  const row = toasts.value.find(t => t.id === id)
  if (!row) return
  clearTimeout(row.timeoutId)
  dropById(id)
}

/** Shows a toast and returns its id (for ``dismissToast``). A call that
 *  collapses into an identical row returns that row's id. */
export function showToast(
  message: string,
  type: ToastItem['type'] = 'info',
  opts: ToastOptions = {},
): number {
  const duration = opts.duration
    ?? (opts.action ? ACTION_DISMISS_AFTER_MS : DISMISS_AFTER_MS)
  // Dedupe: if the same (message, type) is already on screen, bump
  // its count and reset the timer instead of pushing a duplicate row.
  // Slack / WhatsApp do the same — three "Sent" toasts in a row read
  // as noise, "Sent × 3" reads as a count. Action toasts never
  // collapse: each one's callback undoes a different change.
  const existing = opts.action ? undefined : toasts.value.find(
    t => t.message === message && t.type === type && !t.action,
  )
  if (existing) {
    clearTimeout(existing.timeoutId)
    const timeoutId = scheduleDismiss(existing.id, duration)
    toasts.value = toasts.value.map(t =>
      t.id === existing.id
        ? { ...t, count: t.count + 1, timeoutId }
        : t,
    )
    return existing.id
  }

  const id = nextId++
  const timeoutId = scheduleDismiss(id, duration)
  const next = [
    ...toasts.value,
    {
      id, message, type, count: 1, timeoutId,
      action: opts.action, duration, onExpire: opts.onExpire,
    },
  ]
  // Cap visible rows. When overflowing, evict the oldest entries —
  // an evicted toast counts as expired (its Undo is gone for good).
  const evicted: ToastItem[] = []
  while (next.length > MAX_VISIBLE) {
    const dropped = next.shift()
    if (dropped) {
      clearTimeout(dropped.timeoutId)
      evicted.push(dropped)
    }
  }
  toasts.value = next
  evicted.forEach(fireExpire)
  return id
}

export function ToastContainer() {
  return (
    <div class="sh-toast-container" role="region" aria-live="polite">
      {toasts.value.map(t => (
        <div key={t.id} class={`sh-toast sh-toast--${t.type}`}
             onMouseEnter={() => pauseDismiss(t.id)}
             onMouseLeave={() => resumeDismiss(t.id)}>
          <span class="sh-toast-message">{t.message}</span>
          {t.count > 1 && (
            <span class="sh-toast-count" aria-label={tr('toast.occurrences', { n: String(t.count) })}>
              × {t.count}
            </span>
          )}
          {t.action && (
            <button
              type="button"
              class="sh-toast-action"
              onClick={() => {
                const action = t.action!
                clearTimeout(t.timeoutId)
                dropById(t.id)
                action.onClick()
              }}
              onFocus={() => pauseDismiss(t.id)}
              onBlur={() => resumeDismiss(t.id)}
            >
              {t.action.label}
            </button>
          )}
        </div>
      ))}
    </div>
  )
}
