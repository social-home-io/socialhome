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
 *   registered on a GFS that can carry encrypted moments (v_62), and for
 *   a reply only under a public moment (decision 2), with a link to
 *   Settings › Privacy › Public sharing (decision 5). It starts from
 *   "Make new moments public" (off for new registrations, decision 4);
 *   once the person flips it, a late registrations load never overwrites
 *   that (B25). When it's on the button reads "Post publicly".
 *   Where the switch can't be, the row still says why, so public sharing
 *   is never a secret: the household is on a GFS the person isn't
 *   signed up on → "Sign up in Settings" (anyone can, for themselves);
 *   signed up on every GFS but none can carry encrypted moments → says
 *   so, no link (nothing in Settings helps). No GFS at all, or a protected
 *   account: no row.
 *   Both links put a new moment aside instead of asking to discard it;
 *   the next "Share a moment" (and the panel's "Back to your moment")
 *   brings it back.
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
import { useLocation } from 'preact-iso'
import { api, ApiError } from '@/api'
import { addBase } from '@/baseUrl'
import { Button } from './Button'
import { confirmDialog } from './confirm'
import { isRestricted } from './ProtectedNotice'
import { MediaDropzone } from './MediaDropzone'
import { Modal } from './Modal'
import { Switch } from './Switch'
import { showToast } from './Toast'
import { UploadProgressBar } from './UploadProgress'
import { MOMENT_MAX_VIDEO_MS, prepareMomentMedia } from '@/features/momentum/momentMedia'
import { getMomentsPreferences, momentRetentionLabel } from '@/utils/preferences'
import { localClockTime } from '@/utils/relativeTime'
import { currentUser } from '@/store/auth'
import { namedPersonLabel } from '@/store/householdUsers'
import {
  canCarryPublicMoments,
  gfsConnections,
  gfsName,
  loadGfsConnections,
  loadRegistrations,
  parkedMoment,
  registrations,
} from '@/store/momentPublic'
import type { Moment, MomentDetail } from '@/types'
import { t, isOne } from '@/i18n/i18n'
import { formatSeconds } from '@/utils/videoDuration'

const MAX_CONTENT = 1_000
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
/** Bumped per open so late answers from an earlier compose are dropped. */
let openSeq = 0

/** Start a new compose: a fresh sequence (late answers and uploads from
 *  the last one are dropped) and every field empty. Only a parked moment
 *  is ever resumed, and sign-out clears ``parkedMoment``
 *  (``resetMomentPublic``), so the next account always starts here. */
function newCompose(): number {
  const seq = ++openSeq
  parentId.value = null
  parent.value = null
  parentIsPublic.value = false
  content.value = ''
  mediaUrl.value = null
  mediaPreviewUrl.value = null
  mediaType.value = null
  durationMs.value = null
  mediaBusy.value = false
  submitting.value = false
  isPublic.value = false
  publicTouched.value = false
  reachInfoOpen.value = false
  nextAllowedAt.value = null
  return seq
}


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

/** Open the composer dialog. Pass a moment id to compose a reply; omit
 *  for a new top-level moment.
 *
 *  A reply may go public only under a public moment (decision 2): pass
 *  the ``parent`` moment (quoted, and its ``is_public`` offers the toggle,
 *  never pre-ticked) or ``parentIsPublic``. Without either (a deep link)
 *  the parent is read; unknown counts as not public — the toggle stays
 *  hidden. "Make new moments public" applies to new top-level moments
 *  only.
 *
 *  A new moment put aside for Settings comes back as it was, once;
 *  opening a reply meanwhile drops it (one compose at a time). */
export function openMomentumComposer(
  parentMomentId: string | null = null,
  opts: { parentIsPublic?: boolean; parent?: Moment } = {},
): void {
  // Who the public row names, and whether a sign-up hint applies.
  void loadGfsConnections()
  // Only a new moment is ever put aside (``putAside``).
  const resume = parkedMoment.value && parentMomentId === null && hasDraft()
  parkedMoment.value = false
  if (resume) {
    // Same compose as before: keep its sequence, so an upload still on
    // its way lands in it. The default may have changed in Settings
    // meanwhile: apply it before the dialog shows, not after the reload.
    reachInfoOpen.value = false
    submitting.value = false
    if (!publicTouched.value) isPublic.value = sharesByDefault()
    open.value = true
    const seq = openSeq
    void loadRegistrations().then(() => {
      if (seq !== openSeq || !open.value || publicTouched.value) return
      isPublic.value = sharesByDefault()
    })
    loadLimits(seq)
    return
  }
  const seq = newCompose()
  parentId.value = parentMomentId
  parent.value = parentMomentId !== null ? opts.parent ?? null : null
  parentIsPublic.value = parentMomentId !== null
    && (opts.parentIsPublic === true || opts.parent?.is_public === true)
  // Cached registrations show the right state at once; the reload below
  // only fills in what the person hasn't decided themselves.
  isPublic.value = parentMomentId === null && sharesByDefault()
  open.value = true
  void loadRegistrations().then(() => {
    if (seq !== openSeq || !open.value) return
    if (parentId.value === null && !publicTouched.value) isPublic.value = sharesByDefault()
  })
  if (parentMomentId === null) loadLimits(seq)
  else if (!opts.parent) loadParent(seq, parentMomentId)
}

/** Registrations public moments can actually go through (v_62). */
function carriers() {
  return registrations.value.filter(canCarryPublicMoments)
}

/** "Make new moments public" is on for a GFS this moment can go through. */
function sharesByDefault(): boolean {
  return carriers().some((r) => r.default_share)
}

/** This moment could be public at all: not protected, and — for a reply
 *  — the parent is public. */
function publicApplies(): boolean {
  if (isRestricted('public_moments')) return false
  return parentId.value === null || parentIsPublic.value
}

/** The "share publicly" toggle applies: it could be public, and the
 *  person is signed up on a GFS that carries it. */
function canSharePublic(): boolean {
  return publicApplies() && carriers().length > 0
}

/** What the public row shows: the switch, why it's unavailable, or
 *  nothing (no GFS, protected, a private parent). */
type PublicRow = 'switch' | 'unsupported' | 'signup' | null

function publicRow(): PublicRow {
  if (!publicApplies()) return null
  if (carriers().length > 0) return 'switch'
  if (unjoined().length > 0) return 'signup'
  if (registrations.value.length > 0) return 'unsupported'
  return null
}

/** Active GFSs of the household the person hasn't signed up on. */
function unjoined(): string[] {
  const mine = new Set(registrations.value.map((r) => r.gfs_id))
  return (gfsConnections.value ?? []).map((g) => g.id).filter((id) => !mine.has(id))
}

/** Top-level and the next allowed time is still ahead. */
function isLimited(): boolean {
  const at = nextAllowedAt.value
  return parentId.value === null && at !== null && Date.parse(at) > now.value
}

function hasDraft(): boolean {
  return content.value.trim() !== '' || mediaUrl.value !== null || mediaBusy.value
}

/** Close, asking first when there's something to lose (C18). Resolves
 *  whether it closed. */
async function requestClose(): Promise<boolean> {
  if (hasDraft()) {
    const ok = await confirmDialog(t('moment.composer.discard_body'), {
      title: t(parentId.value ? 'moment.composer.discard_reply_title' : 'moment.composer.discard_title'),
      confirmLabel: t('moment.composer.discard'),
      cancelLabel: t('moment.composer.keep_writing'),
      destructive: true,
    })
    if (!ok) return false
  }
  open.value = false
  return true
}

/** Settings › Privacy › Public sharing (decision 5). */
const PUBLIC_SHARING_PATH = '/settings#public-sharing'

/** "Also public on …", named after the GFSs in question. */
function publicTitle(gfsIds: string[]): string {
  if (gfsIds.length === 1) {
    const name = gfsName(gfsIds[0])
    return name
      ? t('moment.composer.public_on', { gfs: name })
      : t('moment.composer.public_on_one')
  }
  return t('moment.composer.public_on_many', { n: String(gfsIds.length) })
}

/** "Public on …: not available", named only when it's one GFS. */
function unavailableTitle(gfsIds: string[]): string {
  const name = gfsIds.length === 1 ? gfsName(gfsIds[0]) : null
  return name
    ? t('moment.composer.public_unavailable', { gfs: name })
    : t('moment.composer.public_unavailable_any')
}

/** Close without asking and remember a new moment's draft, so a visit to
 *  Settings › Public sharing never costs the words. A reply is never put
 *  aside (its links only exist for a public parent; the reply field on
 *  the moment page keeps its own draft). */
function putAside(): void {
  parkedMoment.value = parentId.value === null && hasDraft()
  open.value = false
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
  const loc = useLocation()
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
    const seq = openSeq
    mediaBusy.value = true
    try {
      // Same checks and upload as the reply field on the moment page.
      const media = await prepareMomentMedia(f)
      if (seq !== openSeq) return  // the composer was closed meanwhile
      mediaUrl.value = media?.url ?? null
      mediaPreviewUrl.value = media?.previewUrl ?? null
      mediaType.value = media?.type ?? null
      durationMs.value = media?.durationMs ?? null
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
  const row = publicRow()
  const showPublic = row === 'switch'
  const publicOn = showPublic && isPublic.value
  const goToSharing = (e: Event) => {
    // The router takes the move once the dialog is closed; a new moment
    // waits for the way back instead of being discarded.
    e.preventDefault()
    e.stopPropagation()
    if (submitting.value) return  // it's being posted; nothing to keep
    if (parentId.value === null) {
      putAside()
      loc.route(PUBLIC_SHARING_PATH)
    } else {
      void requestClose().then((closed) => { if (closed) loc.route(PUBLIC_SHARING_PATH) })
    }
  }

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
                      aria-label={t('moment.composer.duration_aria', { len: formatSeconds(durationMs.value), max: String(MOMENT_MAX_VIDEO_MS / 1000) })}>
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
              <strong>{publicTitle(carriers().map((r) => r.gfs_id))}</strong>
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
        {showPublic && (
          <a
            class="sh-momentum-composer-public-settings"
            href={addBase(PUBLIC_SHARING_PATH)}
            onClick={goToSharing}
          >
            {t('moment.composer.public_settings')}
          </a>
        )}
        {(row === 'signup' || row === 'unsupported') && (
          <div class="sh-momentum-composer-public is-unavailable">
            <IconGlobe />
            <span class="sh-momentum-composer-public__text">
              <strong>
                {unavailableTitle(row === 'signup'
                  ? unjoined()
                  : registrations.value.map((r) => r.gfs_id))}
              </strong>
              <span id="sh-momentum-composer-public-hint">
                {t(row === 'signup' ? 'moment.composer.public_signup_hint' : 'moment.composer.public_unsupported')}
              </span>
              {row === 'signup' && (
                <a
                  class="sh-momentum-composer-public-signup"
                  href={addBase(PUBLIC_SHARING_PATH)}
                  aria-describedby="sh-momentum-composer-public-hint"
                  onClick={goToSharing}
                >
                  {t('moment.composer.public_signup_link')}
                </a>
              )}
            </span>
          </div>
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
