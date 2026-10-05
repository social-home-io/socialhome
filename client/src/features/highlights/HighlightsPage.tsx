/**
 * HighlightsPage — host for the personal Highlights pillar (§Highlights).
 *
 * Two tabs: ``inbox`` (recent rings + per-author list) and
 * ``archive`` (month-grid retention-window browser). Tab state is
 * URL-driven via the ``?tab=`` query param so deep links from
 * elsewhere in the app and external bookmarks (``/highlights/archive``
 * redirects to ``/highlights?tab=archive``) land on the right tab.
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { useLocation } from 'preact-iso'
import { HighlightQuickShareDialog } from '@/components/HighlightQuickShareDialog'
import { TabHeader } from '@/components/TabHeader'
import { t } from '@/i18n/i18n'
import { useTitle } from '@/store/pageTitle'
import HighlightsInboxTab from './HighlightsInboxTab'
import HighlightArchiveTab from './HighlightArchiveTab'
import { addBase } from '@/baseUrl'

type HighlightsTab = 'inbox' | 'archive'

const TABS: readonly HighlightsTab[] = ['inbox', 'archive'] as const
const TAB_LABELS: Readonly<Record<HighlightsTab, string>> = {
  inbox:   'Highlights',
  archive: 'Archive',
}

const activeTab = signal<HighlightsTab>('inbox')


function tabFromUrl(url: string): HighlightsTab {
  const q = url.split('?')[1] ?? ''
  const t = new URLSearchParams(q).get('tab')
  return t === 'archive' ? 'archive' : 'inbox'
}


export default function HighlightsPage() {
  useTitle(activeTab.value === 'archive' ? t('page_title.highlight_archive') : t('nav.highlights'))
  const loc = useLocation()

  useEffect(() => {
    activeTab.value = tabFromUrl(loc.url)
  }, [loc.url])

  const onSelectTab = (tab: HighlightsTab) => {
    activeTab.value = tab
    // Preserve any other query params (e.g. ``?tag=`` if a future
    // filter lands on Highlights) by re-emitting them alongside ``tab``.
    const [path, query] = loc.url.split('?')
    const params = new URLSearchParams(query ?? '')
    if (tab === 'inbox') params.delete('tab')
    else params.set('tab', tab)
    const next = params.toString()
      ? `${path.split('?')[0] || '/highlights'}?${params.toString()}`
      : '/highlights'
    if (loc.url !== next) loc.route(next, true)
  }

  return (
    <div class="sh-highlights">
      <TabHeader<HighlightsTab>
        activeTab={activeTab.value}
        visibleTabs={TABS}
        labels={TAB_LABELS}
        ariaLabel="Highlights sections"
        onSelectTab={onSelectTab}
        actions={
          <a href={addBase('/settings#highlights')} class="sh-link">Settings</a>
        }
      />
      {activeTab.value === 'archive' ? <HighlightArchiveTab /> : <HighlightsInboxTab />}
      <HighlightQuickShareDialog
        onShared={(id) => loc.route(`/highlights/${id}`)}
      />
    </div>
  )
}
