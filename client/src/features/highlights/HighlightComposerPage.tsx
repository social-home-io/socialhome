/**
 * HighlightComposerPage — pick media + caption + audience and post one
 * or more frames as a single highlight.
 *
 * Match the WhatsApp-Status experience: pick several photos / videos
 * in one shot, write a caption per frame, hit Post. The server's
 * ``POST /api/highlights/frames`` route already creates-or-appends to
 * today's highlight keyed by ``(author_user_id, highlight_date)``, so the
 * submit path just iterates over the staged frames sequentially.
 *
 * The first frame carries the audience (highlight-level); later frames
 * inherit it server-side. Per-frame ``caption_text`` is supported by
 * the schema today; the legacy ``caption_emoji`` field is no longer
 * surfaced in the UI — the per-frame :class:`EmojiPickButton` splices
 * glyphs straight into the caption text.
 */
import { useEffect, useRef } from 'preact/hooks'
import { signal } from '@preact/signals'
import { useLocation } from 'preact-iso'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { EmojiPickButton } from '@/components/EmojiPickButton'
import { MediaDropzone } from '@/components/MediaDropzone'
import { showToast } from '@/components/Toast'
import { UploadProgressBar, uploadWithProgress } from '@/components/UploadProgress'
import { describeUploadError } from '@/utils/uploadErrors'
import { currentUser } from '@/store/auth'
import type { HighlightAudienceKind, HighlightInboxItem } from '@/types'
import { addBase } from '@/baseUrl'
import { t, isOne } from '@/i18n/i18n'

/** A household the audience picker offers — a confirmed social peer. */
interface RemoteHousehold {
  instance_id: string
  display_name: string
}

/** A person the per-person picker offers. ``household_name`` is null
 *  for members of our own household. */
interface ConnectedPerson {
  user_id: string
  display_name: string
  household_name: string | null
}

/** The slice of ``GET /api/friends`` (``routes/friends.py``) the picker
 *  reads. ``households`` is the *social* peer list
 *  (``list_social_instances``) — the same set the highlight outbound
 *  (``HighlightFederationOutbound._resolve_audience``) fans out to, so
 *  a household invited only through a space link is never offered. */
interface FriendsPayload {
  instance: {
    members: { user_id: string, display_name: string, personal_alias?: string | null }[]
  }
  households: {
    instance_id: string
    display_name: string
    members: { user_id: string, display_name: string, personal_alias?: string | null }[]
  }[]
}

/** Flatten ``/api/friends`` into picker rows. The author is left out of
 *  the people list — they always see their own highlight. */
export function audienceFromFriends(
  payload: FriendsPayload,
  selfUserId: string | null | undefined,
): { households: RemoteHousehold[], people: ConnectedPerson[] } {
  const households: RemoteHousehold[] = (payload.households ?? []).map(h => ({
    instance_id: h.instance_id, display_name: h.display_name,
  }))
  const people: ConnectedPerson[] = []
  for (const m of payload.instance?.members ?? []) {
    if (m.user_id === selfUserId) continue
    people.push({
      user_id: m.user_id,
      display_name: m.personal_alias || m.display_name,
      household_name: null,
    })
  }
  for (const h of payload.households ?? []) {
    for (const m of h.members ?? []) {
      people.push({
        user_id: m.user_id,
        display_name: m.personal_alias || m.display_name,
        household_name: h.display_name,
      })
    }
  }
  return { households, people }
}

/** A media file the user has uploaded but not yet posted. Each entry
 *  becomes one frame on submit. */
interface StagedFrame {
  /** Local-only id for the keyed render + ref map. */
  id:       string
  /** Canonical ``/api/media/{filename}`` URL that lands in the post. */
  url:      string
  /** Short-lived signed URL for the local ``<img>`` / ``<video>`` preview. */
  preview:  string
  type:     'image' | 'video'
  /** Original filename — used for the remove × aria-label. */
  name:     string
  /** Per-frame caption (140 chars max). */
  caption:  string
}

const CAPTION_MAX = 140
/** Server-enforced cap (``MAX_FRAMES_PER_HIGHLIGHT`` in
 *  ``socialhome/services/highlight_service.py``). Surfaced here so a
 *  multi-pick can refuse the overflow before the upload starts. */
const MAX_FRAMES_PER_HIGHLIGHT = 30

const stagedFrames = signal<StagedFrame[]>([])
/** Frames already posted on today's highlight by the current user — read
 *  once on mount so the picker can refuse over the daily cap. */
const mineToday = signal<number>(0)
const audienceKind = signal<HighlightAudienceKind>('all_paired')
const audienceIds = signal<string[]>([])
const submitting = signal<boolean>(false)
const advanced = signal<boolean>(false)
const households = signal<RemoteHousehold[]>([])
const people = signal<ConnectedPerson[]>([])
/** Why the household / people lists couldn't load — shown in the picker
 *  with a Retry so an outage doesn't read as "no connections". */
const audienceError = signal<string | null>(null)

async function loadAudience(): Promise<void> {
  try {
    const payload = await api.get('/api/friends') as FriendsPayload
    const out = audienceFromFriends(payload, currentUser.value?.user_id)
    households.value = out.households
    people.value = out.people
    audienceError.value = null
  } catch (err: unknown) {
    households.value = []
    people.value = []
    audienceError.value = (err as Error)?.message ?? String(err)
  }
}


function AudienceLoadError() {
  return (
    <div role="alert">
      <p class="sh-muted">
        {t('highlight.composer.audience_failed', { error: audienceError.value ?? '' })}
      </p>
      <Button type="button" variant="secondary" onClick={() => void loadAudience()}>
        {t('common.retry')}
      </Button>
    </div>
  )
}

export default function HighlightComposerPage() {
  const loc = useLocation()
  // Per-frame textarea refs so the emoji picker can splice at the
  // caret rather than the end. Keyed by the local-only ``StagedFrame.id``.
  const captionRefs = useRef(new Map<string, HTMLTextAreaElement>())

  useEffect(() => {
    // Reset state on mount.
    stagedFrames.value = []
    audienceKind.value = 'all_paired'
    audienceIds.value = []
    advanced.value = false
    mineToday.value = 0

    // Connected households + people for the picker, from one
    // ``/api/friends`` read. A failure is surfaced in the picker; the
    // default "all connected households" audience still works.
    void loadAudience()

    // Today's frame count for the cap. Authors can post up to
    // ``MAX_FRAMES_PER_HIGHLIGHT`` frames per day; the server returns
    // ``HIGHLIGHT_FRAME_LIMIT`` past that. Surface the number locally so
    // multi-pick refuses the overflow without an upload round-trip.
    api.get('/api/highlights').then((rows: HighlightInboxItem[]) => {
      const me = currentUser.value?.user_id
      if (!me) return
      const todayKey = new Date().toISOString().slice(0, 10)
      const mine = (rows ?? []).find(
        s => s.highlight.author_user_id === me && s.highlight.highlight_date === todayKey,
      )
      mineToday.value = mine?.frames.length ?? 0
    }).catch(() => {})
  }, [])

  const framesLeft = (): number => Math.max(
    0, MAX_FRAMES_PER_HIGHLIGHT - mineToday.value - stagedFrames.value.length,
  )

  const uploadOne = async (file: File): Promise<StagedFrame | null> => {
    try {
      const result = await uploadWithProgress(file)
      return {
        id:      crypto.randomUUID(),
        url:     result.url,
        preview: result.signed_url,
        type:    file.type.startsWith('video/') ? 'video' : 'image',
        name:    file.name,
        caption: '',
      }
    } catch (err: unknown) {
      showToast(describeUploadError(err, { file }), 'error')
      return null
    }
  }

  const acceptFiles = async (files: File[]): Promise<void> => {
    if (files.length === 0) return
    const left = framesLeft()
    if (left <= 0) {
      showToast(
        t('highlight.composer.limit_reached', { n: String(MAX_FRAMES_PER_HIGHLIGHT) }),
        'error',
      )
      return
    }
    const accepted = files.slice(0, left)
    if (files.length > accepted.length) {
      showToast(
        t(isOne(left) ? 'highlight.composer.more_left_one' : 'highlight.composer.more_left', { n: String(left) }),
        'info',
      )
    }
    for (const f of accepted) {
      const staged = await uploadOne(f)
      if (staged) {
        stagedFrames.value = [...stagedFrames.value, staged]
      }
    }
  }

  const removeFrame = (id: string) => {
    stagedFrames.value = stagedFrames.value.filter(f => f.id !== id)
    captionRefs.current.delete(id)
  }

  const updateCaption = (id: string, value: string) => {
    stagedFrames.value = stagedFrames.value.map(f =>
      f.id === id ? { ...f, caption: value.slice(0, CAPTION_MAX) } : f,
    )
  }

  const spliceEmojiIntoFrame = (frameId: string, emoji: string) => {
    const ta = captionRefs.current.get(frameId)
    const idx = stagedFrames.value.findIndex(f => f.id === frameId)
    if (idx < 0) return
    const cur = stagedFrames.value[idx].caption
    const start = ta?.selectionStart ?? cur.length
    const end   = ta?.selectionEnd   ?? start
    const next = (cur.slice(0, start) + emoji + cur.slice(end)).slice(0, CAPTION_MAX)
    stagedFrames.value = stagedFrames.value.map((f, i) =>
      i === idx ? { ...f, caption: next } : f,
    )
    if (ta) {
      requestAnimationFrame(() => {
        ta.focus()
        const pos = (cur.slice(0, start) + emoji).length
        ta.setSelectionRange(pos, pos)
      })
    }
  }

  const toggleId = (id: string) => {
    const set = new Set(audienceIds.value)
    if (set.has(id)) set.delete(id); else set.add(id)
    audienceIds.value = Array.from(set)
  }

  const submit = async (e: Event) => {
    e.preventDefault()
    const frames = stagedFrames.value
    if (frames.length === 0 || submitting.value) return
    submitting.value = true
    let lastHighlightId: string | null = null
    for (let i = 0; i < frames.length; i++) {
      const f = frames[i]
      try {
        const r = await api.post('/api/highlights/frames', {
          media_url:    f.url,
          frame_type:   f.type,
          caption_text: f.caption.trim() || null,
          // Audience rides on the first frame only — later frames
          // inherit the highlight-level audience server-side.
          audience_kind: i === 0 ? audienceKind.value : undefined,
          audience:      i === 0 && audienceKind.value !== 'all_paired'
            ? audienceIds.value : [],
        }) as { highlight: { id: string }; frame: { id: string } }
        lastHighlightId = r.highlight.id
      } catch (err: unknown) {
        showToast(
          t('highlight.composer.frame_failed', { n: String(i + 1), error: String((err as Error)?.message ?? err) }),
          'error',
        )
        // Stop the loop with already-posted frames intact; the user
        // can navigate to the partial highlight and decide.
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
      submitting.value = false
    }
  }

  const left = framesLeft()
  const canPickMore = left > 0

  return (
    <form class="sh-form sh-highlight-composer" onSubmit={submit}>
      <header class="sh-highlights-header">
        <h2>{t('highlight.composer.title')}</h2>
        <a href={addBase('/highlights')} class="sh-link">{t('common.cancel')}</a>
      </header>

      <MediaDropzone
        multiple
        accept="image/*,video/*"
        disabled={!canPickMore}
        hint={t('highlight.composer.drop_hint')}
        pickLabel={t('highlight.quick.pick_label')}
        draggingHint={t('highlight.composer.dragging')}
        onFiles={acceptFiles}
      />
      <UploadProgressBar />
      <p class="sh-muted" style={{ fontSize: 'var(--sh-font-size-xs)' }}>
        {canPickMore
          ? t(isOne(left) ? 'highlight.composer.up_to_left_one' : 'highlight.composer.up_to_left', { n: String(left) })
          : t('highlight.composer.limit_reached', { n: String(MAX_FRAMES_PER_HIGHLIGHT) })}
      </p>

      {stagedFrames.value.length > 0 && (
        <ol class="sh-highlight-frames">
          {stagedFrames.value.map((f, i) => (
            <li key={f.id} class="sh-highlight-frame">
              <div class={`sh-highlight-frame-thumb${f.type === 'video' ? ' sh-highlight-frame-thumb--video' : ''}`}>
                {f.type === 'image' ? (
                  <img src={f.preview} alt="" />
                ) : (
                  <video src={f.preview} controls muted preload="metadata" />
                )}
              </div>
              <div class="sh-highlight-frame-body">
                <div class="sh-highlight-frame-meta">
                  <strong>{t('highlight.composer.frame_of', { n: String(i + 1), total: String(stagedFrames.value.length) })}</strong>
                  <button
                    type="button"
                    class="sh-link sh-highlight-frame-remove"
                    aria-label={t('composer.remove_image', { name: f.name })}
                    onClick={() => removeFrame(f.id)}
                  >✕</button>
                </div>
                <div class="sh-highlight-frame-caption-row">
                  <textarea
                    ref={(el: HTMLTextAreaElement | null) => {
                      if (el) captionRefs.current.set(f.id, el)
                      else captionRefs.current.delete(f.id)
                    }}
                    rows={2}
                    maxLength={CAPTION_MAX}
                    placeholder={t('highlight.quick.caption_placeholder')}
                    value={f.caption}
                    onInput={e => updateCaption(
                      f.id, (e.target as HTMLTextAreaElement).value,
                    )}
                  />
                  <EmojiPickButton
                    openKey={`highlight-frame-${f.id}`}
                    ariaLabel={t('highlight.composer.emoji_aria', { n: String(i + 1) })}
                    onInsert={(emoji) => spliceEmojiIntoFrame(f.id, emoji)}
                  />
                </div>
              </div>
            </li>
          ))}
        </ol>
      )}

      <fieldset class="sh-highlight-composer-audience">
        <legend class="sh-muted">{t('highlight.composer.audience')}</legend>
        <label class="sh-highlight-composer-audience-row">
          <input
            type="radio"
            name="audience"
            checked={audienceKind.value === 'all_paired'}
            onChange={() => {
              audienceKind.value = 'all_paired'
              audienceIds.value = []
            }}
          />
          {t('highlight.composer.audience_all')}
        </label>
        <label class="sh-highlight-composer-audience-row">
          <input
            type="radio"
            name="audience"
            checked={audienceKind.value === 'households'}
            onChange={() => {
              audienceKind.value = 'households'
              audienceIds.value = []
            }}
          />
          {t('highlight.composer.audience_households')}
        </label>
        {audienceKind.value === 'households' && (
          <div class="sh-highlight-composer-audience-list">
            {audienceError.value && <AudienceLoadError />}
            {!audienceError.value && households.value.length === 0 && (
              <p class="sh-muted">{t('highlight.composer.no_households')}</p>
            )}
            {households.value.map(h => (
              <label key={h.instance_id} class="sh-highlight-composer-audience-row">
                <input
                  type="checkbox"
                  checked={audienceIds.value.includes(h.instance_id)}
                  onChange={() => toggleId(h.instance_id)}
                />
                {h.display_name}
              </label>
            ))}
          </div>
        )}
        <button
          type="button"
          class="sh-link sh-highlight-composer-advanced-toggle"
          onClick={() => { advanced.value = !advanced.value }}
        >
          {advanced.value ? t('highlight.composer.hide_people') : t('highlight.composer.show_people')}
        </button>
        {advanced.value && (
          <>
            <label class="sh-highlight-composer-audience-row">
              <input
                type="radio"
                name="audience"
                checked={audienceKind.value === 'users'}
                onChange={() => {
                  audienceKind.value = 'users'
                  audienceIds.value = []
                }}
              />
              {t('highlight.composer.audience_people')}
            </label>
            {audienceKind.value === 'users' && (
              <div class="sh-highlight-composer-audience-list">
                {audienceError.value && <AudienceLoadError />}
                {!audienceError.value && people.value.length === 0 && (
                  <p class="sh-muted">{t('highlight.composer.no_people')}</p>
                )}
                {people.value.map(p => (
                  <label key={p.user_id} class="sh-highlight-composer-audience-row">
                    <input
                      type="checkbox"
                      checked={audienceIds.value.includes(p.user_id)}
                      onChange={() => toggleId(p.user_id)}
                    />
                    {p.display_name}
                    {p.household_name && (
                      <span class="sh-muted"> · {p.household_name}</span>
                    )}
                  </label>
                ))}
              </div>
            )}
          </>
        )}
      </fieldset>

      <div class="sh-form-actions">
        <Button
          type="submit"
          loading={submitting.value}
          disabled={stagedFrames.value.length === 0 || submitting.value}
        >
          {stagedFrames.value.length > 1
            ? t('highlight.composer.post_frames', { n: String(stagedFrames.value.length) })
            : t('highlight.composer.post')}
        </Button>
      </div>
    </form>
  )
}
