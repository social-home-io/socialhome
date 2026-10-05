/**
 * HighlightViewerPage — full-screen tap-through viewer for one highlight (§Highlights).
 *
 * Renders frames sequentially with a top progress bar (one segment per
 * frame). Tap left/right to navigate, hold to pause, swipe up (or click
 * the reactions chip) to drop a quick reaction. When viewing someone
 * else's frame a "Reply" chip surfaces below the frame; tapping it
 * routes to the DM thread with the frame's snapshot pre-loaded.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { signal } from '@preact/signals'
import { useRoute, useLocation } from 'preact-iso'
import { api } from '@/api'
import { Spinner } from '@/components/Spinner'
import { Button } from '@/components/Button'
import { isRestricted } from '@/components/ProtectedNotice'
import { Avatar } from '@/components/Avatar'
import { Modal } from '@/components/Modal'
import { showToast } from '@/components/Toast'
import { currentUser } from '@/store/auth'
import {
  householdDisplayName,
  householdPictureUrl,
  loadHouseholdUsers,
} from '@/store/householdUsers'
import { relativeDocsTime } from '@/utils/relativeTime'
import type { Highlight, HighlightFrame } from '@/types'
import { confirmDialog } from '@/components/confirm'
import { openReport } from '@/components/ReportDialog'
import { openUserActions } from '@/components/UserActionsMenu'
import { ws } from '@/ws'
import { openPublishMenu } from './HighlightPublishMenu'
import { t, isOne } from '@/i18n/i18n'

interface HighlightDetail {
  highlight: Highlight
  frames: HighlightFrame[]
  views?: Record<string, { viewer_user_id: string; viewed_at: string }[]>
  reactions?: Record<string, { reactor_user_id: string; emoji: string }[]>
}

const QUICK_REACTIONS = ['❤️', '🔥', '😂', '😮', '😢', '👏'] as const

const FRAME_DURATION_MS = 6000  // image default; video uses its own length

const detail = signal<HighlightDetail | null>(null)
const loading = signal<boolean>(true)
const currentIndex = signal<number>(0)
const paused = signal<boolean>(false)
const reactionsOpen = signal<boolean>(false)
/** Author-only "Seen by" sheet — when null, sheet is closed. Holds the
 *  frame_id so a later frame change reopens onto the right list. */
const seenBySheetFrameId = signal<string | null>(null)


export default function HighlightViewerPage() {
  const { params } = useRoute()
  const loc = useLocation()
  const highlightId = params.highlightId
  const myId = currentUser.value?.user_id
  // Video frames default muted so a tap into the inbox doesn't blast
  // sound through a quiet room. The user can flip the toggle per-
  // viewer (signal lives outside the component so the choice persists
  // across frame transitions in the same session).
  const [muted, setMuted] = useState(true)

  // ── Load the highlight detail on mount / id change ─────────────────────
  useEffect(() => {
    loading.value = true
    detail.value = null
    currentIndex.value = 0
    paused.value = false
    reactionsOpen.value = false
    seenBySheetFrameId.value = null
    // Resolve raw user_ids (author + viewer + reactor) to display names.
    void loadHouseholdUsers()
    const refetch = (initial: boolean) =>
      api.get(`/api/highlights/${highlightId}`)
        .then((d: HighlightDetail) => {
          detail.value = d
          if (initial) loading.value = false
        })
        .catch((err: unknown) => {
          if (initial) {
            showToast(
              t('highlight.viewer.load_failed', { error: String((err as Error)?.message ?? err) }),
              'error',
            )
            loc.route('/highlights')
          }
        })
    void refetch(true)
    // Live counters for authors viewing their own highlight page —
    // ``highlight.frame_viewed`` / ``highlight.frame_reaction_changed`` arrive
    // when a peer's viewer marks a frame seen or reacts. We mutate the
    // local ``detail`` signal in place (cheap O(1) updates per event)
    // instead of refetching the whole highlight — both events carry every
    // field the UI needs.
    const matches = (data: { highlight_id?: string }) => data.highlight_id === highlightId
    const dispose = [
      ws.on('highlight.frame_viewed', (e) => {
        const data = e.data as {
          highlight_id?: string; frame_id?: string; viewer_user_id?: string
        }
        if (!matches(data) || !detail.value || !data.frame_id) return
        const frameId = data.frame_id
        const viewer = data.viewer_user_id ?? ''
        const views = { ...(detail.value.views ?? {}) }
        const list = views[frameId] ?? []
        // Idempotent — a viewer marking the same frame twice is a no-op.
        if (list.some(v => v.viewer_user_id === viewer)) return
        views[frameId] = [
          ...list,
          { viewer_user_id: viewer, viewed_at: new Date().toISOString() },
        ]
        detail.value = { ...detail.value, views }
      }),
      ws.on('highlight.frame_reaction_changed', (e) => {
        const data = e.data as {
          highlight_id?: string; frame_id?: string;
          reactor_user_id?: string; emoji?: string | null
        }
        if (!matches(data) || !detail.value || !data.frame_id) return
        const frameId = data.frame_id
        const reactor = data.reactor_user_id ?? ''
        const reactions = { ...(detail.value.reactions ?? {}) }
        const list = (reactions[frameId] ?? []).filter(
          r => r.reactor_user_id !== reactor,
        )
        if (data.emoji) {
          list.push({ reactor_user_id: reactor, emoji: data.emoji })
        }
        reactions[frameId] = list
        detail.value = { ...detail.value, reactions }
      }),
    ]
    return () => { dispose.forEach(d => d()) }
  }, [highlightId])

  // Auto-progress: stamp a per-frame timer that ticks the index.
  // Stored as a ref so pause/resume can clear/recreate without losing
  // the "remaining" budget. This is intentionally simple — perfect
  // pause-resume to the millisecond is overkill for v1.
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const frameStartRef = useRef<number>(Date.now())

  useEffect(() => {
    if (!detail.value || loading.value) return
    const frame = detail.value.frames[currentIndex.value]
    if (!frame) return
    frameStartRef.current = Date.now()
    if (timerRef.current) clearTimeout(timerRef.current)
    if (paused.value) return
    const dur = frame.duration_ms && frame.duration_ms > 0
      ? frame.duration_ms
      : FRAME_DURATION_MS
    timerRef.current = setTimeout(() => {
      advance()
    }, dur)
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current)
    }
  }, [currentIndex.value, detail.value?.highlight.id, paused.value, loading.value])

  // Mark each frame viewed exactly once on entry.
  useEffect(() => {
    if (!detail.value) return
    const frame = detail.value.frames[currentIndex.value]
    if (!frame) return
    if (detail.value.highlight.author_user_id === myId) return  // authors don't mark
    api.post(`/api/highlights/frames/${frame.id}/view`, {}).catch(() => {})
  }, [currentIndex.value, detail.value?.highlight.id])

  const advance = () => {
    if (!detail.value) return
    if (currentIndex.value < detail.value.frames.length - 1) {
      currentIndex.value += 1
    } else {
      loc.route('/highlights')  // end of highlight → back to inbox
    }
  }
  const goBack = () => {
    if (currentIndex.value > 0) currentIndex.value -= 1
  }

  // Keyboard navigation — left/right arrows step through frames, Esc
  // closes the viewer. Bound on `window` so users don't have to focus
  // the tap zones first.
  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key === 'ArrowRight') { ev.preventDefault(); advance() }
      else if (ev.key === 'ArrowLeft') { ev.preventDefault(); goBack() }
      else if (ev.key === 'Escape') { ev.preventDefault(); loc.route('/highlights') }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [detail.value?.highlight.id])

  if (loading.value) return <Spinner />
  if (!detail.value) return null

  const highlight = detail.value.highlight
  const frames = detail.value.frames
  const frame = frames[currentIndex.value]
  if (!frame) {
    return (
      <div class="sh-highlight-viewer">
        <p class="sh-muted">{t('highlight.viewer.no_frames')}</p>
        <Button onClick={() => loc.route('/highlights')}>{t('common.back')}</Button>
      </div>
    )
  }
  const isAuthor = highlight.author_user_id === myId

  const onTapLeft = (e: Event) => { e.preventDefault(); goBack() }
  const onTapRight = (e: Event) => { e.preventDefault(); advance() }

  const react = async (emoji: string) => {
    try {
      await api.put(`/api/highlights/frames/${frame.id}/reaction`, { emoji })
      reactionsOpen.value = false
      showToast(t('highlight.viewer.reaction_sent'), 'success')
    } catch (err: unknown) {
      showToast(t('highlight.viewer.reaction_failed', { error: String((err as Error)?.message ?? err) }), 'error')
    }
  }

  const dmReply = () => {
    // The snapshot is built server-side; the SPA jumps to the DM list
    // pre-filled with the author so the user can pick / start a thread.
    loc.route(`/dms?highlight_frame_id=${encodeURIComponent(frame.id)}`)
  }

  const deleteFrame = async () => {
    if (!await confirmDialog(t('highlight.viewer.delete_confirm'), { destructive: true })) return
    try {
      await api.delete(`/api/highlights/frames/${frame.id}`)
      showToast(t('highlight.viewer.frame_removed'), 'info')
      // Drop the frame from local state and re-index.
      const next = frames.filter(f => f.id !== frame.id)
      detail.value = { ...detail.value!, frames: next }
      if (currentIndex.value >= next.length) {
        currentIndex.value = Math.max(0, next.length - 1)
      }
      if (next.length === 0) loc.route('/highlights')
    } catch (err: unknown) {
      showToast(t('highlight.viewer.delete_failed', { error: String((err as Error)?.message ?? err) }), 'error')
    }
  }

  // Build a per-frame views/reactions footer the author sees.
  const myViews = detail.value.views?.[frame.id] ?? []
  const myReactions = detail.value.reactions?.[frame.id] ?? []

  // Active-segment fill animation duration matches the frame's own
  // duration so the progress bar tracks the timer one-to-one. Videos
  // use their explicit duration_ms; images fall back to the default.
  const activeFillDuration = frame.duration_ms && frame.duration_ms > 0
    ? frame.duration_ms
    : FRAME_DURATION_MS

  return (
    <div
      class={
        paused.value
          ? 'sh-highlight-viewer sh-highlight-viewer--paused'
          : 'sh-highlight-viewer'
      }
    >
      {/* Progress bar — one segment per frame. The active segment's
       *  inner fill animates from 0 → 100% over the frame's duration,
       *  giving the same "story" pacing cue Instagram / Snapchat use.
       *  Pause freezes the animation via animation-play-state. */}
      <div class="sh-highlight-progress" aria-hidden="true">
        {frames.map((_, i) => (
          <span
            key={i}
            class={
              i < currentIndex.value
                ? 'sh-highlight-progress-seg sh-highlight-progress-seg--done'
                : i === currentIndex.value
                  ? 'sh-highlight-progress-seg sh-highlight-progress-seg--active'
                  : 'sh-highlight-progress-seg'
            }
          >
            <span
              class="sh-highlight-progress-fill"
              // Setting the duration only on the active fill keeps the
              // CSS lean — done/queued segments don't need it.
              style={
                i === currentIndex.value
                  ? `--seg-dur: ${activeFillDuration}ms`
                  : undefined
              }
            />
          </span>
        ))}
      </div>
      <span class="sr-only" aria-live="polite">
        {t('highlight.composer.frame_of', { n: String(currentIndex.value + 1), total: String(frames.length) })}
      </span>

      <header class="sh-highlight-viewer-header">
        <Avatar
          name={householdDisplayName(highlight.author_user_id)}
          src={householdPictureUrl(highlight.author_user_id)}
          size={32}
        />
        <strong>{householdDisplayName(highlight.author_user_id)}</strong>
        <span class="sh-muted">{highlight.highlight_date}</span>
        {!isAuthor && (
          <button
            type="button"
            class="sh-highlight-viewer-overflow"
            aria-label={t('highlight.more_actions', { name: householdDisplayName(highlight.author_user_id) })}
            onClick={() => openUserActions(highlight.author_user_id)}
          >
            ⋯
          </button>
        )}
        <Button variant="ghost" onClick={() => loc.route('/highlights')}>{t('common.close')}</Button>
      </header>

      <div
        class="sh-highlight-frame"
        // Press-and-hold on the frame stage pauses the timer — Instagram
        // / Snapchat behaviour. Bound here (not on the whole viewer) so
        // tapping the React/Reply/Report footer does not get re-routed
        // through pause-resume on every click.
        onPointerDown={() => { paused.value = true }}
        onPointerUp={() => { paused.value = false }}
        onPointerLeave={() => { paused.value = false }}
        onPointerCancel={() => { paused.value = false }}
      >
        <button class="sh-highlight-tap-left"  onClick={onTapLeft}  aria-label={t('highlight.viewer.prev_frame')} />
        <button class="sh-highlight-tap-right" onClick={onTapRight} aria-label={t('highlight.viewer.next_frame')} />
        {frame.frame_type === 'image' ? (
          <img src={frame.media_url} alt={frame.caption_text ?? ''} class="sh-highlight-frame-media" />
        ) : (
          // Story UX (autoplay-muted, controls=false, tap-to-advance) so this
          // stays a raw <video> rather than VideoMedia. Trade-off: a frame
          // still mid-transcode (frame.media_status === 'processing') shows a
          // black frame here — the status isn't surfaced in the story viewer
          // yet (documented follow-up).
          <video
            // ``key`` forces a fresh element when the frame id changes
            // — without it, switching from one video to another keeps
            // the previous element + its currentTime, which made the
            // new frame stutter on entry.
            key={frame.id}
            src={frame.media_url}
            class="sh-highlight-frame-media"
            autoPlay
            playsInline
            muted={muted}
            controls={false}
            onEnded={advance}
          />
        )}
        {/* Per-frame mute toggle — only meaningful on video frames, but
         *  rendering it consistently keeps the chrome stable when the
         *  user steps between image and video frames. */}
        {frame.frame_type === 'video' && (
          <button
            type="button"
            class="sh-highlight-mute-btn"
            onClick={(ev) => { ev.stopPropagation(); setMuted(m => !m) }}
            aria-pressed={muted}
            aria-label={muted ? t('highlight.viewer.unmute_aria') : t('highlight.viewer.mute_aria')}
            title={muted ? t('highlight.viewer.unmute') : t('highlight.viewer.mute')}
          >
            {muted ? '🔇' : '🔊'}
          </button>
        )}
        {(frame.caption_text || frame.caption_emoji) && (
          <p class="sh-highlight-frame-caption">
            {frame.caption_emoji && (
              <span class="sh-highlight-frame-caption-emoji" aria-hidden="true">
                {frame.caption_emoji}
              </span>
            )}
            {frame.caption_text}
          </p>
        )}
        {/* "Paused" hint appears while the user is holding to pause —
         *  reassures them the timer has actually stopped, and tells
         *  first-time users that hold-to-pause is a thing. */}
        {paused.value && (
          <span class="sh-highlight-paused-hint" aria-hidden="true">
            ⏸ {t('highlight.viewer.paused')}
          </span>
        )}
      </div>

      <footer class="sh-highlight-viewer-footer">
        {!isAuthor && (
          <>
            <button
              type="button"
              class="sh-highlight-react-btn"
              onClick={() => { reactionsOpen.value = !reactionsOpen.value }}
              aria-label={t('highlight.viewer.react')}
            >
              😊 {t('highlight.viewer.react')}
            </button>
            <button
              type="button"
              class="sh-highlight-reply-btn"
              onClick={dmReply}
              aria-label={t('highlight.viewer.reply_aria')}
            >
              💬 {t('highlight.viewer.reply')}
            </button>
            <button
              type="button"
              class="sh-highlight-report-btn"
              onClick={() => openReport('highlight', highlight.id)}
              aria-label={t('highlight.viewer.report_aria')}
            >
              🚩 {t('report.action')}
            </button>
          </>
        )}
        {isAuthor && (
          <>
            <button
              type="button"
              class="sh-highlight-author-meta"
              onClick={() => { seenBySheetFrameId.value = frame.id }}
              aria-label={
                myViews.length === 0 && myReactions.length === 0
                  ? t('highlight.viewer.no_views')
                  : t('highlight.viewer.seen_by_aria', { views: String(myViews.length), reactions: reactionsLabel(myReactions.length) })
              }
            >
              👁 {myViews.length} · {reactionsLabel(myReactions.length)}
            </button>
            {/* §CP.R: a protected account can't mint a public link; it
             *  keeps the button only to unpublish one made before. */}
            {(!isRestricted('public_links') || !!highlight.public_gfs_id) && (
              <Button
                variant="ghost"
                onClick={() => openPublishMenu(highlight.id, !!highlight.public_gfs_id)}
              >
                🔗 {t('highlight.viewer.publish')}
              </Button>
            )}
            <Button variant="danger" onClick={deleteFrame}>{t('highlight.viewer.delete_frame')}</Button>
          </>
        )}
      </footer>

      {isAuthor && seenBySheetFrameId.value === frame.id && (
        <HighlightSeenBySheet
          views={myViews}
          reactions={myReactions}
          onClose={() => { seenBySheetFrameId.value = null }}
        />
      )}

      {reactionsOpen.value && (
        <div class="sh-highlight-react-tray" role="group" aria-label={t('highlight.viewer.quick_reactions')}>
          {QUICK_REACTIONS.map(emoji => (
            <button
              key={emoji}
              type="button"
              class="sh-highlight-react-tray-btn"
              onClick={() => void react(emoji)}
            >
              {emoji}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}


/** "3 reactions" / "1 reaction" in the UI language. */
function reactionsLabel(n: number): string {
  return t(isOne(n) ? 'highlight.reactions_one' : 'highlight.reactions', { n: String(n) })
}

/**
 * HighlightSeenBySheet — modal-backed list of who has viewed this frame
 * and who has dropped a reaction. Author-only; opens from the viewer
 * footer's eye chip. Empty arrays show their own gentle empty state so
 * a brand-new highlight doesn't look broken.
 */
function HighlightSeenBySheet({
  views, reactions, onClose,
}: {
  views: { viewer_user_id: string; viewed_at: string }[]
  reactions: { reactor_user_id: string; emoji: string }[]
  onClose: () => void
}) {
  const totalActivity = views.length + reactions.length
  return (
    <Modal open={true} onClose={onClose} title={t('highlight.seen_by.title')}>
      {totalActivity === 0 && (
        <p class="sh-muted" style={{ marginTop: 0 }}>
          {t('highlight.seen_by.empty')}
        </p>
      )}

      {views.length > 0 && (
        <section class="sh-seenby-section" aria-label={t('highlight.seen_by.viewers')}>
          <h3 class="sh-seenby-heading">
            {t('highlight.seen_by.viewers')} <span class="sh-muted">({views.length})</span>
          </h3>
          <ul class="sh-seenby-list">
            {views.map(v => (
              <li key={v.viewer_user_id} class="sh-seenby-row">
                <Avatar
                  name={householdDisplayName(v.viewer_user_id)}
                  src={householdPictureUrl(v.viewer_user_id)}
                  size={32}
                />
                <span class="sh-seenby-name">
                  {householdDisplayName(v.viewer_user_id)}
                </span>
                <time class="sh-muted">{relativeDocsTime(v.viewed_at)}</time>
              </li>
            ))}
          </ul>
        </section>
      )}

      {reactions.length > 0 && (
        <section class="sh-seenby-section" aria-label={t('highlight.seen_by.reactions')}>
          <h3 class="sh-seenby-heading">
            {t('highlight.seen_by.reactions')} <span class="sh-muted">({reactions.length})</span>
          </h3>
          <ul class="sh-seenby-list">
            {reactions.map(r => (
              <li key={r.reactor_user_id} class="sh-seenby-row">
                <Avatar
                  name={householdDisplayName(r.reactor_user_id)}
                  src={householdPictureUrl(r.reactor_user_id)}
                  size={32}
                />
                <span class="sh-seenby-name">
                  {householdDisplayName(r.reactor_user_id)}
                </span>
                <span class="sh-seenby-emoji" aria-hidden="true">{r.emoji}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </Modal>
  )
}
