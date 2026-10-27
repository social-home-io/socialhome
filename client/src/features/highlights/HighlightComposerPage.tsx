/**
 * HighlightComposerPage — the one way to post a highlight (Home's "You"
 * ring, Highlights' "+ New" and every other entry point open
 * ``/highlights/new``).
 *
 * Strip first: the frames you're adding sit at the top (numbered, a
 * 36 px ✕ each, "Preparing video…" while a video transcodes instead of a
 * preview that 404s), then the selected frame's preview — capped at
 * 240×320 on phones — then Earlier / Later (or drag a tile) and the
 * selected frame's caption.
 *
 * The audience is the day's, not the frame's (owner decision 1):
 *
 * * **First frame of the day** — "Who can see today's highlight", the
 *   Settings default preselected; "Choose households or people…" needs
 *   at least one pick (B21). The retention line reads the author's own
 *   setting ("Stays 7 days (your setting)"), never a fixed number.
 * * **Adding to today's** — a lock card: "Goes to: <who>. You chose this
 *   with today's first frame (08:12). New frames go to the same people."
 *   plus where else it shows (public link / shares, #1159), and "Change
 *   for all of today…", which opens :mod:`AudienceChangeSheet`.
 *
 * Every frame names the audience shown (``audienceBody``) so a frame
 * landing after UTC midnight starts the new day for the same people.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { useLocation } from 'preact-iso'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { EmojiPickButton } from '@/components/EmojiPickButton'
import { MediaDropzone } from '@/components/MediaDropzone'
import { showToast } from '@/components/Toast'
import { UploadProgressBar, uploadWithProgress } from '@/components/UploadProgress'
import { describeUploadError } from '@/utils/uploadErrors'
import { currentUser } from '@/store/auth'
import { failedMedia, mediaFilename, readyMedia } from '@/store/mediaReady'
import type { OwnHighlight } from '@/types'
import { addBase } from '@/baseUrl'
import { t, isOne } from '@/i18n/i18n'
import { AudienceChangeSheet } from './AudienceChangeSheet'
import { AudiencePicker } from './AudiencePicker'
import {
  audienceBody,
  audienceFromFriends,
  audienceOfChoice,
  choiceIsComplete,
  defaultChoice,
  describeAudience,
  directoryFromFriends,
  loadMyToday,
  lockShareNote,
  retentionDays,
  type AudienceChoice,
  type AudienceDirectory,
  type ConnectedPerson,
  type FriendsPayload,
  type RemoteHousehold,
} from './todayAudience'

// Re-exported for callers / tests that read it from the composer.
export { audienceFromFriends }

/** A media file uploaded but not yet posted — one frame on submit. */
interface StagedFrame {
  /** Local-only id for the keyed render. */
  id: string
  /** Canonical ``api/media/{filename}`` URL that lands in the frame. */
  url: string
  /** Short-lived signed URL for the local preview. */
  preview: string
  type: 'image' | 'video'
  /** Original filename — the ✕'s aria-label. */
  name: string
  caption: string
  /** A video still transcoding when it was uploaded. */
  processing: boolean
}

const CAPTION_MAX = 140
/** Server cap (``MAX_FRAMES_PER_HIGHLIGHT``) — a multi-pick refuses the
 *  overflow before uploading. */
const MAX_FRAMES_PER_HIGHLIGHT = 30
const EMPTY_DIRECTORY: AudienceDirectory = { ownName: '', housemates: [], households: [] }

/** Is this staged video still being prepared? Reads the ``media.ready`` /
 *  ``media.failed`` stores, so the tile updates when the worker is done. */
function preparing(f: StagedFrame): boolean {
  if (!f.processing) return false
  const fn = mediaFilename(f.url)
  return !readyMedia.value.has(fn) && !failedMedia.value.has(fn)
}

function timeOf(iso: string | undefined): string {
  if (!iso) return ''
  const d = new Date(iso)
  return Number.isNaN(d.getTime())
    ? ''
    : d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
}

export default function HighlightComposerPage() {
  const loc = useLocation()
  const captionRef = useRef<HTMLTextAreaElement | null>(null)
  const [frames, setFrames] = useState<StagedFrame[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [dragId, setDragId] = useState<string | null>(null)
  const [choice, setChoice] = useState<AudienceChoice>(() => defaultChoice())
  const [today, setToday] = useState<OwnHighlight | null>(null)
  const [posted, setPosted] = useState(0)
  const [published, setPublished] = useState(false)
  const [households, setHouseholds] = useState<RemoteHousehold[]>([])
  const [people, setPeople] = useState<ConnectedPerson[]>([])
  const [directory, setDirectory] = useState<AudienceDirectory>(EMPTY_DIRECTORY)
  const [ownCount, setOwnCount] = useState(1)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [changing, setChanging] = useState(false)
  const defaultMode = useRef(defaultChoice().mode)

  const loadAudience = async (): Promise<void> => {
    try {
      const payload = await api.get('/api/friends') as FriendsPayload
      const me = currentUser.value?.user_id
      const out = audienceFromFriends(payload, me)
      setHouseholds(out.households)
      setPeople(out.people)
      setDirectory(directoryFromFriends(payload, me))
      setOwnCount(Math.max(1, payload.instance?.members?.length ?? 1))
      setLoadError(null)
      // A Settings default may name a household / person no longer
      // paired: drop it, so a frame only goes to someone offered here.
      const hh = new Set(out.households.map(h => h.instance_id))
      const pp = new Set(out.people.map(p => p.user_id))
      setChoice(c => ({
        ...c,
        households: c.households.filter(id => hh.has(id)),
        users: c.users.filter(id => pp.has(id)),
      }))
    } catch (err: unknown) {
      setHouseholds([])
      setPeople([])
      setLoadError((err as Error)?.message ?? String(err))
    }
  }

  useEffect(() => {
    void loadAudience()
    // Today's highlight, if any: its audience is locked, its frames count
    // against the daily cap.
    loadMyToday().then(mine => {
      setPosted(mine?.frames ?? 0)
      setToday(mine?.highlight ?? null)
      setPublished(mine?.published ?? false)
    }).catch(() => {})
  }, [])

  const framesLeft = Math.max(0, MAX_FRAMES_PER_HIGHLIGHT - posted - frames.length)
  const current = frames.find(f => f.id === selected) ?? frames[0] ?? null
  const index = current ? frames.indexOf(current) : -1
  const numberOf = (i: number) => posted + i + 1

  const acceptFiles = async (files: File[]): Promise<void> => {
    if (files.length === 0) return
    if (framesLeft <= 0) {
      showToast(t('highlight.composer.limit_reached', { n: String(MAX_FRAMES_PER_HIGHLIGHT) }), 'error')
      return
    }
    const accepted = files.slice(0, framesLeft)
    if (files.length > accepted.length) {
      showToast(
        t(isOne(framesLeft) ? 'highlight.composer.more_left_one' : 'highlight.composer.more_left',
          { n: String(framesLeft) }),
        'info',
      )
    }
    for (const file of accepted) {
      try {
        const result = await uploadWithProgress(file)
        const staged: StagedFrame = {
          id: crypto.randomUUID(),
          url: result.url,
          preview: result.signed_url,
          type: file.type.startsWith('video/') ? 'video' : 'image',
          name: file.name,
          caption: '',
          processing: result.media_status === 'processing',
        }
        setFrames(prev => [...prev, staged])
        setSelected(staged.id)
      } catch (err: unknown) {
        showToast(describeUploadError(err, { file }), 'error')
      }
    }
  }

  const onPicked = (e: Event) => {
    const input = e.target as HTMLInputElement
    const files = Array.from(input.files ?? [])
    input.value = ''
    void acceptFiles(files)
  }

  const removeFrame = (id: string) => {
    setFrames(prev => {
      const i = prev.findIndex(f => f.id === id)
      const next = prev.filter(f => f.id !== id)
      if (selected === id || selected === null) {
        setSelected(next[Math.min(i, next.length - 1)]?.id ?? null)
      }
      return next
    })
  }

  const move = (id: string, to: number) => {
    setFrames(prev => {
      const from = prev.findIndex(f => f.id === id)
      if (from < 0 || to < 0 || to >= prev.length || from === to) return prev
      const next = [...prev]
      const [f] = next.splice(from, 1)
      next.splice(to, 0, f)
      return next
    })
    setSelected(id)
  }

  const setCaption = (value: string) => {
    if (!current) return
    const caption = value.slice(0, CAPTION_MAX)
    setFrames(prev => prev.map(f => (f.id === current.id ? { ...f, caption } : f)))
  }

  const insertEmoji = (emoji: string) => {
    if (!current) return
    const ta = captionRef.current
    const cur = current.caption
    const start = ta?.selectionStart ?? cur.length
    const end = ta?.selectionEnd ?? start
    setCaption(cur.slice(0, start) + emoji + cur.slice(end))
    if (ta) {
      requestAnimationFrame(() => {
        ta.focus()
        const pos = (cur.slice(0, start) + emoji).length
        ta.setSelectionRange(pos, pos)
      })
    }
  }

  const onStripDrop = (e: DragEvent, target: number | null) => {
    e.preventDefault()
    const files = Array.from(e.dataTransfer?.files ?? [])
    if (files.length > 0) {
      void acceptFiles(files)
    } else if (dragId !== null) {
      move(dragId, target ?? frames.length - 1)
    }
    setDragId(null)
  }

  const complete = today !== null || choiceIsComplete(choice)
  const preparingCount = frames.filter(preparing).length

  const submit = async (e: Event) => {
    e.preventDefault()
    if (frames.length === 0 || submitting || !complete) return
    setSubmitting(true)
    let lastHighlightId: string | null = null
    // Exactly the audience shown — today's, or the one picked for a new
    // day — on every frame, so none can fall back to the Settings default.
    const picked = audienceOfChoice(choice)
    const audience = today !== null
      ? audienceBody(today.audience_kind, today.audience)
      : audienceBody(picked.kind, picked.ids)
    for (let i = 0; i < frames.length; i++) {
      const f = frames[i]
      try {
        const r = await api.post('/api/highlights/frames', {
          media_url: f.url,
          frame_type: f.type,
          caption_text: f.caption.trim() || null,
          ...audience,
        }) as { highlight: { id: string }, frame: { id: string } }
        lastHighlightId = r.highlight.id
      } catch (err: unknown) {
        showToast(
          t('highlight.composer.frame_failed', { n: String(i + 1), error: String((err as Error)?.message ?? err) }),
          'error',
        )
        // Stop with the frames already posted intact.
        break
      }
    }
    if (lastHighlightId) {
      showToast(
        frames.length === 1 ? t('highlight.composer.posted_one') : t('highlight.composer.posted', { n: String(frames.length) }),
        'success',
      )
      loc.route(`/highlights/${lastHighlightId}`)
    } else {
      setSubmitting(false)
    }
  }

  const nameOf = (id: string): string | undefined =>
    people.find(p => p.user_id === id)?.display_name
    ?? households.find(h => h.instance_id === id)?.display_name

  const days = retentionDays()
  const statusLine = frames.length === 0
    ? t('highlight.composer.status_empty')
    : [
      today !== null
        ? t(isOne(frames.length) ? 'highlight.composer.status_new_one' : 'highlight.composer.status_new', { n: String(frames.length) })
        : t(isOne(frames.length) ? 'highlight.composer.status_frames_one' : 'highlight.composer.status_frames', { n: String(frames.length) }),
      preparingCount > 0
        ? t(isOne(preparingCount) ? 'highlight.composer.status_preparing_one' : 'highlight.composer.status_preparing', { n: String(preparingCount) })
        : null,
    ].filter(Boolean).join(' · ')

  const addTile = (
    <label class={`sh-hlc-strip__add${framesLeft <= 0 ? ' sh-hlc-strip__add--off' : ''}`}>
      <span aria-hidden="true" class="sh-hlc-strip__plus">+</span>
      <span>{t('highlight.composer.add_tile')}</span>
      <input
        type="file"
        multiple
        accept="image/*,video/*"
        class="sr-only"
        disabled={framesLeft <= 0}
        aria-label={t('highlight.composer.add_aria')}
        onChange={onPicked}
      />
    </label>
  )

  return (
    <form class="sh-hlc" onSubmit={submit}>
      <a href={addBase('/highlights')} class="sh-hlc__back sh-tap-link">
        <span aria-hidden="true">‹</span> {t('nav.highlights')}
      </a>
      <h1 class="sh-hlc__title">
        {today !== null ? t('highlight.composer.title_append') : t('highlight.composer.title_new')}
      </h1>

      <div class="sh-hlc__body">
        <div class="sh-hlc__preview">
          {current === null ? (
            <MediaDropzone
              multiple
              accept="image/*,video/*"
              disabled={framesLeft <= 0}
              hint={t('highlight.composer.drop_hint')}
              pickLabel={t('highlight.quick.pick_label')}
              draggingHint={t('highlight.composer.dragging')}
              onFiles={acceptFiles}
            />
          ) : (
            <div class="sh-hlc__stage">
              {current.type === 'image' ? (
                <img src={current.preview} alt="" />
              ) : preparing(current) ? (
                <div class="sh-hlc__preparing" role="status">
                  {t('highlight.composer.preparing')}
                </div>
              ) : (
                <video src={current.preview} controls muted playsInline preload="metadata" />
              )}
              {current.caption && (
                <p class="sh-hlc__stage-caption" dir="auto">{current.caption}</p>
              )}
            </div>
          )}
        </div>

        <div class="sh-hlc__side">
          <div
            class="sh-hlc-strip"
            onDragOver={e => e.preventDefault()}
            onDrop={e => onStripDrop(e, null)}
          >
            {posted > 0 && (
              <div class="sh-hlc-strip__posted">
                <span aria-hidden="true" class="sh-hlc-strip__stack" />
                <span>{t('highlight.composer.posted_stack', { n: String(posted) })}</span>
              </div>
            )}
            {frames.map((f, i) => (
              <div
                key={f.id}
                class={`sh-hlc-strip__tile${current?.id === f.id ? ' sh-hlc-strip__tile--on' : ''}`}
                draggable
                onDragStart={() => setDragId(f.id)}
                onDragEnd={() => setDragId(null)}
                onDragOver={e => e.preventDefault()}
                onDrop={e => { e.stopPropagation(); onStripDrop(e, i) }}
              >
                <button
                  type="button"
                  class="sh-hlc-strip__pick"
                  aria-pressed={current?.id === f.id}
                  aria-label={t('highlight.composer.select_frame', { n: String(numberOf(i)), name: f.name })}
                  onClick={() => setSelected(f.id)}
                >
                  {f.type === 'image' ? (
                    <img src={f.preview} alt="" />
                  ) : preparing(f) ? (
                    <span class="sh-hlc-strip__preparing">{t('highlight.composer.preparing')}</span>
                  ) : (
                    <video src={f.preview} muted preload="metadata" />
                  )}
                </button>
                <span class="sh-hlc-strip__num" aria-hidden="true">{numberOf(i)}</span>
                <button
                  type="button"
                  class="sh-hlc-strip__remove"
                  aria-label={t('composer.remove_image', { name: f.name })}
                  onClick={() => removeFrame(f.id)}
                >✕</button>
              </div>
            ))}
            {addTile}
          </div>
          <UploadProgressBar />

          {current !== null && (
            <>
              <div class="sh-hlc__order">
                <span class="sh-muted">
                  {t('highlight.composer.frame_of', { n: String(numberOf(index)), total: String(posted + frames.length) })}
                </span>
                <Button
                  type="button"
                  variant="secondary"
                  disabled={index <= 0}
                  aria-label={t('highlight.composer.move_earlier', { n: String(numberOf(index)) })}
                  onClick={() => move(current.id, index - 1)}
                >
                  <span aria-hidden="true">‹</span> {t('highlight.composer.earlier')}
                </Button>
                <Button
                  type="button"
                  variant="secondary"
                  disabled={index >= frames.length - 1}
                  aria-label={t('highlight.composer.move_later', { n: String(numberOf(index)) })}
                  onClick={() => move(current.id, index + 1)}
                >
                  {t('highlight.composer.later')} <span aria-hidden="true">›</span>
                </Button>
              </div>
              {frames.length > 1 && (
                <p class="sh-muted sh-hlc__hint">{t('highlight.composer.reorder_hint')}</p>
              )}

              <label class="sh-hlc__caption">
                <span class="sh-hlc__label">
                  {t('highlight.composer.caption_for', { n: String(numberOf(index)) })}
                </span>
                <span class="sh-hlc__caption-row">
                  <textarea
                    ref={captionRef}
                    rows={2}
                    dir="auto"
                    maxLength={CAPTION_MAX}
                    placeholder={t('highlight.quick.caption_placeholder')}
                    value={current.caption}
                    onInput={e => setCaption((e.target as HTMLTextAreaElement).value)}
                  />
                  <EmojiPickButton
                    openKey={`highlight-frame-${current.id}`}
                    ariaLabel={t('highlight.composer.emoji_aria', { n: String(numberOf(index)) })}
                    onInsert={insertEmoji}
                  />
                </span>
              </label>
            </>
          )}

          {today !== null ? (
            <section class="sh-hlc-lock" aria-labelledby="sh-hlc-lock-title">
              <span class="sh-hlc-lock__icon" aria-hidden="true">🔒</span>
              <div class="sh-hlc-lock__text">
                <p id="sh-hlc-lock-title" class="sh-hlc-lock__who">
                  {t('highlight.lock.goes_to', { who: describeAudience(today.audience_kind, today.audience, nameOf) })}
                </p>
                <p class="sh-muted">
                  {t('highlight.lock.chose_at', { time: timeOf(today.created_at) })}
                  {' '}{lockShareNote(published)}
                </p>
                <Button type="button" variant="secondary" onClick={() => setChanging(true)}>
                  {t('highlight.lock.change')}
                </Button>
              </div>
            </section>
          ) : (
            <fieldset class="sh-hlc__audience">
              <legend class="sh-hlc__label">{t('highlight.composer.audience_title')}</legend>
              <AudiencePicker
                name="sh-hlc-audience"
                value={choice}
                onChange={setChoice}
                households={households}
                people={people}
                ownName={directory.ownName}
                ownCount={ownCount}
                loadError={loadError}
                onRetry={() => void loadAudience()}
                sub={mode => (mode === defaultMode.current && mode !== 'choose'
                  ? t('highlight.composer.default_sub')
                  : undefined)}
              />
              <p class="sh-muted sh-hlc__retention">
                <span aria-hidden="true">🕒 </span>
                {t(isOne(days) ? 'highlight.composer.retention_one' : 'highlight.composer.retention', { n: String(days) })}
              </p>
            </fieldset>
          )}
        </div>
      </div>

      <div class="sh-hlc__bar">
        <span class="sh-hlc__status" aria-live="polite">{statusLine}</span>
        <a href={addBase('/highlights')} class="sh-link sh-tap-link sh-hlc__cancel">{t('common.cancel')}</a>
        <Button
          type="submit"
          loading={submitting}
          disabled={frames.length === 0 || submitting || !complete}
        >
          {today !== null ? t('highlight.composer.add') : t('highlight.composer.share')}
        </Button>
      </div>

      {today !== null && (
        <AudienceChangeSheet
          open={changing}
          highlight={today}
          published={published}
          frames={posted}
          households={households}
          people={people}
          directory={directory}
          ownCount={ownCount}
          loadError={loadError}
          onRetry={() => void loadAudience()}
          onClose={() => setChanging(false)}
          onChanged={(h, stillPublished) => {
            setToday(h)
            setPublished(stillPublished)
            setChanging(false)
            showToast(
              t('highlight.change.done', { who: describeAudience(h.audience_kind, h.audience, nameOf) }),
              'success',
            )
          }}
        />
      )}
    </form>
  )
}
