/**
 * SpaceListPage — the one Spaces page (redesign PR 11):
 *
 *   header      "Find a space" · New space (primary) · Join with a code
 *   invitations pending invites, with the space's real name and inviter
 *   Yours       spaces you're a member of
 *   Following   spaces you read along (read-only)
 *   Discover    spaces you can join — Your household · Paired households ·
 *               Global (``SpaceDiscover``; the chip lives in ``?discover=``)
 *   Archived    collapsed at the bottom, never mixed with Yours
 *
 * A row's end slot carries ONE number: the filled "needs you" count for
 * an owner / admin / moderator when something waits for them, else the
 * quiet "N new" of the space's chat — never both. With no space at all
 * the page shows one empty card (and the header drops its buttons, so the
 * CTAs aren't duplicated); a failed read shows the load error with Retry,
 * never "No spaces yet".
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { useLocation } from 'preact-iso'
import { signal } from '@preact/signals'
import { useTitle } from '@/store/pageTitle'
import { spaces, loadSpaces } from '@/store/spaces'
import type { Space } from '@/types'
import { SpaceListSkeleton } from '@/components/Skeleton'
import { Button } from '@/components/Button'
import { LoadErrorState } from '@/components/LoadErrorState'
import { openSpaceCreate } from '@/components/SpaceCreateDialog'
import { RemoteInviteInboxBanner } from '@/components/RemoteInviteInboxBanner'
import { ConnIcon } from '@/features/connections/ConnIcon'
import { openSpaceJoinByCode } from './SpaceJoinByCodeDialog'
import { isOne, t } from '@/i18n/i18n'
import { connectionState, ws } from '@/ws'
import { currentUser } from '@/store/auth'
import {
  applySpaceChatUnreadFrame,
  loadSpaceChatUnread,
  spaceChatDeleteNeedsReload,
  spaceChatUnreadOf,
} from '@/store/spaceChat'
import { matchesTerm, parseDiscoverSource } from './discover'
import { SpaceDiscover } from './SpaceDiscover'
import { SpaceListRow, activityMeta } from './SpaceListRow'

const loading = signal(true)
const failed = signal(false)

async function loadAll(): Promise<void> {
  loading.value = spaces.value.length === 0
  const [ok] = await Promise.all([loadSpaces(), loadSpaceChatUnread()])
  failed.value = !ok
  loading.value = false
}

/** The row's end slot: a filled needs-you count (it wins), else the
 *  quiet count of unread chat messages. */
export function rowEnd(s: Space): { needs: number; unread: number } {
  const needs = s.needs_you ?? 0
  return { needs, unread: needs > 0 ? 0 : spaceChatUnreadOf(s.id) }
}

function SpaceRowEnd({ needs, unread }: { needs: number; unread: number }) {
  if (needs > 0) return <span class="sh-spaces-attn">{needs > 99 ? '99+' : needs}</span>
  if (unread > 0) {
    return <span class="sh-spaces-new">{t('spaces.list.new', { n: unread > 99 ? '99+' : String(unread) })}</span>
  }
  return null
}

/** Everything the row says, in reading order. */
function rowLabel(s: Space, meta: string, needs: number, unread: number): string {
  const parts = [s.name, meta]
  if (needs > 0) parts.push(t(isOne(needs) ? 'spaces.list.needs_you_one' : 'spaces.list.needs_you', { n: String(needs) }))
  else if (unread > 0) parts.push(t(isOne(unread) ? 'spaces.list.chat_unread_one' : 'spaces.list.chat_unread', { n: String(unread) }))
  return parts.filter(Boolean).join(', ')
}

function MemberRow({ space: s, following }: { space: Space; following?: boolean }) {
  const activity = activityMeta(s.member_count, s.last_activity_at)
  const meta = following
    ? [t('spaces.list.following_meta'), activity].filter(Boolean).join(' · ')
    : activity
  const { needs, unread } = rowEnd(s)
  return (
    <SpaceListRow
      to={`/spaces/${s.id}`}
      iconUrl={s.icon_url}
      emoji={s.emoji}
      name={s.name}
      meta={meta}
      label={rowLabel(s, meta, needs, unread)}
      end={<SpaceRowEnd needs={needs} unread={unread} />}
    />
  )
}

function SectionHead({ id, title, count, desc }: { id: string; title: string; count?: number; desc?: string }) {
  return (
    <>
      <div class="sh-conn-sect-head">
        <div class="sh-conn-sect-head__title">
          <h2 id={id}>{title}</h2>
          {count !== undefined && (
            <span class="sh-conn-count" aria-label={t('connections.count_label', { n: String(count) })}>{count}</span>
          )}
        </div>
      </div>
      {desc && <p class="sh-conn-sect-desc">{desc}</p>}
    </>
  )
}

export default function SpaceListPage() {
  useTitle(t('nav.spaces'))
  const loc = useLocation()
  const discover = parseDiscoverSource(loc.query?.discover) ?? 'household'
  const [term, setTerm] = useState('')
  const scrolled = useRef(false)

  useEffect(() => { void loadAll() }, [])

  // Opened as ``/spaces?discover=…`` (the retired /spaces/browse, a shared
  // link): bring Discover into view once, after the first load.
  const isLoading = loading.value
  // The URL the page opened with — a later chip click doesn't scroll.
  const wantsDiscover = useRef(!!loc.query?.discover).current
  useEffect(() => {
    if (scrolled.current || isLoading || !wantsDiscover) return
    scrolled.current = true
    document.getElementById('discover')?.scrollIntoView?.({ block: 'start' })
  }, [isLoading, wantsDiscover])

  // Live chat activity: bump a space's count by the same rules as its
  // chat; a chat the list has no row for yet re-reads the counts, as do a
  // deleted message in a chat that shows a count and a WS reconnect.
  useEffect(() => {
    const offMsg = ws.on('dm.message', (e) => {
      const res = applySpaceChatUnreadFrame(e.data, currentUser.value?.user_id)
      if (res === 'unknown') void loadSpaceChatUnread()
    })
    const offDel = ws.on('dm.message_deleted', (e) => {
      if (spaceChatDeleteNeedsReload(e.data)) void loadSpaceChatUnread()
    })
    let prevConn = connectionState.value
    const offConn = connectionState.subscribe((next) => {
      if (prevConn === 'reconnecting' && next === 'open') void loadSpaceChatUnread()
      prevConn = next
    })
    return () => { offMsg(); offDel(); offConn() }
  }, [])

  if (loading.value) return <SpaceListSkeleton />
  if (failed.value) {
    return (
      <div class="sh-spaces">
        <LoadErrorState headingLevel={2} message={t('spaces.list.load_failed')} onRetry={() => { void loadAll() }} />
      </div>
    )
  }

  const all = spaces.value
  const archived = all.filter((s) => s.archived)
  const active = all.filter((s) => !s.archived)
  const match = (s: Space) => matchesTerm(term, s.name, s.description)
  const yours = active.filter((s) => s.my_role !== 'subscriber').filter(match)
  const following = active.filter((s) => s.my_role === 'subscriber').filter(match)
  const seatedIds = new Set(all.map((s) => s.id))
  const none = all.length === 0
  const searching = term.trim() !== ''
  const reload = () => { void loadAll() }

  return (
    <div class="sh-spaces">
      {!none && (
        <div class="sh-spaces-head">
          <label class="sh-spaces-find">
            <span class="sr-only">{t('spaces.list.find')}</span>
            <ConnIcon name="search" />
            <input
              type="search"
              class="sh-spaces-find__input"
              placeholder={t('spaces.list.find')}
              value={term}
              onInput={(e) => setTerm((e.target as HTMLInputElement).value)}
            />
          </label>
          <div class="sh-spaces-head__actions">
            <Button onClick={openSpaceCreate}>
              <ConnIcon name="plus" /> {t('spaces.list.new_space')}
            </Button>
            <Button variant="secondary" onClick={openSpaceJoinByCode}>
              {t('spaces.list.join_with_code')}
            </Button>
          </div>
        </div>
      )}

      <RemoteInviteInboxBanner onAccepted={reload} />

      {none && (
        <div class="sh-spaces-card sh-spaces-empty">
          <div aria-hidden="true" class="sh-spaces-empty__art">🏡</div>
          <h2>{t('spaces.list.empty_title')}</h2>
          <p>{t('spaces.list.empty_body')}</p>
          <div class="sh-spaces-empty__actions">
            <Button onClick={openSpaceCreate}>
              <ConnIcon name="plus" /> {t('spaces.list.new_space')}
            </Button>
            <Button variant="secondary" onClick={openSpaceJoinByCode}>
              {t('spaces.list.join_with_code')}
            </Button>
          </div>
        </div>
      )}

      {yours.length > 0 && (
        <section class="sh-spaces-sect" aria-labelledby="sh-spaces-yours">
          <SectionHead id="sh-spaces-yours" title={t('spaces.list.yours')} count={yours.length} />
          <ul class="sh-spaces-card sh-spaces-list">
            {yours.map((s) => <MemberRow key={s.id} space={s} />)}
          </ul>
        </section>
      )}

      {following.length > 0 && (
        <section class="sh-spaces-sect" aria-labelledby="sh-spaces-following">
          <SectionHead
            id="sh-spaces-following"
            title={t('spaces.list.following')}
            count={following.length}
            desc={t('spaces.list.following_desc')}
          />
          <ul class="sh-spaces-card sh-spaces-list">
            {following.map((s) => <MemberRow key={s.id} space={s} following />)}
          </ul>
        </section>
      )}

      {searching && !none && yours.length === 0 && following.length === 0 && (
        <p class="sh-spaces-note">{t('spaces.list.no_match', { term: term.trim() })}</p>
      )}

      <SpaceDiscover seatedIds={seatedIds} source={discover} term={term} onChanged={reload} />

      {archived.length > 0 && (
        <details class="sh-spaces-card sh-spaces-archived">
          <summary
            class="sh-spaces-archived__sum"
            aria-label={t('spaces.list.archived_label', { n: String(archived.length) })}
          >
            <span class="sh-spaces-archived__ic" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <rect x="3" y="4" width="18" height="5" rx="1" /><path d="M5 9v10h14V9M10 13h4" />
              </svg>
            </span>
            <span class="sh-spaces-archived__body">
              <h2 class="sh-spaces-archived__t">{t('spaces.list.archived')}</h2>
              <span class="sh-spaces-row__m">{archived.map((s) => s.name).join(', ')}</span>
            </span>
            <span class="sh-conn-count" aria-label={t('connections.count_label', { n: String(archived.length) })}>
              {archived.length}
            </span>
            <span class="sh-spaces-archived__chev" aria-hidden="true"><ConnIcon name="chev" /></span>
          </summary>
          <ul class="sh-spaces-list">
            {archived.map((s) => (
              <SpaceListRow
                key={s.id}
                to={`/spaces/${s.id}`}
                iconUrl={s.icon_url}
                emoji={s.emoji}
                name={s.name}
                meta={t('spaces.list.archived_meta')}
                muted
              />
            ))}
          </ul>
        </details>
      )}
    </div>
  )
}
