import type { ComponentChildren } from 'preact'
import { useEffect, useRef } from 'preact/hooks'

interface ModalProps {
  open: boolean
  onClose: () => void
  title: string
  children: ComponentChildren
}

/**
 * Modal dialog surface (§23.21 accessibility).
 *
 * All household dialogs (Confirm, SpaceCreate, NewDm, BazaarCreate,
 * CalendarEvent, Report, etc.) compose on top of this component, so
 * wiring ``role="dialog"`` + ``aria-modal`` + a focus trap + Escape
 * handling here lifts the whole suite of dialogs without touching
 * each subclass.
 *
 * Focus-trap semantics (keyboard-only users):
 *   - On open, focus moves into the dialog automatically.
 *   - Tab at the last focusable element wraps to the first.
 *   - Shift+Tab at the first focusable element wraps to the last.
 *   - Escape invokes ``onClose`` — on the TOPMOST dialog only. Dialogs
 *     stack (a confirm prompt opens over the dialog that asked for it),
 *     and every open Modal listens on ``document``; without the stack
 *     check one Escape closed the confirm AND the dialog behind it, so
 *     cancelling a revoke also threw the user out of the invite tray.
 */

/** Open dialogs, oldest first. Only the last entry reacts to keys. */
const _stack: object[] = []
export function Modal({ open, onClose, title, children }: ModalProps) {
  const dialogRef = useRef<HTMLDivElement | null>(null)
  const titleId = useRef<string>(`sh-modal-title-${Math.random().toString(36).slice(2, 8)}`)
  // Stash ``onClose`` in a ref so the focus-trap effect only depends on
  // ``open``. Callers usually pass an inline arrow (``onClose={() =>
  // open.value = false}``) — without this ref, every parent re-render
  // would change the function identity, re-run the effect, and yank
  // focus back to the close ``×`` button on every keystroke. (Hit on
  // SpaceCreateDialog: typing the space name kept jumping out of the
  // input.)
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose

  useEffect(() => {
    if (!open) return
    const dialog = dialogRef.current
    if (!dialog) return

    // Remember the element that had focus pre-open so we can restore.
    const previouslyFocused = document.activeElement as HTMLElement | null

    // Claim the top of the dialog stack for as long as we are open.
    const token = {}
    _stack.push(token)
    const isTopmost = () => _stack[_stack.length - 1] === token

    // Move focus into the dialog — first focusable element on
    // pointer-precise devices. On touch devices we land on the
    // dialog itself (tabindex=-1) so the soft keyboard doesn't pop up
    // immediately — that covers half the bottom-sheet and obscures
    // the form. The user reaches the first input with one Tab or a
    // single tap; the trade-off is a calmer first impression. */
    const focusables = _focusable(dialog)
    const isCoarsePointer =
      typeof window !== 'undefined'
      && typeof window.matchMedia === 'function'
      && window.matchMedia('(pointer: coarse)').matches
    if (isCoarsePointer) {
      dialog.focus()
    } else {
      const target = focusables[0] ?? dialog
      target.focus()
    }

    function onKeyDown(ev: KeyboardEvent) {
      // A dialog underneath a newer one neither closes nor traps focus.
      if (!isTopmost()) return
      if (ev.key === 'Escape') {
        ev.preventDefault()
        onCloseRef.current()
        return
      }
      if (ev.key !== 'Tab') return
      const items = _focusable(dialog!)
      if (items.length === 0) return
      const first = items[0]
      const last = items[items.length - 1]
      if (ev.shiftKey && document.activeElement === first) {
        ev.preventDefault()
        last.focus()
      } else if (!ev.shiftKey && document.activeElement === last) {
        ev.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      const at = _stack.indexOf(token)
      if (at !== -1) _stack.splice(at, 1)
      // Restore focus to whatever triggered the dialog.
      previouslyFocused?.focus?.()
    }
  }, [open])

  if (!open) return null
  return (
    <div class="sh-modal-overlay" onClick={onClose}>
      <div
        ref={dialogRef}
        class="sh-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId.current}
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Mobile drag-affordance — purely decorative on web (the modal
         *  doesn't actually drag), but the visual cue tells touch users
         *  this is a dismissible sheet. Hidden via CSS on ≥640px. */}
        <div class="sh-modal-grabber" aria-hidden="true" />
        <div class="sh-modal-header">
          <h2 id={titleId.current}>{title}</h2>
          <button class="sh-modal-close" onClick={onClose} aria-label="Close dialog">
            &times;
          </button>
        </div>
        <div class="sh-modal-body">{children}</div>
      </div>
    </div>
  )
}


const _FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

function _focusable(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(_FOCUSABLE_SELECTOR))
    .filter(el => !el.hasAttribute('aria-hidden'))
}
