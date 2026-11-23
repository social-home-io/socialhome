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

/**
 * Whether a keydown in a growing text field sends it — one rule for the
 * chat composer and comments:
 *
 *  - mouse / keyboard ``(pointer: fine)``: Enter sends, Shift+Enter is a
 *    new line (Slack, WhatsApp Web);
 *  - touch ``(pointer: coarse)``: Return is a new line and the Send
 *    button sends. An on-screen keyboard has no Shift+Enter, and a
 *    half-typed thought shouldn't go out by accident (WhatsApp mobile);
 *  - an Enter that confirms an input-method choice (Japanese, Chinese…)
 *    never sends: ``isComposing``, or ``keyCode`` 229 in Safari.
 */
export function isSendKey(e: KeyboardEvent, win: Window = window): boolean {
  if (e.key !== 'Enter' || e.shiftKey) return false
  if (e.isComposing || e.keyCode === 229) return false
  return !win.matchMedia?.('(pointer: coarse)').matches
}
