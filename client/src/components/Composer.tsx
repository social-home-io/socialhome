/**
 * Composer — post creation surface (§23.44).
 * Type picker, auto-grow textarea, context selector, submit.
 * Image / video / file uploads are done first (multipart → /api/media/upload),
 * then the resulting URL is passed through to ``onSubmit`` so the caller can
 * attach it to the post create call.
 */
import { signal } from '@preact/signals'
import { useRef, useState } from 'preact/hooks'
import { api } from '@/api'
import { Avatar } from './Avatar'
import { Button } from './Button'
import {
  EmojiAutocomplete,
  checkForEmojiTrigger,
  closeEmojiAutocomplete,
  handleEmojiAutocompleteKey,
} from './EmojiAutocomplete'
import { EmojiPickButton } from './EmojiPickButton'
import { LocationPicker, type LocationDraft } from './LocationPicker'
import { MarkdownToolbar } from './MarkdownToolbar'
import { PollBuilder, type PollDraft } from './PollUI'
import { ScheduleBuilder, type ScheduleDraft } from './ScheduleBuilder'
import { SttButton } from './SttButton'
import { showToast } from './Toast'
import { UploadProgressBar, uploadWithProgress } from './UploadProgress'
import { currentUser } from '@/store/auth'

const MAX_LENGTH = 5000
const MAX_IMAGES = 5

interface ImageEntry {
  /** Canonical URL stored in ``image_urls`` and persisted on the post. */
  url: string
  /** Short-lived signed URL used only for the local preview ``<img>``. */
  preview: string
  /** Original filename — only kept for the toast / accessibility text. */
  name: string
}

/** Extra fields the composer hands back alongside type/content/mediaUrl.
 *  Carries the location draft (location-share post) and image-post
 *  multi-image URL list. ``mediaUrl`` (the third positional arg of
 *  ``onSubmit``) stays for video / file posts only. */
export interface ComposerExtras {
  location?: LocationDraft
  /** 1..``MAX_IMAGES`` canonical (unsigned) URLs for an image post. */
  imageUrls?: string[]
}

interface ComposerProps {
  onSubmit: (
    type: string,
    content: string,
    mediaUrl?: string,
    extras?: ComposerExtras,
  ) => Promise<string | void>
  context?: string
  placeholder?: string
  /** Set when the composer lives inside a space feed — the schedule
   * poll attaches as a space-scoped poll so finalize → space calendar
   * auto-create fires. */
  spaceId?: string
}

const content = signal('')
const postType = signal('text')
const submitting = signal(false)

// bazaar listings are space-scoped — they only make sense inside a
// space feed where buyers see them. The household feed composer omits
// the icon so the option doesn't dangle there with no working flow.
const TYPE_ICONS_HOUSEHOLD: Record<string, string> = {
  text: '🔤', image: '📷', video: '🎬', file: '📄',
  poll: '📊', schedule: '📅', location: '📍',
}
const TYPE_ICONS_SPACE: Record<string, string> = {
  ...TYPE_ICONS_HOUSEHOLD,
  bazaar: '🛍',
}

const MEDIA_TYPES = new Set(['image', 'video', 'file'])
// Types whose body lives in a dedicated builder modal (poll question,
// schedule slots, location pin) — the textarea stays available for an
// optional caption (location, especially), but the type-specific data
// rides on a draft populated by the modal.
const BUILDER_TYPES = new Set(['poll', 'schedule', 'location'])
// Types whose builder fully replaces the textarea — poll/schedule have
// their question / slots inside the modal so a redundant "What's on
// your mind" field next to the modal trigger is confusing. Location
// keeps the textarea visible so the user can add an optional caption
// alongside the pin.
const TEXTAREA_HIDDEN_FOR = new Set(['poll', 'schedule'])

function typeAcceptsMedia(t: string): boolean { return MEDIA_TYPES.has(t) }
function typeUsesBuilder(t: string): boolean { return BUILDER_TYPES.has(t) }
function typeHidesTextarea(t: string): boolean { return TEXTAREA_HIDDEN_FOR.has(t) }

function inferTypeFromFile(file: File): 'image' | 'video' | 'file' {
  if (file.type.startsWith('image/')) return 'image'
  if (file.type.startsWith('video/')) return 'video'
  return 'file'
}

export function Composer({ onSubmit, context, placeholder, spaceId }: ComposerProps) {
  const user = currentUser.value
  const TYPE_ICONS = spaceId ? TYPE_ICONS_SPACE : TYPE_ICONS_HOUSEHOLD
  const charCount = content.value.length
  const showCount = charCount > MAX_LENGTH * 0.8
  const overLimit = charCount > MAX_LENGTH
  const textareaRef = useRef<HTMLTextAreaElement | null>(null)
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  const [scheduleOpen, setScheduleOpen] = useState(false)
  const [pendingSchedule, setPendingSchedule] = useState<ScheduleDraft | null>(null)
  const [pollOpen, setPollOpen] = useState(false)
  const [pendingPoll, setPendingPoll] = useState<PollDraft | null>(null)
  const [locationOpen, setLocationOpen] = useState(false)
  const [pendingLocation, setPendingLocation] = useState<LocationDraft | null>(null)
  // Single-URL slot used by ``video`` / ``file`` posts.
  const [mediaUrl, setMediaUrl] = useState<string | null>(null)
  // Short-lived signed URL used only for the video-preview ``<video>``.
  const [mediaPreviewUrl, setMediaPreviewUrl] = useState<string | null>(null)
  const [mediaName, setMediaName] = useState<string | null>(null)
  // Multi-image slot used by ``image`` posts (1 to ``MAX_IMAGES``).
  // Each entry carries the canonical URL we send on create and the
  // signed preview URL we drop into the local thumbnail.
  const [images, setImages] = useState<ImageEntry[]>([])
  const [dragActive, setDragActive] = useState(false)

  const resetAttached = () => {
    setMediaUrl(null)
    setMediaPreviewUrl(null)
    setMediaName(null)
    setImages([])
  }

  /** Upload one file and route it into the right slot:
   *  ``image`` → push onto the ``images`` list (capped),
   *  ``video`` / ``file`` → fill the single ``mediaUrl`` slot. */
  const acceptFile = async (file: File) => {
    const inferred = inferTypeFromFile(file)
    postType.value = inferred
    try {
      const result = await uploadWithProgress(file)
      if (inferred === 'image') {
        setImages((prev) => {
          if (prev.length >= MAX_IMAGES) return prev
          return [
            ...prev,
            { url: result.url, preview: result.signed_url, name: file.name },
          ]
        })
      } else {
        setMediaUrl(result.url)
        setMediaPreviewUrl(result.signed_url)
        setMediaName(file.name)
      }
      showToast(`Attached: ${file.name}`, 'success')
    } catch (err: unknown) {
      showToast(
        `Upload failed: ${(err as Error).message ?? err}`, 'error',
      )
    }
  }

  /** Upload every file in the iterable, capped collectively at
   *  ``MAX_IMAGES`` for image posts. Video / file pickers only ever
   *  pass a single file (the ``<input>`` for those types is single-
   *  select), so the cap matters mainly for the image multi-select. */
  const acceptFiles = async (files: Iterable<File>) => {
    for (const f of files) {
      const inferred = inferTypeFromFile(f)
      // Stop early for image posts that already filled the cap; this
      // matches the UX in BazaarCreateDialog.
      if (
        inferred === 'image'
        && (images.length >= MAX_IMAGES)
      ) {
        break
      }
      await acceptFile(f)
    }
  }

  const removeImage = (url: string) => {
    setImages((prev) => prev.filter((e) => e.url !== url))
  }

  const onFilePicked = async (e: Event) => {
    const input = e.target as HTMLInputElement
    const files = Array.from(input.files ?? [])
    if (files.length === 0) return
    await acceptFiles(files)
    input.value = ''  // allow re-selecting the same file later
  }

  const onDrop = async (e: DragEvent) => {
    e.preventDefault()
    setDragActive(false)
    const dropped = Array.from(e.dataTransfer?.files ?? [])
    if (dropped.length > 0) await acceptFiles(dropped)
  }

  const onDragOver = (e: DragEvent) => {
    e.preventDefault()
    if (!dragActive) setDragActive(true)
  }

  const onDragLeave = (e: DragEvent) => {
    if (e.currentTarget === e.target) setDragActive(false)
  }

  /** Replace ``content[start:end]`` with ``emoji`` and restore the
   *  caret immediately after the inserted glyph. Used by both the
   *  ``:foo`` autocomplete (range = the typed token) and the picker
   *  button (range = caret position). */
  const spliceEmoji = (emoji: string, range: [number, number]) => {
    const [start, end] = range
    const before = content.value.slice(0, start)
    const after = content.value.slice(end)
    content.value = (before + emoji + after).slice(0, MAX_LENGTH)
    requestAnimationFrame(() => {
      const ta = textareaRef.current
      if (ta) {
        const pos = (before + emoji).length
        ta.focus()
        ta.setSelectionRange(pos, pos)
      }
    })
  }
  const insertEmojiAtCursor = (emoji: string) => {
    const ta = textareaRef.current
    const pos = ta ? ta.selectionStart : content.value.length
    spliceEmoji(emoji, [pos, pos])
  }

  const handleSubmit = async (e: Event) => {
    e.preventDefault()
    if (submitting.value || overLimit) return
    // Poll/schedule carry their content in a builder modal; media types
    // carry it in the upload. Only "text" requires a body in the
    // textarea before we'll submit.
    const needsText = !typeAcceptsMedia(postType.value) && !typeUsesBuilder(postType.value)
    const hasBody = content.value.trim().length > 0
    const isImage = postType.value === 'image'
    if (needsText && !hasBody) return
    // Image posts need at least one uploaded image; video/file posts
    // need ``mediaUrl`` set. A caption alone isn't enough either way.
    if (isImage && images.length === 0 && !hasBody) return
    if (typeAcceptsMedia(postType.value) && !isImage && !mediaUrl && !hasBody) return
    if (postType.value === 'schedule' && !pendingSchedule) {
      setScheduleOpen(true)
      return
    }
    if (postType.value === 'poll' && !pendingPoll) {
      setPollOpen(true)
      return
    }
    if (postType.value === 'location' && !pendingLocation) {
      setLocationOpen(true)
      return
    }
    submitting.value = true
    try {
      const extras: ComposerExtras = {}
      if (pendingLocation) extras.location = pendingLocation
      if (isImage && images.length > 0) {
        extras.imageUrls = images.map((e) => e.url)
      }
      const newPostId = await onSubmit(
        postType.value,
        content.value,
        // Image posts route their URLs through ``extras.imageUrls``;
        // ``mediaUrl`` here is the single-URL slot used only by
        // video/file posts.
        isImage ? undefined : (mediaUrl ?? undefined),
        Object.keys(extras).length > 0 ? extras : undefined,
      )
      if (postType.value === 'schedule' && pendingSchedule && newPostId) {
        const base = spaceId
          ? `/api/spaces/${spaceId}/posts/${newPostId}/schedule-poll`
          : `/api/posts/${newPostId}/schedule-poll`
        try {
          await api.post(base, {
            title: pendingSchedule.title,
            slots: pendingSchedule.slots,
          })
        } catch (err: unknown) {
          showToast(
            `Schedule poll failed: ${(err as Error)?.message ?? err}`,
            'error',
          )
        }
      }
      if (postType.value === 'poll' && pendingPoll && newPostId) {
        const pollUrl = spaceId
          ? `/api/spaces/${spaceId}/posts/${newPostId}/poll`
          : `/api/posts/${newPostId}/poll`
        try {
          await api.post(pollUrl, {
            question:       pendingPoll.question,
            options:        pendingPoll.options,
            allow_multiple: pendingPoll.allow_multiple,
            closes_at:      pendingPoll.closes_at,
          })
        } catch (err: unknown) {
          showToast(
            `Poll creation failed: ${(err as Error)?.message ?? err}`,
            'error',
          )
        }
      }
      content.value = ''
      resetAttached()
      setPendingSchedule(null)
      setPendingPoll(null)
      setPendingLocation(null)
    } finally {
      submitting.value = false
    }
  }

  const showMediaAttach = typeAcceptsMedia(postType.value)

  return (
    <form class={`sh-composer ${dragActive ? 'sh-composer--dragging' : ''}`}
          onSubmit={handleSubmit}
          onDrop={onDrop}
          onDragOver={onDragOver}
          onDragLeave={onDragLeave}>
      <div class="sh-composer-header">
        <Avatar name={user?.display_name || '?'} src={user?.picture_url} size={32} />
        <div class="sh-composer-type-picker">
          {Object.entries(TYPE_ICONS).map(([type, icon]) => (
            <button key={type} type="button"
              class={`sh-type-btn ${postType.value === type ? 'sh-type-btn--active' : ''}`}
              onClick={() => {
                postType.value = type
                if (!typeAcceptsMedia(type)) resetAttached()
              }}
              title={type}>
              {icon}
            </button>
          ))}
        </div>
      </div>
      {postType.value === 'text' && (
        <MarkdownToolbar
          textareaRef={textareaRef}
          onUpdate={(newText) => { content.value = newText.slice(0, MAX_LENGTH) }}
        />
      )}
      {!typeHidesTextarea(postType.value) && (
        <>
          <textarea
            ref={textareaRef}
            class="sh-composer-input"
            placeholder={placeholder || "What's on your mind?"}
            value={content.value}
            onInput={(e) => {
              const t = e.target as HTMLTextAreaElement
              content.value = t.value
              checkForEmojiTrigger(t.value, t.selectionStart ?? 0, t, spliceEmoji)
            }}
            onKeyDown={(e) => {
              if (handleEmojiAutocompleteKey(e)) {
                e.preventDefault()
              }
            }}
            onBlur={() => closeEmojiAutocomplete()}
            rows={3}
            maxLength={MAX_LENGTH}
          />
          {showCount && (
            <div class={`sh-char-count ${overLimit ? 'sh-char-count--over' : ''}`}>
              {charCount}/{MAX_LENGTH}
            </div>
          )}
        </>
      )}
      {/* Image post: multi-image grid preview + "Add more" tile up to
          ``MAX_IMAGES``. Each tile has a ✕ to drop just that image
          without resetting the whole post. */}
      {postType.value === 'image' && images.length > 0 && (
        <div class="sh-composer-images">
          {images.map((img) => (
            <div key={img.url} class="sh-composer-image-tile">
              <img src={img.preview} alt={img.name} />
              <button type="button" class="sh-composer-remove-attach"
                      aria-label={`Remove ${img.name}`}
                      onClick={() => removeImage(img.url)}>✕</button>
            </div>
          ))}
          {images.length < MAX_IMAGES && (
            <button type="button" class="sh-composer-image-add"
                    onClick={() => fileInputRef.current?.click()}>
              <span aria-hidden="true">＋</span>
              <span>Add photo</span>
            </button>
          )}
        </div>
      )}
      {/* Video / file: keep the single-attachment tile + remove button. */}
      {showMediaAttach && postType.value !== 'image' && mediaUrl && (
        <div class="sh-composer-attachment">
          {postType.value === 'video' && (
            <video class="sh-composer-preview" src={mediaPreviewUrl ?? mediaUrl} controls muted />
          )}
          {postType.value === 'file' && (
            <span class="sh-composer-file-pill">📄 {mediaName}</span>
          )}
          <button type="button" class="sh-composer-remove-attach"
                  aria-label="Remove attachment"
                  onClick={resetAttached}>✕</button>
        </div>
      )}
      {/* Empty-state dropzone: shown when no images yet (image post) or
          no media yet (video/file post). The ``<input>`` carries
          ``multiple`` for image posts, single for the rest. */}
      {showMediaAttach
        && (postType.value === 'image' ? images.length === 0 : !mediaUrl) && (
        <div class="sh-composer-dropzone">
          <span>
            {dragActive
              ? `Drop to attach ${postType.value}`
              : postType.value === 'image'
                ? 'Drag photos here, or'
                : `Drag a ${postType.value} here, or`}
          </span>
          <button type="button" class="sh-link"
                  onClick={() => fileInputRef.current?.click()}>
            {postType.value === 'image' ? 'choose photos…' : 'choose a file…'}
          </button>
          <input ref={fileInputRef} type="file"
                 multiple={postType.value === 'image'}
                 accept={postType.value === 'image' ? 'image/*'
                       : postType.value === 'video' ? 'video/*' : ''}
                 style={{ display: 'none' }}
                 onChange={onFilePicked} />
        </div>
      )}
      {/* Hidden picker reused by the "Add photo" tile so a user can
          extend an existing image post without re-rendering the
          dropzone path. The ``<input>`` above only renders in the
          empty state, so we mount a second one here for the
          fill-with-more case. */}
      {postType.value === 'image' && images.length > 0
        && images.length < MAX_IMAGES && (
        <input ref={fileInputRef} type="file" multiple accept="image/*"
               style={{ display: 'none' }}
               onChange={onFilePicked} />
      )}
      <UploadProgressBar />
      {postType.value === 'schedule' && pendingSchedule && (
        <div class="sh-muted" style={{ fontSize: 'var(--sh-font-size-xs)' }}>
          📅 {pendingSchedule.slots.length} time
          {pendingSchedule.slots.length === 1 ? '' : 's'} proposed
          {' — '}
          <button type="button" class="sh-link-button"
                  onClick={() => setScheduleOpen(true)}>
            edit
          </button>
        </div>
      )}
      {postType.value === 'location' && pendingLocation && (
        <div class="sh-muted" style={{ fontSize: 'var(--sh-font-size-xs)' }}>
          📍 {pendingLocation.label
            ? `${pendingLocation.label} (${pendingLocation.lat.toFixed(4)}, ${pendingLocation.lon.toFixed(4)})`
            : `${pendingLocation.lat.toFixed(4)}, ${pendingLocation.lon.toFixed(4)}`}
          {' — '}
          <button type="button" class="sh-link-button"
                  onClick={() => setLocationOpen(true)}>
            edit
          </button>
        </div>
      )}
      {postType.value === 'poll' && pendingPoll && (
        <div class="sh-muted" style={{ fontSize: 'var(--sh-font-size-xs)' }}>
          📊 "{pendingPoll.question}" · {pendingPoll.options.length} options
          {' — '}
          <button type="button" class="sh-link-button"
                  onClick={() => setPollOpen(true)}>
            edit
          </button>
        </div>
      )}
      <div class="sh-composer-footer">
        {context && <span class="sh-context-badge">🌐 {context}</span>}
        {!typeHidesTextarea(postType.value) && (
          <EmojiPickButton
            openKey="composer"
            onInsert={insertEmojiAtCursor}
          />
        )}
        {postType.value === 'text' && (
          <SttButton onText={(t) => {
            const sep = content.value && !/\s$/.test(content.value) ? ' ' : ''
            content.value = (content.value + sep + t).slice(0, MAX_LENGTH)
          }} />
        )}
        <Button type="submit" loading={submitting.value}
          disabled={(() => {
            if (overLimit) return true
            if (typeUsesBuilder(postType.value)) return false  // opens modal or posts
            if (typeAcceptsMedia(postType.value)) {
              return !mediaUrl && !content.value.trim()
            }
            return !content.value.trim()
          })()}>
          {postType.value === 'schedule' && !pendingSchedule
            ? 'Propose times…'
            : postType.value === 'poll' && !pendingPoll
              ? 'Build poll…'
              : postType.value === 'location' && !pendingLocation
                ? 'Pick location…'
                : 'Post'}
        </Button>
      </div>
      <ScheduleBuilder
        open={scheduleOpen}
        onSubmit={(draft) => {
          setPendingSchedule(draft)
          setScheduleOpen(false)
        }}
        onClose={() => setScheduleOpen(false)}
      />
      <PollBuilder
        open={pollOpen}
        onSubmit={(draft) => {
          setPendingPoll(draft)
          setPollOpen(false)
        }}
        onClose={() => setPollOpen(false)}
      />
      <LocationPicker
        open={locationOpen}
        onSubmit={(draft) => {
          setPendingLocation(draft)
          setLocationOpen(false)
        }}
        onClose={() => setLocationOpen(false)}
      />
      <EmojiAutocomplete />
    </form>
  )
}
