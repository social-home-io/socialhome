/**
 * GalleryUpload — the ways files get into the upload queue from a
 * gallery page: the picker (multi-select), the phone camera (a
 * ``capture`` input) and a drop anywhere on the gallery (folders too).
 */
import type { ComponentChildren } from 'preact'
import { useEffect, useRef, useState } from 'preact/hooks'
import { t } from '@/i18n/i18n'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'
import { addFiles, type UploadTarget } from './uploadQueue'
import { ACCEPT, dragHasFiles, filesFromDrop } from './uploadFiles'

export interface UploadControls {
  /** Open the file picker. */
  pick: () => void
  /** Open the camera (phones). */
  camera: () => void
  /** The last files came from the camera ("Take another"). */
  fromCamera: boolean
  /** Hidden inputs + the drop overlay; render once on the page. */
  elements: ComponentChildren
}

/** Wire the picker, the camera and drop-anywhere to ``into``. With
 *  ``enabled`` false (a follower, the "Posts" album) nothing is offered
 *  and a drop is left to the browser. */
export function useUploadControls(into: UploadTarget | null, enabled: boolean): UploadControls {
  const pickRef = useRef<HTMLInputElement | null>(null)
  const camRef = useRef<HTMLInputElement | null>(null)
  const [over, setOver] = useState(false)
  const [fromCamera, setFromCamera] = useState(false)
  // Listeners outlive renders: they read the latest target.
  const intoRef = useRef(into)
  intoRef.current = into

  const take = (files: File[], camera: boolean) => {
    if (files.length === 0) return
    if (!addFiles(files, intoRef.current)) {
      showToast(t('gallery.upload.busy_elsewhere'), 'info')
      return
    }
    setFromCamera(camera)
  }

  useEffect(() => {
    if (!enabled) return
    let depth = 0
    const onEnter = (e: DragEvent) => {
      if (!dragHasFiles(e.dataTransfer)) return
      e.preventDefault()
      depth++
      setOver(true)
    }
    const onOver = (e: DragEvent) => {
      if (!dragHasFiles(e.dataTransfer)) return
      e.preventDefault()
      if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy'
    }
    const onLeave = (e: DragEvent) => {
      if (!dragHasFiles(e.dataTransfer)) return
      depth = Math.max(0, depth - 1)
      if (depth === 0) setOver(false)
    }
    const onDrop = (e: DragEvent) => {
      if (!dragHasFiles(e.dataTransfer) || !e.dataTransfer) return
      e.preventDefault()
      depth = 0
      setOver(false)
      void filesFromDrop(e.dataTransfer).then(files => take(files, false))
    }
    window.addEventListener('dragenter', onEnter)
    window.addEventListener('dragover', onOver)
    window.addEventListener('dragleave', onLeave)
    window.addEventListener('drop', onDrop)
    return () => {
      window.removeEventListener('dragenter', onEnter)
      window.removeEventListener('dragover', onOver)
      window.removeEventListener('dragleave', onLeave)
      window.removeEventListener('drop', onDrop)
      setOver(false)
    }
  }, [enabled])

  const onPicked = (e: Event, camera: boolean) => {
    const input = e.target as HTMLInputElement
    const files = Array.from(input.files ?? [])
    // Reset so picking the same file again still fires ``change``.
    input.value = ''
    take(files, camera)
  }

  const overlayText = into && !into.isDefault && into.albumId
    ? t('gallery.upload.drop_into', { name: into.name })
    : t('gallery.upload.drop_here')

  return {
    pick: () => pickRef.current?.click(),
    camera: () => camRef.current?.click(),
    fromCamera,
    elements: enabled ? (
      <>
        <input
          ref={pickRef}
          type="file"
          accept={ACCEPT}
          multiple
          class="sr-only"
          tabIndex={-1}
          aria-hidden="true"
          data-testid="gallery-upload-input"
          onChange={(e) => onPicked(e, false)}
        />
        <input
          ref={camRef}
          type="file"
          accept="image/*,video/*"
          capture="environment"
          class="sr-only"
          tabIndex={-1}
          aria-hidden="true"
          data-testid="gallery-camera-input"
          onChange={(e) => onPicked(e, true)}
        />
        {over && (
          <div class="sh-upload-drop" aria-hidden="true">
            <div class="sh-upload-drop__card">
              <span class="sh-upload-drop__icon">⤓</span>
              {overlayText}
            </div>
          </div>
        )}
      </>
    ) : null,
  }
}

/** The page's upload buttons: "Upload" and, on phones, "Camera". */
export function UploadButtons({ controls }: { controls: UploadControls }) {
  return (
    <>
      <Button onClick={controls.pick}>{t('gallery.upload_btn')}</Button>
      {/* Phones only (CSS): a desktop has no camera worth a button. */}
      <span class="sh-upload-camera-btn">
        <Button
          variant="secondary"
          aria-label={t('gallery.upload.camera_aria')}
          onClick={controls.camera}
        >
          <span aria-hidden="true">📷</span> {t('gallery.upload.camera')}
        </Button>
      </span>
    </>
  )
}
