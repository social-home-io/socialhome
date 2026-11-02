/**
 * MoveToAlbumDialog — pick the album the selected photos move to (a photo
 * lives in exactly one album), or name a new one. The albums of the same
 * gallery only — the household's, or (v_79) the space's the photos are in:
 * a space photo stays in its space, and the "Posts" album fills itself.
 */
import { useEffect, useState } from 'preact/hooks'
import { t } from '@/i18n/i18n'
import { api } from '@/api'
import { Modal } from '@/components/Modal'
import { Button } from '@/components/Button'
import { albumsUrl } from './galleryRoutes'

export interface AlbumChoice { id: string; name: string; is_system?: boolean; item_count?: number }

export function MoveToAlbumDialog({ count, currentAlbumId, spaceId, onClose, onMove }: {
  /** How many photos move — the title says so. */
  count: number
  currentAlbumId: string
  /** The space whose albums are offered; the household's when absent. */
  spaceId?: string
  onClose: () => void
  /** Move into ``albumId``; resolves when done (the dialog then closes). */
  onMove: (albumId: string, albumName: string) => Promise<void>
}) {
  const [albums, setAlbums] = useState<AlbumChoice[] | null>(null)
  const [failed, setFailed] = useState(false)
  const [pick, setPick] = useState<string>('')
  const [newName, setNewName] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let live = true
    api.get<AlbumChoice[]>(`${albumsUrl(spaceId)}?limit=200`)
      .then((rows) => {
        if (!live) return
        const list = rows.filter(a => !a.is_system && a.id !== currentAlbumId)
        setAlbums(list)
        setPick(list[0]?.id ?? 'new')
      })
      .catch(() => { if (live) setFailed(true) })
    return () => { live = false }
  }, [currentAlbumId, spaceId])

  const creating = pick === 'new'
  const canSubmit = !busy && (creating ? newName.trim().length > 0 : !!pick)

  const submit = async (e: Event) => {
    e.preventDefault()
    if (!canSubmit) return
    setBusy(true)
    try {
      if (creating) {
        const made = await api.post<AlbumChoice>(albumsUrl(spaceId), { name: newName.trim() })
        await onMove(made.id, made.name)
      } else {
        const album = albums?.find(a => a.id === pick)
        await onMove(pick, album?.name ?? '')
      }
      onClose()
    } catch {
      setBusy(false)
    }
  }

  return (
    <Modal open onClose={onClose} title={t('gallery.manage.move_title', { n: String(count) })}>
      <form onSubmit={submit} class="sh-move-album">
        {failed ? (
          <p class="sh-error" role="alert">{t('gallery.manage.move_load_failed')}</p>
        ) : albums === null ? (
          <p class="sh-muted" role="status">{t('common.loading')}</p>
        ) : (
          <fieldset class="sh-move-album-list">
            <legend class="sr-only">{t('gallery.manage.move_legend')}</legend>
            {albums.map(a => (
              <label key={a.id} class="sh-move-album-row">
                <input
                  type="radio"
                  name="sh-move-album"
                  value={a.id}
                  checked={pick === a.id}
                  onChange={() => setPick(a.id)}
                />
                <span class="sh-move-album-name">{a.name}</span>
                {typeof a.item_count === 'number' && (
                  <span class="sh-muted">{t(a.item_count === 1 ? 'gallery.items_one' : 'gallery.items', { n: String(a.item_count) })}</span>
                )}
              </label>
            ))}
            <label class="sh-move-album-row">
              <input
                type="radio"
                name="sh-move-album"
                value="new"
                checked={creating}
                onChange={() => setPick('new')}
              />
              <span class="sh-move-album-name">{t('gallery.manage.move_new')}</span>
            </label>
            {creating && (
              <input
                type="text"
                class="sh-move-album-new"
                maxLength={80}
                value={newName}
                aria-label={t('gallery.form.name')}
                placeholder={t('gallery.form.name_placeholder')}
                onInput={(e) => setNewName((e.target as HTMLInputElement).value)}
                autoFocus
              />
            )}
          </fieldset>
        )}
        <p class="sh-muted sh-move-album-hint">{t('gallery.manage.move_hint')}</p>
        <div class="sh-form-actions">
          <Button variant="secondary" type="button" onClick={onClose}>{t('common.cancel')}</Button>
          <Button type="submit" loading={busy} disabled={!canSubmit || failed}>
            {t('gallery.manage.move_confirm')}
          </Button>
        </div>
      </form>
    </Modal>
  )
}
