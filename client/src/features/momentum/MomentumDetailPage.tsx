/**
 * MomentumDetailPage — single moment + replies + reactions.
 *
 * Routed at ``/momentum/{momentId}``. Subscribes to ``moment.*`` WS
 * frames and refetches on any update affecting this moment id (new
 * reply, reaction change, deletion).
 */
import { useEffect, useRef } from 'preact/hooks'
import { signal } from '@preact/signals'
import { useLocation, useRoute } from 'preact-iso'
import { api } from '@/api'
import { Avatar } from '@/components/Avatar'
import { Button } from '@/components/Button'
import { openLightbox } from '@/components/ImageLightbox'
import { VideoMedia } from '@/components/VideoMedia'
import {
  MomentumComposerDialog,
  openMomentumComposer,
} from '@/components/MomentumComposerDialog'
import { MomentumDetailSkeleton } from '@/components/Skeleton'
import { showToast } from '@/components/Toast'
import { LoadErrorState } from '@/components/LoadErrorState'
import { addBase } from '@/baseUrl'
import { confirmDialog } from '@/components/confirm'
import { openReport } from '@/components/ReportDialog'
import { openPerson } from '@/components/PersonSheet'
import { blockedUserIds, loadBlocks } from '@/store/blocks'
import { currentUser } from '@/store/auth'
import {
  loadHouseholdUsers,
  namedPersonLabel,
  namedPersonPicture,
} from '@/store/householdUsers'
import { t, isOne, formatLocale } from '@/i18n/i18n'
import { useTitle } from '@/store/pageTitle'
import { relativeChatTime } from '@/utils/relativeTime'
import { ws } from '@/ws'
import type { Moment, MomentDetail } from '@/types'
import { renderHashtagged } from './hashtags'
import { MomentSaveToggle } from './MomentSaveToggle'

const QUICK_REACTIONS = ['❤️', '🔥', '😂', '😮', '😢', '👏'] as const

const detail = signal<MomentDetail | null>(null)
/** Why there is no moment to show: ``gone`` (404 — deleted, expired or
 *  hidden) or ``failed`` (the read didn't come back). */
const failure = signal<'gone' | 'failed' | null>(null)
const loading = signal<boolean>(true)


export default function MomentumDetailPage() {
  useTitle(t('page_title.moment'))
  const { params } = useRoute()
  const loc = useLocation()
  const momentId = params.momentId
  const me = currentUser.value?.user_id
  const retryRef = useRef<() => void>(() => {})

  useEffect(() => {
    loading.value = true
    detail.value = null
    void loadBlocks()  // hide replies from blocked authors
    void loadHouseholdUsers()  // resolve display names + avatars from raw user_ids
    failure.value = null
    const refetch = (initial: boolean) =>
      api.get(`/api/moments/${momentId}`)
        .then((d: MomentDetail) => {
          detail.value = d
          failure.value = null
        })
        .catch((err: unknown) => {
          // 404 = deleted, expired or hidden — say so, also when it goes
          // while open. Anything else is a failed load, not "gone"; a
          // failed refresh keeps what is on screen.
          if ((err as { status?: number })?.status === 404) failure.value = 'gone'
          else if (detail.value === null) failure.value = 'failed'
        })
        .finally(() => { if (initial) loading.value = false })
    retryRef.current = () => void refetch(false)
    void refetch(true)
    const matches = (data: { moment_id?: string; parent_moment_id?: string | null }) =>
      data.moment_id === momentId || data.parent_moment_id === momentId
    const dispose = [
      ws.on('moment.created',          (e) => {
        if (matches(e.data as { parent_moment_id?: string })) void refetch(false)
      }),
      ws.on('moment.deleted',          (e) => {
        if (matches(e.data as { moment_id?: string })) void refetch(false)
      }),
      ws.on('moment.reaction_changed', (e) => {
        if (matches(e.data as { moment_id?: string })) void refetch(false)
      }),
    ]
    return () => { dispose.forEach(d => d()) }
  }, [momentId])

  if (loading.value) return <MomentumDetailSkeleton />
  if (failure.value === 'gone') {
    return (
      <div class="sh-momentum-detail">
        <div class="sh-empty-state" role="status">
          <div aria-hidden="true">⌛</div>
          <h2>{t('moment.detail.gone_title')}</h2>
          <p>{t('moment.detail.gone_body')}</p>
          <div class="sh-empty-state__cta-row">
            <a class="sh-btn sh-btn--primary" href={addBase('/momentum')}
              onClick={(ev) => { ev.preventDefault(); loc.route('/momentum') }}>
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
  if (!detail.value) return null
  const m = detail.value.moment
  const isAuthor = m.author_user_id === me
  // Past its expiry only its author (kept under Yours, decision 6a) or
  // someone who saved it (decision 6b) still sees it: replies close and
  // the share link has ended.
  const ended = Date.parse(m.expires_at) <= Date.now()

  const react = async (emoji: string) => {
    try {
      await api.put(`/api/moments/${m.id}/reaction`, { emoji })
    } catch (err: unknown) {
      showToast(t('highlight.viewer.reaction_failed', { error: String((err as Error)?.message ?? err) }), 'error')
    }
  }

  const clearReaction = async () => {
    try {
      await api.delete(`/api/moments/${m.id}/reaction`)
    } catch (err: unknown) {
      showToast(t('moment.detail.clear_failed', { error: String((err as Error)?.message ?? err) }), 'error')
    }
  }

  const remove = async () => {
    if (!await confirmDialog(t('moment.detail.delete_confirm'), { destructive: true })) return
    try {
      await api.delete(`/api/moments/${m.id}`)
      showToast(t('moment.detail.deleted'), 'info')
      loc.route('/momentum')
    } catch (err: unknown) {
      showToast(t('highlight.viewer.delete_failed', { error: String((err as Error)?.message ?? err) }), 'error')
    }
  }

  const report = () => openReport('moment', m.id)

  // a link that opens THIS moment for anyone who has it — the key is
  // in the link's fragment, the GFS only holds ciphertext.
  const copyShareLink = async () => {
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

  const myReaction = detail.value.reactions.find(
    r => r.reactor_user_id === me,
  )?.emoji
  // Roll up other reactions into a count map for the chip row.
  const counts: Record<string, number> = {}
  for (const r of detail.value.reactions) {
    counts[r.emoji] = (counts[r.emoji] || 0) + 1
  }

  const renderRow = (mm: Moment) => {
    const replyMine = mm.author_user_id === me
    return (
      <li key={mm.id} class="sh-momentum-row">
        <Avatar
          name={namedPersonLabel(mm.author_user_id, mm.author)}
          src={namedPersonPicture(mm.author_user_id, mm.author)}
          size={32}
        />
        <div class="sh-momentum-row-body">
          <div class="sh-momentum-row-head">
            <strong class="sh-momentum-row-author">
              {replyMine ? t('highlight.inbox.you') : namedPersonLabel(mm.author_user_id, mm.author)}
            </strong>
            <span class="sh-muted">· {relativeChatTime(mm.created_at)}</span>
            {!replyMine && (
              <button
                type="button"
                class="sh-momentum-row-overflow"
                aria-label={t('highlight.more_actions', { name: namedPersonLabel(mm.author_user_id, mm.author) })}
                onClick={(ev) => {
                  ev.preventDefault()
                  ev.stopPropagation()
                  openPerson(mm.author_user_id, {
                    person: {
                      display_name: namedPersonLabel(mm.author_user_id, mm.author),
                      picture_url: namedPersonPicture(mm.author_user_id, mm.author),
                    },
                  })
                }}
              >
                ⋯
              </button>
            )}
          </div>
          {mm.content && (
            <p class="sh-momentum-row-content">
              {renderHashtagged(mm.content, (t, ev) => {
                ev.preventDefault()
                ev.stopPropagation()
                loc.route(`/momentum?tag=${encodeURIComponent(t)}`)
              })}
            </p>
          )}
          {mm.media_type === 'image' && mm.media_url && (
            <button
              type="button"
              class="sh-momentum-row-media-button"
              aria-label={t('moment.open_photo')}
              onClick={(ev) => {
                ev.preventDefault()
                ev.stopPropagation()
                openLightbox({
                  items: [{
                    url:       mm.media_url!,
                    item_type: 'photo',
                    caption:   mm.content || null,
                  }],
                })
              }}
            >
              <img src={mm.media_url} alt="" loading="lazy"
                class="sh-momentum-row-media" />
            </button>
          )}
          {mm.media_type === 'video' && mm.media_url && (
            <VideoMedia src={mm.media_url} poster={mm.media_thumbnail_url} mediaStatus={mm.media_status}
              class="sh-momentum-row-media" />
          )}
          {mm.reaction_count > 0 && (
            <div class="sh-momentum-row-chips">
              <span class="sh-momentum-chip sh-momentum-chip--readonly"
                    aria-label={t(isOne(mm.reaction_count) ? 'highlight.reactions_one' : 'highlight.reactions', { n: String(mm.reaction_count) })}>
                ❤️ {mm.reaction_count}
              </span>
            </div>
          )}
        </div>
      </li>
    )
  }

  return (
    <div class="sh-momentum-detail">
      <header class="sh-momentum-detail-header">
        <Avatar
          name={namedPersonLabel(m.author_user_id, m.author)}
          src={namedPersonPicture(m.author_user_id, m.author)}
          size={48}
        />
        <div class="sh-momentum-detail-meta">
          <strong>{isAuthor ? t('highlight.inbox.you') : namedPersonLabel(m.author_user_id, m.author)}</strong>
          <span class="sh-muted">
            {new Date(m.created_at).toLocaleString(formatLocale(), {
              dateStyle: 'medium',
              timeStyle: 'short',
            })}
          </span>
        </div>
        {!isAuthor && (
          <button
            type="button"
            class="sh-momentum-row-overflow"
            aria-label={t('highlight.more_actions', { name: namedPersonLabel(m.author_user_id, m.author) })}
            onClick={() => openPerson(m.author_user_id, {
              person: {
                display_name: namedPersonLabel(m.author_user_id, m.author),
                picture_url: namedPersonPicture(m.author_user_id, m.author),
                bio: m.author?.bio ?? null,
              },
            })}
          >
            ⋯
          </button>
        )}
        <Button variant="ghost" onClick={() => loc.route('/momentum')}>
          {t('common.close')}
        </Button>
      </header>

      {/* A GFS-followed author's public bio — the only "about" line for
          someone with no household connection. Full width under the
          header so it reads on a phone too. */}
      {!isAuthor && m.author?.bio && (
        <p class="sh-momentum-detail-bio" data-testid="moment-author-bio">
          {m.author.bio}
        </p>
      )}

      {m.content && (
        <p class="sh-momentum-detail-content">
          {renderHashtagged(m.content, (t, ev) => {
            ev.preventDefault()
            ev.stopPropagation()
            loc.route(`/momentum?tag=${encodeURIComponent(t)}`)
          })}
        </p>
      )}
      {m.media_type === 'image' && m.media_url && (
        <button
          type="button"
          class="sh-momentum-detail-media-button"
          aria-label={t('moment.open_photo')}
          onClick={() => openLightbox({
            items: [{
              url:       m.media_url!,
              item_type: 'photo',
              caption:   m.content || null,
            }],
          })}
        >
          <img
            src={m.media_url}
            alt={m.content ? '' : t('moment.photo_from', { name: namedPersonLabel(m.author_user_id, m.author) })}
            class="sh-momentum-detail-media"
          />
        </button>
      )}
      {m.media_type === 'video' && m.media_url && (
        <VideoMedia src={m.media_url} poster={m.media_thumbnail_url} mediaStatus={m.media_status}
          class="sh-momentum-detail-media" />
      )}

      <section class="sh-momentum-reactions" aria-label={t('highlight.seen_by.reactions')}>
        <div class="sh-momentum-reaction-counts">
          {Object.entries(counts).map(([emoji, n]) => (
            <span key={emoji} class="sh-momentum-reaction-count">
              {emoji} {n}
            </span>
          ))}
        </div>
        <div class="sh-momentum-reaction-picker">
          {QUICK_REACTIONS.map(emoji => (
            <button
              key={emoji}
              type="button"
              class={
                myReaction === emoji
                  ? 'sh-momentum-reaction-btn sh-momentum-reaction-btn--mine'
                  : 'sh-momentum-reaction-btn'
              }
              onClick={() => void (myReaction === emoji
                ? clearReaction()
                : react(emoji))}
              aria-label={t('moment.detail.react_aria', { emoji })}
            >
              {emoji}
            </button>
          ))}
          {myReaction && (
            <Button variant="ghost" onClick={clearReaction}>
              {t('moment.detail.clear_reaction')}
            </Button>
          )}
        </div>
        {/* v_67: no route to the author's household — say where a
            reaction goes before it is made. */}
        {!isAuthor && m.reactions_reach === 'household' && (
          <p class="sh-muted sh-momentum-reactions-note">
            {t('moment.detail.reactions_household_only')}
          </p>
        )}
      </section>

      {ended && (
        <p class="sh-muted sh-momentum-detail-ended">
          {t(isAuthor ? 'moment.detail.archived' : 'moment.detail.saved_ended')}
        </p>
      )}
      <footer class="sh-momentum-detail-actions">
        {!ended && (
          <Button onClick={() => openMomentumComposer(m.id, { parentIsPublic: m.is_public === true })}>
            💬 {t('highlight.viewer.reply')}
          </Button>
        )}
        {!isAuthor && <MomentSaveToggle key={m.id} moment={m} />}
        {!isAuthor && (
          <Button variant="ghost" onClick={report}>🚩 {t('report.action')}</Button>
        )}
        {isAuthor && m.is_public && !ended && (
          <Button
            variant="secondary"
            onClick={() => void copyShareLink()}
            title={t('moment.share.title')}
          >
            🔗 {t('moment.share.copy')}
          </Button>
        )}
        {isAuthor && (
          // Ghost (not filled-danger) so the destructive action doesn't
          // compete with the primary Reply — the confirm dialog carries
          // the "are you sure" weight. Matches the ghost Report above.
          <Button variant="ghost" onClick={remove}>{t('common.delete')}</Button>
        )}
      </footer>

      {(() => {
        const blocked = blockedUserIds.value
        const visibleReplies = detail.value!.replies.filter(
          r => !blocked.has(r.author_user_id),
        )
        return visibleReplies.length > 0 && (
          <section class="sh-momentum-replies" aria-label={t('moment.detail.replies_aria')}>
            <h3>
              {t(isOne(visibleReplies.length) ? 'moment.detail.replies_one' : 'moment.detail.replies', { n: String(visibleReplies.length) })}
            </h3>
            <ul class="sh-momentum-list">
              {visibleReplies.map(renderRow)}
            </ul>
            {!ended && (
              <div class="sh-momentum-replies-cta">
                <Button
                  variant="secondary"
                  onClick={() => openMomentumComposer(m.id, { parentIsPublic: m.is_public === true })}
                >
                  💬 {t('moment.detail.add_reply')}
                </Button>
              </div>
            )}
          </section>
        )
      })()}
      {/* Mounted at the page tail so the Reply button can open the
       *  shared dialog. The WS ``moment.created`` listener already
       *  refetches the thread when the new reply lands, so no
       *  ``onPosted`` callback is needed here. */}
      <MomentumComposerDialog />
    </div>
  )
}
