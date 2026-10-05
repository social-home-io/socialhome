/**
 * MomentumPage — host for the Momentum pillar (§Momentum).
 *
 * Two tabs: ``inbox`` (Twitter-style live moments) and ``archive``
 * (date-grouped retention-window list with optional ``?tag=``
 * hashtag filter). Tab state is URL-driven via ``?tab=``;
 * ``/momentum/archive[?tag=...]`` redirects to
 * ``/momentum?tab=archive[&tag=...]`` so old hashtag links and
 * external bookmarks survive.
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { useLocation } from 'preact-iso'
import { TabHeader } from '@/components/TabHeader'
import { MomentumComposerDialog } from '@/components/MomentumComposerDialog'
import { t } from '@/i18n/i18n'
import { useTitle } from '@/store/pageTitle'
import MomentumInboxTab from './MomentumInboxTab'
import MomentumArchiveTab from './MomentumArchiveTab'

type MomentumTab = 'inbox' | 'archive'

const TABS: readonly MomentumTab[] = ['inbox', 'archive'] as const
const TAB_LABELS: Readonly<Record<MomentumTab, string>> = {
  inbox:   'Momentum',
  archive: 'Archive',
}

const activeTab = signal<MomentumTab>('inbox')


function tabFromUrl(url: string): MomentumTab {
  const q = url.split('?')[1] ?? ''
  const t = new URLSearchParams(q).get('tab')
  return t === 'archive' ? 'archive' : 'inbox'
}


export default function MomentumPage() {
  useTitle(activeTab.value === 'archive' ? t('page_title.moments_archive') : t('nav.momentum'))
  const loc = useLocation()

  useEffect(() => {
    activeTab.value = tabFromUrl(loc.url)
  }, [loc.url])

  const onSelectTab = (tab: MomentumTab) => {
    activeTab.value = tab
    // Preserve other query params (notably ``?tag=`` for hashtag
    // filters) when switching tabs so the user keeps their context.
    const [, query] = loc.url.split('?')
    const params = new URLSearchParams(query ?? '')
    if (tab === 'inbox') {
      params.delete('tab')
      params.delete('tag')  // tag is archive-scoped — drop it on Inbox.
    } else {
      params.set('tab', tab)
    }
    const next = params.toString()
      ? `/momentum?${params.toString()}`
      : '/momentum'
    if (loc.url !== next) loc.route(next, true)
  }

  return (
    <div class="sh-momentum-host">
      <TabHeader<MomentumTab>
        activeTab={activeTab.value}
        visibleTabs={TABS}
        labels={TAB_LABELS}
        ariaLabel="Momentum sections"
        onSelectTab={onSelectTab}
      />
      {activeTab.value === 'archive' ? <MomentumArchiveTab /> : <MomentumInboxTab />}
      <MomentumComposerDialog />
    </div>
  )
}
