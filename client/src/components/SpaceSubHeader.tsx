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
 * listing every section. The active tab is kept fully visible — on
 * mount, when it changes, and whenever the strip resizes (actions and
 * the ⋯ button mount after the first paint) — via
 * ``useScrollActiveTabIntoView``.
 */
import type { Signal } from '@preact/signals'
import { useRef } from 'preact/hooks'
import { Avatar } from './Avatar'
import { isOne, t } from '@/i18n/i18n'
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

/** The tab's name in the UI language. Reuses the nav labels where the
 *  tab is the space's own copy of a household section. */
function tabLabel(tab: SpaceTab): string {
  switch (tab) {
    case 'feed':       return t('nav.feed')
    case 'members':    return t('spaces.members')
    case 'pages':      return t('nav.pages')
    case 'calendar':   return t('nav.calendar')
    case 'tasks':      return t('nav.tasks')
    case 'stickies':   return t('nav.stickies')
    case 'gallery':    return t('nav.gallery')
    case 'bazaar':     return t('nav.bazaar')
    case 'map':        return t('space.tab.map')
    case 'moderation': return t('space.tab.moderation')
  }
}

export function SpaceSubHeader({
  name, emoji, iconUrl, memberCount,
  activeTab, visibleTabs, onSelectTab, tabLabels, actions,
}: SpaceSubHeaderProps) {
  const stripRef = useRef<HTMLElement | null>(null)
  const labels = Object.fromEntries(
    visibleTabs.map((tab) => [tab, tabLabels?.[tab] ?? tabLabel(tab)]),
  ) as Record<SpaceTab, string>
  const overflowing = useTabStripOverflow(stripRef, [visibleTabs])
  // A string key, not the arrays/objects themselves: hosts often pass
  // fresh literals each render, and re-running the reveal on every
  // render would yank back a strip the user just scrolled by hand.
  useScrollActiveTabIntoView(
    stripRef, activeTab.value, [visibleTabs.map((tab) => labels[tab]).join('\u0000')],
  )

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
            {t(isOne(memberCount) ? 'space.header.members_one' : 'space.header.members',
               { count: String(memberCount) })}
          </span>
        )}
      </div>
      <nav
        ref={stripRef}
        class="sh-space-tabs"
        role="tablist"
        aria-label={t('space.header.sections')}
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
