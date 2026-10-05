/**
 * MediaDropzone — drag-and-drop + file-picker affordance for media
 * uploads. Matches the styling of the main post Composer's dropzone so
 * the highlight / momentum composers feel like one consistent surface.
 *
 * The component does NOT manage the upload itself — callers receive
 * the picked / dropped File[] via ``onFiles`` and decide what to do
 * with them (upload, validate, derive previews, etc.). That keeps
 * call-site flexibility for per-feature constraints (highlight's
 * per-day frame cap, momentum's 15-second video probe).
 */
import { useState } from 'preact/hooks'
import { showToast } from './Toast'
import { t } from '@/i18n/i18n'

interface MediaDropzoneProps {
  /** Called once per drop / pick with every selected File. */
  onFiles: (files: File[]) => void | Promise<void>
  /** Allow multi-file selection. Default: single file. */
  multiple?: boolean
  /** Native ``accept`` attribute for the input — e.g. ``image/*,video/*``. */
  accept?: string
  /** When true, the dropzone refuses interaction (no drag highlight,
   *  no clicks). Used by callers that have hit a per-day cap. */
  disabled?: boolean
  /** Resting hint, e.g. "Drag photos here, or". */
  hint?: string
  /** Action-link label, e.g. "choose photos…". */
  pickLabel?: string
  /** Hint shown while a drag is hovering, e.g. "Drop to attach". */
  draggingHint?: string
}

export function MediaDropzone({
  onFiles,
  multiple = false,
  accept,
  disabled = false,
  hint = t('composer.drop.hint_file'),
  pickLabel = t('composer.drop.pick_file'),
  draggingHint = t('composer.drop.dragging'),
}: MediaDropzoneProps) {
  const [dragActive, setDragActive] = useState(false)

  const handleFiles = async (files: File[]) => {
    if (disabled || files.length === 0) return
    await onFiles(files)
  }

  const onPicked = async (e: Event) => {
    const input = e.target as HTMLInputElement
    const files = Array.from(input.files ?? [])
    if (files.length === 0) {
      // The HA Android Companion App's WebView occasionally fires
      // ``change`` with an empty FileList — the picker UI confirmed
      // a photo but the WebChromeClient didn't propagate the URI back
      // to the page. Without this toast the user sees pure silence
      // and assumes the upload is broken. Surfacing it directs them
      // to retry or fall back to drag-and-drop / desktop.
      showToast(t('composer.drop.picker_empty'), 'error')
      return
    }
    await handleFiles(files)
    input.value = ''
  }

  const onDrop = async (e: DragEvent) => {
    e.preventDefault()
    // Stop bubbling so a parent that also wires drop-anywhere handlers
    // (e.g. the post Composer's form-level drop) doesn't fire a second
    // time and double-process the same files.
    e.stopPropagation()
    setDragActive(false)
    if (disabled) return
    const dropped = Array.from(e.dataTransfer?.files ?? [])
    await handleFiles(dropped)
  }

  const onDragOver = (e: DragEvent) => {
    e.preventDefault()
    if (disabled) return
    if (!dragActive) setDragActive(true)
  }

  const onDragLeave = (e: DragEvent) => {
    if (e.currentTarget === e.target) setDragActive(false)
  }

  const cls = [
    'sh-mediadrop',
    dragActive ? 'sh-mediadrop--dragging' : '',
    disabled ? 'sh-mediadrop--disabled' : '',
  ].filter(Boolean).join(' ')

  return (
    <div
      class={cls}
      onDrop={onDrop}
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
    >
      <span>{dragActive ? draggingHint : hint}</span>
      {/*
        ``<label>`` wraps the file input so the user's tap on the
        affordance flows directly into the chooser via the
        platform's native label semantics — no synthetic JS
        ``.click()`` indirection. This is the pattern every other
        working file-input site in the SPA uses already
        (``SpaceSettingsPage``, ``SettingsPage``,
        ``SpaceProfileDialog``, ``BazaarCreateDialog``,
        ``CalendarEventDialog``). The previous shape — a separate
        ``<button onClick={() => inputRef.current?.click()}>`` —
        opened the picker on the HA Android Companion App's
        WebView but the chooser result didn't always make it back
        into ``input.files``: picker closed, nothing happened.
        DM thread's paperclip uses the same synthetic-click pattern
        and DOES work, which weakens the "synthetic click is the
        bug" theory; the empty-files diagnostic toast in
        ``onPicked`` is what tells us which failure mode this
        actually is (URI dropped vs. ``change`` never firing).
        ``.sr-only`` keeps the input 1×1 px offscreen so the
        chooser callback still fires reliably.

        Disabled visual feedback comes from the parent
        ``.sh-mediadrop--disabled`` class (opacity 0.5, cursor
        not-allowed), which propagates to the label via CSS
        inheritance. Clicking a label that wraps a disabled input
        is a no-op per the HTML spec, so we rely on the input's
        ``disabled`` attribute alone for the gating logic.
      */}
      <label class="sh-link">
        {pickLabel}
        <input
          type="file"
          multiple={multiple}
          accept={accept}
          disabled={disabled}
          class="sr-only"
          onChange={onPicked}
        />
      </label>
    </div>
  )
}
