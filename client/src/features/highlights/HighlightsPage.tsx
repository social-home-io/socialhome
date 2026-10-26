/**
 * HighlightsPage — host for the personal Highlights pillar (§Highlights).
 *
 * Two tabs, each with its own URL (the Spaces pattern): Today at
 * ``/highlights`` (:class:`HighlightsInboxTab`) and the Archive at
 * ``/highlights/archive``. The row is a ``<nav>`` of links with
 * ``aria-current="page"`` on the open one, so Back, Refresh and a
 * shared link keep the tab. An older ``/highlights?tab=archive`` link
 * is replaced by ``/highlights/archive``. "+ New" opens the composer;
 * the gear opens Settings › Privacy › Highlights.
 */
import { useEffect } from 'preact/hooks'
import { useLocation } from 'preact-iso'
import { t } from '@/i18n/i18n'
import { useTitle } from '@/store/pageTitle'
import HighlightsInboxTab from './HighlightsInboxTab'
import HighlightArchiveTab from './HighlightArchiveTab'
import { addBase } from '@/baseUrl'

export type HighlightsTab = 'today' | 'archive'

const TAB_PATH: Record<HighlightsTab, string> = {
  today: '/highlights',
  archive: '/highlights/archive',
}

/** Which tab a Highlights URL shows. */
export function highlightsTabFromUrl(url: string): HighlightsTab {
  const path = url.split(/[?#]/, 1)[0].replace(/\/+$/, '')
  return path === TAB_PATH.archive ? 'archive' : 'today'
}

function legacyArchiveQuery(url: string): boolean {
  return new URLSearchParams(url.split('?')[1] ?? '').get('tab') === 'archive'
}

const SVG = {
  viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': 2,
  'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true', focusable: 'false',
} as const

export default function HighlightsPage() {
  const { url, route } = useLocation()
  const tab = highlightsTabFromUrl(url)
  useTitle(tab === 'archive' ? t('page_title.highlight_archive') : t('nav.highlights'))

  useEffect(() => {
    if (tab === 'today' && legacyArchiveQuery(url)) route(TAB_PATH.archive, true)
  }, [url, route, tab])

  const labels: Record<HighlightsTab, string> = {
    today: t('highlight.tab.today'),
    archive: t('highlight.tab.archive'),
  }

  return (
    <div class="sh-highlights">
      <div class="sh-space-subheader sh-space-subheader--sections sh-hl-subheader">
        <nav class="sh-space-sections" aria-label={t('highlight.sections_aria')}>
          <ul class="sh-space-sections__row">
            {(['today', 'archive'] as const).map(key => (
              <li key={key}>
                <a
                  href={addBase(TAB_PATH[key])}
                  class={tab === key ? 'sh-space-section sh-space-section--active' : 'sh-space-section'}
                  aria-current={tab === key ? 'page' : undefined}
                >
                  <span class="sh-space-section__label">{labels[key]}</span>
                </a>
              </li>
            ))}
          </ul>
        </nav>
        <a href={addBase('/highlights/new')} class="sh-btn sh-btn--primary sh-hl-new">
          <svg {...SVG} width={18} height={18}><path d="M12 5v14M5 12h14" /></svg>
          {t('highlight.inbox.new')}
        </a>
        <a
          href={addBase('/settings#highlights')}
          class="sh-space-gear"
          aria-label={t('highlight.today.settings_aria')}
          title={t('highlight.today.settings_aria')}
        >
          <svg {...SVG} width={20} height={20}>
            <circle cx="12" cy="12" r="3" />
            <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" />
          </svg>
        </a>
      </div>
      {tab === 'archive' ? <HighlightArchiveTab /> : <HighlightsInboxTab />}
    </div>
  )
}
