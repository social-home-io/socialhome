/**
 * FavouritesPage — your own favourite photos and videos
 * (``/gallery/favourites``), last marked first.
 *
 * Owner decision D12: a favourite (♡) is **private** — nobody else learns
 * who marked what, there are no comments or reactions on gallery photos.
 * One list spans every album you can see: the household's (a private
 * album only while you are one of its people) and your spaces'. A photo
 * in the trash is left out until it comes back.
 *
 * Opened from the "♡ Favourites" chip on the albums page. Tiles are real
 * links (``/gallery/favourites/:itemId`` opens the viewer, Back closes
 * it). Un-hearting a photo in the viewer keeps it on screen with an empty
 * ♡ — one more tap undoes it — and it leaves the list when the viewer
 * closes. Select several to take them off the list or save them.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { useLocation } from 'preact-iso'
import { t } from '@/i18n/i18n'
import { useTitle } from '@/store/pageTitle'
import { api } from '@/api'
import { ws } from '@/ws'
import { addBase } from '@/baseUrl'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'
import { householdUsers, loadHouseholdUsers } from '@/store/householdUsers'
import { spaceMembers, loadSpaceMembers } from '@/store/spaceMembers'
import { personLabel } from '@/utils/names'
import { GalleryViewer, type ViewerItem } from './GalleryViewer'
import { SelectionBar, type SelectionAction } from './SelectionBar'
import { useSelection } from './useSelection'
import { albumDisplayName } from './albumName'
import { HeartIcon } from './HeartIcon'
import { bulk, downloadError, downloadItems, itemsWord, setFavourite } from './galleryManage'
import { backLeadsTo, favouritesPath, galleryPath, markBackTo } from './galleryRoutes'

export interface FavouriteRow extends ViewerItem {
  album_id: string
  is_favourite: boolean
  favourited_at: string
  album: {
    id: string
    name: string
    space_id: string | null
    is_system: boolean
    default_name: boolean
  } | null
}

/** Page size — the server's default (``routes/gallery.py``). */
export const FAVOURITE_PAGE = 60
/** A link to a favourite past the loaded pages reads at most this many
 *  more pages looking for it. */
const SEARCH_PAGES = 20

/** The favourites were opened from the albums' chip (Back steps back). */
let favFromList = false
export function openedFavouritesFromList(): void { favFromList = true }

function uploaderName(row: FavouriteRow): string {
  const space = row.album?.space_id
  if (space) {
    const m = spaceMembers.value[space]?.get(row.uploaded_by)
    return m ? personLabel(m.space_display_name || m.display_name, null) : ''
  }
  const u = householdUsers.value.get(row.uploaded_by)
  return u ? personLabel(u.display_name, u.username) : ''
}

function albumName(row: FavouriteRow | undefined): string {
  return row?.album ? albumDisplayName(row.album) : ''
}

function cursorOf(row: FavouriteRow): string {
  return `&before=${encodeURIComponent(row.favourited_at)}&before_id=${encodeURIComponent(row.id)}`
}

export default function FavouritesPage({ itemId }: { itemId: string | null }) {
  useTitle(t('gallery.fav.title'))
  const loc = useLocation()
  const locRef = useRef(loc)
  locRef.current = loc
  const [rows, setRows] = useState<FavouriteRow[] | null>(null)
  const [more, setMore] = useState(false)
  const [failed, setFailed] = useState(false)
  const [busy, setBusy] = useState<'remove' | 'save' | null>(null)
  const rowsRef = useRef<FavouriteRow[] | null>(null)
  rowsRef.current = rows
  const searched = useRef(0)
  const fetching = useRef(false)
  const fromGrid = useRef(false)
  const lastItem = useRef<string | null>(null)

  useEffect(() => {
    if (favFromList) markBackTo(galleryPath())
    return () => { favFromList = false }
  }, [])

  const load = async (quiet = false) => {
    const have = rowsRef.current?.length ?? 0
    const limit = quiet ? Math.min(500, Math.max(FAVOURITE_PAGE, have)) : FAVOURITE_PAGE
    try {
      const next = await api.get<FavouriteRow[]>(`/api/gallery/favourites?limit=${limit}`)
      setRows(next)
      setMore(next.length >= limit)
      setFailed(false)
    } catch {
      if (!quiet || rowsRef.current === null) setFailed(true)
      else showToast(t('gallery.refresh_failed'), 'error')
    }
  }
  const loadMore = async () => {
    const list = rowsRef.current
    const last = list?.[list.length - 1]
    if (!last || fetching.current) return
    fetching.current = true
    try {
      const next = await api.get<FavouriteRow[]>(`/api/gallery/favourites?limit=${FAVOURITE_PAGE}${cursorOf(last)}`)
      setRows(prev => {
        const seen = new Set((prev ?? []).map(r => r.id))
        return [...(prev ?? []), ...next.filter(r => !seen.has(r.id))]
      })
      setMore(next.length >= FAVOURITE_PAGE)
    } catch {
      showToast(t('gallery.refresh_failed'), 'error')
    } finally {
      fetching.current = false
    }
  }

  useEffect(() => {
    void load()
    void loadHouseholdUsers()
    // A photo deleted, restored, moved or re-captioned elsewhere: re-read
    // quietly, once for a burst of frames.
    let timer: ReturnType<typeof setTimeout> | null = null
    const soon = () => {
      if (timer) clearTimeout(timer)
      timer = setTimeout(() => void load(true), 400)
    }
    const offs = [
      ws.on('gallery.item_deleted', soon),
      ws.on('gallery.item_restored', soon),
      ws.on('gallery.item_updated', soon),
      ws.on('gallery.album_deleted', soon),
      ws.on('gallery.album_updated', soon),
    ]
    return () => {
      if (timer) clearTimeout(timer)
      offs.forEach(off => off())
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // Names for the viewer's "Added by" in space albums.
  const spaceIds = [...new Set((rows ?? []).map(r => r.album?.space_id).filter((s): s is string => !!s))]
  useEffect(() => {
    for (const sid of spaceIds) void loadSpaceMembers(sid)
  }, [spaceIds.join(',')]) // eslint-disable-line react-hooks/exhaustive-deps

  const list = rows ?? []
  const index = itemId ? list.findIndex(r => r.id === itemId) : -1

  // A link to a favourite not loaded yet: read on; not there → say so.
  useEffect(() => {
    if (!itemId) { searched.current = 0; return }
    const now = rowsRef.current
    if (now === null || now.some(r => r.id === itemId) || fetching.current) return
    if (more && searched.current < SEARCH_PAGES) {
      searched.current += 1
      void loadMore()
      return
    }
    showToast(t('gallery.item_missing'), 'info')
    locRef.current.route(favouritesPath(), true)
  }, [itemId, rows, more]) // eslint-disable-line react-hooks/exhaustive-deps

  // The viewer closed: the photos un-hearted in it leave the list, and
  // focus goes back to the tile seen last.
  useEffect(() => {
    if (itemId) {
      if (backLeadsTo(favouritesPath())) fromGrid.current = true
      if (fromGrid.current) markBackTo(favouritesPath())
      lastItem.current = itemId
      return
    }
    fromGrid.current = false
    setRows(prev => prev && prev.some(r => !r.is_favourite) ? prev.filter(r => r.is_favourite) : prev)
    const seen = lastItem.current
    lastItem.current = null
    if (!seen) return
    const tile = document.querySelector<HTMLElement>(`[data-item-id="${CSS.escape(seen)}"]`)
    tile?.focus({ preventScroll: true })
    tile?.scrollIntoView?.({ block: 'nearest' })
  }, [itemId])

  const sel = useSelection(list.map(r => r.id))
  const picked = list.filter(r => sel.ids.has(r.id))

  const back = () => {
    if (favFromList || backLeadsTo(galleryPath())) history.back()
    else loc.route(galleryPath())
  }

  const removePicked = async () => {
    if (!picked.length) return
    setBusy('remove')
    try {
      const res = await bulk('unfavourite', picked.map(r => r.id))
      const gone = new Set(res.done)
      setRows(prev => (prev ?? []).filter(r => !gone.has(r.id)))
      sel.clear()
      showToast(t('gallery.fav.removed', { what: itemsWord(picked) }), 'info', {
        action: {
          label: t('common.undo'),
          onClick: () => {
            void bulk('favourite', [...gone]).then(() => load(true))
              .catch(() => showToast(t('gallery.fav.failed'), 'error'))
          },
        },
      })
    } catch {
      showToast(t('gallery.fav.failed'), 'error')
    } finally {
      setBusy(null)
    }
  }

  const savePicked = async () => {
    if (!picked.length) return
    setBusy('save')
    try {
      await downloadItems(picked.map(r => r.id), t('gallery.fav.title'))
    } catch (err) {
      showToast(downloadError(err), 'error')
    } finally {
      setBusy(null)
    }
  }

  const actions: SelectionAction[] = [
    { key: 'unfavourite', icon: 'favourite', label: t('gallery.fav.remove'), onClick: () => void removePicked(), busy: busy === 'remove', disabled: busy !== null },
    { key: 'save', icon: 'save', label: t('gallery.manage.save'), onClick: () => void savePicked(), busy: busy === 'save', disabled: busy !== null },
  ]

  const closeViewer = () => {
    if (fromGrid.current) history.back()
    else locRef.current.route(favouritesPath(), true)
  }
  const current = index >= 0 ? list[index] : undefined

  return (
    <div class="sh-favourites">
      <header class="sh-page-header sh-trash-header">
        <Button variant="secondary" onClick={back}>← {t('gallery.albums')}</Button>
        <h1>{t('gallery.fav.title')}</h1>
        {list.length > 0 && !sel.active && (
          <Button variant="secondary" onClick={() => sel.start()}>{t('gallery.manage.select')}</Button>
        )}
      </header>

      {/* The empty state says it itself. */}
      {list.length > 0 && (
        <p class="sh-trash-info">
          <HeartIcon class="sh-fav-info-heart" filled={false} /> {t('gallery.fav.private_note')}
        </p>
      )}

      {sel.active && (
        <SelectionBar count={sel.ids.size} onClose={sel.clear} actions={actions} />
      )}

      {failed && rows === null ? (
        <div class="sh-empty-state" role="alert">
          <div aria-hidden="true">📡</div>
          <h2>{t('gallery.fav.load_failed')}</h2>
          <Button onClick={() => { setFailed(false); void load() }}>{t('common.retry')}</Button>
        </div>
      ) : rows === null ? (
        <div class="sh-image-grid" role="status" aria-label={t('common.loading')} aria-busy="true">
          {Array.from({ length: 8 }, (_, i) => (
            <span key={i} class="sh-skeleton sh-skeleton--rect sh-gallery-skel-tile" aria-hidden="true" />
          ))}
        </div>
      ) : list.length === 0 ? (
        <div class="sh-empty-state">
          <div aria-hidden="true" class="sh-fav-empty-heart"><HeartIcon size={44} filled={false} /></div>
          <h2>{t('gallery.fav.empty_title')}</h2>
          <p>{t('gallery.fav.empty_body')}</p>
          <Button variant="secondary" onClick={back}>{t('gallery.fav.to_albums')}</Button>
        </div>
      ) : (
        <>
          <div class="sh-image-grid">
            {list.map(r => (
              <FavouriteTile
                key={r.id}
                row={r}
                selecting={sel.active}
                picked={sel.ids.has(r.id)}
                handlers={sel.tileHandlers(r.id)}
                onOpen={() => { fromGrid.current = true }}
              />
            ))}
          </div>
          {more && (
            <div class="sh-gallery-more">
              <Button variant="secondary" onClick={() => void loadMore()}>{t('feed.load_more')}</Button>
            </div>
          )}
        </>
      )}

      {itemId && (
        <GalleryViewer
          items={list}
          index={index}
          total={list.length}
          albumName={albumName(current)}
          inSpace={!!current?.album?.space_id}
          uploaderName={() => (current ? uploaderName(current) : '')}
          // Deleting stays in the album (who may, and the trash, are the
          // album's); this list is for looking at your best ones.
          canDelete={() => false}
          onDelete={async () => undefined}
          reportFor={() => null}
          onGo={(i) => {
            const next = list[i]
            if (next) locRef.current.route(favouritesPath(next.id), true)
          }}
          onClose={closeViewer}
          onDownload={async (it) => {
            try {
              await downloadItems([it.id], albumName(list.find(r => r.id === it.id)))
            } catch (err) {
              showToast(downloadError(err), 'error')
            }
          }}
          isFavourite={(it) => list.find(r => r.id === it.id)?.is_favourite ?? false}
          onFavourite={async (it, on) => {
            await setFavourite(it.id, on)
            setRows(prev => (prev ?? []).map(r => r.id === it.id ? { ...r, is_favourite: on } : r))
          }}
        />
      )}
    </div>
  )
}

function FavouriteTile({ row, selecting, picked, handlers, onOpen }: {
  row: FavouriteRow
  selecting: boolean
  picked: boolean
  handlers: ReturnType<ReturnType<typeof useSelection>['tileHandlers']>
  onOpen: () => void
}) {
  const kind = row.item_type === 'video' ? t('gallery.item_video') : t('gallery.item_photo')
  const label = t('gallery.fav.tile_aria', { type: kind, album: albumName(row) })
  const media = (
    <>
      <img src={row.thumbnail_url} alt="" loading="lazy" decoding="async" />
      {row.item_type === 'video' && <span class="sh-video-badge" aria-hidden="true">▶</span>}
    </>
  )
  if (selecting) {
    return (
      <button
        type="button"
        role="checkbox"
        aria-checked={picked}
        class={'sh-gallery-item sh-gallery-item--selectable' + (picked ? ' sh-gallery-item--picked' : '')}
        data-item-id={row.id}
        aria-label={label}
        {...handlers}
      >
        {media}
        <span class="sh-select-check" aria-hidden="true" />
      </button>
    )
  }
  return (
    <a
      href={addBase(favouritesPath(row.id))}
      class="sh-gallery-item"
      data-item-id={row.id}
      aria-label={label}
      {...handlers}
      onClick={(e: MouseEvent) => {
        handlers.onClick(e)
        if (!e.defaultPrevented) onOpen()
      }}
    >
      {media}
    </a>
  )
}
