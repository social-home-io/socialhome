/**
 * BackToTop — floating button that appears once the user has scrolled
 * past the fold and snaps the viewport back to the top on click.
 *
 * Used on long lists (feed, DM thread, gallery) where reaching the top
 * to refresh / start a new post is a slog. Mounted once at the App
 * level and listens to ``window.scroll``; it's invisible above the
 * threshold so resting state doesn't add chrome.
 *
 * The button is a single fixed-position circle in the bottom-right.
 * Honours ``prefers-reduced-motion``: smooth scroll on the default
 * preference, instant snap when the user has reduced motion turned on.
 *
 * Sticky action bars: a form's ``.sh-form-actions`` save row (and the
 * timetable's phone brush bar) stick to the bottom of the viewport —
 * right where this button sits. Whenever such a bar overlaps the
 * button's resting spot, the button lifts above it (``--sh-back-to-top-lift``)
 * so it never covers a Save button. Re-measured (once per animation
 * frame) on scroll, resize, and when such a bar mounts or unmounts.
 *
 * Modal layers: the button sits on the toast layer, above modals, and
 * the page behind an open dialog keeps its scroll position — so on a
 * phone it used to float over a bottom sheet's inputs (the right end of
 * a Name field). While any modal surface is open (``aria-modal="true"``:
 * every ``Modal``, sheets, the lightbox, the mobile nav drawer — or a
 * native ``<dialog open>``) the button is not rendered at all; it can't
 * act on the dialog anyway, since it scrolls the page behind it.
 */
import { useEffect, useLayoutEffect, useRef } from 'preact/hooks'
import { signal } from '@preact/signals'
import { t } from '@/i18n/i18n'

const SHOW_AFTER_PX = 600

/** Bars that stick to the viewport bottom and hold controls. */
export const STICKY_BAR_SELECTOR = '.sh-form-actions, .sh-timetable-brushbar'

/** Open modal surfaces — the button hides while any is in the DOM. */
export const MODAL_SELECTOR = '[aria-modal="true"], dialog[open]'

/** Breathing room between a lifted button and the bar under it. */
const LIFT_GAP_PX = 8

interface Box { top: number, bottom: number, left: number, right: number }

/**
 * How far (px) the button must rise so it clears every bar that overlaps
 * its resting spot. ``rest`` is the button's box with no lift applied.
 * Bars that are hidden (zero height) or don't overlap it count for nothing.
 */
export function liftOverStickyBars(rest: Box, bars: Box[]): number {
  let lift = 0
  for (const bar of bars) {
    if (bar.bottom - bar.top <= 0) continue
    const overlapsX = bar.left < rest.right && bar.right > rest.left
    const overlapsY = bar.top < rest.bottom && bar.bottom > rest.top
    if (overlapsX && overlapsY) {
      lift = Math.max(lift, rest.bottom - bar.top + LIFT_GAP_PX)
    }
  }
  return Math.ceil(lift)
}

const visible = signal(false)
const lift = signal(0)
const modalOpen = signal(false)

/** True when a mutation added or removed an element matching
 *  ``selector`` (or a subtree holding one). */
function touches(records: MutationRecord[], selector: string): boolean {
  for (const rec of records) {
    for (const list of [rec.addedNodes, rec.removedNodes]) {
      for (const node of list) {
        if (!(node instanceof Element)) continue
        if (node.matches(selector) || node.querySelector(selector)) return true
      }
    }
  }
  return false
}

/** True when a mutation added or removed a sticky bar (or a subtree
 *  holding one) — the only DOM changes that can move the bar. */
export function touchesStickyBar(records: MutationRecord[]): boolean {
  return touches(records, STICKY_BAR_SELECTOR)
}

/** True when a mutation may have opened or closed a modal: one mounted
 *  or unmounted, or an ``aria-modal`` / ``open`` attribute flipped. */
export function touchesModal(records: MutationRecord[]): boolean {
  return records.some(rec => rec.type === 'attributes') || touches(records, MODAL_SELECTOR)
}

function checkModal() {
  const open = document.querySelector(MODAL_SELECTOR) !== null
  if (open !== modalOpen.value) modalOpen.value = open
}

export function BackToTop() {
  const ref = useRef<HTMLButtonElement | null>(null)
  const frame = useRef(0)
  const pending = useRef(false)

  const measureNow = () => {
    pending.current = false
    const btn = ref.current
    if (!btn) return
    const r = btn.getBoundingClientRect()
    // Undo the current lift to get the resting box.
    const rest = { top: r.top + lift.value, bottom: r.bottom + lift.value, left: r.left, right: r.right }
    const bars = [...document.querySelectorAll(STICKY_BAR_SELECTOR)]
      .map(el => el.getBoundingClientRect())
    const next = liftOverStickyBars(rest, bars)
    if (next !== lift.value) lift.value = next
  }

  /** Coalesce bursts (scroll fires per frame or faster) into one layout
   *  read per animation frame. */
  const measure = () => {
    if (pending.current) return
    pending.current = true
    frame.current = requestAnimationFrame(measureNow)
  }

  useEffect(() => {
    const onScroll = () => {
      visible.value = window.scrollY > SHOW_AFTER_PX
      measure()
    }
    onScroll()
    window.addEventListener('scroll', onScroll, { passive: true })
    window.addEventListener('resize', measure)
    return () => {
      window.removeEventListener('scroll', onScroll)
      window.removeEventListener('resize', measure)
      if (pending.current) cancelAnimationFrame(frame.current)
      pending.current = false
    }
  }, [])

  // While scrolled past the fold: re-measure on first paint and whenever
  // a sticky bar mounts or unmounts (a tab switch swaps a form in without
  // scrolling), and hide while a modal is open. The observer only runs
  // past the fold, and its filters are a cheap ``matches`` /
  // ``querySelector`` on the changed nodes.
  useLayoutEffect(() => {
    if (!visible.value) return
    checkModal()
    measure()
    if (typeof MutationObserver === 'undefined') return
    const mo = new MutationObserver(records => {
      if (touchesModal(records)) checkModal()
      if (touchesStickyBar(records)) measure()
    })
    mo.observe(document.body, {
      childList: true, subtree: true,
      attributes: true, attributeFilter: ['aria-modal', 'open'],
    })
    return () => mo.disconnect()
  }, [visible.value])

  // Coming back from under a dialog: the bars may have moved meanwhile.
  const wasHidden = useRef(false)
  useLayoutEffect(() => {
    const reappeared = wasHidden.current && !modalOpen.value
    wasHidden.current = modalOpen.value
    if (reappeared && visible.value) measure()
  }, [modalOpen.value])

  if (!visible.value || modalOpen.value) return null

  const onClick = () => {
    const reduced = typeof window !== 'undefined'
      && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
    window.scrollTo({ top: 0, behavior: reduced ? 'auto' : 'smooth' })
  }

  return (
    <button
      ref={ref}
      type="button"
      class="sh-back-to-top"
      style={lift.value ? { '--sh-back-to-top-lift': `${lift.value}px` } : undefined}
      onClick={onClick}
      aria-label={t('common.back_to_top')}
      title={t('common.back_to_top')}
    >
      ↑
    </button>
  )
}
