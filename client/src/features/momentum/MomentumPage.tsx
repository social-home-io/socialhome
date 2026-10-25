/**
 * MomentumPage — host for the Momentum pillar (§Momentum).
 *
 * Two tabs: ``inbox`` (the moments you see) and ``yours`` (your own
 * moments, kept for you — decision 6a). Tab state is URL-driven via
 * ``?tab=``. A hashtag link (``/momentum?tag=<name>``) shows the moments
 * with that tag under the Moments tab. Older links keep working:
 * ``?tab=archive&tag=…`` is the tag view, ``?tab=archive`` alone is
 * Yours, and ``/momentum/archive[?tag=…]`` redirects here.
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { useLocation } from 'preact-iso'
import { TabHeader } from '@/components/TabHeader'
import { MomentumComposerDialog } from '@/components/MomentumComposerDialog'
import { t } from '@/i18n/i18n'
import { useTitle } from '@/store/pageTitle'
import MomentumInboxTab from './MomentumInboxTab'
import MomentumTagTab from './MomentumTagTab'
import MomentumYoursTab from './MomentumYoursTab'

type MomentumTab = 'inbox' | 'yours'

const TABS: readonly MomentumTab[] = ['inbox', 'yours'] as const
const tabLabels = (): Readonly<Record<MomentumTab, string>> => ({
  inbox: t('nav.momentum'),
  yours: t('moment.tab.yours'),
})

export interface MomentumView {
  tab: MomentumTab
  /** The hashtag filter (Moments tab only), or ``null``. */
  tag: string | null
}

/** Which tab (and tag) a ``/momentum`` URL shows. */
export function momentumViewFromUrl(url: string): MomentumView {
  const params = new URLSearchParams(url.split('?')[1] ?? '')
  const tab = params.get('tab')
  const tag = params.get('tag') || null
  if (tab === 'yours') return { tab: 'yours', tag: null }
  if (tag) return { tab: 'inbox', tag }
  // The old Archive tab is your own moments now.
  if (tab === 'archive') return { tab: 'yours', tag: null }
  return { tab: 'inbox', tag: null }
}

const view = signal<MomentumView>({ tab: 'inbox', tag: null })


export default function MomentumPage() {
  useTitle(view.value.tab === 'yours' ? t('page_title.moments_yours') : t('nav.momentum'))
  const loc = useLocation()

  useEffect(() => {
    view.value = momentumViewFromUrl(loc.url)
  }, [loc.url])

  const onSelectTab = (tab: MomentumTab) => {
    // A tag filter belongs to the Moments list; switching tabs drops it.
    const next = tab === 'yours' ? '/momentum?tab=yours' : '/momentum'
    view.value = { tab, tag: null }
    if (loc.url !== next) loc.route(next, true)
  }

  const { tab, tag } = view.value
  return (
    <div class="sh-momentum-host">
      <TabHeader<MomentumTab>
        activeTab={tab}
        visibleTabs={TABS}
        labels={tabLabels()}
        ariaLabel={t('moment.sections_aria')}
        onSelectTab={onSelectTab}
      />
      {tab === 'yours'
        ? <MomentumYoursTab />
        : tag ? <MomentumTagTab /> : <MomentumInboxTab />}
      <MomentumComposerDialog />
    </div>
  )
}
