/**
 * AppsTabs — the "Your apps | Store" switch beside the Apps title.
 *
 * A real tab strip (WAI-ARIA tabs, automatic activation): ``role="tablist"``
 * with one ``role="tab"`` per view, a roving ``tabindex`` so Tab enters on
 * the selected view, Arrow Left / Right (mirrored in RTL), Home and End
 * move and switch. Each tab controls the panel ``sh-apps-panel-<tab>``.
 */
import { useRef } from 'preact/hooks'
import { t } from '@/i18n/i18n'
import { APPS_TABS, type AppsTab } from './appsTab'

/* For ``i18n:check``: t('apps.tab.yours') t('apps.tab.store') */
const LABEL_KEY: Record<AppsTab, string> = {
  yours: 'apps.tab.yours',
  store: 'apps.tab.store',
}

export const tabId = (tab: AppsTab) => `sh-apps-tab-${tab}`
export const panelId = (tab: AppsTab) => `sh-apps-panel-${tab}`

export function AppsTabs({ value, onChange }: {
  value: AppsTab
  onChange: (tab: AppsTab) => void
}) {
  const listRef = useRef<HTMLDivElement>(null)

  const go = (tab: AppsTab) => {
    onChange(tab)
    listRef.current?.querySelector<HTMLButtonElement>(`#${tabId(tab)}`)?.focus()
  }

  const onKeyDown = (e: KeyboardEvent) => {
    const i = APPS_TABS.indexOf(value)
    const rtl = listRef.current?.closest('[dir="rtl"]') != null
    const next = rtl ? 'ArrowLeft' : 'ArrowRight'
    const prev = rtl ? 'ArrowRight' : 'ArrowLeft'
    let to: AppsTab | null = null
    if (e.key === next) to = APPS_TABS[(i + 1) % APPS_TABS.length]
    else if (e.key === prev) to = APPS_TABS[(i - 1 + APPS_TABS.length) % APPS_TABS.length]
    else if (e.key === 'Home') to = APPS_TABS[0]
    else if (e.key === 'End') to = APPS_TABS[APPS_TABS.length - 1]
    if (!to) return
    e.preventDefault()
    go(to)
  }

  return (
    <div
      ref={listRef}
      class="sh-apps-tabs"
      role="tablist"
      aria-label={t('nav.apps')}
      onKeyDown={onKeyDown}
    >
      {APPS_TABS.map(tab => (
        <button
          key={tab}
          type="button"
          role="tab"
          id={tabId(tab)}
          class={`sh-apps-tabs__tab${tab === value ? ' is-on' : ''}`}
          aria-selected={tab === value}
          // Only the shown view's panel is in the page.
          aria-controls={tab === value ? panelId(tab) : undefined}
          tabIndex={tab === value ? 0 : -1}
          onClick={() => { if (tab !== value) onChange(tab) }}
        >
          {t(LABEL_KEY[tab])}
        </button>
      ))}
    </div>
  )
}
