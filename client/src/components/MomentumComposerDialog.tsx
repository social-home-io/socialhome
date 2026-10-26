/**
 * MomentumComposerDialog — Modal-wrapped composer for new moments + replies
 * (UX r3 PR 9: you see where it goes and when you can post).
 *
 * Tapping "Share a moment…" or a reply button opens this dialog over the
 * current page. The deep links ``/momentum/new`` and
 * ``/momentum/{id}/reply`` redirect to ``/momentum`` and open it too.
 *
 * What it says, before anything is written:
 *
 * * **Reach** — one honest line ("Paired households and theirs, up to 3
 *   hops · 24 h"; a protected account: "Your household and paired
 *   households") with an (i) disclosure for the details, including how
 *   long followers keep it (the author's own setting). No "Change" link:
 *   ``max_hops`` only changes what *you* see, never how far yours go.
 * * **Public** — a separate switch, shown only when the person is
 *   registered on a GFS, and for a reply only under a public moment
 *   (decision 2). It starts from "Make new moments public" (off for new
 *   registrations, decision 4); once the person flips it, a late
 *   registrations load never overwrites that (B25). When it's on the
 *   button reads "Post publicly".
 * * **Rate limit** — one top-level moment per 15 minutes (decision 13).
 *   ``GET /api/moments/limits`` says when the next one can go; the banner
 *   says so and the button reads "Post at 13:24" (disabled) until then.
 *   Writing stays possible; replies are exempt. A 429 carries the same
 *   ``next_allowed_at`` and lands in the same banner, draft kept.
 *
 * Post waits for the media (no posting mid-upload, B9); closing with text
 * or media asks before discarding it (C18).
 *
 * Module-level signals drive open / close + parent state (the
 * ``CalendarEventDialog`` pattern); host pages mount one
 * ``<MomentumComposerDialog />``.
 */
import { signal } from '@preact/signals'
import { useEffect, useRef } from 'preact/hooks'
import { api, ApiError } from '@/api'
import { Button } from './Button'
import { confirmDialog } from './confirm'
import { isRestricted } from './ProtectedNotice'
import { MediaDropzone } from './MediaDropzone'
import { Modal } from './Modal'
import { Switch } from './Switch'
import { showToast } from './Toast'
import { UploadProgressBar, uploadWithProgress } from './UploadProgress'
import { describeUploadError } from '@/utils/uploadErrors'
import { getMomentsPreferences, momentRetentionLabel } from '@/utils/preferences'
import { localClockTime } from '@/utils/relativeTime'
import { currentUser } from '@/store/auth'
import { namedPersonLabel } from '@/store/householdUsers'
import {
  loadRegistrations,
  registrations,
} from '@/store/momentPublic'
import type { GfsConnection, Moment, MomentDetail } from '@/types'
import { t, isOne } from '@/i18n/i18n'
import { formatSeconds, readVideoDurationMs } from '@/utils/videoDuration'

const MAX_CONTENT = 1_000
const MAX_VIDEO_MS = 15_000
/** How often the "in N min" countdown re-renders while limited. */
const LIMIT_TICK_MS = 15_000

const open = signal<boolean>(false)
const parentId = signal<string | null>(null)
/** The moment a reply answers, for the quote (``null`` until known). */
const parent = signal<Moment | null>(null)
const submitting = signal<boolean>(false)
const content = signal<string>('')
const mediaUrl = signal<string | null>(null)
const mediaPreviewUrl = signal<string | null>(null)
const mediaType = signal<'image' | 'video' | null>(null)
const durationMs = signal<number | null>(null)
/** A picked file is being checked or uploaded — Post waits (B9). */
const mediaBusy = signal<boolean>(false)
const isPublic = signal<boolean>(false)
/** The person flipped the public switch in this compose — a registrations
 *  load that lands later must not overwrite their choice (B25). */
const publicTouched = signal<boolean>(false)
/** Whether the moment being replied to is public (false for new moments). */
const parentIsPublic = signal<boolean>(false)
/** When the next top-level moment can go (UTC ISO), ``null`` = now. */
const nextAllowedAt = signal<string | null>(null)
/** Wall clock for the countdown; bumped by a timer while limited. */
const now = signal<number>(Date.now())
const reachInfoOpen = signal<boolean>(false)
/** Active GFS connections, for naming the one the public switch uses. */
const gfsNames = signal<Record<string, string>>({})
/** Bumped per open so late answers from an earlier compose are dropped. */
let openSeq = 0

function setNextAllowed(iso: unknown): void {
  now.value = Date.now()
  nextAllowedAt.value =
    typeof iso === 'string' && Date.parse(iso) > now.value ? iso : null
}

function loadLimits(seq: number): void {
  api.get('/api/moments/limits')
    .then((r: { next_allowed_at?: string | null } | null) => {
      if (seq === openSeq) setNextAllowed(r?.next_allowed_at ?? null)
    })
    .catch(() => { /* unknown: the 429 still says it at Post */ })
}

function loadParent(seq: number, id: string): void {
  api.get(`/api/moments/${id}`)
    .then((d: MomentDetail) => {
      if (seq !== openSeq || !d?.moment) return
      parent.value = d.moment
      parentIsPublic.value = d.moment.is_public === true
    })
    .catch(() => { /* no quote; unknown counts as not public */ })
}

function loadGfsNames(): void {
  if (Object.keys(gfsNames.value).length > 0) return
  api.get('/api/gfs/connections')
    .then((rows: GfsConnection[] | null) => {
      gfsNames.value = Object.fromEntries((rows ?? []).map((g) => [g.id, g.display_name]))
    })
    .catch(() => { /* the row says "a GFS" instead */ })
}

/** Open the composer dialog. Pass a moment id to compose a reply; omit
 *  for a new top-level moment.
 *
 *  A reply may go public only under a public moment (decision 2): pass
 *  the ``parent`` moment (quoted, and its ``is_public`` offers the toggle,
 *  never pre-ticked) or ``parentIsPublic``. Without either (a deep link)
 *  the parent is read; unknown counts as not public — the toggle stays
 *  hidden. "Make new moments public" applies to new top-level moments
 *  only. */
export function openMomentumComposer(
  parentMomentId: string | null = null,
  opts: { parentIsPublic?: boolean; parent?: Moment } = {},
): void {
  const seq = ++openSeq
  parentId.value = parentMomentId
  parent.value = parentMomentId !== null ? opts.parent ?? null : null
  parentIsPublic.value = parentMomentId !== null
    && (opts.parentIsPublic === true || opts.parent?.is_public === true)
  content.value = ''
  mediaUrl.value = null
  mediaPreviewUrl.value = null
  mediaType.value = null
  durationMs.value = null
  mediaBusy.value = false
  submitting.value = false
  publicTouched.value = false
  reachInfoOpen.value = false
  nextAllowedAt.value = null
  const defaultShare = () => registrations.value.some((r) => r.default_share)
  // Cached registrations show the right state at once; the reload below
  // only fills in what the person hasn't decided themselves.
  isPublic.value = parentMomentId === null && defaultShare()
  open.value = true
  void loadRegistrations().then(() => {
    if (seq !== openSeq || !open.value) return
    if (registrations.value.length > 0) loadGfsNames()
    if (parentId.value === null && !publicTouched.value) isPublic.value = defaultShare()
  })
  if (parentMomentId === null) loadLimits(seq)
  else if (!opts.parent) loadParent(seq, parentMomentId)
}

/** The "share publicly" toggle applies: registered, not protected, and —
 *  for a reply — the parent is public. */
function canSharePublic(): boolean {
  if (registrations.value.length === 0 || isRestricted('public_moments')) return false
  return parentId.value === null || parentIsPublic.value
}

/** Top-level and the next allowed time is still ahead. */
function isLimited(): boolean {
  const at = nextAllowedAt.value
  return parentId.value === null && at !== null && Date.parse(at) > now.value
}

function hasDraft(): boolean {
  return content.value.trim() !== '' || mediaUrl.value !== null || mediaBusy.value
}

/** Close, asking first when there's something to lose (C18). */
async function requestClose(): Promise<void> {
  if (hasDraft()) {
    const ok = await confirmDialog(t('moment.composer.discard_body'), {
      title: t(parentId.value ? 'moment.composer.discard_reply_title' : 'moment.composer.discard_title'),
      confirmLabel: t('moment.composer.discard'),
      cancelLabel: t('moment.composer.keep_writing'),
      destructive: true,
    })
    if (!ok) return
  }
  open.value = false
}

function publicTitle(): string {
  const regs = registrations.value
  if (regs.length === 1) {
    const name = gfsNames.value[regs[0].gfs_id]
    return name
      ? t('moment.composer.public_on', { gfs: name })
      : t('moment.composer.public_on_one')
  }
  return t('moment.composer.public_on_many', { n: String(regs.length) })
}

function IconPeople() {
  return (
    <svg class="sh-momentum-composer-icon" width="20" height="20" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
      aria-hidden="true" focusable="false">
      <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" /><circle cx="9" cy="7" r="4" />
      <path d="M22 21v-2a4 4 0 0 0-3-3.87" /><path d="M16 3.13a4 4 0 0 1 0 7.75" />
    </svg>
  )
}

function IconGlobe() {
  return (
    <svg class="sh-momentum-composer-icon" width="20" height="20" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
      aria-hidden="true" focusable="false">
      <circle cx="12" cy="12" r="10" /><path d="M2 12h20" />
      <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
    </svg>
  )
}

function IconClock() {
  return (
    <svg class="sh-momentum-composer-icon" width="20" height="20" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
      aria-hidden="true" focusable="false">
      <circle cx="12" cy="12" r="10" /><path d="M12 6v6l4 2" />
    </svg>
  )
}

interface Props {
  /** Called after a successful post. Receives the created moment so the
   *  caller can refresh its local state without a refetch. */
  onPosted?: (m: Moment) => void
}

export function MomentumComposerDialog({ onPosted }: Props = {}) {
  const textareaRef = useRef<HTMLTextAreaElement | null>(null)

  // Auto-focus the textarea when the dialog opens. The Modal focus-trap
  // lands on the dialog itself by default; for a composer the operator
  // expects to start typing immediately.
  useEffect(() => {
    if (open.value) {
      // Defer one frame so the Modal's own focus management runs first.
      const id = requestAnimationFrame(() => {
        textareaRef.current?.focus()
      })
      return () => cancelAnimationFrame(id)
    }
  }, [open.value])

  // While limited, keep "in N min" current and lift the limit on time.
  useEffect(() => {
    if (!open.value || nextAllowedAt.value === null) return
    const id = setInterval(() => {
      now.value = Date.now()
      if (nextAllowedAt.value !== null && Date.parse(nextAllowedAt.value) <= now.value) {
        nextAllowedAt.value = null
      }
    }, LIMIT_TICK_MS)
    return () => clearInterval(id)
  }, [open.value, nextAllowedAt.value])

  const acceptFiles = async (files: File[]) => {
    const f = files[0]
    if (!f) return
    const isVideo = f.type.startsWith('video/')
    const isImage = f.type.startsWith('image/')
    if (!isVideo && !isImage) {
      showToast(t('highlight.quick.pick_media'), 'error')
      return
    }
    const seq = openSeq
    mediaBusy.value = true
    try {
      if (isVideo) {
        const dur = await readVideoDurationMs(f)
        if (dur === null) {
          showToast(t('moment.composer.video_unreadable'), 'error')
          return
        }
        if (dur > MAX_VIDEO_MS + 50) {
          showToast(
            t('moment.composer.video_too_long', { max: String(MAX_VIDEO_MS / 1000), len: formatSeconds(dur) }),
            'error',
          )
          return
        }
        durationMs.value = Math.round(dur)
      }
      const result = await uploadWithProgress(f)
      if (seq !== openSeq) return  // the composer was closed meanwhile
      mediaUrl.value = result.url
      mediaPreviewUrl.value = result.signed_url
      mediaType.value = isVideo ? 'video' : 'image'
    } catch (err: unknown) {
      showToast(describeUploadError(err, { file: f }), 'error')
      mediaUrl.value = null
      mediaPreviewUrl.value = null
      mediaType.value = null
      durationMs.value = null
    } finally {
      if (seq === openSeq) mediaBusy.value = false
    }
  }

  const clearMedia = () => {
    mediaUrl.value = null
    mediaPreviewUrl.value = null
    mediaType.value = null
    durationMs.value = null
  }

  const submit = async (e: Event) => {
    e.preventDefault()
    if (mediaBusy.value || isLimited()) return
    const text = content.value.trim()
    if (!text && !mediaUrl.value) {
      showToast(t('moment.composer.empty'), 'error')
      return
    }
    if (text.length > MAX_CONTENT) {
      showToast(t('moment.composer.too_long', { n: String(MAX_CONTENT) }), 'error')
      return
    }
    submitting.value = true
    try {
      const body: Record<string, unknown> = { content: text }
      if (mediaUrl.value) {
        body.media_url = mediaUrl.value
        body.media_type = mediaType.value
        if (mediaType.value === 'video') body.duration_ms = durationMs.value
      }
      if (parentId.value) body.parent_moment_id = parentId.value
      if (registrations.value.length > 0) {
        body.is_public = canSharePublic() && isPublic.value
      }
      const m = await api.post('/api/moments', body) as Moment
      open.value = false
      onPosted?.(m)
    } catch (err: unknown) {
      submitting.value = false
      if (err instanceof ApiError && err.status === 429 && typeof err.extra.next_allowed_at === 'string') {
        // Same banner as before writing; the draft stays.
        setNextAllowed(err.extra.next_allowed_at)
        if (isLimited()) return
      }
      const msg = (err as Error)?.message ?? String(err)
      showToast(t('moment.composer.failed', { error: msg }), 'error')
    }
  }

  const remaining = MAX_CONTENT - content.value.length
  const isReply = parentId.value !== null
  const isProtected = currentUser.value?.protected === true
  const limited = isLimited()
  const nextTime = limited ? localClockTime(nextAllowedAt.value!) : ''
  const minutesLeft = limited
    ? Math.max(1, Math.ceil((Date.parse(nextAllowedAt.value!) - now.value) / 60_000))
    : 0
  const showPublic = canSharePublic()
  const publicOn = showPublic && isPublic.value

  const reachLine = isReply
    ? t(isProtected ? 'moment.composer.reach_reply_protected' : 'moment.composer.reach_reply')
    : t(isProtected ? 'moment.entry.reach_protected' : 'moment.entry.reach')
  const reachMore = isProtected
    ? t('moment.composer.reach_more_protected')
    : isReply
      ? t('moment.composer.reach_more_reply')
      : t('moment.composer.reach_more', {
        days: momentRetentionLabel(getMomentsPreferences().follower_retention_days),
      })

  let postLabel: string
  if (isReply) postLabel = t(publicOn ? 'moment.composer.reply_public' : 'moment.composer.reply')
  else if (limited) postLabel = t('moment.composer.post_at', { time: nextTime })
  else postLabel = t(publicOn ? 'moment.composer.post_public' : 'moment.composer.post')

  const p = parent.value
  const parentName = p
    ? (p.author_user_id === currentUser.value?.user_id
      ? t('moment.card.you')
      : namedPersonLabel(p.author_user_id, p.author))
    : ''

  return (
    <Modal
      open={open.value}
      onClose={() => { void requestClose() }}
      title={isReply ? t('moment.composer.reply') : t('moment.composer.title')}
      class="sh-momentum-composer-modal"
    >
      <form class="sh-momentum-composer-form" onSubmit={submit}>
        {limited && (
          <div class="sh-momentum-composer-limit" role="status">
            <IconClock />
            <div>
              <strong>{t('moment.composer.limit_title', { time: nextTime, n: String(minutesLeft) })}</strong>
              <span>{t('moment.composer.limit_body')}</span>
            </div>
          </div>
        )}

        {isReply && p && (
          <blockquote class="sh-momentum-composer-quote">
            <bdi class="sh-momentum-composer-quote__name" dir="auto">{parentName}</bdi>
            {p.content && (
              <span class="sh-momentum-composer-quote__text" dir="auto">{p.content}</span>
            )}
          </blockquote>
        )}

        <textarea
          ref={textareaRef}
          class="sh-momentum-composer-text"
          rows={4}
          dir="auto"
          maxLength={MAX_CONTENT}
          placeholder={isReply ? t('moment.composer.reply_placeholder') : t('moment.composer.placeholder')}
          aria-label={isReply ? t('moment.composer.reply_aria') : t('moment.composer.aria')}
          value={content.value}
          onInput={(e) => { content.value = (e.target as HTMLTextAreaElement).value }}
        />
        <div class="sh-momentum-composer-meta">
          <span class={remaining < 0 ? 'sh-error' : 'sh-muted'}>
            {t(isOne(remaining) ? 'highlight.quick.chars_left_one' : 'highlight.quick.chars_left', { n: String(remaining) })}
          </span>
        </div>

        <div class="sh-momentum-composer-media">
          {mediaUrl.value && mediaType.value === 'image' && (
            <div class="sh-composer-attachment">
              <img
                src={mediaPreviewUrl.value ?? mediaUrl.value}
                alt=""
                class="sh-momentum-composer-preview"
              />
              <button
                type="button"
                class="sh-composer-remove-attach"
                aria-label={t('composer.remove_attachment')}
                onClick={clearMedia}
              >✕</button>
            </div>
          )}
          {mediaUrl.value && mediaType.value === 'video' && (
            <div class="sh-composer-attachment">
              <video
                src={mediaPreviewUrl.value ?? mediaUrl.value}
                class="sh-momentum-composer-preview"
                controls
                muted
                preload="metadata"
              />
              {durationMs.value !== null && (
                <span class="sh-momentum-composer-duration"
                      aria-label={t('moment.composer.duration_aria', { len: formatSeconds(durationMs.value), max: String(MAX_VIDEO_MS / 1000) })}>
                  <span aria-hidden="true">⏱ {t('moment.composer.duration', { len: formatSeconds(durationMs.value) })}</span>
                </span>
              )}
              <button
                type="button"
                class="sh-composer-remove-attach"
                aria-label={t('composer.remove_attachment')}
                onClick={clearMedia}
              >✕</button>
            </div>
          )}
          {!mediaUrl.value && (
            <MediaDropzone
              accept="image/*,video/*"
              disabled={mediaBusy.value}
              hint={t('highlight.quick.drop_hint')}
              pickLabel={t('highlight.quick.pick_label')}
              draggingHint={t('composer.drop.dragging')}
              onFiles={acceptFiles}
            />
          )}
          <UploadProgressBar />
        </div>

        <div class="sh-momentum-composer-reach">
          <IconPeople />
          <span class="sh-momentum-composer-reach__line">{reachLine}</span>
          <button
            type="button"
            class="sh-momentum-composer-info"
            aria-expanded={reachInfoOpen.value ? 'true' : 'false'}
            aria-controls="sh-momentum-composer-reach-more"
            aria-label={t('moment.composer.reach_info_aria')}
            onClick={() => { reachInfoOpen.value = !reachInfoOpen.value }}
          >
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
              stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
              aria-hidden="true" focusable="false">
              <circle cx="12" cy="12" r="10" /><path d="M12 16v-4" /><path d="M12 8h.01" />
            </svg>
          </button>
        </div>
        {reachInfoOpen.value && (
          <p id="sh-momentum-composer-reach-more" class="sh-momentum-composer-reach-more">
            {reachMore}
          </p>
        )}

        {showPublic && (
          <label class={`sh-momentum-composer-public${publicOn ? ' is-on' : ''}`}>
            <IconGlobe />
            <span class="sh-momentum-composer-public__text">
              <strong>{publicTitle()}</strong>
              <span id="sh-momentum-composer-public-hint">{t('moment.composer.public_hint')}</span>
            </span>
            <Switch
              checked={isPublic.value}
              aria-describedby="sh-momentum-composer-public-hint"
              onChange={(next) => {
                publicTouched.value = true
                isPublic.value = next
              }}
            />
          </label>
        )}

        <div class="sh-form-actions sh-momentum-composer-actions">
          <Button type="button" variant="secondary" onClick={() => { void requestClose() }}>{t('common.cancel')}</Button>
          <Button
            type="submit"
            loading={submitting.value}
            disabled={mediaBusy.value || limited}
          >
            {postLabel}
          </Button>
        </div>
      </form>
    </Modal>
  )
}
