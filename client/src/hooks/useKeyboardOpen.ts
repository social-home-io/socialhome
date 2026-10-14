/**
 * useKeyboardOpen — "is the phone's on-screen keyboard up?"
 *
 * The bottom tab bar (``MobileNav``) steps aside while the user types so
 * the keyboard, the field and the bar don't fight over a phone's short
 * remaining viewport. No browser exposes the keyboard directly, so this
 * combines two signals:
 *
 * 1. **Focus** — the focused element is one that opens a text keyboard
 *    (text-like ``<input>``, ``<textarea>``, ``contenteditable``). Pickers
 *    (date, colour, file), checkboxes, ranges, ``<select>`` and buttons
 *    don't count, nor do read-only / disabled fields or
 *    ``inputmode="none"``.
 * 2. **Viewport shrink** — ``visualViewport`` loses more than
 *    ``KEYBOARD_SHRINK_PX`` of height against the tallest height seen at
 *    the current width (iOS Safari + Android Chrome's default
 *    ``resizes-visual`` keep ``innerHeight`` and shrink only the visual
 *    viewport; WebViews / ``resizes-content`` shrink both, hence the
 *    remembered baseline). Keying the baseline by width resets it on
 *    rotation. Heights are multiplied by ``visualViewport.scale`` so a
 *    pinch-zoom doesn't read as a keyboard.
 *
 * Both must hold, and only on a device with a coarse (touch) pointer — a
 * desktop or a tablet with a hardware keyboard never loses the bar,
 * because its viewport doesn't shrink on focus. Without
 * ``visualViewport`` (very old WebViews) focus alone decides on touch
 * devices.
 *
 * Ingress (haos): the SPA runs in a same-origin iframe inside HA's
 * frontend. On iOS the keyboard shrinks the *top* window's visual
 * viewport, not the frame's, so the watcher measures ``window.top``
 * when it can reach it. A cross-origin frame can't be measured at all,
 * so there focus alone decides (touch devices only).
 *
 * While open, ``<html>`` carries ``sh-kbd-open`` so CSS can hand the
 * bar's reserved space (``.sh-main`` bottom padding, chat heights, the
 * back-to-top FAB) back to the content. The watcher is shared and
 * ref-counted: every ``useKeyboardOpen()`` consumer reads one signal.
 */
import { useEffect } from 'preact/hooks'
import { signal, type ReadonlySignal } from '@preact/signals'
import { STICKY_BAR_SELECTOR } from '@/components/BackToTop'

/** A keyboard is "up" once the viewport loses more than this many px.
 *  Browser chrome (URL bar collapse) moves ~50–80 px; keyboards ≥ 200. */
export const KEYBOARD_SHRINK_PX = 150

/** Class set on ``<html>`` while the keyboard is up. */
export const KBD_OPEN_CLASS = 'sh-kbd-open'

/** ``<input type>`` values that do NOT raise a text keyboard. */
const NO_KEYBOARD_INPUT_TYPES = new Set([
  'button', 'checkbox', 'color', 'date', 'datetime-local', 'file',
  'hidden', 'image', 'month', 'radio', 'range', 'reset', 'submit',
  'time', 'week',
])

/** True when focusing ``el`` raises the on-screen text keyboard. */
export function opensKeyboard(el: Element | null | undefined): boolean {
  if (!el || !(el instanceof HTMLElement)) return false
  if (el.getAttribute('inputmode') === 'none') return false
  if (el instanceof HTMLInputElement) {
    if (el.readOnly || el.disabled) return false
    return !NO_KEYBOARD_INPUT_TYPES.has(el.type.toLowerCase())
  }
  if (el instanceof HTMLTextAreaElement) return !el.readOnly && !el.disabled
  return el.isContentEditable === true
}

/**
 * The window whose visual viewport reflects the keyboard: the top
 * window when we're in a same-origin frame (ingress), else our own.
 * ``null`` for a cross-origin frame — nothing there measures the keyboard.
 */
export function measuringWindow(win: Window): Window | null {
  if (win.top === win || win.top === null) return win
  try {
    // Reading a property throws SecurityError across origins.
    void win.top.innerHeight
    return win.top
  } catch {
    return null
  }
}

/** Breathing room between a revealed field and whatever is below it. */
const REVEAL_GAP_PX = 8

/**
 * Height (px) that sticky bottom bars (a form's Save row) cover at the
 * bottom of the visible area — excluding a bar that holds ``field``.
 */
export function coveredBottom(doc: Document, field: Element, visibleBottom: number): number {
  let covered = 0
  for (const bar of doc.querySelectorAll(STICKY_BAR_SELECTOR)) {
    if (bar.contains(field)) continue
    const r = bar.getBoundingClientRect()
    if (r.bottom - r.top <= 0 || r.top >= visibleBottom || r.bottom < visibleBottom - 1) continue
    covered = Math.max(covered, visibleBottom - r.top)
  }
  return Math.ceil(covered)
}

const _open = signal(false)
/** Live keyboard state — read via ``useKeyboardOpen()`` in components. */
export const keyboardOpen: ReadonlySignal<boolean> = _open

let refs = 0
let teardown: (() => void) | null = null

function isTouch(win: Window): boolean {
  return win.matchMedia?.('(any-pointer: coarse)').matches ?? false
}

function install(win: Window): () => void {
  const doc = win.document
  const measure = measuringWindow(win)
  const vv = measure?.visualViewport ?? null
  /** Tallest (scale-normalised) viewport height seen per width. */
  const baseline = new Map<number, number>()
  let pendingBlur: ReturnType<typeof setTimeout> | null = null

  const shrunk = (): boolean => {
    if (!measure || !vv) return true // no way to measure: focus decides
    const width = Math.round(measure.innerWidth)
    const height = vv.height * vv.scale
    const tallest = Math.max(baseline.get(width) ?? 0, measure.innerHeight, height)
    baseline.set(width, tallest)
    return tallest - height > KEYBOARD_SHRINK_PX
  }

  const compute = () => {
    // Always sample, so the baseline learns the keyboard-less height.
    const viewportShrunk = shrunk()
    const next = isTouch(win) && opensKeyboard(doc.activeElement) && viewportShrunk
    if (next === _open.value) return
    _open.value = next
    doc.documentElement.classList.toggle(KBD_OPEN_CLASS, next)
    if (next) revealFocused()
  }

  // The browser scrolls a focused field into the visible area on its
  // own, but it ignores fixed / sticky chrome — re-check once the bar is
  // gone, treating a sticky Save row (``.sh-form-actions``) as covered
  // space so the field doesn't end up hidden behind it.
  const revealFocused = () => {
    win.requestAnimationFrame(() => {
      const el = doc.activeElement
      if (!(el instanceof HTMLElement) || !opensKeyboard(el)) return
      const own = win.visualViewport
      const bottom = own ? own.offsetTop + own.height : win.innerHeight
      // Two passes: a Save row below the field only sticks to the
      // visible bottom once the field has scrolled up to it, so the
      // space it covers is known only after the first scroll.
      for (let pass = 0; pass < 2; pass++) {
        const covered = coveredBottom(doc, el, bottom)
        if (el.getBoundingClientRect().bottom <= bottom - covered) return
        const prev = el.style.scrollMarginBottom
        el.style.scrollMarginBottom = `${covered + REVEAL_GAP_PX}px`
        el.scrollIntoView({ block: 'nearest' })
        el.style.scrollMarginBottom = prev
      }
    })
  }

  const onFocusIn = () => {
    if (pendingBlur !== null) { clearTimeout(pendingBlur); pendingBlur = null }
    compute()
  }
  // Focus moving field → field fires focusout then focusin; deferring
  // the blur check keeps the bar from flashing in between.
  const onFocusOut = () => {
    if (pendingBlur !== null) clearTimeout(pendingBlur)
    pendingBlur = setTimeout(() => { pendingBlur = null; compute() }, 0)
  }

  doc.addEventListener('focusin', onFocusIn)
  doc.addEventListener('focusout', onFocusOut)
  vv?.addEventListener('resize', compute)
  win.addEventListener('resize', compute)
  win.addEventListener('orientationchange', compute)
  compute()

  return () => {
    if (pendingBlur !== null) clearTimeout(pendingBlur)
    doc.removeEventListener('focusin', onFocusIn)
    doc.removeEventListener('focusout', onFocusOut)
    vv?.removeEventListener('resize', compute)
    win.removeEventListener('resize', compute)
    win.removeEventListener('orientationchange', compute)
    _open.value = false
    doc.documentElement.classList.remove(KBD_OPEN_CLASS)
  }
}

/**
 * Start the shared watcher (ref-counted); returns the release function.
 * The last release removes every listener and clears the state.
 */
export function startKeyboardWatch(win: Window = window): () => void {
  if (refs === 0) teardown = install(win)
  refs += 1
  let released = false
  return () => {
    if (released) return
    released = true
    refs -= 1
    if (refs === 0 && teardown) { teardown(); teardown = null }
  }
}

/** ``true`` while the on-screen keyboard is up (touch devices only). */
export function useKeyboardOpen(): boolean {
  useEffect(() => startKeyboardWatch(), [])
  return keyboardOpen.value
}
