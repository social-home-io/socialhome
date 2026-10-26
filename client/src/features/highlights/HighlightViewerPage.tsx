/**
 * HighlightViewerPage — watch one person's highlight (§Highlights, r3
 * design "Watching a highlight: pause, react, reply").
 *
 * - **Chrome** is dark in both themes, with 44 px Pause / Play, More (⋯)
 *   and Close. Space or K pauses (the video with the timer); ← / → step
 *   frames (mirrored right-to-left); Esc closes. The progress bar steps
 *   instead of filling under ``prefers-reduced-motion``.
 * - **The frame** keeps its caption inside, on a gradient that never
 *   clips it (H6). Tap the start third for back, the rest for next
 *   (logical sides, so RTL mirrors — C17); press and hold to pause.
 * - **Watching someone else's:** the emoji tray sits under the frame and
 *   shows *your* reaction (``my_reaction``); tap it again to remove, tap
 *   another to change. "Reply to <name> privately…" sends the reply into
 *   your 1:1 chat with them, quoting the frame (owner decision 11).
 * - **Your own:** Seen by N (each viewer with their reaction), the
 *   reactions, Share; ⋯ also publishes a link and deletes a frame or the
 *   whole day.
 * - **Every sheet** pauses the timer and has a heading and a ✕.
 * - **Between people** (C8): desktop has ‹ › for frames and the people
 *   before / up next; after the last frame the viewer moves on to the
 *   next person with unseen frames (Today order), else closes back to
 *   where it was opened from (a share card, Home, Highlights).
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { signal } from '@preact/signals'
import { useRoute, useLocation } from 'preact-iso'
import { api } from '@/api'
import { Spinner } from '@/components/Spinner'
import { Button } from '@/components/Button'
import { isRestricted } from '@/components/ProtectedNotice'
import { Avatar } from '@/components/Avatar'
import { openDialogCount } from '@/components/Modal'
import { showToast } from '@/components/Toast'
import { viaPostQuery } from '@/components/HighlightShareCard'
import { confirmDialog } from '@/components/confirm'
import { openReport } from '@/components/ReportDialog'
import { openPerson } from '@/components/PersonSheet'
import { currentUser } from '@/store/auth'
import { loadHouseholdUsers, namedPersonLabel, namedPersonPicture } from '@/store/householdUsers'
import { localSinceTime } from '@/utils/relativeTime'
import type { Highlight, HighlightFrame, HighlightInboxItem } from '@/types'
import { ws } from '@/ws'
import { t, isOne } from '@/i18n/i18n'
import { openPublishMenu } from './HighlightPublishMenu'
import { audienceChip, type TodayPerson } from './today'
import { CHIP_ICON, VIEWER_ICON } from './icons'
import { neighbours } from './viewerQueue'
import { viewerOpenedFrom } from './viewerReturn'
import { replyPrivately } from './privateReply'
import {
  SeenBySheet, SheetRow, ShareSheet, ViewerSheet, type SeenReaction, type SeenView,
} from './viewerSheets'

interface HighlightDetail {
  highlight: Highlight
  frames: HighlightFrame[]
  views?: Record<string, SeenView[]>
  reactions?: Record<string, SeenReaction[]>
}

const QUICK_REACTIONS = ['❤️', '😂', '😮', '😢', '🔥', '👏'] as const

const FRAME_DURATION_MS = 6000  // image default; video uses its own length

type Sheet = 'more' | 'seen' | 'share' | null

const detail = signal<HighlightDetail | null>(null)
const loading = signal<boolean>(true)
const currentIndex = signal<number>(0)
/** Press-and-hold on the frame. */
const held = signal<boolean>(false)
/** Pause / Play, Space or K. */
const userPaused = signal<boolean>(false)
/** The reply field has focus: the frame waits while they type. */
const typing = signal<boolean>(false)
const sheet = signal<Sheet>(null)
/** Today, for who comes before / up next. Empty until loaded. */
const today = signal<HighlightInboxItem[]>([])
/** People watched in this session: their views may not be on the server
 *  yet, and they are never "up next" again. */
const watched = new Set<string>()

/** A key aimed at a text field: its arrows move the caret, never a frame. */
function isEditable(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false
  return target.isContentEditable || !!target.closest('input, textarea, select, [contenteditable="true"]')
}

function isRtl(): boolean {
  return document.documentElement.dir === 'rtl'
}

/** "3 reactions" / "1 reaction" in the UI language. */
function reactionsLabel(n: number): string {
  return t(isOne(n) ? 'highlight.reactions_one' : 'highlight.reactions', { n: String(n) })
}

function todayKey(): string {
  return new Date().toISOString().slice(0, 10)
}

function personFirstName(p: TodayPerson): string {
  const name = namedPersonLabel(p.authorId, p.highlight.author)
  return name.split(/\s+/)[0] || name
}

export default function HighlightViewerPage() {
  const { params, query } = useRoute()
  const loc = useLocation()
  const highlightId = params.highlightId
  // Opened from a space / household share card: the share post is what
  // lets a reader outside the audience see it — every call carries it.
  const via = viaPostQuery(query?.via_post)
  const myId = currentUser.value?.user_id
  // Video frames start muted so opening one doesn't blast sound through a
  // quiet room; the choice holds across frames in this visit.
  const [muted, setMuted] = useState(true)
  const [reply, setReply] = useState('')
  const [sending, setSending] = useState(false)

  // ── Load the highlight detail (and Today, for the people around it) ──
  useEffect(() => {
    loading.value = true
    detail.value = null
    currentIndex.value = 0
    held.value = false
    userPaused.value = false
    typing.value = false
    sheet.value = null
    setReply('')
    // Resolve raw user_ids (author + viewer + reactor) to display names.
    void loadHouseholdUsers()
    const refetch = (initial: boolean) =>
      api.get(`/api/highlights/${highlightId}${via}`)
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
            loc.route('/highlights', true)
          }
        })
    void refetch(true)
    // Up next / before: a glance, so a failed read just leaves them out.
    api.get('/api/highlights')
      .then((rows: HighlightInboxItem[]) => { today.value = rows ?? [] })
      .catch(() => { today.value = [] })
    // Live counters for authors viewing their own highlight —
    // ``highlight.frame_viewed`` / ``highlight.frame_reaction_changed``
    // carry only the person's id, and a friend on another household is not
    // in the local cache: refetch so "Seen by" comes back named.
    const matches = (data: { highlight_id?: string }) => data.highlight_id === highlightId
    const onActivity = (e: { data: unknown }) => {
      if (matches(e.data as { highlight_id?: string }) && detail.value) void refetch(false)
    }
    const dispose = [
      ws.on('highlight.frame_viewed', onActivity),
      ws.on('highlight.frame_reaction_changed', onActivity),
    ]
    return () => { dispose.forEach(d => d()) }
  }, [highlightId, via])

  // The frame stands still while held, paused, while they type a reply
  // and while any dialog or sheet is open — nothing moves under them.
  const stopped = held.value || userPaused.value || typing.value || openDialogCount.value > 0
  const curFrameId = detail.value?.frames[currentIndex.value]?.id
  const authorId = detail.value?.highlight.author_user_id ?? null
  const around = authorId ? neighbours(today.value, myId, authorId, watched) : null

  /** Back to where the viewer was opened from (C8); a deep link closes
   *  to Highlights. Moving between people replaced the history entry,
   *  so the opener is one step back. */
  const close = () => {
    if (viewerOpenedFrom() !== null) window.history.back()
    else loc.route('/highlights', true)
  }

  const goToPerson = (p: TodayPerson) => {
    if (authorId) watched.add(authorId)
    loc.route(`/highlights/${p.openId}`, true)
  }

  const advance = () => {
    if (!detail.value) return
    if (currentIndex.value < detail.value.frames.length - 1) {
      currentIndex.value += 1
      return
    }
    // The end of this person: the next one with unseen frames, else out.
    if (around?.upNext) goToPerson(around.upNext)
    else {
      if (authorId) watched.add(authorId)
      close()
    }
  }
  const goBack = () => {
    if (currentIndex.value > 0) currentIndex.value -= 1
  }

  const togglePause = () => {
    if (stopped) {
      userPaused.value = false
      held.value = false
      if (typing.value && document.activeElement instanceof HTMLElement) document.activeElement.blur()
      typing.value = false
    } else {
      userPaused.value = true
    }
  }

  // Auto-progress: a per-frame timer. A pause keeps what is left of the
  // frame (``remainingRef``), so it resumes where the bar stands.
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const remainingRef = useRef<{ frameId: string, ms: number } | null>(null)
  useEffect(() => {
    if (!detail.value || loading.value) return
    const frame = detail.value.frames[currentIndex.value]
    if (!frame) return
    if (remainingRef.current?.frameId !== frame.id) {
      remainingRef.current = {
        frameId: frame.id,
        ms: frame.duration_ms && frame.duration_ms > 0 ? frame.duration_ms : FRAME_DURATION_MS,
      }
    }
    if (timerRef.current) clearTimeout(timerRef.current)
    if (stopped) return
    const left = remainingRef.current
    const startedAt = Date.now()
    timerRef.current = setTimeout(() => { advance() }, left.ms)
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current)
      left.ms = Math.max(0, left.ms - (Date.now() - startedAt))
    }
  }, [currentIndex.value, detail.value?.highlight.id, stopped, loading.value, around?.upNext?.openId])

  // A video frame stops with the timer — else it plays on under a sheet
  // and its ``ended`` advances the viewer anyway.
  const videoRef = useRef<HTMLVideoElement | null>(null)
  useEffect(() => {
    const v = videoRef.current
    if (!v) return
    if (stopped) v.pause()
    else void v.play()?.catch(() => {})
  }, [stopped, curFrameId])

  // Mark each frame viewed exactly once on entry.
  useEffect(() => {
    if (!detail.value) return
    const frame = detail.value.frames[currentIndex.value]
    if (!frame) return
    if (detail.value.highlight.author_user_id === myId) return  // authors don't mark
    api.post(`/api/highlights/frames/${frame.id}/view${via}`, {}).catch(() => {})
  }, [currentIndex.value, detail.value?.highlight.id, via])

  // Keys on ``window`` so nothing needs focus first. Never while a dialog
  // is open (it owns its keys) or for a key typed in a field.
  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => {
      if (openDialogCount.value > 0 || isEditable(ev.target)) return
      if (ev.altKey || ev.ctrlKey || ev.metaKey) return
      const forward = isRtl() ? 'ArrowLeft' : 'ArrowRight'
      const backward = isRtl() ? 'ArrowRight' : 'ArrowLeft'
      if (ev.key === forward) { ev.preventDefault(); advance() }
      else if (ev.key === backward) { ev.preventDefault(); goBack() }
      else if (ev.key === ' ' || ev.key === 'k' || ev.key === 'K') {
        // A focused button's Space is its click — that click is the toggle.
        if (ev.key === ' ' && ev.target instanceof HTMLButtonElement) return
        ev.preventDefault()
        togglePause()
      } else if (ev.key === 'Escape') {
        ev.preventDefault()
        close()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  if (loading.value) return <Spinner />
  if (!detail.value) return null

  const highlight = detail.value.highlight
  const frames = detail.value.frames
  const frame = frames[currentIndex.value]
  if (!frame) {
    return (
      <div class="sh-hlv">
        <div class="sh-hlv__col">
          <p>{t('highlight.viewer.no_frames')}</p>
          <Button onClick={close}>{t('common.back')}</Button>
        </div>
      </div>
    )
  }
  const isAuthor = highlight.author_user_id === myId
  const authorName = namedPersonLabel(highlight.author_user_id, highlight.author)
  const authorPicture = namedPersonPicture(highlight.author_user_id, highlight.author)
  const authorFirst = authorName.split(/\s+/)[0] || authorName
  const chip = audienceChip(highlight, isAuthor)
  const frameOf = t('highlight.composer.frame_of', { n: String(currentIndex.value + 1), total: String(frames.length) })
  const views = detail.value.views?.[frame.id] ?? []
  const reactions = detail.value.reactions?.[frame.id] ?? []
  const mine = frame.my_reaction ?? null

  const setMine = (frameId: string, emoji: string | null) => {
    const d = detail.value
    if (!d) return
    detail.value = { ...d, frames: d.frames.map(f => (f.id === frameId ? { ...f, my_reaction: emoji } : f)) }
  }

  const react = async (emoji: string) => {
    // Always the frame the tap landed on, never one the timer moved to.
    const target = frame
    const before = target.my_reaction ?? null
    const next = before === emoji ? null : emoji
    setMine(target.id, next)
    try {
      const url = `/api/highlights/frames/${target.id}/reaction${via}`
      if (next === null) await api.delete(url)
      else await api.put(url, { emoji })
    } catch (err: unknown) {
      setMine(target.id, before)
      showToast(t('highlight.viewer.reaction_failed', { error: String((err as Error)?.message ?? err) }), 'error')
    }
  }

  const sendReply = async (ev: Event) => {
    ev.preventDefault()
    const text = reply.trim()
    if (!text || sending) return
    setSending(true)
    try {
      const conversationId = await replyPrivately({
        frameId: frame.id, authorId: highlight.author_user_id, content: text, via,
      })
      setReply('')
      showToast(t('highlight.viewer.reply_sent', { name: authorFirst }), 'success', {
        action: { label: t('highlight.viewer.reply_open'), onClick: () => loc.route(`/dms/${conversationId}`) },
      })
    } catch (err: unknown) {
      showToast(t('highlight.viewer.reply_failed', { error: String((err as Error)?.message ?? err) }), 'error')
    } finally {
      setSending(false)
    }
  }

  const deleteFrame = async () => {
    if (!await confirmDialog(t('highlight.viewer.delete_confirm'), { destructive: true })) return
    try {
      await api.delete(`/api/highlights/frames/${frame.id}`)
      sheet.value = null
      showToast(t('highlight.viewer.frame_removed'), 'info')
      const next = frames.filter(f => f.id !== frame.id)
      if (next.length === 0) { close(); return }
      detail.value = { ...detail.value!, frames: next }
      if (currentIndex.value >= next.length) currentIndex.value = next.length - 1
    } catch (err: unknown) {
      showToast(t('highlight.viewer.delete_failed', { error: String((err as Error)?.message ?? err) }), 'error')
    }
  }

  const deleteDay = async () => {
    const n = frames.length
    const ok = await confirmDialog(
      t(isOne(n) ? 'highlight.viewer.delete_day_confirm_one' : 'highlight.viewer.delete_day_confirm', { n: String(n) }),
      { destructive: true, confirmLabel: t('highlight.viewer.delete_day_confirm_button') },
    )
    if (!ok) return
    try {
      await api.delete(`/api/highlights/${highlight.id}`)
      sheet.value = null
      showToast(t('highlight.viewer.highlight_removed'), 'info')
      close()
    } catch (err: unknown) {
      showToast(t('highlight.viewer.delete_failed', { error: String((err as Error)?.message ?? err) }), 'error')
    }
  }

  const others = frames.length - 1
  const isToday = highlight.highlight_date === todayKey()
  const canPublish = !isRestricted('public_links') || !!highlight.public_gfs_id

  // Active-segment fill runs for the frame's own duration so the bar
  // tracks the timer one-to-one.
  const activeFillDuration = frame.duration_ms && frame.duration_ms > 0 ? frame.duration_ms : FRAME_DURATION_MS
  const viewerLabel = isAuthor ? t('highlight.today.yours_title') : t('highlight.viewer.dialog_aria', { name: authorName })

  const who = (
    <>
      <Avatar name={authorName} src={authorPicture} size={36} />
      <span class="sh-hlv__whotext">
        <span class="sh-hlv__name">
          {isAuthor ? t('highlight.today.yours_title') : <bdi dir="auto">{authorName}</bdi>}
        </span>
        <span class="sh-hlv__meta">
          <bdi><time dateTime={frame.created_at}>{localSinceTime(frame.created_at)}</time></bdi>
          <span aria-hidden="true">·</span>
          {CHIP_ICON[chip.icon]}
          <span class="sr-only">{t('highlight.chip.aria', { who: chip.text })}</span>
          <span class="sh-hlv__aud" aria-hidden="true">{chip.text}</span>
        </span>
      </span>
    </>
  )

  return (
    <div
      class={stopped ? 'sh-hlv sh-hlv--paused' : 'sh-hlv'}
      role="region"
      aria-label={viewerLabel}
    >
      <div class="sh-hlv__col">
        {/* One segment per frame; the active one fills over the frame's
         *  duration and freezes while paused. */}
        <div class="sh-hlv__prog" aria-hidden="true">
          {frames.map((_, i) => (
            <span
              key={i}
              class={
                i < currentIndex.value
                  ? 'sh-hlv__seg sh-hlv__seg--done'
                  : i === currentIndex.value ? 'sh-hlv__seg sh-hlv__seg--active' : 'sh-hlv__seg'
              }
            >
              <span
                class="sh-hlv__fill"
                style={i === currentIndex.value ? `--seg-dur: ${activeFillDuration}ms` : undefined}
              />
            </span>
          ))}
        </div>
        <span class="sr-only" aria-live="polite">{frameOf}</span>

        <header class="sh-hlv__head">
          {isAuthor
            ? <span class="sh-hlv__who">{who}</span>
            : (
              <button
                type="button"
                class="sh-hlv__who"
                aria-label={t('highlight.viewer.menu_profile', { name: authorName })}
                onClick={() => openPerson(highlight.author_user_id, {
                  person: { display_name: authorName, picture_url: authorPicture },
                })}
              >
                {who}
              </button>
            )}
          <button
            type="button"
            class="sh-hlv__btn"
            aria-label={stopped ? t('highlight.viewer.play_aria') : t('highlight.viewer.pause_aria')}
            aria-keyshortcuts="Space K"
            onClick={togglePause}
          >
            {stopped ? VIEWER_ICON.play : VIEWER_ICON.pause}
          </button>
          <button
            type="button"
            class="sh-hlv__btn"
            aria-label={t('highlight.viewer.more_aria')}
            aria-haspopup="dialog"
            onClick={() => { sheet.value = 'more' }}
          >
            {VIEWER_ICON.more}
          </button>
          <button
            type="button"
            class="sh-hlv__btn"
            aria-label={t('highlight.viewer.close_aria')}
            aria-keyshortcuts="Escape"
            onClick={close}
          >
            {VIEWER_ICON.close}
          </button>
        </header>

        <div
          class="sh-hlv__stage"
          // Press-and-hold pauses (the stage only, so the controls under
          // it never pause-resume on every click).
          onPointerDown={() => { held.value = true }}
          onPointerUp={() => { held.value = false }}
          onPointerLeave={() => { held.value = false }}
          onPointerCancel={() => { held.value = false }}
        >
          {frame.frame_type === 'image' ? (
            <img src={frame.media_url} alt={frame.caption_text ?? ''} class="sh-hlv__media" />
          ) : (
            // Story UX (autoplay-muted, no controls, tap to advance), so a
            // raw <video>. ``key`` gives each frame a fresh element.
            <video
              key={frame.id}
              ref={videoRef}
              src={frame.media_url}
              poster={frame.thumbnail_url}
              class="sh-hlv__media"
              autoPlay
              playsInline
              muted={muted}
              controls={false}
              onEnded={advance}
            />
          )}
          <button
            type="button"
            class="sh-hlv__tap sh-hlv__tap--back"
            onClick={(e) => { e.preventDefault(); goBack() }}
            aria-label={t('highlight.viewer.prev_frame')}
          />
          <button
            type="button"
            class="sh-hlv__tap sh-hlv__tap--next"
            onClick={(e) => { e.preventDefault(); advance() }}
            aria-label={t('highlight.viewer.next_frame')}
          />
          {frame.frame_type === 'video' && (
            <button
              type="button"
              class="sh-hlv__btn sh-hlv__mute"
              onClick={(ev) => { ev.stopPropagation(); setMuted(m => !m) }}
              onPointerDown={(ev) => ev.stopPropagation()}
              aria-pressed={!muted}
              aria-label={muted ? t('highlight.viewer.unmute_aria') : t('highlight.viewer.mute_aria')}
            >
              {muted ? VIEWER_ICON.muted : VIEWER_ICON.sound}
            </button>
          )}
          {stopped && (
            <span class="sh-hlv__paused" aria-hidden="true">
              {VIEWER_ICON.pause}{t('highlight.viewer.paused')}
            </span>
          )}
          {(frame.caption_text || frame.caption_emoji) && (
            <p class="sh-hlv__cap" dir="auto">
              {frame.caption_emoji && <span class="sh-hlv__capemoji" aria-hidden="true">{frame.caption_emoji} </span>}
              {frame.caption_text}
            </p>
          )}
        </div>

        {!isAuthor && (
          <div class="sh-hlv__tray">
            <div class="sh-hlv__emo" role="group" aria-label={t('highlight.viewer.react_group')}>
              {QUICK_REACTIONS.map(emoji => (
                <button
                  key={emoji}
                  type="button"
                  class={mine === emoji ? 'sh-hlv__emobtn is-on' : 'sh-hlv__emobtn'}
                  aria-pressed={mine === emoji}
                  aria-label={mine === emoji
                    ? t('highlight.viewer.react_mine_aria', { emoji })
                    : t('highlight.viewer.react_aria', { emoji })}
                  onClick={() => void react(emoji)}
                >
                  {emoji}
                </button>
              ))}
            </div>
            <form class="sh-hlv__reply" onSubmit={(e) => void sendReply(e)}>
              <input
                type="text"
                class="sh-hlv__replyin"
                dir="auto"
                value={reply}
                maxLength={2000}
                placeholder={t('highlight.viewer.reply_placeholder', { name: authorFirst })}
                aria-label={t('highlight.viewer.reply_placeholder', { name: authorFirst })}
                onInput={(e) => setReply((e.target as HTMLInputElement).value)}
                onFocus={() => { typing.value = true }}
                onBlur={() => { typing.value = false }}
                onKeyDown={(e) => { if (e.key === 'Escape') (e.target as HTMLInputElement).blur() }}
              />
              {reply.trim() && (
                <button
                  type="submit"
                  class="sh-hlv__btn sh-hlv__send"
                  disabled={sending}
                  aria-label={t('highlight.viewer.reply_send')}
                >
                  {VIEWER_ICON.send}
                </button>
              )}
            </form>
          </div>
        )}

        {isAuthor && (
          <div class="sh-hlv__foot">
            <button
              type="button"
              class="sh-hlv__stat"
              aria-haspopup="dialog"
              onClick={() => { sheet.value = 'seen' }}
            >
              {VIEWER_ICON.eye}
              {views.length === 0
                ? t('highlight.viewer.no_views')
                : t('highlight.viewer.seen_n', { n: String(views.length) })}
            </button>
            {reactions.length > 0 && (
              <button
                type="button"
                class="sh-hlv__stat"
                aria-haspopup="dialog"
                aria-label={reactionsLabel(reactions.length)}
                onClick={() => { sheet.value = 'seen' }}
              >
                <span class="sh-hlv__stat-emo" aria-hidden="true">
                  {[...new Set(reactions.map(r => r.emoji))].slice(0, 3).join('')}
                </span>
                <span aria-hidden="true">{reactions.length}</span>
              </button>
            )}
            <span class="sh-hlv__grow" />
            <button
              type="button"
              class="sh-hlv__stat"
              aria-haspopup="dialog"
              onClick={() => { sheet.value = 'share' }}
            >
              {VIEWER_ICON.share}
              {t('highlight.viewer.share')}
            </button>
          </div>
        )}

        <div class="sh-hlv__side sh-hlv__side--start">
          <button type="button" class="sh-hlv__btn" onClick={goBack} aria-label={t('highlight.viewer.prev_frame')}>
            {VIEWER_ICON.prev}
          </button>
          {around?.prev && (
            <PersonPeek
              person={around.prev}
              label={personFirstName(around.prev)}
              aria={t('highlight.viewer.prev_person_aria', {
                name: namedPersonLabel(around.prev.authorId, around.prev.highlight.author),
              })}
              onClick={() => goToPerson(around.prev!)}
            />
          )}
        </div>
        <div class="sh-hlv__side sh-hlv__side--end">
          <button type="button" class="sh-hlv__btn" onClick={advance} aria-label={t('highlight.viewer.next_frame')}>
            {VIEWER_ICON.next}
          </button>
          {around?.next && (
            <PersonPeek
              person={around.next}
              label={around.upNext
                ? t('highlight.viewer.up_next', { name: personFirstName(around.next) })
                : personFirstName(around.next)}
              aria={t(around.upNext ? 'highlight.viewer.up_next_aria' : 'highlight.viewer.next_person_aria',
                { name: namedPersonLabel(around.next.authorId, around.next.highlight.author) })}
              onClick={() => goToPerson(around.next!)}
            />
          )}
        </div>
      </div>

      {sheet.value === 'more' && (
        <ViewerSheet
          title={t('highlight.viewer.menu_heading', { n: String(currentIndex.value + 1), total: String(frames.length) })}
          onClose={() => { sheet.value = null }}
        >
          <div class="sh-hlv-menu">
            {isAuthor ? (
              <>
                <SheetRow
                  icon={VIEWER_ICON.share}
                  title={t('highlight.viewer.menu_share')}
                  sub={t('highlight.viewer.share_note')}
                  onClick={() => { sheet.value = 'share' }}
                />
                {canPublish && (
                  <SheetRow
                    icon={VIEWER_ICON.link}
                    title={highlight.public_gfs_id ? t('highlight.viewer.menu_public_link') : t('highlight.viewer.menu_publish')}
                    sub={highlight.public_gfs_id ? t('highlight.viewer.menu_public_link_sub') : t('highlight.viewer.menu_publish_sub')}
                    onClick={() => { sheet.value = null; openPublishMenu(highlight.id, !!highlight.public_gfs_id) }}
                  />
                )}
                <hr class="sh-hlv-menu__sep" />
                <SheetRow
                  danger
                  icon={VIEWER_ICON.trash}
                  title={t('highlight.viewer.menu_delete_frame')}
                  sub={others === 0
                    ? t('highlight.viewer.menu_delete_frame_only')
                    : t(isOne(others) ? 'highlight.viewer.menu_delete_frame_sub_one' : 'highlight.viewer.menu_delete_frame_sub',
                      { n: String(others) })}
                  onClick={() => void deleteFrame()}
                />
                <SheetRow
                  danger
                  icon={VIEWER_ICON.trash}
                  title={isToday ? t('highlight.viewer.menu_delete_today') : t('highlight.viewer.menu_delete_highlight')}
                  sub={t(isOne(frames.length) ? 'highlight.viewer.menu_delete_day_sub_one' : 'highlight.viewer.menu_delete_day_sub',
                    { n: String(frames.length) })}
                  onClick={() => void deleteDay()}
                />
              </>
            ) : (
              <>
                <SheetRow
                  icon={VIEWER_ICON.person}
                  title={t('highlight.viewer.menu_profile', { name: authorName })}
                  onClick={() => {
                    sheet.value = null
                    openPerson(highlight.author_user_id, { person: { display_name: authorName, picture_url: authorPicture } })
                  }}
                />
                <SheetRow
                  icon={VIEWER_ICON.flag}
                  title={t('highlight.viewer.menu_report')}
                  onClick={() => { sheet.value = null; openReport('highlight', highlight.id) }}
                />
              </>
            )}
          </div>
        </ViewerSheet>
      )}
      {sheet.value === 'seen' && isAuthor && (
        <SeenBySheet
          frameLabel={t('highlight.viewer.frame_lower', { n: String(currentIndex.value + 1), total: String(frames.length) })}
          views={views}
          reactions={reactions}
          onClose={() => { sheet.value = null }}
        />
      )}
      {sheet.value === 'share' && isAuthor && myId && (
        <ShareSheet highlight={highlight} me={myId} onClose={() => { sheet.value = null }} />
      )}
    </div>
  )
}

/** A person before / after this one: a dimmed still with their avatar. */
function PersonPeek({ person, label, aria, onClick }: {
  person: TodayPerson
  label: string
  aria: string
  onClick: () => void
}) {
  const first = person.frames[person.frames.length - 1]
  const still = first ? (first.frame_type === 'video' ? first.thumbnail_url : first.media_url) : undefined
  const name = namedPersonLabel(person.authorId, person.highlight.author)
  return (
    <>
      <button type="button" class="sh-hlv__peek" aria-label={aria} onClick={onClick}>
        {still && <img class="sh-hlv__peek-still" src={still} alt="" />}
        <span class="sh-hlv__peek-avatar">
          <Avatar name={name} src={namedPersonPicture(person.authorId, person.highlight.author)} size={40} />
        </span>
      </button>
      <span class="sh-hlv__peek-label" aria-hidden="true"><bdi dir="auto">{label}</bdi></span>
    </>
  )
}
