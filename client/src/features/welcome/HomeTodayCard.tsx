/**
 * HomeTodayCard — Home's one "today" card (r3 design, owner decision 12).
 *
 * - **Rings** — the glance: a "You" ring that adds to today's highlight,
 *   then one ring per person with frames you haven't watched in the last
 *   24 hours (one per person, :func:`todayView`). An unseen ring carries
 *   a count badge — a visible marker, not a glow alone (C16).
 * - **"Momentum · N new"** — moments by others since you last had
 *   Momentum open (``store/momentumSeen``; the last 24 hours on a fresh
 *   browser), with who posted them. Read-only use of ``GET /api/moments``.
 * - **Hidden when nothing is new**, and while loading or after a failed
 *   load: Home is a glance, the pages have their own error states.
 *   A section the person turned off (``hide_highlights`` /
 *   ``hide_momentum``) is neither fetched nor shown.
 */
import { useEffect, useId, useState } from 'preact/hooks'
import { api } from '@/api'
import { ws } from '@/ws'
import { addBase } from '@/baseUrl'
import { formatLocale, isOne, t } from '@/i18n/i18n'
import { Avatar } from '@/components/Avatar'
import { currentUser } from '@/store/auth'
import { userPreferences } from '@/store/userPreferences'
import { markMomentumSeen, momentumSeenAt } from '@/store/momentumSeen'
import { namedPersonLabel, namedPersonPicture } from '@/store/householdUsers'
import { normaliseTimestamp } from '@/utils/relativeTime'
import { TODAY_WINDOW_MS, todayView, type TodayPerson } from '@/features/highlights/today'
import type { HighlightInboxItem, Moment } from '@/types'

const NAMES_SHOWN = 2

interface Loaded {
  highlights: HighlightInboxItem[]
  moments: Moment[]
}

/** Moments by others the person hasn't had a chance to see yet. */
export function newMoments(
  moments: readonly Moment[], me: string | null | undefined, seenAt: number | null,
  now: number = Date.now(),
): Moment[] {
  const since = seenAt ?? now - TODAY_WINDOW_MS
  return moments.filter(m => (
    m.author_user_id !== me
    && Date.parse(normaliseTimestamp(m.created_at)) > since
    && Date.parse(normaliseTimestamp(m.expires_at)) > now
  ))
}

/** "Anna and Thomas" / "A, B and 2 more" — distinct authors, newest first. */
export function authorNames(moments: readonly Moment[]): string {
  const names: string[] = []
  for (const m of moments) {
    const name = namedPersonLabel(m.author_user_id, m.author)
    if (name && !names.includes(name)) names.push(name)
  }
  const list = (items: string[]) => {
    try {
      return new Intl.ListFormat(formatLocale(), { style: 'long', type: 'conjunction' }).format(items)
    } catch {
      return items.join(', ')
    }
  }
  if (names.length <= NAMES_SHOWN + 1) return list(names)
  const more = names.length - NAMES_SHOWN
  return t(isOne(more) ? 'welcome.today.names_more_one' : 'welcome.today.names_more', {
    names: names.slice(0, NAMES_SHOWN).join(', '), n: String(more),
  })
}

const SVG = {
  viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': 2,
  'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true', focusable: 'false',
} as const

function Ring({ person }: { person: TodayPerson }) {
  const name = namedPersonLabel(person.authorId, person.highlight.author)
  const first = name.split(/\s+/)[0] || name
  return (
    <li>
      <a
        class="sh-home-ring sh-home-ring--new"
        href={addBase(`/highlights/${person.openId}`)}
        aria-label={t(isOne(person.unseen) ? 'welcome.today.ring_aria_one' : 'welcome.today.ring_aria',
          { name, n: String(person.unseen) })}
      >
        <span class="sh-home-ring__avatar">
          <Avatar name={name} src={namedPersonPicture(person.authorId, person.highlight.author)} size={56} />
          <span class="sh-home-ring__badge" aria-hidden="true">{person.unseen}</span>
        </span>
        <span class="sh-home-ring__label" aria-hidden="true"><bdi dir="auto">{first}</bdi></span>
      </a>
    </li>
  )
}

export function HomeTodayCard() {
  const me = currentUser.value?.user_id
  const { hide_highlights: noHighlights, hide_momentum: noMomentum } = userPreferences.value
  const [data, setData] = useState<Loaded | null>(null)
  const headingId = useId()

  useEffect(() => {
    if (noHighlights && noMomentum) { setData(null); return }
    let live = true
    const load = () => {
      Promise.all([
        noHighlights ? Promise.resolve([]) : api.get('/api/highlights') as Promise<HighlightInboxItem[]>,
        noMomentum ? Promise.resolve([]) : api.get('/api/moments') as Promise<Moment[]>,
      ])
        .then(([highlights, moments]) => {
          if (live) setData({ highlights: highlights ?? [], moments: moments ?? [] })
        })
        // A glance surface: a failed load keeps what was there (or nothing).
        .catch(() => undefined)
    }
    load()
    const offs = [
      ws.on('highlight.frame_added', load),
      ws.on('highlight.frame_removed', load),
      ws.on('highlight.removed', load),
      ws.on('moment.created', load),
      ws.on('moment.deleted', load),
    ]
    return () => { live = false; offs.forEach(off => off()) }
  }, [noHighlights, noMomentum])

  if (!data) return null
  const fresh = noHighlights ? [] : todayView(data.highlights, me).fresh
  const moments = noMomentum ? [] : newMoments(data.moments, me, momentumSeenAt(me))
  if (fresh.length === 0 && moments.length === 0) return null

  return (
    <section class="sh-card sh-home-today" aria-labelledby={headingId}>
      <h2 id={headingId} class="sr-only">{t('welcome.today.aria')}</h2>
      {fresh.length > 0 && (
        <div class="sh-home-today__highlights">
          <div class="sh-home-today__head">
            <h3>{t('nav.highlights')}</h3>
            <a class="sh-home-today__all" href={addBase('/highlights')} aria-label={t('welcome.today.all_aria')}>
              {t('welcome.today.all')}
              <svg {...SVG} width={18} height={18} class="sh-flip-rtl"><path d="M9 6l6 6-6 6" /></svg>
            </a>
          </div>
          <ul class="sh-home-rings">
            <li>
              <a class="sh-home-ring sh-home-ring--you" href={addBase('/highlights/new')}
                 aria-label={t('highlight.today.share')}>
                <span class="sh-home-ring__avatar">
                  <Avatar name={currentUser.value?.display_name ?? ''}
                          src={currentUser.value?.picture_url ?? null} size={56} />
                  <span class="sh-home-ring__add" aria-hidden="true">
                    <svg {...SVG} width={14} height={14} stroke-width={3}><path d="M12 5v14M5 12h14" /></svg>
                  </span>
                </span>
                <span class="sh-home-ring__label" aria-hidden="true">{t('highlight.inbox.you')}</span>
              </a>
            </li>
            {fresh.map(p => <Ring key={p.authorId} person={p} />)}
          </ul>
        </div>
      )}
      {moments.length > 0 && (
        <a class="sh-home-today__momentum" href={addBase('/momentum')}
           onClick={() => markMomentumSeen(me)}>
          <span class="sh-home-today__icon" aria-hidden="true">
            <svg {...SVG} width={20} height={20}><path d="M3 11l18-8-8 18-2-8z" /></svg>
          </span>
          <span class="sh-home-today__text">
            <strong>
              {t(isOne(moments.length) ? 'welcome.today.momentum_one' : 'welcome.today.momentum',
                { n: String(moments.length) })}
            </strong>
            <span class="sh-muted"><bdi dir="auto">{authorNames(moments)}</bdi></span>
          </span>
          <svg {...SVG} width={20} height={20} class="sh-flip-rtl sh-home-today__chev"><path d="M9 6l6 6-6 6" /></svg>
        </a>
      )}
    </section>
  )
}
