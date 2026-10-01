/**
 * SpaceSubHeader — sticky strip directly below the global TopBar that
 * holds the space's tab nav plus a compact identity badge (small
 * avatar + member count) and trailing action slot (settings button,
 * notif prefs menu).
 *
 * Visual style mirrors the household feed's warm cream surfaces:
 * `--sh-bg-tertiary` background, `--sh-border` hairline, pill-shaped
 * tabs with a terracotta accent on the active one. The strip
 * ``position: sticky``-stacks under the TopBar via `--sh-topbar-height`.
 *
 * Purely presentational: the page owns the ``activeTab`` signal and
 * the data-loading callback. We only render and dispatch.
 *
 * Mobile overflow: when the strip can't fit every tab in its
 * container (measured by ``useTabStripOverflow``), a ⋯ More button
 * appears next to the actions slot and opens a vertical popover
 * listing every section. The active tab is auto-scrolled into view
 * on mount + when it changes via ``useScrollActiveTabIntoView``.
 */
import type { Signal } from '@preact/signals'
import { useRef } from 'preact/hooks'
import { Avatar } from './Avatar'
import {
  TabOverflowMenu,
  useScrollActiveTabIntoView,
  useTabStripOverflow,
} from './TabStripOverflow'

export type SpaceTab =
  | 'feed'
  | 'members'
  | 'pages'
  | 'calendar'
  | 'tasks'
  | 'stickies'
  | 'gallery'
  | 'bazaar'
  | 'map'
  | 'moderation'

interface SpaceSubHeaderProps {
  name: string
  emoji: string | null
  iconUrl?: string | null
  memberCount: number | null
  activeTab: Signal<SpaceTab>
  visibleTabs: readonly SpaceTab[]
  onSelectTab: (tab: SpaceTab) => void
  /** Per-tab label overrides (e.g. the Calendar tab reads "Timetable"
   *  when it holds only the space timetable). */
  tabLabels?: Partial<Record<SpaceTab, string>>
  /** Optional trailing slot — settings button, notif prefs menu, etc. */
  actions?: preact.ComponentChildren
}

function tabLabel(tab: SpaceTab): string {
  return tab.charAt(0).toUpperCase() + tab.slice(1)
}

export function SpaceSubHeader({
  name, emoji, iconUrl, memberCount,
  activeTab, visibleTabs, onSelectTab, tabLabels, actions,
}: SpaceSubHeaderProps) {
  const stripRef = useRef<HTMLElement | null>(null)
  const overflowing = useTabStripOverflow(stripRef, [visibleTabs])
  useScrollActiveTabIntoView(stripRef, activeTab.value)

  const labels = Object.fromEntries(
    visibleTabs.map((t) => [t, tabLabels?.[t] ?? tabLabel(t)]),
  ) as Record<SpaceTab, string>

  return (
    <div class="sh-space-subheader" role="presentation">
      <div class="sh-space-subheader-identity">
        {/* The cover is the brand banner (SpaceHero); the compact identity
         *  chip is the space's icon — its uploaded image, else its emoji,
         *  else initials. */}
        {iconUrl ? (
          <Avatar src={iconUrl} name={name} size={28} />
        ) : emoji ? (
          <span class="sh-space-subheader-emoji" aria-hidden="true">{emoji}</span>
        ) : (
          <Avatar name={name} size={28} />
        )}
        {memberCount !== null && (
          <span class="sh-space-subheader-meta">
            {memberCount} {memberCount === 1 ? 'member' : 'members'}
          </span>
        )}
      </div>
      <nav
        ref={stripRef}
        class="sh-space-tabs"
        role="tablist"
        aria-label="Space sections"
      >
        {visibleTabs.map(tab => (
          <button
            key={tab}
            type="button"
            role="tab"
            aria-selected={activeTab.value === tab}
            class={
              activeTab.value === tab
                ? 'sh-tab sh-tab--active'
                : 'sh-tab'
            }
            onClick={() => onSelectTab(tab)}
          >
            {labels[tab]}
          </button>
        ))}
      </nav>
      {overflowing && (
        <TabOverflowMenu<SpaceTab>
          visibleTabs={visibleTabs}
          activeTab={activeTab.value}
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
