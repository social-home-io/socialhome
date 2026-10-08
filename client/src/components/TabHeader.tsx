/**
 * TabHeader — generic tab strip used by single-page surfaces that
 * need an in-page tab nav (DM panel, Organize, Highlights, Momentum).
 *
 * Mirrors the visual + interaction treatment of :class:`SpaceSubHeader`
 * (terracotta accent on the active tab, pill-shaped buttons, sticky
 * under the TopBar) — including the mobile overflow-menu pattern via
 * the shared :file:`TabStripOverflow` helpers: a ⋯ More button
 * surfaces next to the actions slot whenever the inline strip can't
 * fit every tab, opening a vertical popover listing every section
 * with the active one highlighted. Active tab is auto-scrolled into
 * view on mount / when it changes so a user who lands on a far-right
 * tab doesn't see a strip scrolled to the start.
 *
 * Purely presentational — the host page owns tab state.
 */
import type { ComponentChildren } from 'preact'
import { useRef } from 'preact/hooks'
import {
  TabOverflowMenu,
  useScrollActiveTabIntoView,
  useTabStripOverflow,
} from './TabStripOverflow'
import { t } from '@/i18n/i18n'

interface TabHeaderProps<T extends string> {
  /** Identifier of the tab the page treats as active. */
  activeTab: T
  /** Tabs to render, in display order. Filter externally if a tab
   *  should be hidden for the current user. */
  visibleTabs: readonly T[]
  /** Display label per tab — keeps the component free of i18n
   *  knowledge so callers can pass localised strings. */
  labels: Readonly<Record<T, string>>
  onSelectTab: (tab: T) => void
  /** Accessible name for the ``<nav role="tablist">`` element. */
  ariaLabel: string
  /** Optional trailing slot — header action buttons, badges, etc. */
  actions?: ComponentChildren
  /** Optional unread count per tab — a pill after the label (hidden
   *  at zero), announced as "N unread". */
  badges?: Partial<Readonly<Record<T, number>>>
}


export function TabHeader<T extends string>({
  activeTab,
  visibleTabs,
  labels,
  onSelectTab,
  ariaLabel,
  actions,
  badges,
}: TabHeaderProps<T>) {
  const stripRef = useRef<HTMLElement | null>(null)
  const overflowing = useTabStripOverflow(stripRef, [visibleTabs])
  // Stable string key — see SpaceSubHeader for why not the objects.
  useScrollActiveTabIntoView(
    stripRef, activeTab, [visibleTabs.map((t) => labels[t]).join('\u0000')],
  )

  return (
    <div class="sh-space-subheader" role="presentation">
      <nav ref={stripRef} class="sh-space-tabs" role="tablist" aria-label={ariaLabel}>
        {visibleTabs.map((tab) => (
          <button
            key={tab}
            type="button"
            role="tab"
            aria-selected={activeTab === tab}
            class={
              activeTab === tab
                ? 'sh-tab sh-tab--active'
                : 'sh-tab'
            }
            onClick={() => onSelectTab(tab)}
          >
            {labels[tab]}
            {(badges?.[tab] ?? 0) > 0 && (
              <>
                <span class="sh-tab-unread" aria-hidden="true">
                  {badges![tab]! > 99 ? '99+' : badges![tab]}
                </span>
                <span class="sr-only">
                  {` (${t('nav.unread', { count: String(badges![tab]) })})`}
                </span>
              </>
            )}
          </button>
        ))}
      </nav>
      {overflowing && (
        <TabOverflowMenu<T>
          visibleTabs={visibleTabs}
          activeTab={activeTab}
          labels={labels}
          onSelectTab={onSelectTab}
        />
      )}
      {actions && (
        <div class="sh-space-subheader-actions">{actions}</div>
      )}
    </div>
  )
}
