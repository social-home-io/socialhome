/**
 * Global single-key keyboard shortcuts.
 *
 * * ``/`` — focus the top-bar search field.
 * * ``n`` — focus the current page's composer (feed / space post box,
 *   DM reply box) when the page has one.
 * * ``?`` — open the keyboard-shortcuts help dialog.
 *
 * Cmd/Ctrl+K (QuickSwitcher) and Esc (dialogs) live with their own
 * components and are only *listed* in the help dialog.
 *
 * Targets opt in with a ``data-shortcut`` attribute (``search`` /
 * ``composer``) instead of CSS classes, so a restyle can't silently
 * break a shortcut.
 *
 * :func:`installKeyboardShortcuts` is called explicitly from ``App``
 * (never as an import side effect) and returns its teardown.
 */
import { signal } from '@preact/signals'

/** Whether the help dialog is open. */
export const shortcutsHelpOpen = signal(false)

export function openShortcutsHelp(): void {
  shortcutsHelpOpen.value = true
}

/** Open overlays that own the keyboard while visible: any aria-modal
 *  dialog (Modal, lightbox, mobile drawer) and the QuickSwitcher. */
const MODAL_SELECTOR = '[aria-modal="true"], .sh-switcher-overlay'

function isTypingTarget(el: EventTarget | null): boolean {
  if (!(el instanceof HTMLElement)) return false
  if (el.isContentEditable) return true
  // ``contenteditable`` attribute check too — jsdom doesn't implement
  // ``isContentEditable``.
  if (el.closest('[contenteditable]:not([contenteditable="false"])')) return true
  const tag = el.tagName
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT'
}

/** True when a keypress must NOT trigger a global shortcut: the user
 *  is typing, holds a modifier (Shift is allowed — ``?`` needs it on
 *  most layouts), is mid IME composition, auto-repeats, another
 *  handler already claimed it, or a modal owns the keyboard. */
export function shouldIgnoreShortcut(e: KeyboardEvent): boolean {
  if (e.defaultPrevented || e.repeat || e.isComposing) return true
  if (e.metaKey || e.ctrlKey || e.altKey) return true
  if (isTypingTarget(e.target) || isTypingTarget(document.activeElement)) return true
  return document.querySelector(MODAL_SELECTOR) !== null
}

function focusTarget(name: 'search' | 'composer'): boolean {
  // Prefer the target inside the main column so a composer in a
  // background panel never wins over the page's own.
  const el = document.querySelector<HTMLElement>(`#main [data-shortcut="${name}"]`)
    ?? document.querySelector<HTMLElement>(`[data-shortcut="${name}"]`)
  if (!el) return false
  el.focus()
  return true
}

/** Handle one keydown. Returns true when a shortcut fired. */
export function handleShortcutKey(e: KeyboardEvent): boolean {
  if (shouldIgnoreShortcut(e)) return false
  let handled = false
  switch (e.key) {
    case '/':
      handled = focusTarget('search')
      break
    case 'n':
      handled = focusTarget('composer')
      break
    case '?':
      openShortcutsHelp()
      handled = true
      break
  }
  // Only swallow the key when it did something — otherwise ``/`` on a
  // page without a search box keeps its browser default (Firefox
  // quick-find).
  if (handled) e.preventDefault()
  return handled
}

/** Attach the global listener. Returns the teardown. */
export function installKeyboardShortcuts(): () => void {
  document.addEventListener('keydown', handleShortcutKey)
  return () => document.removeEventListener('keydown', handleShortcutKey)
}
