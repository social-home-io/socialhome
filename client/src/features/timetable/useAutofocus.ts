/**
 * Focus (and optionally select) a field when its dialog opens.
 *
 * ``Modal`` focuses its first focusable element — the close "×" — so a
 * dialog whose first job is typing a name moves focus on.
 * Skipped on touch-primary devices, where Modal deliberately keeps the
 * soft keyboard down so it doesn't cover half the sheet.
 */
import { useEffect } from 'preact/hooks'

export function useAutofocus(
  ref: { current: HTMLInputElement | null },
  open: boolean,
  opts: { select?: boolean } = {},
): void {
  useEffect(() => {
    if (!open) return
    const coarse = typeof window.matchMedia === 'function'
      && window.matchMedia('(pointer: coarse)').matches
    if (coarse) return
    // A microtask runs after the whole effect flush, i.e. after Modal
    // (an ancestor, so its effect fires after ours) moved focus.
    queueMicrotask(() => {
      const el = ref.current
      if (!el) return
      el.focus()
      if (opts.select) el.select()
    })
  }, [open, ref, opts.select])
}
