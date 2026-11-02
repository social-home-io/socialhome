/**
 * AlbumMenu — the album's ⋯: rename and describe it, choose its cover,
 * choose who sees it (its owner, household only — ``AudienceSheet``),
 * delete it.
 *
 * Deleting a household album sends it to the trash with its photos (30
 * days; restoring any of them brings the album back) and the toast offers
 * Undo for 8 s. A space album has no trash, so its delete is held for 8 s
 * and only sent when the Undo runs out. Offered to the album's owner, a
 * household admin (household) or a space moderator (space) — the server
 * checks again.
 */
import { useState } from 'preact/hooks'
import { t, isOne } from '@/i18n/i18n'
import { api } from '@/api'
import { Modal } from '@/components/Modal'
import { Button } from '@/components/Button'
import { OverflowMenu, type MenuItem } from '@/components/OverflowMenu'
import { confirmDialog } from '@/components/confirm'
import { showToast } from '@/components/Toast'
import { undoableDelete } from '@/utils/undoableDelete'

export interface MenuAlbum {
  id: string
  space_id: string | null
  name: string
  description?: string | null
  cover_item_id?: string | null
  item_count: number
}

export interface CoverChoice { id: string; thumbnail_url: string; item_type: string }

export function AlbumMenu({ album, covers, onChanged, onDeleted, onRestored, onAudience }: {
  album: MenuAlbum
  /** The owner's "Who sees this album?" (household only, D7). */
  onAudience?: () => void
  /** The album's photos loaded so far — the cover picker shows them. */
  covers: CoverChoice[]
  /** Name / description / cover saved: re-read the album. */
  onChanged: () => void
  /** Gone from view (trashed, or held for its delete): leave the album. */
  onDeleted: () => void
  /** Undo of a household delete: the album is back. */
  onRestored: (albumId: string) => void
}) {
  const [editing, setEditing] = useState(false)
  const [picking, setPicking] = useState(false)

  const remove = async () => {
    const n = album.item_count
    const inSpace = !!album.space_id
    const ok = await confirmDialog(
      inSpace
        ? t(isOne(n) ? 'gallery.manage.album_delete_body_space_one' : 'gallery.manage.album_delete_body_space', { n: String(n) })
        : t(isOne(n) ? 'gallery.manage.album_delete_body_one' : 'gallery.manage.album_delete_body', { n: String(n) }),
      {
        title: t('gallery.manage.album_delete_title', { name: album.name }),
        confirmLabel: t('gallery.manage.album_delete_confirm'),
        destructive: true,
      },
    )
    if (!ok) return
    const url = `/api/gallery/albums/${encodeURIComponent(album.id)}`
    if (inSpace) {
      undoableDelete({
        ids: [album.id],
        message: t('gallery.manage.album_deleted_space', { name: album.name }),
        commit: ({ keepalive }) => api.delete(url, { keepalive }),
        hide: onDeleted,
      })
      return
    }
    try {
      await api.delete(url)
    } catch (err) {
      showToast(String((err as Error)?.message ?? err), 'error')
      return
    }
    onDeleted()
    showToast(t('gallery.manage.album_trashed', { name: album.name }), 'info', {
      action: {
        label: t('common.undo'),
        onClick: () => {
          void api.post(`${url}/restore`)
            .then(() => onRestored(album.id))
            .catch(() => showToast(t('gallery.manage.undo_failed'), 'error'))
        },
      },
    })
  }

  const items: MenuItem[] = [
    { key: 'edit', label: t('gallery.manage.album_edit'), onSelect: () => setEditing(true) },
    {
      key: 'cover',
      label: t('gallery.manage.album_cover'),
      onSelect: () => setPicking(true),
      disabled: covers.length === 0,
    },
    ...(onAudience
      ? [{ key: 'audience', label: t('gallery.audience.menu'), onSelect: onAudience }]
      : []),
    { key: 'delete', label: t('gallery.manage.album_delete'), onSelect: () => void remove(), danger: true },
  ]

  return (
    <>
      <OverflowMenu label={t('gallery.manage.album_menu', { name: album.name })} items={items} menuWidth={220}>
        <span aria-hidden="true">⋯</span>
      </OverflowMenu>
      {editing && (
        <AlbumEditDialog album={album} onClose={() => setEditing(false)} onSaved={onChanged} />
      )}
      {picking && (
        <CoverPicker album={album} covers={covers} onClose={() => setPicking(false)} onSaved={onChanged} />
      )}
    </>
  )
}

function AlbumEditDialog({ album, onClose, onSaved }: {
  album: MenuAlbum
  onClose: () => void
  onSaved: () => void
}) {
  const [name, setName] = useState(album.name)
  const [description, setDescription] = useState(album.description ?? '')
  const [busy, setBusy] = useState(false)
  const submit = async (e: Event) => {
    e.preventDefault()
    if (!name.trim() || busy) return
    setBusy(true)
    try {
      await api.patch(`/api/gallery/albums/${encodeURIComponent(album.id)}`, {
        name: name.trim(),
        description: description.trim(),
      })
      onSaved()
      onClose()
    } catch (err) {
      showToast(String((err as Error)?.message ?? err), 'error')
      setBusy(false)
    }
  }
  return (
    <Modal open onClose={onClose} title={t('gallery.manage.album_edit')}>
      <form onSubmit={submit} class="sh-edit-item">
        <label>
          {t('gallery.form.name')}
          <input
            type="text"
            maxLength={80}
            required
            value={name}
            onInput={(e) => setName((e.target as HTMLInputElement).value)}
          />
        </label>
        <label>
          {t('gallery.form.description')}
          <textarea
            maxLength={500}
            rows={3}
            value={description}
            onInput={(e) => setDescription((e.target as HTMLTextAreaElement).value)}
          />
        </label>
        <div class="sh-form-actions">
          <Button variant="secondary" type="button" onClick={onClose}>{t('common.cancel')}</Button>
          <Button type="submit" loading={busy} disabled={!name.trim()}>{t('common.save')}</Button>
        </div>
      </form>
    </Modal>
  )
}

function CoverPicker({ album, covers, onClose, onSaved }: {
  album: MenuAlbum
  covers: CoverChoice[]
  onClose: () => void
  onSaved: () => void
}) {
  const [pick, setPick] = useState(album.cover_item_id ?? '')
  const [busy, setBusy] = useState(false)
  const submit = async (e: Event) => {
    e.preventDefault()
    if (!pick || busy) return
    setBusy(true)
    try {
      await api.patch(`/api/gallery/albums/${encodeURIComponent(album.id)}`, { cover_item_id: pick })
      onSaved()
      showToast(t('gallery.manage.cover_saved'), 'success')
      onClose()
    } catch (err) {
      showToast(String((err as Error)?.message ?? err), 'error')
      setBusy(false)
    }
  }
  return (
    <Modal open onClose={onClose} title={t('gallery.manage.album_cover')} class="sh-cover-picker-dialog">
      <form onSubmit={submit}>
        <fieldset class="sh-cover-picker">
          <legend class="sr-only">{t('gallery.manage.album_cover')}</legend>
          {covers.map((c, i) => (
            <label key={c.id} class={'sh-cover-choice' + (pick === c.id ? ' sh-cover-choice--on' : '')}>
              <input
                type="radio"
                name="sh-cover"
                class="sr-only"
                value={c.id}
                checked={pick === c.id}
                onChange={() => setPick(c.id)}
                aria-label={t('gallery.manage.cover_choice', { n: String(i + 1) })}
              />
              <img src={c.thumbnail_url} alt="" loading="lazy" decoding="async" />
            </label>
          ))}
        </fieldset>
        <div class="sh-form-actions">
          <Button variant="secondary" type="button" onClick={onClose}>{t('common.cancel')}</Button>
          <Button type="submit" loading={busy} disabled={!pick}>{t('common.save')}</Button>
        </div>
      </form>
    </Modal>
  )
}
