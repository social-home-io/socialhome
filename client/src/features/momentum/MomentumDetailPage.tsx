/**
 * MomentumDetailPage — single moment + replies + reactions.
 *
 * Routed at ``/momentum/{momentId}``. Subscribes to ``moment.*`` WS
 * frames and refetches on any update affecting this moment id (new
 * reply, reaction change, deletion).
 */
import { useEffect } from 'preact/hooks'
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
import { confirmDialog } from '@/components/confirm'
import { openReport } from '@/components/ReportDialog'
import { openUserActions } from '@/components/UserActionsMenu'
import { blockedUserIds, loadBlocks } from '@/store/blocks'
import { currentUser } from '@/store/auth'
import {
  householdDisplayName,
  householdPictureUrl,
  loadHouseholdUsers,
} from '@/store/householdUsers'
import { t, isOne, formatLocale } from '@/i18n/i18n'
import { useTitle } from '@/store/pageTitle'
import { relativeChatTime } from '@/utils/relativeTime'
import { ws } from '@/ws'
import type { Moment, MomentDetail } from '@/types'
import { renderHashtagged } from './hashtags'

const QUICK_REACTIONS = ['❤️', '🔥', '😂', '😮', '😢', '👏'] as const

const detail = signal<MomentDetail | null>(null)
const loading = signal<boolean>(true)


export default function MomentumDetailPage() {
  useTitle(t('page_title.moment'))
  const { params } = useRoute()
  const loc = useLocation()
  const momentId = params.momentId
  const me = currentUser.value?.user_id

  useEffect(() => {
    loading.value = true
    detail.value = null
    void loadBlocks()  // hide replies from blocked authors
    void loadHouseholdUsers()  // resolve display names + avatars from raw user_ids
    const refetch = (initial: boolean) =>
      api.get(`/api/moments/${momentId}`)
        .then((d: MomentDetail) => {
          detail.value = d
          if (initial) loading.value = false
        })
        .catch((err: unknown) => {
          if (initial) {
            showToast(t('moment.detail.load_failed', { error: String((err as Error)?.message ?? err) }),
              'error')
            loc.route('/momentum')
          }
        })
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
  if (!detail.value) return null
  const m = detail.value.moment
  const isAuthor = m.author_user_id === me

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
          name={householdDisplayName(mm.author_user_id)}
          src={householdPictureUrl(mm.author_user_id)}
          size={32}
        />
        <div class="sh-momentum-row-body">
          <div class="sh-momentum-row-head">
            <strong class="sh-momentum-row-author">
              {replyMine ? t('highlight.inbox.you') : householdDisplayName(mm.author_user_id)}
            </strong>
            <span class="sh-muted">· {relativeChatTime(mm.created_at)}</span>
            {!replyMine && (
              <button
                type="button"
                class="sh-momentum-row-overflow"
                aria-label={t('highlight.more_actions', { name: householdDisplayName(mm.author_user_id) })}
                onClick={(ev) => {
                  ev.preventDefault()
                  ev.stopPropagation()
                  openUserActions(mm.author_user_id)
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
                loc.route(`/momentum?tab=archive&tag=${encodeURIComponent(t)}`)
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
          name={householdDisplayName(m.author_user_id)}
          src={householdPictureUrl(m.author_user_id)}
          size={48}
        />
        <div class="sh-momentum-detail-meta">
          <strong>{isAuthor ? t('highlight.inbox.you') : householdDisplayName(m.author_user_id)}</strong>
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
            aria-label={t('highlight.more_actions', { name: householdDisplayName(m.author_user_id) })}
            onClick={() => openUserActions(m.author_user_id)}
          >
            ⋯
          </button>
        )}
        <Button variant="ghost" onClick={() => loc.route('/momentum')}>
          {t('common.close')}
        </Button>
      </header>

      {m.content && (
        <p class="sh-momentum-detail-content">
          {renderHashtagged(m.content, (t, ev) => {
            ev.preventDefault()
            ev.stopPropagation()
            loc.route(`/momentum?tab=archive&tag=${encodeURIComponent(t)}`)
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
            alt={m.content ? '' : t('moment.photo_from', { name: householdDisplayName(m.author_user_id) })}
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
      </section>

      <footer class="sh-momentum-detail-actions">
        <Button onClick={() => openMomentumComposer(m.id)}>
          💬 {t('highlight.viewer.reply')}
        </Button>
        {!isAuthor && (
          <Button variant="ghost" onClick={report}>🚩 {t('report.action')}</Button>
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
            <div class="sh-momentum-replies-cta">
              <Button
                variant="secondary"
                onClick={() => openMomentumComposer(m.id)}
              >
                💬 {t('moment.detail.add_reply')}
              </Button>
            </div>
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
