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
 *
 * **Keyboard inset.** It also publishes ``--sh-kbd-inset`` on ``<html>``
 * — how many px of the layout viewport's bottom the keyboard covers
 * (``keyboardInset``). It is 0 where the keyboard resizes the layout
 * viewport (Android WebView / HA companion app: ``100dvh`` already
 * shrank) and the keyboard height where only the visual viewport
 * shrinks (iOS Safari, Android Chrome's default ``resizes-visual``),
 * so chat columns and bottom sheets subtract it to end right above the
 * keyboard instead of behind it. In a same-origin ingress frame it's
 * the part of the frame the top window's keyboard covers.
 *
 * **Pinned chats.** A chat thread (``.sh-thread``) is laid out to fill
 * the screen and never scrolls the page; when its composer is focused
 * and fits above the keyboard the page is held at ``scrollY = 0`` so
 * the browser's own "reveal the field" pan (iOS scrolls the page up
 * under the keyboard) doesn't push the thread's top off screen once the
 * column has shrunk to fit.
 *
 * **Closing.** Focus leaving the field (tapping 😀 / 📎) closes the
 * keyboard, but it takes a moment to animate away. The state stays
 * open until the viewport has grown back (or ``CLOSE_GRACE_MS`` at the
 * latest) so the bottom bar doesn't spring back over the composer —
 * under the finger that is mid-tap — while the keyboard is still up.
 */
import { useEffect } from 'preact/hooks'
import { signal, type ReadonlySignal } from '@preact/signals'
import { STICKY_BAR_SELECTOR } from '@/components/BackToTop'

/** A keyboard is "up" once the viewport loses more than this many px.
 *  Browser chrome (URL bar collapse) moves ~50–80 px; keyboards ≥ 200. */
export const KEYBOARD_SHRINK_PX = 150

/** Class set on ``<html>`` while the keyboard is up. */
export const KBD_OPEN_CLASS = 'sh-kbd-open'

/** CSS custom property (on ``<html>``) with the px the keyboard covers
 *  at the bottom of the layout viewport; removed while closed. */
export const KBD_INSET_VAR = '--sh-kbd-inset'

/** Focus inside one of these pins the page to the top while typing —
 *  the surface sizes itself to the visible area and must not scroll. */
export const PINNED_SURFACE_SELECTOR = '.sh-thread'

/** Upper bound on how long the state outlives the focused field when
 *  the viewport never reports the keyboard going away. */
export const CLOSE_GRACE_MS = 800

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

/** Geometry ``keyboardInset`` needs, all in CSS px. */
export interface InsetGeometry {
  /** Height of our own layout viewport (``window.innerHeight``). */
  innerHeight: number
  /** The measuring window's visual viewport (``offsetTop``, ``height``). */
  vvTop: number
  vvHeight: number
  /** Our frame's top / bottom in the top window's layout viewport —
   *  only for a same-origin frame (haos ingress); omitted at top level. */
  frame?: { top: number, bottom: number }
}

/**
 * Px of our layout viewport's bottom hidden behind the keyboard: the
 * part below the visual viewport's bottom edge. 0 when the keyboard
 * resized the layout viewport itself or the browser panned the visual
 * viewport down instead (then nothing below it is hidden).
 */
export function keyboardInset(g: InsetGeometry): number {
  const visibleBottom = g.vvTop + g.vvHeight
  const covered = g.frame
    ? g.frame.bottom - visibleBottom
    : g.innerHeight - visibleBottom
  return Math.round(Math.min(Math.max(covered, 0), g.innerHeight))
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
  let closeGrace: ReturnType<typeof setTimeout> | null = null
  const root = doc.documentElement

  const shrunk = (): boolean => {
    if (!measure || !vv) return true // no way to measure: focus decides
    const width = Math.round(measure.innerWidth)
    const height = vv.height * vv.scale
    const tallest = Math.max(baseline.get(width) ?? 0, measure.innerHeight, height)
    baseline.set(width, tallest)
    return tallest - height > KEYBOARD_SHRINK_PX
  }

  const inset = (): number => {
    if (!measure || !vv) return 0
    let frame: InsetGeometry['frame']
    if (measure !== win) {
      const el = win.frameElement
      if (!el) return 0
      const r = el.getBoundingClientRect()
      frame = { top: r.top, bottom: r.bottom }
    }
    return keyboardInset({
      innerHeight: win.innerHeight, vvTop: vv.offsetTop, vvHeight: vv.height, frame,
    })
  }

  const clearGrace = () => {
    if (closeGrace !== null) { clearTimeout(closeGrace); closeGrace = null }
  }

  const setOpen = (next: boolean) => {
    clearGrace()
    if (next === _open.value) return
    _open.value = next
    root.classList.toggle(KBD_OPEN_CLASS, next)
    if (!next) root.style.removeProperty(KBD_INSET_VAR)
  }

  const compute = () => {
    // Always sample, so the baseline learns the keyboard-less height.
    const viewportShrunk = shrunk()
    const typing = opensKeyboard(doc.activeElement)
    let next = isTouch(win) && typing && viewportShrunk
    // Focus left the field but the keyboard is still on its way out:
    // hold the open layout until the viewport grows back.
    if (!next && _open.value && !typing && viewportShrunk && measure && vv) {
      if (closeGrace === null) {
        closeGrace = setTimeout(() => { closeGrace = null; setOpen(false) }, CLOSE_GRACE_MS)
      }
      next = true
    } else {
      clearGrace()
    }
    if (!next) {
      setOpen(false)
      return
    }
    const opening = !_open.value
    root.style.setProperty(KBD_INSET_VAR, `${inset()}px`)
    if (opening) {
      _open.value = true
      root.classList.add(KBD_OPEN_CLASS)
    }
    // After the class + inset are applied, so the thread is measured at
    // its keyboard-up size.
    if (typing) pinSurface()
    if (opening) revealFocused()
  }

  // A chat thread fills the visible area once ``--sh-kbd-inset`` is
  // applied; undo any page scroll the browser added to reveal its
  // composer (iOS pans the page under the keyboard) — but only when the
  // composer really sits above the keyboard at the top of the page, so
  // a layout that doesn't fit falls back to the normal reveal.
  const pinSurface = () => {
    const el = doc.activeElement
    if (measure !== win || !(el instanceof HTMLElement)) return
    if (!el.closest(PINNED_SURFACE_SELECTOR) || win.scrollY === 0) return
    const own = win.visualViewport
    const visibleBottom = own ? own.offsetTop + own.height : win.innerHeight
    if (el.getBoundingClientRect().bottom + win.scrollY > visibleBottom + 1) return
    win.scrollTo(0, 0)
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
  vv?.addEventListener('scroll', compute)
  win.addEventListener('resize', compute)
  win.addEventListener('orientationchange', compute)
  compute()

  return () => {
    if (pendingBlur !== null) clearTimeout(pendingBlur)
    clearGrace()
    doc.removeEventListener('focusin', onFocusIn)
    doc.removeEventListener('focusout', onFocusOut)
    vv?.removeEventListener('resize', compute)
    vv?.removeEventListener('scroll', compute)
    win.removeEventListener('resize', compute)
    win.removeEventListener('orientationchange', compute)
    _open.value = false
    root.classList.remove(KBD_OPEN_CLASS)
    root.style.removeProperty(KBD_INSET_VAR)
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
