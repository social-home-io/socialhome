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
import { LocationPicker, type LocationDraft } from './LocationPicker'
import { MarkdownToolbar } from './MarkdownToolbar'
import { PollBuilder, type PollDraft } from './PollUI'
import { ScheduleBuilder, type ScheduleDraft } from './ScheduleBuilder'
import { SttButton } from './SttButton'
import { showToast } from './Toast'
import { UploadProgressBar, uploadWithProgress } from './UploadProgress'
import { currentUser } from '@/store/auth'

const MAX_LENGTH = 5000

/** Extra fields the composer hands back alongside type/content/mediaUrl.
 *  Currently only carries `location` for the location-share post type. */
export interface ComposerExtras {
  location?: LocationDraft
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
  const [mediaUrl, setMediaUrl] = useState<string | null>(null)
  const [mediaName, setMediaName] = useState<string | null>(null)
  const [dragActive, setDragActive] = useState(false)

  const resetAttached = () => {
    setMediaUrl(null)
    setMediaName(null)
  }

  const acceptFile = async (file: File) => {
    const inferred = inferTypeFromFile(file)
    postType.value = inferred
    try {
      const url = await uploadWithProgress(file)
      setMediaUrl(url)
      setMediaName(file.name)
      showToast(`Attached: ${file.name}`, 'success')
    } catch (err: unknown) {
      showToast(
        `Upload failed: ${(err as Error).message ?? err}`, 'error',
      )
    }
  }

  const onFilePicked = async (e: Event) => {
    const input = e.target as HTMLInputElement
    const file = input.files?.[0]
    if (!file) return
    await acceptFile(file)
    input.value = ''  // allow re-selecting the same file later
  }

  const onDrop = async (e: DragEvent) => {
    e.preventDefault()
    setDragActive(false)
    const file = e.dataTransfer?.files?.[0]
    if (file) await acceptFile(file)
  }

  const onDragOver = (e: DragEvent) => {
    e.preventDefault()
    if (!dragActive) setDragActive(true)
  }

  const onDragLeave = (e: DragEvent) => {
    if (e.currentTarget === e.target) setDragActive(false)
  }

  const handleSubmit = async (e: Event) => {
    e.preventDefault()
    if (submitting.value || overLimit) return
    // Poll/schedule carry their content in a builder modal; media types
    // carry it in the upload. Only "text" requires a body in the
    // textarea before we'll submit.
    const needsText = !typeAcceptsMedia(postType.value) && !typeUsesBuilder(postType.value)
    const hasBody = content.value.trim().length > 0
    if (needsText && !hasBody) return
    if (typeAcceptsMedia(postType.value) && !mediaUrl && !hasBody) return
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
      const newPostId = await onSubmit(
        postType.value,
        content.value,
        mediaUrl ?? undefined,
        pendingLocation ? { location: pendingLocation } : undefined,
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
            onInput={(e) => content.value = (e.target as HTMLTextAreaElement).value}
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
      {showMediaAttach && mediaUrl && (
        <div class="sh-composer-attachment">
          {postType.value === 'image' && (
            <img class="sh-composer-preview" src={mediaUrl} alt={mediaName ?? ''} />
          )}
          {postType.value === 'video' && (
            <video class="sh-composer-preview" src={mediaUrl} controls muted />
          )}
          {postType.value === 'file' && (
            <span class="sh-composer-file-pill">📄 {mediaName}</span>
          )}
          <button type="button" class="sh-composer-remove-attach"
                  aria-label="Remove attachment"
                  onClick={resetAttached}>✕</button>
        </div>
      )}
      {showMediaAttach && !mediaUrl && (
        <div class="sh-composer-dropzone">
          <span>
            {dragActive
              ? `Drop to attach ${postType.value}`
              : `Drag a ${postType.value} here, or`}
          </span>
          <button type="button" class="sh-link"
                  onClick={() => fileInputRef.current?.click()}>
            choose a file…
          </button>
          <input ref={fileInputRef} type="file"
                 accept={postType.value === 'image' ? 'image/*'
                       : postType.value === 'video' ? 'video/*' : ''}
                 style={{ display: 'none' }}
                 onChange={onFilePicked} />
        </div>
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
        <SttButton onText={(t) => {
          const sep = content.value && !/\s$/.test(content.value) ? ' ' : ''
          content.value = (content.value + sep + t).slice(0, MAX_LENGTH)
        }} />
        {context && <span class="sh-context-badge">🌐 {context}</span>}
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
    </form>
  )
}
