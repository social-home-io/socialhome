/**
 * MomentumDetailPage — one moment with its replies (UX r3, PR 10).
 *
 * Routed at ``/momentum/{momentId}`` and ``/momentum/{momentId}/reply``
 * (the same page with the reply field focused — the one reply entry point).
 * A single column:
 *
 * - **‹ Moments** back to the list the person came from, tab and tag kept
 *   (:mod:`./backLink`).
 * - **The moment** as a :class:`MomentCard` head: ⋯ actions, optimistic
 *   reaction chips with who reacted ("Anna, Lena and Oma Hilde reacted",
 *   names known in this household), the v_69 totals line.
 * - **Your own moment: a neutral status band** — who sees it and for how
 *   long; "Public on <GFS>" with Copy link when it is public. Public is
 *   chosen in the composer only (owner decision 3): no "Make public", and
 *   no "Stop sharing" (withdrawing would delete the moment for paired
 *   followers too).
 * - **Replies in place**, each with a 40 px ⋯ — yours: Delete reply; a
 *   household admin: Remove reply; anyone else's: Report — and the reply
 *   field at the end.
 * - **A link to a reply** (``?reply={id}``, the reply notification) opens
 *   the moment with that reply picked out and scrolled into view; opening
 *   a reply's own id goes there too. A reply whose moment is gone shows
 *   alone, saying so.
 *
 * Gone (404 — deleted, expired or hidden) and failed (the read didn't come
 * back) stay distinct (#1168). Subscribes to ``moment.*`` WS frames and
 * refetches on any change to this moment or its replies.
 */
import { useEffect, useRef } from 'preact/hooks'
import { signal } from '@preact/signals'
import { useLocation, useRoute } from 'preact-iso'
import { api } from '@/api'
import { addBase } from '@/baseUrl'
import { Avatar } from '@/components/Avatar'
import { Button } from '@/components/Button'
import { openLightbox } from '@/components/ImageLightbox'
import { LoadErrorState } from '@/components/LoadErrorState'
import { OverflowMenu, type MenuItem } from '@/components/OverflowMenu'
import { PersonButton } from '@/components/PersonButton'
import { openReport } from '@/components/ReportDialog'
import { MomentumDetailSkeleton } from '@/components/Skeleton'
import { showToast } from '@/components/Toast'
import { VideoMedia } from '@/components/VideoMedia'
import { confirmDialog } from '@/components/confirm'
import { formatLocale, isOne, t } from '@/i18n/i18n'
import { currentUser } from '@/store/auth'
import { blockedUserIds, loadBlocks } from '@/store/blocks'
import {
  householdUsers,
  loadHouseholdUsers,
  namedPersonLabel,
  namedPersonPicture,
} from '@/store/householdUsers'
import { loadRegistrations, registrations } from '@/store/momentPublic'
import { markSeen } from '@/store/notifications'
import { useTitle } from '@/store/pageTitle'
import type { Moment, MomentDetail } from '@/types'
import { getMomentsPreferences, momentRetentionLabel } from '@/utils/preferences'
import { relativeDocsTime } from '@/utils/relativeTime'
import { ws } from '@/ws'
import { backIsYours, momentumBackTarget } from './backLink'
import { loadGfsNames, publicOnLabel } from './gfsNames'
import { renderHashtagged } from './hashtags'
import { MomentCard, relativeTime } from './MomentCard'
import { MomentReplyField } from './MomentReplyField'

const detail = signal<MomentDetail | null>(null)
/** Why there is no moment to show: ``gone`` (404 — deleted, expired or
 *  hidden) or ``failed`` (the read didn't come back). */
const failure = signal<'gone' | 'failed' | null>(null)
const loading = signal<boolean>(true)
/** Showing a reply alone: the moment it answers is gone. */
const parentGone = signal<boolean>(false)

/** How many names the reaction line lists before "and N others". */
const MAX_NAMES = 3

/** "Anna, Lena and Oma Hilde reacted" / "Anna and 3 others reacted" —
 *  ``null`` when nobody is named (a bare count says nothing new). */
export function reactedSummary(names: string[], others: number): string | null {
  if (names.length === 0) return null
  const parts = [...names]
  if (others > 0) {
    parts.push(t(isOne(others) ? 'moment.detail.reacted_others_one' : 'moment.detail.reacted_others', { n: String(others) }))
  }
  let list: string
  try {
    list = new Intl.ListFormat(formatLocale(), { style: 'long', type: 'conjunction' }).format(parts)
  } catch {
    list = parts.join(', ')
  }
  return t('moment.detail.reacted', { names: list })
}

/** ``Tue 09:02`` — when others stop seeing it (always within a day). */
function untilTime(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString(formatLocale(), { weekday: 'short', hour: 'numeric', minute: '2-digit' })
}

export default function MomentumDetailPage() {
  useTitle(t('page_title.moment'))
  const { params } = useRoute()
  const loc = useLocation()
  const momentId = params.momentId
  const pickedId = loc.query?.reply ?? null
  const wantsReply = (loc.path ?? '').replace(/\/+$/, '').endsWith('/reply')
  const me = currentUser.value?.user_id
  const retryRef = useRef<() => void>(() => {})
  const replyRef = useRef<HTMLTextAreaElement | null>(null)
  /** The focus / scroll for this visit happened (once, not per refresh). */
  const settledRef = useRef<string | null>(null)

  useEffect(() => {
    loading.value = true
    detail.value = null
    failure.value = null
    parentGone.value = false
    settledRef.current = null
    void loadBlocks()  // hide replies from blocked authors
    void loadHouseholdUsers()  // names for the reaction line and replies
    void loadRegistrations().then(() => {
      if (registrations.value.length > 0) void loadGfsNames()
    })

    /** The moment is gone: a reply link still shows the reply, alone. */
    const showReplyAlone = async (): Promise<boolean> => {
      if (!pickedId) return false
      try {
        const r = await api.get(`/api/moments/${pickedId}`) as MomentDetail
        if (r.moment.parent_moment_id !== momentId) return false
        detail.value = { ...r, replies: [] }
        parentGone.value = true
        failure.value = null
        return true
      } catch {
        return false
      }
    }

    const refetch = (initial: boolean) =>
      api.get(`/api/moments/${momentId}`)
        .then((d: MomentDetail) => {
          const parentId = d.moment.parent_moment_id
          if (parentId) {
            // A reply's own address: open the moment it answers, with the
            // reply picked out (that page shows it alone if the moment is
            // gone).
            loc.route(`/momentum/${parentId}?reply=${encodeURIComponent(d.moment.id)}`, true)
            return
          }
          detail.value = d
          failure.value = null
        })
        .catch(async (err: unknown) => {
          // 404 = deleted, expired or hidden — say so, also when it goes
          // while open. Anything else is a failed load, not "gone"; a
          // failed refresh keeps what is on screen.
          if ((err as { status?: number })?.status === 404) {
            if (await showReplyAlone()) return
            detail.value = null
            failure.value = 'gone'
          } else if (detail.value === null) {
            failure.value = 'failed'
          }
        })
        .finally(() => { if (initial) loading.value = false })
    retryRef.current = () => void refetch(false)
    void refetch(true)
    const matches = (data: { moment_id?: string; parent_moment_id?: string | null }) =>
      data.moment_id === momentId || data.parent_moment_id === momentId
      || (pickedId !== null && data.moment_id === pickedId)
    const dispose = [
      ws.on('moment.created', (e) => {
        if (matches(e.data as { parent_moment_id?: string })) void refetch(false)
      }),
      ws.on('moment.deleted', (e) => {
        if (matches(e.data as { moment_id?: string })) void refetch(false)
      }),
      ws.on('moment.reaction_changed', (e) => {
        if (matches(e.data as { moment_id?: string })) void refetch(false)
      }),
    ]
    return () => {
      dispose.forEach(d => d())
      // The next page starts from its skeleton, not this moment.
      loading.value = true
      detail.value = null
    }
  }, [momentId, pickedId])

  // Once this moment shows (this render's state, not the signals' by the
  // time the effect runs): pick out the linked reply, or focus the field.
  const shownId = detail.value?.moment.id
  const shown = !loading.value && (shownId === momentId || (pickedId !== null && shownId === pickedId))
  useEffect(() => {
    if (!shown || settledRef.current === momentId) return
    settledRef.current = momentId
    // The moment is on screen: its reaction and reply notifications read
    // as read on every device.
    markSeen(`/momentum/${momentId}`)
    if (pickedId) {
      const el = document.getElementById(`moment-reply-${pickedId}`)
      if (el) {
        const scroll = () => el.scrollIntoView({ block: 'center' })
        scroll()
        el.focus({ preventScroll: true })
        // A picture above it that is still loading pushes the reply down:
        // scroll again once it has its size.
        el.closest('.sh-momentum-detail')?.querySelectorAll('img').forEach((img) => {
          if (!img.complete) img.addEventListener('load', scroll, { once: true })
        })
      }
    } else if (wantsReply) {
      replyRef.current?.focus()
    }
  }, [shown, momentId, pickedId, wantsReply])

  const backTarget = momentumBackTarget()
  const goBack = (ev: MouseEvent) => {
    ev.preventDefault()
    loc.route(backTarget)
  }
  const backLink = (
    <a class="sh-moment-back" href={addBase(backTarget)} onClick={goBack}>
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
        aria-hidden="true" focusable="false" class="sh-moment-back__icon sh-flip-rtl">
        <path d="M15 18l-6-6 6-6" />
      </svg>
      <span>{t(backIsYours(backTarget) ? 'moment.tab.yours' : 'moment.tab.moments')}</span>
    </a>
  )

  if (loading.value) return <MomentumDetailSkeleton />
  if (failure.value === 'gone') {
    return (
      <div class="sh-momentum-detail">
        {backLink}
        <div class="sh-empty-state" role="status">
          <div aria-hidden="true">⌛</div>
          <h2>{t('moment.detail.gone_title')}</h2>
          <p>{t('moment.detail.gone_body')}</p>
          <div class="sh-empty-state__cta-row">
            <a class="sh-btn sh-btn--primary" href={addBase(backTarget)} onClick={goBack}>
              {t('moment.detail.back')}
            </a>
          </div>
        </div>
      </div>
    )
  }
  if (failure.value === 'failed') {
    return (
      <div class="sh-momentum-detail">
        {backLink}
        <LoadErrorState
          message={t('moment.detail.load_failed_title')}
          detail={t('moment.detail.load_failed_body')}
          retryLabel={t('load.try_again')}
          onRetry={() => retryRef.current()}
          headingLevel={2}
        />
      </div>
    )
  }
  const d = detail.value
  if (!d) return null
  // An older server sends no per-emoji counts: roll up what is held here.
  let m = d.moment
  if (!m.reaction_counts) {
    const counts: Record<string, number> = {}
    for (const r of d.reactions) counts[r.emoji] = (counts[r.emoji] || 0) + 1
    m = {
      ...m,
      reaction_counts: counts,
      my_reaction: m.my_reaction ?? d.reactions.find(r => r.reactor_user_id === me)?.emoji ?? null,
    }
  }
  const isAuthor = m.author_user_id === me
  // Past its expiry only its author (kept under Yours, decision 6a) or
  // someone who saved it (decision 6b) still sees it: replies close and
  // the share link has ended.
  const ended = Date.parse(m.expires_at) <= Date.now()

  const patchMoment = (_id: string, patch: Partial<Moment>) => {
    const cur = detail.value
    if (cur) detail.value = { ...cur, moment: { ...cur.moment, ...patch } }
  }
  const dropReply = (id: string) => {
    const cur = detail.value
    if (cur) detail.value = { ...cur, replies: cur.replies.filter(r => r.id !== id) }
  }
  const addReply = (r: Moment) => {
    const cur = detail.value
    if (!cur || cur.replies.some(x => x.id === r.id)) return
    detail.value = { ...cur, replies: [...cur.replies, r] }
  }

  // Who reacted: you first, then the people this household knows (local
  // names only — never resolved from elsewhere); the rest as a count where
  // the count is complete.
  const blocked = blockedUserIds.value
  const users = householdUsers.value
  const names: string[] = []
  if (m.my_reaction) names.push(t('moment.card.you'))
  for (const r of d.reactions) {
    if (r.reactor_user_id === me || blocked.has(r.reactor_user_id)) continue
    const u = users.get(r.reactor_user_id)
    if (u) names.push(namedPersonLabel(r.reactor_user_id, null))
  }
  const named = names.slice(0, MAX_NAMES)
  const countKnown = isAuthor || m.received_via === 'self'
    || m.reaction_totals_at != null || m.reactions_reach === 'household'
  const others = countKnown ? Math.max(0, m.reaction_count - named.length) : 0
  const reactedBy = reactedSummary(named, others)

  const totalsAt = !isAuthor && m.reactions_reach !== 'household'
    && typeof m.reaction_totals_at === 'number'
    ? new Date(m.reaction_totals_at).toISOString()
    : null

  const visibleReplies = d.replies.filter(r => !blocked.has(r.author_user_id))

  return (
    <div class="sh-momentum-detail">
      {backLink}
      {parentGone.value && (
        <p class="sh-moment-parent-gone" role="status">{t('moment.detail.parent_gone')}</p>
      )}

      <MomentCard
        m={m}
        me={me}
        onPatch={patchMoment}
        onDeleted={() => loc.route(backTarget)}
        page={{
          status: isAuthor ? <StatusBand m={m} ended={ended} /> : undefined,
          reactedBy,
          onReply: () => replyRef.current?.focus(),
        }}
      />
      {/* v_69: the counts above are everyone's, as the author's
          household last counted them. */}
      {totalsAt !== null && (
        <p class="sh-muted sh-momentum-reactions-note" data-testid="moment-reaction-totals">
          {t('moment.detail.reactions_totals', { time: relativeDocsTime(totalsAt) })}
        </p>
      )}
      {/* A GFS-followed author's public bio — the only "about" line for
          someone with no household connection. */}
      {!isAuthor && m.author?.bio && (
        <p class="sh-momentum-detail-bio" data-testid="moment-author-bio" dir="auto">
          {m.author.bio}
        </p>
      )}
      {ended && !isAuthor && (
        <p class="sh-muted sh-momentum-detail-ended">{t('moment.detail.saved_ended')}</p>
      )}

      {visibleReplies.length > 0 && (
        <section class="sh-moment-replies" aria-labelledby="sh-moment-replies-h">
          <h2 id="sh-moment-replies-h" class="sh-moment-replies__h">
            {t(isOne(visibleReplies.length) ? 'moment.detail.replies_one' : 'moment.detail.replies', { n: String(visibleReplies.length) })}
          </h2>
          <ul class="sh-moment-replies__list">
            {visibleReplies.map(r => (
              <ReplyRow key={r.id} r={r} me={me} picked={r.id === pickedId} onRemoved={dropReply} />
            ))}
          </ul>
        </section>
      )}

      {!ended && !parentGone.value && (
        <MomentReplyField parent={m} inputRef={replyRef} onPosted={addReply} />
      )}
    </div>
  )
}

/** Who sees your moment, and for how long — neutral, never an action to
 *  widen or withdraw it (decision 3). */
function StatusBand({ m, ended }: { m: Moment; ended: boolean }) {
  const isProtected = currentUser.value?.protected === true
  const isPublic = m.is_public === true && !isProtected && !ended
  const days = momentRetentionLabel(getMomentsPreferences().follower_retention_days)
  const time = untilTime(m.expires_at)

  const copyShareLink = async () => {
    // A link that opens THIS moment for anyone who has it — the key is in
    // the link's fragment, the GFS only holds ciphertext.
    let url: string
    try {
      url = (await api.post<{ url: string }>(`/api/moments/${m.id}/share-link`, {})).url
    } catch (err: unknown) {
      showToast(t('moment.share.failed', { error: String((err as Error)?.message ?? err) }), 'error')
      return
    }
    try {
      await navigator.clipboard.writeText(url)
      showToast(t('moment.share.copied'), 'success')
    } catch {
      showToast(url, 'info')
    }
  }

  let title: string
  let sub: string | null
  if (ended) {
    title = t('moment.detail.archived')
    sub = null
  } else if (isPublic) {
    title = publicOnLabel(registrations.value)
    sub = t('moment.detail.status_public_also', { days })
  } else if (isProtected) {
    title = t('moment.detail.status_reach_protected')
    sub = t('moment.detail.status_until_protected', { time })
  } else {
    title = t('moment.detail.status_reach')
    sub = t('moment.detail.status_until', { time, days })
  }

  return (
    <div class={`sh-moment-status${isPublic ? ' sh-moment-status--public' : ''}`}
      role="group" aria-label={t('moment.card.who_sees')}>
      <span class="sh-moment-status__icon" aria-hidden="true">
        {isPublic ? <GlobeIcon /> : <PeopleIcon />}
      </span>
      <div class="sh-moment-status__body">
        <strong class="sh-moment-status__title">{title}</strong>
        {sub && <span class="sh-moment-status__sub">{sub}</span>}
        {isPublic && (
          <div class="sh-moment-status__actions">
            <Button variant="secondary" onClick={() => void copyShareLink()}
              title={t('moment.share.title')}>
              {t('moment.detail.copy_link')}
            </Button>
          </div>
        )}
      </div>
    </div>
  )
}

function PeopleIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="2" stroke-linecap="round" stroke-linejoin="round" focusable="false">
      <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" /><circle cx="9" cy="7" r="4" />
      <path d="M22 21v-2a4 4 0 0 0-3-3.87" /><path d="M16 3.13a4 4 0 0 1 0 7.75" />
    </svg>
  )
}

function GlobeIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="2" stroke-linecap="round" stroke-linejoin="round" focusable="false">
      <circle cx="12" cy="12" r="10" /><path d="M2 12h20" />
      <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
    </svg>
  )
}

interface ReplyRowProps {
  r: Moment
  me: string | undefined
  /** The reply a link pointed at: picked out. */
  picked: boolean
  onRemoved: (id: string) => void
}

function ReplyRow({ r, me, picked, onRemoved }: ReplyRowProps) {
  const loc = useLocation()
  const mine = r.author_user_id === me
  const isAdmin = currentUser.value?.is_admin === true
  const realName = namedPersonLabel(r.author_user_id, r.author)
  const picture = namedPersonPicture(r.author_user_id, r.author)
  const hint = () => ({ display_name: realName, picture_url: picture })

  const remove = async (asAdmin: boolean) => {
    const ok = await confirmDialog(
      t(asAdmin ? 'moment.detail.remove_reply_confirm' : 'moment.detail.delete_reply_confirm'),
      { destructive: true },
    )
    if (!ok) return
    try {
      await api.delete(`/api/moments/${r.id}`)
      onRemoved(r.id)
      showToast(t(asAdmin ? 'moment.detail.reply_removed' : 'moment.detail.reply_deleted'), 'info')
    } catch (err: unknown) {
      showToast(t('highlight.viewer.delete_failed', { error: String((err as Error)?.message ?? err) }), 'error')
    }
  }

  const items: MenuItem[] = []
  if (mine) {
    items.push({ key: 'delete', label: t('moment.detail.delete_reply'), danger: true, onSelect: () => void remove(false) })
  } else {
    if (isAdmin) {
      items.push({ key: 'remove', label: t('moment.detail.remove_reply'), danger: true, onSelect: () => void remove(true) })
    }
    items.push({ key: 'report', label: t('report.action'), onSelect: () => openReport('moment', r.id) })
  }

  return (
    <li id={`moment-reply-${r.id}`} tabIndex={-1}
      class={picked ? 'sh-moment-reply sh-moment-reply--picked' : 'sh-moment-reply'}>
      <PersonButton userId={r.author_user_id} hint={hint} class="sh-person-btn--avatar" decorative>
        <Avatar name={realName} src={picture} size={36} />
      </PersonButton>
      <div class="sh-moment-reply__body">
        <div class="sh-moment-reply__head">
          <PersonButton userId={r.author_user_id} hint={hint} class="sh-person-btn--name sh-moment-reply__namebtn">
            <bdi class="sh-moment-reply__name" dir="auto">{mine ? t('moment.card.you') : realName}</bdi>
          </PersonButton>
          <span class="sh-moment-reply__time">
            {' · '}<time dateTime={r.created_at}>{relativeTime(r.created_at)}</time>
          </span>
        </div>
        {r.content && (
          <p class="sh-moment-reply__text" dir="auto">
            {renderHashtagged(r.content, (tag, ev) => {
              ev.preventDefault()
              loc.route(`/momentum?tag=${encodeURIComponent(tag)}`)
            })}
          </p>
        )}
        {r.media_type === 'image' && r.media_url && (
          <button
            type="button"
            class="sh-moment-card__media"
            aria-label={t('moment.open_photo')}
            onClick={() => openLightbox({
              items: [{ url: r.media_url!, item_type: 'photo', caption: r.content || null }],
            })}
          >
            <img src={r.media_url} alt="" loading="lazy" />
          </button>
        )}
        {r.media_type === 'video' && r.media_url && (
          <VideoMedia src={r.media_url} poster={r.media_thumbnail_url} mediaStatus={r.media_status}
            class="sh-moment-card__video" />
        )}
        {r.reaction_count > 0 && (
          <span class="sh-moment-reply__rx">
            {t(isOne(r.reaction_count) ? 'highlight.reactions_one' : 'highlight.reactions', { n: String(r.reaction_count) })}
          </span>
        )}
      </div>
      <OverflowMenu label={t('moment.detail.reply_actions')} items={items}
        triggerClass="sh-moment-reply__more" wrapClass="sh-moment-reply__menu">
        <span aria-hidden="true">⋯</span>
      </OverflowMenu>
    </li>
  )
}
