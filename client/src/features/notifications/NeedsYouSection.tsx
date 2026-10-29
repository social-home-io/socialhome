/**
 * NeedsYouSection — the pinned "Needs you" region on top of the
 * Notifications page and the bell panel (D12, :mod:`./needsYou`).
 *
 *   NEEDS YOU (5)
 *   [avatar] Kim (Gamma House) wants to join Salon            ●
 *            12 min ago
 *            [Approve] [Decline]
 *   [space]  3 requests wait for you in Garden
 *            [Review]
 *   Show 2 more
 *
 * A ``region`` labelled by its heading. Each item is the ordinary row (one
 * link) with its actions as sibling buttons below it — never a button in a
 * link — and each action names the row it acts on (``aria-describedby``).
 * The chip counts pending items, not unread ones.
 */
import { useEffect, useState } from 'preact/hooks'
import { appHref } from '@/baseUrl'
import { Button } from '@/components/Button'
import { isOne, t } from '@/i18n/i18n'
import { spaces } from '@/store/spaces'
import { ws } from '@/ws'
import { relativeDocsTime } from '@/utils/relativeTime'
import type { Notification } from '@/types'
import { NotificationRow, RowAvatar, Sentence, markRowRead, withRead } from './NotificationRow'
import { groupSentence } from './rowText'
import {
  NEEDS_YOU_CAP,
  NEEDS_YOU_TYPES,
  acting,
  approve,
  declineWithUndo,
  dropHandled,
  flagsOf,
  flushDeclines,
  inlineAction,
  loadNeedsYou,
  needsYou,
  needsYouEntries,
  playHref,
  reviewHref,
  type NeedsYouEntry,
} from './needsYou'

interface Props {
  /** Bell panel: smaller rows. */
  compact?: boolean
  /** A link in the section was followed (the bell closes its panel). */
  onNavigate?: () => void
}

/** Keep the list live while a section is mounted: a new request, or one
 *  someone else handled, refreshes it; leaving sends pending declines. */
function useNeedsYouLive(): void {
  useEffect(() => {
    void loadNeedsYou()
    const offNew = ws.on('notification.new', (e) => {
      const type = (e.data as { notif_type?: string }).notif_type ?? ''
      if (NEEDS_YOU_TYPES.has(type)) void loadNeedsYou()
    })
    const offCount = ws.on('notification.unread_count', (e) => {
      const handled = (e.data as { handled?: unknown }).handled
      if (Array.isArray(handled) && handled.length > 0) {
        dropHandled(handled.filter((k): k is string => typeof k === 'string'))
      }
    })
    const onHide = () => flushDeclines()
    window.addEventListener('pagehide', onHide)
    return () => {
      offNew()
      offCount()
      window.removeEventListener('pagehide', onHide)
      flushDeclines() // navigating away commits a decline at once
    }
  }, [])
}

function spaceName(spaceId: string, rows: Notification[]): string {
  return spaces.value.find(s => s.id === spaceId)?.name
    ?? rows.find(r => r.params?.space_name)?.params?.space_name
    ?? ''
}

/** What each flag says next to "Review". */
const FLAG_KEYS: Record<string, string> = {
  edge: 'notifications.needs_you.flag.edge',
  unpaired: 'notifications.needs_you.flag.unpaired',
  admin: 'notifications.needs_you.flag.admin',
  age: 'notifications.needs_you.flag.age',
}

function Actions({ entry, describedBy, onNavigate }: {
  entry: NeedsYouEntry
  describedBy: string
  onNavigate?: () => void
}) {
  const row = entry.kind === 'single' ? entry.row : null
  const action = row ? inlineAction(row) : 'review'
  if (row && action === 'decide') {
    return (
      <div class="sh-needs__actions">
        <Button aria-describedby={describedBy} onClick={() => void approve(row)}>
          {t('notifications.needs_you.approve')}
        </Button>
        <Button variant="secondary" aria-describedby={describedBy} onClick={() => declineWithUndo(row)}>
          {t('notifications.needs_you.decline')}
        </Button>
      </div>
    )
  }
  if (row && action === 'game') {
    return (
      <div class="sh-needs__actions">
        <a
          class="sh-btn sh-btn--primary"
          href={appHref(playHref(row))}
          aria-describedby={describedBy}
          onClick={() => { void markRowRead(row).catch(() => {}); onNavigate?.() }}
        >
          {t('notifications.needs_you.play')}
        </a>
        <Button variant="secondary" aria-describedby={describedBy} onClick={() => declineWithUndo(row)}>
          {t('notifications.needs_you.decline')}
        </Button>
      </div>
    )
  }
  if (row && action === 'invite') {
    return (
      <div class="sh-needs__actions">
        <a
          class="sh-btn sh-btn--primary"
          href={appHref(row.link_url || '/spaces')}
          aria-describedby={describedBy}
          onClick={() => { void markRowRead(row).catch(() => {}); onNavigate?.() }}
        >
          {t('notifications.needs_you.view_invite')}
        </a>
        <Button variant="secondary" aria-describedby={describedBy} onClick={() => declineWithUndo(row)}>
          {t('notifications.needs_you.decline')}
        </Button>
      </div>
    )
  }
  // Why a single request isn't one tap: said next to its Review button.
  const why = (row ? flagsOf(row) : []).filter(f => FLAG_KEYS[f]).map(f => t(FLAG_KEYS[f]))
  return (
    <div class="sh-needs__actions">
      {why.length > 0 && <span class="sh-needs__why">{why.join(' · ')}</span>}
      <a
        class="sh-btn sh-btn--secondary"
        href={appHref(reviewHref(entry))}
        aria-describedby={describedBy}
        onClick={() => onNavigate?.()}
      >
        {t('notifications.needs_you.review')}
      </a>
    </div>
  )
}

function GroupRow({ entry, titleId, compact, onNavigate }: {
  entry: Extract<NeedsYouEntry, { kind: 'group' }>
  titleId: string
  compact?: boolean
  onNavigate?: () => void
}) {
  const name = spaceName(entry.spaceId, entry.rows)
  const unread = entry.rows.some(r => !r.read_at)
  const newest = entry.rows.reduce((a, r) => (r.created_at > a ? r.created_at : a), entry.rows[0].created_at)
  // The space's avatar (rounded square): a group has no single actor.
  const face: Notification = {
    ...entry.rows[0],
    type: 'moderation_pending',
    params: { actors: [], actor_count: 0, new: 1, total: 1, space_name: name || null },
  }
  return (
    <a
      class={`sh-notif-row${unread ? ' sh-notif-row--unread' : ''}${compact ? ' sh-notif-row--compact' : ''}`}
      href={appHref(reviewHref(entry))}
      onClick={() => onNavigate?.()}
    >
      <RowAvatar n={face} size={compact ? 36 : 40} />
      <span class="sh-notif-content">
        {unread && <><span class="sr-only">{t('notifications.unread')},</span>{' '}</>}
        <span class="sh-notif-title" id={titleId}>
          <Sentence segs={groupSentence(entry.rows.length, name)} />
        </span>
        {' '}
        <span class="sh-notif-meta">
          <time class="sh-notif-time" dateTime={newest}>{relativeDocsTime(newest)}</time>
        </span>
      </span>
      <span class="sh-notif-dot" aria-hidden="true" />
    </a>
  )
}

export function NeedsYouSection({ compact, onNavigate }: Props) {
  useNeedsYouLive()
  const [expanded, setExpanded] = useState(false)
  const entries = needsYouEntries(needsYou.value, acting.value)
  const pendingCount = entries.reduce((sum, e) => sum + (e.kind === 'group' ? e.rows.length : 1), 0)
  if (entries.length === 0) return null
  const shown = expanded ? entries : entries.slice(0, NEEDS_YOU_CAP)
  const more = entries.length - shown.length
  const headingId = compact ? 'sh-needs-h-bell' : 'sh-needs-h-page'

  const open = (n: Notification) => {
    if (!n.read_at) {
      needsYou.value = withRead(needsYou.value, n)
      void markRowRead(n).catch(() => {})
    }
    if (n.link_url) onNavigate?.()
  }

  return (
    <section class={`sh-needs${compact ? ' sh-needs--compact' : ''}`} aria-labelledby={headingId}>
      <h2 class="sh-needs__h" id={headingId}>
        {t('notifications.needs_you.title')}
        <span
          class="sh-needs__count"
          aria-label={t(isOne(pendingCount) ? 'notifications.needs_you.count_one' : 'notifications.needs_you.count', { n: String(pendingCount) })}
        >
          {pendingCount}
        </span>
      </h2>
      <ul class="sh-needs__list">
        {shown.map((entry) => {
          const titleId = `sh-needs-${compact ? 'b' : 'p'}-${entry.key.replace(/[^\w-]/g, '_')}`
          return (
            <li key={entry.key} class="sh-needs__item">
              {entry.kind === 'single'
                ? <NotificationRow n={entry.row} compact={compact} titleId={titleId} onOpen={open} />
                : <GroupRow entry={entry} titleId={titleId} compact={compact} onNavigate={onNavigate} />}
              <Actions entry={entry} describedBy={titleId} onNavigate={onNavigate} />
            </li>
          )
        })}
      </ul>
      {(more > 0 || expanded) && entries.length > NEEDS_YOU_CAP && (
        <button
          type="button"
          class="sh-link sh-needs__more"
          aria-expanded={expanded}
          onClick={() => setExpanded(!expanded)}
        >
          {expanded
            ? t('notifications.needs_you.show_less')
            : t(isOne(more) ? 'notifications.needs_you.show_more_one' : 'notifications.needs_you.show_more', { n: String(more) })}
        </button>
      )}
    </section>
  )
}

/** The ids the section shows — the timeline leaves them out. */
export function pinnedIds(): ReadonlySet<string> {
  return new Set(needsYou.value.map(n => n.id))
}
