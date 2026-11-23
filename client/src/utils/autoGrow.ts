/**
 * Auto-growing text fields — the chat composer and the comment field.
 *
 * A ``<textarea rows={1}>`` grows with what is typed up to a cap, then
 * scrolls inside; it shrinks again when text is deleted or sent. Call
 * :func:`growTextarea` after every value change (typing, an inserted
 * emoji, a reset after sending).
 */

/**
 * Fit ``el``'s height to its content, at most ``cap`` px. ``height:
 * auto`` first lets it shrink when lines go. The scrollbar shows only
 * once the cap is reached: below it a classic (Linux / Windows)
 * scrollbar would reserve a gutter and show step arrows at rest.
 */
export function growTextarea(el: HTMLTextAreaElement, cap: number): void {
  el.style.height = 'auto'
  // Under ``box-sizing: border-box`` ``height`` includes the border but
  // ``scrollHeight`` does not: add the border so one line fits without a
  // scrollbar.
  const borderDelta = el.offsetHeight - el.clientHeight
  const target = Math.min(el.scrollHeight + borderDelta, cap)
  el.style.height = `${target}px`
  el.style.overflowY = target >= cap ? 'auto' : 'hidden'
}
