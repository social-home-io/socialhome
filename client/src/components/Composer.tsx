/**
 * Composer — post creation surface (§23.44).
 * Type picker, auto-grow textarea, context selector, submit.
 * Image / video / file uploads are done first (multipart → /api/media/upload),
 * then the resulting URL is passed through to ``onSubmit`` so the caller can
 * attach it to the post create call.
 */
import { signal } from '@preact/signals'
import { useEffect, useRef, useState } from 'preact/hooks'
import { api } from '@/api'
import { Avatar } from './Avatar'
import { Button } from './Button'
import { DraftBanner, saveDraft, clearDraft } from './DraftPersistence'
import {
  EmojiAutocomplete,
  checkForEmojiTrigger,
  closeEmojiAutocomplete,
  handleEmojiAutocompleteKey,
} from './EmojiAutocomplete'
import { EmojiPickButton } from './EmojiPickButton'
import {
  MentionAutocomplete,
  checkForMentionTrigger,
  closeMentionAutocomplete,
  handleMentionAutocompleteKey,
  mentionInputAria,
} from './MentionAutocomplete'
import { LinkPreviewCard } from './LinkPreviewCard'
import { LocationPicker, type LocationDraft } from './LocationPicker'
import { MarkdownToolbar } from './MarkdownToolbar'
import { MediaDropzone } from './MediaDropzone'
import { PollBuilder, type PollDraft } from './PollUI'
import { ScheduleBuilder, type ScheduleDraft } from './ScheduleBuilder'
import { SttButton } from './SttButton'
import { supportsStt } from '@/platform'
import { openHighlightPicker } from './HighlightPickerDialog'
import { openBazaarCreate } from './BazaarCreateDialog'
import { showToast } from './Toast'
import { UploadProgressBar, uploadProgress, uploadWithProgress } from './UploadProgress'
import { describeUploadError } from '@/utils/uploadErrors'
import { currentUser } from '@/store/auth'
import { useLinkPreview } from '@/hooks/useLinkPreview'
import { t } from '@/i18n/i18n'

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
  /** The author removed the link card — sent as ``no_link_preview``. */
  noLinkPreview?: boolean
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
  /** Post types the space allows (§23.49 `allowed_post_types`). When
   * provided, the type picker only shows these — so a member never
   * picks a type the space has disabled and eats a 403 on submit.
   * Omitted (household feed, or a remote stub before config lands) →
   * every type stays available. */
  allowedTypes?: readonly string[]
  /** When true (and ``spaceId`` is set) the composer shows a 🛍 shortcut
   * that opens the full Bazaar new-listing dialog (§23.15). Gated on the
   * space's ``bazaar`` feature toggle. */
  bazaarEnabled?: boolean
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
// Bazaar is a first-class space tab (§23.15), not a feed post type — the
// composer surfaces it as a dedicated shortcut button that opens the
// full new-listing dialog, not as a picker entry that mints an empty
// bazaar post. So the space picker is the same set as the household one.
const TYPE_ICONS_SPACE: Record<string, string> = {
  ...TYPE_ICONS_HOUSEHOLD,
}
// Human-readable labels paired with each icon. The picker buttons
// are emoji-only; without these labels, screen-reader users hear
// nothing meaningful when they tab through the row, and ``title``
// alone is unreliable on mobile.
const TYPE_LABELS: Record<string, string> = {
  text:     'Text post',
  image:    'Image post',
  video:    'Video post',
  file:     'File attachment',
  poll:     'Poll',
  schedule: 'Schedule',
  location: 'Location share',
  bazaar:   'Bazaar listing',
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

/** "Pascal Vizeli" → "Pascal".  Single-word names pass through. */
function firstName(displayName: string | undefined | null): string {
  if (!displayName) return ''
  const t = displayName.trim()
  const sp = t.indexOf(' ')
  return sp === -1 ? t : t.slice(0, sp)
}

/** Type-aware placeholder copy for the composer textarea — keeps the
 *  invitation specific to what the author just chose ("Add a
 *  caption…" reads warmer than "What's on your mind?" when the user
 *  is uploading a photo).  The default text-post case folds in the
 *  user's first name when we know it. */
function placeholderFor(type: string, displayName: string | null | undefined): string {
  switch (type) {
    case 'image':    return 'Add a caption…'
    case 'video':    return 'Tell us about this clip…'
    case 'file':     return 'What\'s this file for?'
    case 'location': return 'Where are you headed? (optional caption)'
    case 'bazaar':   return 'Describe what you\'re sharing or selling…'
    case 'text':
    default: {
      const name = firstName(displayName)
      return name
        ? `What's on your mind, ${name}?`
        : 'What\'s on your mind?'
    }
  }
}

function inferTypeFromFile(file: File): 'image' | 'video' | 'file' {
  if (file.type.startsWith('image/')) return 'image'
  if (file.type.startsWith('video/')) return 'video'
  return 'file'
}

export function Composer({ onSubmit, context, placeholder, spaceId, allowedTypes, bazaarEnabled }: ComposerProps) {
  const user = currentUser.value
  const TYPE_ICONS = spaceId ? TYPE_ICONS_SPACE : TYPE_ICONS_HOUSEHOLD
  // Restrict the picker to the space's allowed post types when supplied.
  // ``allowedTypes`` may list types with no composer button (transcript,
  // event, highlight_share) — the intersection with TYPE_ICONS is what
  // renders.
  const typeEntries = Object.entries(TYPE_ICONS).filter(
    ([type]) => !allowedTypes || allowedTypes.includes(type),
  )
  // Keep the selected type valid: if the space disabled whatever is
  // currently selected (e.g. ``text`` off), fall back to the first
  // type that's still offered so the active button + submit stay in sync.
  const allowedKey = allowedTypes ? allowedTypes.join(',') : null
  useEffect(() => {
    if (allowedTypes && !typeEntries.some(([t]) => t === postType.value)) {
      postType.value = typeEntries[0]?.[0] ?? 'text'
    }
  }, [allowedKey])
  const charCount = content.value.length
  const linkPreview = useLinkPreview(postType.value === 'text' ? content.value : '')
  const showCount = charCount > MAX_LENGTH * 0.8
  const overLimit = charCount > MAX_LENGTH
  const textareaRef = useRef<HTMLTextAreaElement | null>(null)
  const [scheduleOpen, setScheduleOpen] = useState(false)
  const [pendingSchedule, setPendingSchedule] = useState<ScheduleDraft | null>(null)
  const [pollOpen, setPollOpen] = useState(false)
  const [pendingPoll, setPendingPoll] = useState<PollDraft | null>(null)
  const [locationOpen, setLocationOpen] = useState(false)
  const [pendingLocation, setPendingLocation] = useState<LocationDraft | null>(null)
  // Markdown toolbar collapses by default — was always-visible and
  // dominated the composer's first impression with a CMS-editor strip
  // before the user had typed anything. The "Aa" button reveals it
  // on demand; once revealed inside a draft it stays open until the
  // composer resets after submit.
  const [formatOpen, setFormatOpen] = useState(false)
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

  // Draft persistence: text content auto-saves to localStorage so a
  // tab close / accidental nav doesn't lose what the user was typing.
  // Keyed by ``context`` so the household feed and a per-space feed
  // don't clobber each other's drafts. Attached media isn't part of
  // the draft (uploads have already landed on the server, but their
  // canonical URLs aren't worth re-attaching after a session reset).
  const draftKey = `composer:${context}`
  // Banner only shows once per mount: if the user dismisses it, we
  // don't want it to come back on the next keystroke as the auto-save
  // re-populates localStorage.
  const [bannerHandled, setBannerHandled] = useState(false)
  const draftSaveTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => {
    if (draftSaveTimer.current !== null) {
      clearTimeout(draftSaveTimer.current)
    }
    draftSaveTimer.current = setTimeout(() => {
      saveDraft(draftKey, content.value)
    }, 800)
    return () => {
      if (draftSaveTimer.current !== null) clearTimeout(draftSaveTimer.current)
    }
  }, [content.value, draftKey])

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
      showToast(describeUploadError(err, { file }), 'error')
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
    if (files.length === 0) {
      // HA Android Companion App WebView occasionally fires ``change``
      // with an empty FileList — the user confirmed a photo in the
      // picker but the WebChromeClient didn't propagate the URI back
      // into ``input.files``. Toast it so they know to retry rather
      // than staring at silent failure.
      showToast(
        'The file picker didn\'t return a photo. Tap "Add photo" again, '
        + 'or drag a file in.',
        'error',
      )
      return
    }
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
      // The server builds the card itself from the text; the composer only
      // says whether the author removed it.
      if (postType.value === 'text' && linkPreview.dismissed) {
        extras.noLinkPreview = true
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
      // Drop the saved draft now that the post is on the wire — the
      // post itself is the canonical record from here on.
      clearDraft(draftKey)
      setBannerHandled(true)
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
      {!bannerHandled && content.value === '' && (
        <DraftBanner
          context={draftKey}
          onRestore={(restored) => {
            content.value = restored
            postType.value = 'text'
            setBannerHandled(true)
          }}
          onDiscard={() => setBannerHandled(true)}
        />
      )}
      <div class="sh-composer-header">
        <Avatar name={user?.display_name || '?'} src={user?.picture_url} size={32} />
        <div class="sh-composer-type-picker">
          {typeEntries.map(([type, icon]) => {
            const label = TYPE_LABELS[type] ?? type
            return (
              <button key={type} type="button"
                class={`sh-type-btn ${postType.value === type ? 'sh-type-btn--active' : ''}`}
                onClick={() => {
                  postType.value = type
                  if (!typeAcceptsMedia(type)) resetAttached()
                }}
                aria-label={label}
                aria-pressed={postType.value === type}
                title={label}>
                {icon}
              </button>
            )
          })}
          {(!allowedTypes || allowedTypes.includes('highlight_share')) && (
            <button
              type="button"
              class="sh-type-btn"
              onClick={() => openHighlightPicker({
                scope: spaceId ? 'space' : 'household',
                spaceId: spaceId ?? null,
              })}
              aria-label="Share a highlight"
              title="Share a highlight"
            >
              ⭕
            </button>
          )}
          {spaceId && bazaarEnabled && (
            <button
              type="button"
              class="sh-type-btn"
              onClick={() => openBazaarCreate(spaceId)}
              aria-label="List something in the Bazaar"
              title="List something in the Bazaar"
            >
              🛍
            </button>
          )}
        </div>
      </div>
      {postType.value === 'text' && formatOpen && (
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
            data-shortcut="composer"
            aria-keyshortcuts="n"
            placeholder={
              placeholder
                ?? placeholderFor(postType.value, user?.display_name)
            }
            value={content.value}
            onInput={(e) => {
              const t = e.target as HTMLTextAreaElement
              content.value = t.value
              checkForEmojiTrigger(t.value, t.selectionStart ?? 0, t, spliceEmoji)
              // Space composer only: ``@`` opens the member picker; the
              // splice is the same text-range replace the emoji pick uses.
              checkForMentionTrigger(
                t.value, t.selectionStart ?? 0, t, spaceId, spliceEmoji,
              )
            }}
            onKeyDown={(e) => {
              if (handleMentionAutocompleteKey(e) || handleEmojiAutocompleteKey(e)) {
                e.preventDefault()
              }
            }}
            onBlur={() => {
              closeEmojiAutocomplete()
              closeMentionAutocomplete()
            }}
            {...(spaceId ? mentionInputAria(textareaRef.current) : {})}
            rows={3}
            maxLength={MAX_LENGTH}
          />
          {showCount && (
            <div
              class={`sh-char-count ${overLimit ? 'sh-char-count--over' : ''}`}
              role="status"
              aria-live="polite"
            >
              {charCount}/{MAX_LENGTH}
              {overLimit && <span class="sh-char-count-over-label"> (over limit)</span>}
            </div>
          )}
          {postType.value === 'text' && linkPreview.url && (
            <div class="sh-composer-link-preview" aria-live="polite">
              {linkPreview.dismissed ? (
                <button type="button" class="sh-link sh-composer-link-preview-restore"
                        onClick={linkPreview.restore}>
                  {t('link_preview.restore')}
                </button>
              ) : linkPreview.preview ? (
                <LinkPreviewCard preview={linkPreview.preview}
                                 onRemove={linkPreview.dismiss} />
              ) : linkPreview.loading ? (
                <span class="sh-muted sh-composer-link-preview-loading">
                  {t('link_preview.loading')}
                </span>
              ) : null}
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
            /*
             * ``<label>`` wraps the file input so the user-gesture
             * flows straight into the chooser via the platform's
             * native label semantics — see ``MediaDropzone`` for the
             * full rationale. The synthetic ``inputRef.current.click()``
             * pattern opened the picker but, on the HA Android
             * Companion App's WebView, the resulting
             * ``onShowFileChooser`` URI didn't always propagate back
             * to ``input.files`` — the picker closed and "nothing
             * happened". The wrap pattern matches every other working
             * file-picker surface in the SPA.
             */
            <label class="sh-composer-image-add">
              <span aria-hidden="true">＋</span>
              <span>Add photo</span>
              {/*
               * Single-pick + specific MIME list for the same
               * HA-Android-WebView reason documented on the
               * MediaDropzone usage above. The user picks one
               * photo per "+ Add photo" tap; the tile stays
               * available until ``MAX_IMAGES`` so multiple
               * additions still work, just one chooser at a time.
               */}
              <input
                type="file"
                accept="image/jpeg,image/png,image/webp,image/gif,image/heic,image/heif"
                class="sr-only"
                onChange={onFilePicked}
              />
            </label>
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
          no media yet (video/file post). Hidden while an upload is in
          flight — :component:`UploadProgressBar` takes over as the
          single surface that reads "something is happening". Before
          this, the dropzone kept inviting "Drag photos here…" while
          the bar below counted bytes, which on mobile read as two
          competing affordances stacked in 60 px of vertical space. */}
      {showMediaAttach
        && uploadProgress.value === null
        && (postType.value === 'image' ? images.length === 0 : !mediaUrl) && (
        /*
         * Picker is single-pick + specific image MIME list (not
         * ``multiple`` + ``image/*``) on the picker path because the
         * HA Android Companion App's WebView ``WebChromeClient`` was
         * suppressing the chooser's result callback in
         * multi-select / ``image/*`` mode (Google Photos picker) —
         * the user picked a photo, the picker closed, and the
         * input's ``change`` event never fired, so the diagnostic
         * toast for empty files didn't even surface. The DM
         * thread's paperclip (no ``accept``, no ``multiple``)
         * works on the same device, which is the structural
         * difference this aligns with. Drag-and-drop still
         * accepts multiple files via ``DataTransfer.files`` — only
         * the chooser path is single-pick. The "+ Add photo" tile
         * lets the user stack multiple images one at a time.
         */
        <MediaDropzone
          multiple={false}
          accept={postType.value === 'image'
            ? 'image/jpeg,image/png,image/webp,image/gif,image/heic,image/heif'
            : postType.value === 'video' ? 'video/*' : undefined}
          hint={postType.value === 'image'
            ? 'Drag photos here, or'
            : `Drag a ${postType.value} here, or`}
          pickLabel={postType.value === 'image' ? 'choose a photo…' : 'choose a file…'}
          draggingHint={`Drop to attach ${postType.value}`}
          onFiles={acceptFiles}
        />
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
          <button
            type="button"
            class={
              formatOpen
                ? 'sh-composer-format sh-composer-format--active'
                : 'sh-composer-format'
            }
            aria-pressed={formatOpen}
            aria-label="Toggle Markdown formatting bar"
            title="Toggle Markdown formatting bar"
            onClick={() => setFormatOpen((v) => !v)}
          >
            Aa
          </button>
        )}
        {postType.value === 'text' && supportsStt() && (
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
              // Image posts use the multi-file ``images`` array; video
              // and file posts use the single ``mediaUrl`` slot. Before
              // this fix the gate only looked at ``mediaUrl``, so an
              // image upload that landed in ``images`` left the Post
              // button disabled — symptom Pascal saw as "selected a
              // photo but nothing happens afterwards".
              const hasMedia = postType.value === 'image'
                ? images.length > 0
                : !!mediaUrl
              return !hasMedia && !content.value.trim()
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
      {spaceId && <MentionAutocomplete />}
    </form>
  )
}
