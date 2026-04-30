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
 */
import type { Signal } from '@preact/signals'
import { Avatar } from './Avatar'

export type SpaceTab =
  | 'feed'
  | 'members'
  | 'pages'
  | 'calendar'
  | 'gallery'
  | 'map'
  | 'moderation'

interface SpaceSubHeaderProps {
  name: string
  emoji: string | null
  coverUrl: string | null
  memberCount: number | null
  activeTab: Signal<SpaceTab>
  visibleTabs: readonly SpaceTab[]
  onSelectTab: (tab: SpaceTab) => void
  /** Optional trailing slot — settings button, notif prefs menu, etc. */
  actions?: preact.ComponentChildren
}

function tabLabel(tab: SpaceTab): string {
  return tab.charAt(0).toUpperCase() + tab.slice(1)
}

export function SpaceSubHeader({
  name, emoji, coverUrl, memberCount,
  activeTab, visibleTabs, onSelectTab, actions,
}: SpaceSubHeaderProps) {
  return (
    <div class="sh-space-subheader" role="presentation">
      <div class="sh-space-subheader-identity">
        <Avatar src={coverUrl} name={emoji ? `${emoji} ${name}` : name} size={28} />
        {memberCount !== null && (
          <span class="sh-space-subheader-meta">
            {memberCount} {memberCount === 1 ? 'member' : 'members'}
          </span>
        )}
      </div>
      <nav
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
            {tabLabel(tab)}
          </button>
        ))}
      </nav>
      {actions && (
        <div class="sh-space-subheader-actions">{actions}</div>
      )}
    </div>
  )
}
