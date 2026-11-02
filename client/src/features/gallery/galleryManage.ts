/**
 * galleryManage — what a selection (or one photo) can do: delete with
 * Undo, move to another album, save to the device.
 *
 * - Household: a delete goes to the trash at once (30 days) and the toast
 *   offers Undo for 8 s — Undo brings the photos back from the trash.
 * - Space: there is no trash (the delete goes out to the other
 *   households), so the delete is HELD for 8 s and only sent when the
 *   toast runs out (``undoableDelete``); Undo then never has to resend.
 * - Downloads are the stored files (WebP / WebM — never a re-encode) under
 *   a human name; several come as one ZIP the server streams.
 *
 * Every request names the photos by id and the server checks each one
 * again; what it refuses comes back in ``refused``.
 */
import { api, ApiError } from '@/api'
import { t, isOne } from '@/i18n/i18n'
import { showToast } from '@/components/Toast'
import { undoableDelete } from '@/utils/undoableDelete'
import { personLabel } from '@/utils/names'

export interface BulkResult { done: string[]; refused: string[] }

/** Most ids one request carries — the server's ``BULK_MAX_ITEMS``. */
export const BULK_MAX = 500

/** ``POST /api/gallery/bulk`` in slices of ``BULK_MAX``. */
export async function bulk(
  action: 'delete' | 'restore' | 'purge' | 'move' | 'favourite' | 'unfavourite',
  ids: readonly string[],
  albumId?: string,
  opts: { keepalive?: boolean } = {},
): Promise<BulkResult> {
  const out: BulkResult = { done: [], refused: [] }
  for (let i = 0; i < ids.length; i += BULK_MAX) {
    const part = await api.post<BulkResult>('/api/gallery/bulk', {
      action, item_ids: ids.slice(i, i + BULK_MAX), ...(albumId ? { album_id: albumId } : {}),
    }, opts)
    out.done.push(...part.done)
    out.refused.push(...part.refused)
  }
  return out
}

/** Mark one photo a favourite of this person (♡) or not — private:
 *  nobody else learns it (D12). */
export async function setFavourite(id: string, on: boolean): Promise<void> {
  const url = `/api/gallery/items/${encodeURIComponent(id)}/favourite`
  if (on) await api.put(url)
  else await api.delete(url)
}

/** "3 photos" / "1 video" / "4 items" — the noun for a mixed batch. */
export function itemsWord(items: readonly { item_type: string }[]): string {
  const n = items.length
  const one = isOne(n)
  const kinds = new Set(items.map(i => i.item_type))
  const key = kinds.size === 1 && kinds.has('video') ? 'videos'
    : kinds.size === 1 && kinds.has('photo') ? 'photos' : 'mixed'
  return t(`gallery.manage.count_${key}${one ? '_one' : ''}`, { n: String(n) })
}

/** Who added the photos a person may NOT delete — "Ben", "Ben and Lena" —
 *  for the partial-permission confirm. */
export function othersLine(
  items: readonly { uploaded_by: string }[],
  nameOf: (userId: string) => string,
): string {
  const names = [...new Set(items.map(i => nameOf(i.uploaded_by) || t('gallery.manage.someone')))]
  if (names.length <= 1) return names[0] ?? ''
  return t('gallery.manage.names_and', {
    names: names.slice(0, -1).join(', '), last: names[names.length - 1],
  })
}

/** Household delete: to the trash now, Undo for 8 s brings them back
 *  (``onChanged`` then re-reads; after the delete the caller does). */
export async function trashWithUndo(
  ids: readonly string[],
  message: (n: number) => string,
  onChanged: () => void,
): Promise<BulkResult> {
  const result = await bulk('delete', ids)
  if (result.done.length) {
    const done = result.done
    showToast(message(done.length), 'info', {
      action: {
        label: t('common.undo'),
        onClick: () => {
          void bulk('restore', done).then((back) => {
            onChanged()
            if (back.refused.length) showToast(t('gallery.manage.undo_partial'), 'error')
          }).catch(() => showToast(t('gallery.manage.undo_failed'), 'error'))
        },
      },
    })
  }
  return result
}

/** Space delete: hidden at once, sent when the 8 s Undo runs out. */
export function deleteLaterWithUndo(
  ids: readonly string[],
  message: string,
  onCommitted: () => void,
): void {
  undoableDelete({
    ids,
    message,
    commit: async ({ keepalive }) => {
      await bulk('delete', ids, undefined, { keepalive })
      onCommitted()
    },
  })
}

/** Hand a blob to the browser as a saved file. */
export function saveBlob(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = name
  a.rel = 'noopener'
  document.body.appendChild(a)
  a.click()
  a.remove()
  // Give the browser a moment to start the download before freeing it.
  setTimeout(() => URL.revokeObjectURL(url), 30_000)
}

/** Save photos and videos as stored: one as itself, several as a ZIP
 *  (``zipName``.zip). The feed's "Posts" album is named in the reader's
 *  language inside the file names. */
export async function downloadItems(ids: readonly string[], zipName: string): Promise<void> {
  const { blob, filename } = await api.download('/api/gallery/download', {
    item_ids: ids,
    album_label: t('gallery.system_name'),
    zip_name: zipName,
  })
  saveBlob(blob, filename ?? (ids.length > 1 ? `${zipName}.zip` : zipName))
}

/** Save everything someone added to the household gallery as one ZIP
 *  (leaving the household). */
export async function downloadPersonPhotos(username: string, displayName: string): Promise<void> {
  const zipName = t('gallery.manage.person_zip', { name: personLabel(displayName, username) })
  const { blob, filename } = await api.download(
    `/api/gallery/people/${encodeURIComponent(username)}/photos/zip?zip_name=${encodeURIComponent(zipName)}`,
  )
  saveBlob(blob, filename ?? `${zipName}.zip`)
}

/** The message for a failed download — "too many" says how many fit. */
export function downloadError(err: unknown): string {
  if (err instanceof ApiError && err.status === 413) {
    const p = (err.extra.params ?? {}) as Record<string, unknown>
    return t('gallery.manage.download_too_large', { n: String(p.max_items ?? BULK_MAX) })
  }
  return t('gallery.manage.download_failed')
}
