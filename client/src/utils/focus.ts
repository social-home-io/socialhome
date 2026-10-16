/**
 * Return focus to ``target`` (the control that opened a dialog) after
 * the dialog closes — unless something else already took focus.
 *
 * Preact 11 runs a removed component's ``useEffect`` cleanup after
 * paint. By then a surface that replaced the dialog (another dialog, the
 * call page) may have focused its own control; pulling focus back to the
 * old opener would strand keyboard and screen-reader users outside the
 * surface now on screen. Focus is "free" when it sits on ``<body>``
 * (the browser parks it there when the focused node is removed), on a
 * detached node, or still inside the closing ``surface``.
 */
export function restoreFocus(target: HTMLElement | null, surface?: Element | null): void {
  if (!target?.focus) return
  const active = document.activeElement
  const free = !active
    || active === document.body
    || active === document.documentElement
    || !active.isConnected
    || (surface != null && surface.contains(active))
  if (free) target.focus()
}
