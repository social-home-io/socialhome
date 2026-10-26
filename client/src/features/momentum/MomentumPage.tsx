/**
 * MomentumPage — host for the Momentum pillar (§Momentum, UX r3 PR 8).
 *
 * Tabs are links in a ``<nav>`` with ``aria-current`` (the Spaces
 * pattern): ``/momentum`` (Moments), ``/momentum/yours`` (your own
 * moments, decision 6a) and ``/momentum/discover`` (people sharing
 * publicly on a GFS, decision 5). Discover shows only once this household
 * has an active GFS and never for a protected account (it can't follow
 * through a GFS); its own address still opens it. Next to the tabs: the ``#`` filter — a popover of trending tags,
 * never a permanent row — and the gear (Settings › Momentum).
 *
 * A hashtag filter is ``/momentum?tag=<name>`` and shows as one removable
 * chip above the list. Older links keep working: ``?tab=yours`` and
 * ``?tab=archive`` go to ``/momentum/yours``, ``?tab=archive&tag=…`` to
 * the tag filter, ``/momentum/public/discover`` goes to
 * ``/momentum/discover``, and ``/momentum/archive`` redirects (its own
 * route).
 */
import { useEffect, useId, useRef, useState } from 'preact/hooks'
import { useLocation } from 'preact-iso'
import { api } from '@/api'
import { addBase } from '@/baseUrl'
import { MomentumComposerDialog } from '@/components/MomentumComposerDialog'
import { isRestricted } from '@/components/ProtectedNotice'
import { t } from '@/i18n/i18n'
import { gfsConnections, loadGfsConnections } from '@/store/momentPublic'
import { useTitle } from '@/store/pageTitle'
import MomentumInboxTab from './MomentumInboxTab'
import MomentumYoursTab from './MomentumYoursTab'
import PublicDiscoveryPage from './PublicDiscoveryPage'
import { noteMomentumList } from './backLink'

export type MomentumTab = 'moments' | 'yours' | 'discover'

export interface MomentumView {
  tab: MomentumTab
  /** The hashtag filter (Moments only), or ``null``. */
  tag: string | null
  /** An old URL's new address (replace, don't push), or ``null``. */
  redirect: string | null
}

const TAB_PATH: Readonly<Record<MomentumTab, string>> = {
  moments: '/momentum',
  yours: '/momentum/yours',
  discover: '/momentum/discover',
}

const tagPath = (tag: string) => `/momentum?tag=${encodeURIComponent(tag)}`

/** Which tab (and tag) a Momentum URL shows, or where an old one moved. */
export function momentumViewFromUrl(url: string): MomentumView {
  const [path, query = ''] = url.split('?')
  const params = new URLSearchParams(query)
  const clean = path.replace(/\/+$/, '')
  if (clean === '/momentum/yours') return { tab: 'yours', tag: null, redirect: null }
  if (clean === '/momentum/discover') return { tab: 'discover', tag: null, redirect: null }
  // Discover was a page of its own before it became a tab.
  if (clean === '/momentum/public/discover') {
    return { tab: 'discover', tag: null, redirect: TAB_PATH.discover }
  }
  const tab = params.get('tab')
  const tag = params.get('tag') || null
  if (tab === 'yours' || (tab === 'archive' && !tag)) {
    // The old Archive tab is your own moments now.
    const show = params.get('show') === 'saved' ? '?show=saved' : ''
    return { tab: 'yours', tag: null, redirect: `${TAB_PATH.yours}${show}` }
  }
  if (tab === 'archive' && tag) return { tab: 'moments', tag, redirect: tagPath(tag) }
  return { tab: 'moments', tag, redirect: null }
}

const tabLabel = (tab: MomentumTab): string =>
  tab === 'yours' ? t('moment.tab.yours')
    : tab === 'discover' ? t('moment.tab.discover')
      : t('moment.tab.moments')

const TITLE_KEY: Readonly<Record<MomentumTab, string>> = {
  moments: 'nav.momentum',
  yours: 'page_title.moments_yours',
  discover: 'nav.momentum',
}

export default function MomentumPage() {
  const loc = useLocation()
  const view = momentumViewFromUrl(loc.url)
  // A moment opened from here goes back to this tab and tag.
  noteMomentumList(view.redirect ?? loc.url)
  useTitle(t(TITLE_KEY[view.tab]))
  const restricted = isRestricted('public_moments')

  useEffect(() => {
    if (view.redirect && view.redirect !== loc.url) loc.route(view.redirect, true)
  }, [view.redirect])
  useEffect(() => {
    // Fresh per visit: a GFS connected meanwhile (pairing) shows Discover.
    if (!restricted) void loadGfsConnections(true)
  }, [restricted])

  const { tab, tag } = view
  // Discover: someone with a GFS to discover on (and allowed to follow
  // through one); its own address keeps its tab in view.
  const tabs = (Object.keys(TAB_PATH) as MomentumTab[]).filter(k =>
    k !== 'discover' || tab === 'discover'
    || (!restricted && (gfsConnections.value?.length ?? 0) > 0))
  return (
    <div class="sh-momentum-host">
      <div class="sh-space-subheader sh-space-subheader--sections sh-momentum-subheader">
        <nav class="sh-space-sections" aria-label={t('moment.sections_aria')}>
          <ul class="sh-space-sections__row">
            {tabs.map(k => (
              <li key={k}>
                <a
                  href={addBase(TAB_PATH[k])}
                  class={k === tab ? 'sh-space-section sh-space-section--active' : 'sh-space-section'}
                  aria-current={k === tab ? 'page' : undefined}
                >
                  {tabLabel(k)}
                </a>
              </li>
            ))}
          </ul>
        </nav>
        <div class="sh-space-subheader-actions">
          {tab !== 'discover' && <TagPicker />}
          <a class="sh-momentum-iconbtn" href={addBase('/settings#momentum')}
            aria-label={t('moment.settings_aria')} title={t('moment.settings_aria')}>
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
              stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">
              <circle cx="12" cy="12" r="3" />
              <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
            </svg>
          </a>
        </div>
      </div>
      {tab === 'moments' && tag && (
        <div class="sh-momentum-filter">
          <a class="sh-momentum-tagchip" href={addBase('/momentum')}
            aria-label={t('moment.filter.remove_aria', { tag })}>
            <bdi dir="auto">#{tag}</bdi>
            <span aria-hidden="true" class="sh-momentum-tagchip__x">✕</span>
          </a>
        </div>
      )}
      {tab === 'yours' ? <MomentumYoursTab />
        : tab === 'discover' ? <PublicDiscoveryPage />
          : <MomentumInboxTab tag={tag} />}
      <MomentumComposerDialog />
    </div>
  )
}

interface TrendingTag { tag: string; count: number }

/** The ``#`` button: a non-modal popover of trending tags (links). Esc or
 *  a press outside closes it and focus returns to the button. */
function TagPicker() {
  const [open, setOpen] = useState(false)
  const [tags, setTags] = useState<TrendingTag[] | null>(null)
  const [failed, setFailed] = useState(false)
  const wrap = useRef<HTMLDivElement>(null)
  const button = useRef<HTMLButtonElement>(null)
  const panelId = useId()

  const close = (refocus: boolean) => {
    setOpen(false)
    if (refocus) button.current?.focus()
  }

  useEffect(() => {
    if (!open) return
    setFailed(false)
    api.get<{ hashtags: TrendingTag[] }>('/api/moments/hashtags', { limit: '12' })
      .then(r => setTags(r?.hashtags ?? []))
      .catch(() => setFailed(true))
    const onDown = (e: PointerEvent) => {
      if (!wrap.current?.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('pointerdown', onDown)
    return () => document.removeEventListener('pointerdown', onDown)
  }, [open])

  return (
    <div class="sh-momentum-tagpicker" ref={wrap}
      onKeyDown={(e) => { if (e.key === 'Escape' && open) { e.stopPropagation(); close(true) } }}>
      <button
        ref={button}
        type="button"
        class="sh-momentum-iconbtn"
        aria-label={t('moment.filter.button_aria')}
        title={t('moment.filter.button_aria')}
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen(v => !v)}
      >
        <span aria-hidden="true" class="sh-momentum-iconbtn__hash">#</span>
      </button>
      {open && (
        <div id={panelId} class="sh-momentum-tagpanel">
          <h2 class="sh-momentum-tagpanel__title">{t('moment.filter.title')}</h2>
          {failed ? (
            <p class="sh-muted">{t('moment.filter.load_failed')}</p>
          ) : tags === null ? (
            <p class="sh-muted">{t('moment.filter.loading')}</p>
          ) : tags.length === 0 ? (
            <p class="sh-muted">
              {t('moment.archive.hashtag_hint_before')}
              <code>#{t('moment.archive.hashtag_example')}</code>
              {t('moment.archive.hashtag_hint_after')}
            </p>
          ) : (
            <ul class="sh-momentum-tagpanel__list">
              {tags.map(x => (
                <li key={x.tag}>
                  <a href={addBase(tagPath(x.tag))} class="sh-momentum-tagpanel__tag"
                    onClick={() => setOpen(false)}>
                    <bdi dir="auto">#{x.tag}</bdi>
                    <span class="sh-momentum-tagpanel__count">{x.count}</span>
                  </a>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}
