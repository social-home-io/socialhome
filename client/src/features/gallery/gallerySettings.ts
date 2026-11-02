/**
 * The household gallery's rules (gallery D6) — ``GET /api/gallery/settings``.
 *
 * ``may_add`` is whether the signed-in person may add photos and albums to
 * the household gallery right now: always, except a child (a protected
 * account) while an admin turned "Children may add photos" off. The
 * gallery hides its upload and new-album buttons then and says why; the
 * server refuses anyway (403 ``CHILD_UPLOAD_OFF``).
 */
import { signal } from '@preact/signals'
import { api } from '@/api'

export interface GallerySettings {
  children_may_upload: boolean
  may_add: boolean
  can_edit: boolean
}

/** ``null`` until read (or when it couldn't be) — everything is offered
 *  then, the server has the last word. */
export const gallerySettings = signal<GallerySettings | null>(null)

export async function loadGallerySettings(): Promise<GallerySettings | null> {
  try {
    gallerySettings.value = await api.get<GallerySettings>('/api/gallery/settings')
  } catch {
    // Keep what was read: an offline moment mustn't hide the buttons.
  }
  return gallerySettings.value
}

export async function saveChildrenMayUpload(on: boolean): Promise<GallerySettings> {
  const next = await api.patch<GallerySettings>('/api/gallery/settings', {
    children_may_upload: on,
  })
  gallerySettings.value = next
  return next
}

/** Whether the person may add to the household gallery (unknown = yes). */
export function mayAddToGallery(): boolean {
  return gallerySettings.value?.may_add ?? true
}
