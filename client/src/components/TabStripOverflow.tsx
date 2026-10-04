/**
 * Shared overflow-menu pieces used by both :class:`SpaceSubHeader` and
 * :class:`TabHeader`. Lets either surface re-use the exact same
 * affordance — ResizeObserver-driven overflow detection, ⋯ More button
 * next to the actions slot, vertical popover with ARIA keyboard model,
 * active-tab scrollIntoView — without duplicating ~80 lines of JSX +
 * hooks.
 *
 * The two host components own their own tab-strip <nav> + actions so
 * they can keep their own DOM ordering (the space subheader has an
 * identity badge before the strip; the generic TabHeader doesn't).
 * They pass the strip ref into ``useTabStripOverflow`` to drive the
 * ⋯ button, and render :component:`TabOverflowMenu` next to the
 * actions when overflow is detected.
 */
import type { JSX, RefObject } from 'preact'
import { useEffect, useLayoutEffect, useRef, useState } from 'preact/hooks'
import { t } from '@/i18n/i18n'

/** Width (px) of the edge fade the strip CSS paints on a side that
 *  still has hidden tabs. Kept in sync with the ``mask-image`` stops
 *  on ``.sh-space-subheader .sh-space-tabs[data-fade-*]`` so a tab we
 *  "reveal" is never left sitting under the fade. */
export const TAB_STRIP_FADE_PX = 16

interface HorizontalSpan {
  left: number
  right: number
}

/**
 * How far (physical px, for ``Element.scrollBy``) the strip must scroll
 * so the active tab is fully readable.
 *
 * Returns 0 when the tab already sits inside the strip's visible window
 * shrunk by ``inset`` on each side (the edge fade), so tapping a visible
 * tab never makes the row jump. Otherwise it centres the tab, which also
 * shows its neighbours — the cue that more tabs exist either side. The
 * browser clamps the result at the scroll bounds, so a first/last tab
 * simply lands flush against its edge. Works in RTL too because it is
 * computed from viewport rects, not from ``scrollLeft`` (whose sign
 * convention differs between LTR and RTL).
 */
export function activeTabScrollDelta(
  strip: HorizontalSpan,
  tab: HorizontalSpan,
  inset: number = TAB_STRIP_FADE_PX,
): number {
  const visibleLeft = strip.left + inset
  const visibleRight = strip.right - inset
  if (tab.left >= visibleLeft - 0.5 && tab.right <= visibleRight + 0.5) return 0
  const stripCentre = (strip.left + strip.right) / 2
  const tabCentre = (tab.left + tab.right) / 2
  return tabCentre - stripCentre
}

/** Resolve the physical scroll range of a horizontal scroller in a
 *  direction-agnostic way (Chrome/Firefox report a negative
 *  ``scrollLeft`` under ``direction: rtl``). */
function edgeState(el: HTMLElement): { moreLeft: boolean; moreRight: boolean } {
  const max = el.scrollWidth - el.clientWidth
  if (max <= 1) return { moreLeft: false, moreRight: false }
  const rtl = getComputedStyle(el).direction === 'rtl'
  // Distance scrolled from the physical left edge.
  const fromLeft = rtl ? max + el.scrollLeft : el.scrollLeft
  return { moreLeft: fromLeft > 1, moreRight: max - fromLeft > 1 }
}

export function useTabStripOverflow(
  stripRef: RefObject<HTMLElement>,
  // The deps that should re-trigger an overflow re-measure (e.g. the
  // tab list itself — adding a tab can flip overflow on or off).
  deps: ReadonlyArray<unknown>,
): boolean {
  const [overflowing, setOverflowing] = useState(false)
  useLayoutEffect(() => {
    const el = stripRef.current
    if (!el) return
    const check = () => {
      setOverflowing(el.scrollWidth - el.clientWidth > 1)
      // Fade only the edge(s) that actually hide tabs. A permanent
      // right-edge fade sat over the last tab's label even when the
      // strip was scrolled all the way to it.
      const { moreLeft, moreRight } = edgeState(el)
      el.toggleAttribute('data-fade-left', moreLeft)
      el.toggleAttribute('data-fade-right', moreRight)
    }
    check()
    // ``ResizeObserver`` is not in jsdom by default — guard the
    // construction so existing component tests don't have to stub
    // it just to mount the strip. Production browsers (every
    // target Social Home runs on) ship it natively.
    const RO: typeof ResizeObserver | undefined =
      typeof ResizeObserver !== 'undefined' ? ResizeObserver : undefined
    const ro = RO ? new RO(check) : null
    ro?.observe(el)
    el.addEventListener('scroll', check, { passive: true })
    window.addEventListener('resize', check)
    return () => {
      ro?.disconnect()
      el.removeEventListener('scroll', check)
      window.removeEventListener('resize', check)
    }
    // ``stripRef`` itself is stable across renders (the consumer
    // hands us the same ref instance); only the deps the consumer
    // explicitly passes should re-fire the measurement.
  }, deps)
  return overflowing
}

/**
 * Keep the active tab fully visible inside the horizontally-scrolling
 * strip.
 *
 * Runs when the active tab changes AND whenever the strip or its tabs
 * change size. The second trigger is the mobile fix: the strip's width
 * is not final at mount — the tab list grows once permissions load,
 * the trailing actions (notification bell, settings) mount, and the ⋯
 * overflow button appears once overflow is measured — so a one-shot
 * reveal at mount saw a roomy strip, did nothing, and left e.g. "Pages"
 * clipped under the ⋯ button on a 390 px phone once the strip shrank.
 *
 * Uses ``scrollBy`` on the strip itself rather than ``scrollIntoView``,
 * which would also scroll every scrollable ancestor (the page).
 */
export function useScrollActiveTabIntoView(
  stripRef: RefObject<HTMLElement>,
  activeKey: string,
  deps: ReadonlyArray<unknown> = [],
): void {
  useLayoutEffect(() => {
    const el = stripRef.current
    if (!el) return
    const reveal = () => {
      const active = el.querySelector<HTMLElement>('[aria-selected="true"]')
      if (!active) return
      const delta = activeTabScrollDelta(
        el.getBoundingClientRect(),
        active.getBoundingClientRect(),
      )
      if (Math.abs(delta) < 1) return
      if (typeof el.scrollBy === 'function') {
        el.scrollBy({ left: delta, behavior: 'auto' })
      } else {
        el.scrollLeft += delta
      }
    }
    reveal()
    const RO: typeof ResizeObserver | undefined =
      typeof ResizeObserver !== 'undefined' ? ResizeObserver : undefined
    const ro = RO ? new RO(reveal) : null
    if (ro) {
      ro.observe(el)
      // Tab widths shift when web fonts land or a label is renamed.
      for (const child of Array.from(el.children)) ro.observe(child)
    }
    return () => ro?.disconnect()
  }, [activeKey, ...deps])
}

interface TabOverflowMenuProps<T extends string> {
  visibleTabs: readonly T[]
  activeTab: T
  labels: Readonly<Record<T, string>>
  onSelectTab: (tab: T) => void
}

export function TabOverflowMenu<T extends string>({
  visibleTabs,
  activeTab,
  labels,
  onSelectTab,
}: TabOverflowMenuProps<T>): JSX.Element {
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const menuRef = useRef<HTMLDivElement | null>(null)
  const [menuOpen, setMenuOpen] = useState(false)

  // Standard ARIA menu keyboard model: Escape closes (and returns
  // focus to the trigger), Arrow keys cycle, Home/End jump, focus
  // moves to the active item on open.
  useEffect(() => {
    if (!menuOpen) return
    const items = (): HTMLButtonElement[] =>
      Array.from(menuRef.current?.querySelectorAll<HTMLButtonElement>(
        'button[role="menuitemradio"]',
      ) ?? [])
    requestAnimationFrame(() => {
      const all = items()
      const target = all.find((i) => i.getAttribute('aria-checked') === 'true')
        ?? all[0]
      target?.focus()
    })
    const onClick = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setMenuOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setMenuOpen(false)
        const trigger = wrapRef.current?.querySelector<HTMLButtonElement>(
          '.sh-space-tabs-overflow__trigger',
        )
        trigger?.focus()
        return
      }
      const all = items()
      if (all.length === 0) return
      const idx = all.findIndex((i) => i === document.activeElement)
      if (e.key === 'ArrowDown') {
        e.preventDefault()
        all[(idx + 1 + all.length) % all.length].focus()
      } else if (e.key === 'ArrowUp') {
        e.preventDefault()
        all[(idx - 1 + all.length) % all.length].focus()
      } else if (e.key === 'Home') {
        e.preventDefault()
        all[0].focus()
      } else if (e.key === 'End') {
        e.preventDefault()
        all[all.length - 1].focus()
      }
    }
    document.addEventListener('click', onClick)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('click', onClick)
      document.removeEventListener('keydown', onKey)
    }
  }, [menuOpen])

  const choose = (tab: T) => {
    onSelectTab(tab)
    setMenuOpen(false)
  }

  return (
    <div class="sh-space-tabs-overflow" ref={wrapRef}>
      <button
        type="button"
        class="sh-space-tabs-overflow__trigger"
        aria-haspopup="menu"
        aria-expanded={menuOpen}
        aria-label={t('tabs.more_sections')}
        title={t('tabs.more_sections')}
        onClick={() => setMenuOpen((o) => !o)}
      >
        <span aria-hidden="true">⋯</span>
      </button>
      {menuOpen && (
        <div
          ref={menuRef}
          class="sh-space-tabs-overflow__panel"
          role="menu"
          aria-label={t('tabs.all_sections')}
        >
          {visibleTabs.map((tab) => (
            <button
              key={tab}
              type="button"
              role="menuitemradio"
              aria-checked={activeTab === tab}
              tabIndex={activeTab === tab ? 0 : -1}
              class={activeTab === tab
                ? 'sh-space-tabs-overflow__item sh-space-tabs-overflow__item--active'
                : 'sh-space-tabs-overflow__item'}
              onClick={() => choose(tab)}
            >
              <span class="sh-space-tabs-overflow__label">{labels[tab]}</span>
              {activeTab === tab && (
                <span class="sh-space-tabs-overflow__check" aria-hidden="true">
                  ✓
                </span>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
