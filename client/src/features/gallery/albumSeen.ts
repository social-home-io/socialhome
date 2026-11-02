/**
 * The "neu" dot on album cards (household gallery D9).
 *
 * The server sets ``has_new`` on an album when someone else added a photo
 * or video since the viewer last opened it (household and space albums
 * alike). Opening an album clears it: ``noteAlbumSeen`` tells the server
 * (``POST /api/gallery/albums/:id/seen``) and reads the album's "new
 * photos" notification rows as seen (they open the same path).
 */
import { api } from '@/api'
import { markSeen } from '@/store/notifications'
import { galleryPath } from './galleryRoutes'

/** The viewer has the album on screen. A failure is silent: the dot just
 *  shows again on the next list. */
export async function noteAlbumSeen(albumId: string, spaceId: string | null): Promise<void> {
  markSeen(galleryPath(spaceId, albumId))
  try {
    await api.post(`/api/gallery/albums/${encodeURIComponent(albumId)}/seen`, {})
  } catch { /* a hint only */ }
}

/** *rows* with the album's dot gone (the same array when it had none). */
export function withoutDot<T extends { id: string, has_new?: boolean }>(
  rows: T[], albumId: string,
): T[] {
  if (!rows.some(a => a.id === albumId && a.has_new)) return rows
  return rows.map(a => (a.id === albumId ? { ...a, has_new: false } : a))
}
