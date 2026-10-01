/**
 * TouchDragHint — on a touch screen, a one-time tip above the board
 * that cards move by press-and-hold (or the ⋯ menu). Dismissed for good
 * in ``localStorage`` (a per-viewer convenience: blocked storage just
 * means the tip comes back next visit).
 */
import { useState } from 'preact/hooks'
import { t } from '@/i18n/i18n'

export const DRAG_HINT_KEY = 'sh-tasks-drag-hint-dismissed'

function coarse(): boolean {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    && window.matchMedia('(pointer: coarse)').matches
}

function dismissed(): boolean {
  try {
    return localStorage.getItem(DRAG_HINT_KEY) === '1'
  } catch {
    return false
  }
}

export function TouchDragHint() {
  const [show, setShow] = useState(() => coarse() && !dismissed())
  if (!show) return null
  const dismiss = () => {
    setShow(false)
    try {
      localStorage.setItem(DRAG_HINT_KEY, '1')
    } catch {
      // Storage blocked — hidden for this visit only.
    }
  }
  return (
    <div class="sh-board-hint" role="note">
      <span class="sh-board-hint__text">{t('tasks.board.touch_hint')}</span>
      <button type="button" class="sh-icon-btn sh-row-action sh-board-hint__close"
              aria-label={t('tasks.board.touch_hint_dismiss')} onClick={dismiss}>
        <span aria-hidden="true">✕</span>
      </button>
    </div>
  )
}
