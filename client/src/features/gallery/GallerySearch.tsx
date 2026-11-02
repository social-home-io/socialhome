/**
 * Gallery search (owner decision D11) — the field on the albums page (the
 * household's and every space's) and the results that replace the albums
 * while it holds a search.
 *
 * A search finds photos and videos by their caption, their album's name,
 * who added them ("by Lena", "von Lena") and the month or year they were
 * taken ("July 2025", "Juli 2025"). Never by faces, objects or places — the
 * empty state says so, so nobody waits for a "beach" that was never
 * written down.
 *
 * The words live in the address (``/gallery/search?q=``, a space's under
 * its Gallery section): Back leaves the search, a refresh keeps it, a
 * result opens the viewer at ``/gallery/search/:itemId?q=`` and Back
 * closes it. The household's search covers every album the person sees
 * (the household's and their spaces'); a space's only that space's. The
 * server leaves out what the person may not see — a private album they are
 * not in, someone they blocked, the trash.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { useLocation } from 'preact-iso'
import { t, isOne } from '@/i18n/i18n'
import { api } from '@/api'
import { ws } from '@/ws'
import { addBase } from '@/baseUrl'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'
import { householdUsers } from '@/store/householdUsers'
import { spaceMembers, loadSpaceMembers } from '@/store/spaceMembers'
import { personLabel } from '@/utils/names'
import { snippetParts, snippetText } from '@/utils/snippet'
import { ConnIcon } from '@/features/connections/ConnIcon'
import { GalleryViewer, type ViewerItem } from './GalleryViewer'
import { albumDisplayName } from './albumName'
import { downloadError, downloadItems, setFavourite } from './galleryManage'
import { backLeadsTo, galleryPath, markBackTo, searchPath } from './galleryRoutes'

/** Shortest search that runs (the server's own rule). */
export const MIN_CHARS = 2
/** Results per page — the server's default. */
export const SEARCH_PAGE = 60
/** Typing settles this long before the address (and the search) follows. */
const SETTLE_MS = 300

interface AlbumRef {
  id: string
  name: string
  space_id: string | null
  is_system: boolean
  default_name: boolean
}

export interface SearchRow extends ViewerItem {
  album_id: string
  album: AlbumRef
  /** The caption around the match, ``<mark>`` around the words. */
  snippet: string
  is_favourite?: boolean
  media_status?: 'processing' | 'failed' | 'ready'
}

interface SearchAlbum {
  id: string
  space_id: string | null
  name: string
  cover_url?: string | null
  item_count: number
  is_system?: boolean
  is_default?: boolean
  default_name?: boolean
}

interface SearchResponse {
  items: SearchRow[]
  albums: SearchAlbum[]
  more: boolean
}

/** The field — the same element whether the albums or the results show
 *  below it, so typing never loses focus. */
export function GallerySearchField({ spaceId, q, searching }: {
  spaceId: string | null
  /** The search in the address. */
  q: string
  /** The results are showing (the address is a search). */
  searching: boolean
}) {
  const loc = useLocation()
  const locRef = useRef(loc)
  locRef.current = loc
  const input = useRef<HTMLInputElement | null>(null)
  const [text, setText] = useState(q)
  const typing = useRef(false)

  // The address changed by itself (Back, a link): the field follows.
  useEffect(() => {
    if (!typing.current) setText(q)
  }, [q])

  const leave = () => {
    typing.current = false
    if (backLeadsTo(galleryPath(spaceId))) history.back()
    else locRef.current.route(galleryPath(spaceId), true)
  }

  const follow = (value: string) => {
    typing.current = false
    const want = value.trim()
    if (!want) {
      if (searching) leave()
      return
    }
    if (want.length < MIN_CHARS && !searching) return
    if (want === q) return
    if (searching) {
      locRef.current.route(searchPath(spaceId, want), true)
    } else {
      locRef.current.route(searchPath(spaceId, want))
      // A step back leads to the albums.
      markBackTo(galleryPath(spaceId))
    }
  }

  useEffect(() => {
    if (!typing.current) return
    const timer = setTimeout(() => follow(text), SETTLE_MS)
    return () => clearTimeout(timer)
  }, [text]) // eslint-disable-line react-hooks/exhaustive-deps

  const placeholder = t(spaceId ? 'gallery.search.placeholder_space' : 'gallery.search.placeholder')
  return (
    <form
      role="search"
      class="sh-gallery-search"
      onSubmit={(e) => { e.preventDefault(); follow(text) }}
    >
      <span class="sh-gallery-search__icon" aria-hidden="true"><ConnIcon name="search" /></span>
      <input
        ref={input}
        type="search"
        class="sh-gallery-search__input"
        placeholder={placeholder}
        aria-label={t('gallery.search.label')}
        value={text}
        autoComplete="off"
        spellcheck={false}
        enterKeyHint="search"
        onInput={(e) => {
          typing.current = true
          setText((e.currentTarget as HTMLInputElement).value)
        }}
        onKeyDown={(e) => {
          if (e.key === 'Escape' && (text || searching)) {
            e.preventDefault()
            setText('')
            if (searching) leave()
          }
        }}
      />
      {text && (
        <button
          type="button"
          class="sh-gallery-search__clear"
          aria-label={t('gallery.search.clear')}
          onClick={() => {
            setText('')
            input.current?.focus()
            if (searching) leave()
          }}
        >
          <ConnIcon name="x" />
        </button>
      )}
    </form>
  )
}

function uploaderName(row: SearchRow): string {
  const space = row.album.space_id
  if (space) {
    const m = spaceMembers.value[space]?.get(row.uploaded_by)
    return m ? personLabel(m.space_display_name || m.display_name, null) : ''
  }
  const u = householdUsers.value.get(row.uploaded_by)
  return u ? personLabel(u.display_name, u.username) : ''
}

/** What a search found: matching albums, then the photos as a grid. */
export function GallerySearchResults({ spaceId, q, itemId }: {
  spaceId: string | null
  q: string
  itemId: string | null
}) {
  const loc = useLocation()
  const locRef = useRef(loc)
  locRef.current = loc
  const [rows, setRows] = useState<SearchRow[] | null>(null)
  const [albums, setAlbums] = useState<SearchAlbum[]>([])
  const [more, setMore] = useState(false)
  const [failed, setFailed] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const asked = useRef('')
  const fromGrid = useRef(false)
  const lastItem = useRef<string | null>(null)
  const rowsRef = useRef<SearchRow[] | null>(null)
  rowsRef.current = rows

  const query = q.trim()
  const short = query.length < MIN_CHARS

  const url = (offset: number, limit = SEARCH_PAGE) => {
    const params = new URLSearchParams({ q: query, limit: String(limit), offset: String(offset) })
    if (spaceId) params.set('space_id', spaceId)
    return `/api/gallery/search?${params.toString()}`
  }

  const load = async (quiet = false) => {
    const key = `${spaceId ?? ''}\n${query}`
    asked.current = key
    if (!quiet) { setRows(null); setFailed(false) }
    const have = rowsRef.current?.length ?? 0
    const limit = quiet ? Math.min(200, Math.max(SEARCH_PAGE, have)) : SEARCH_PAGE
    try {
      const body = await api.get<SearchResponse>(url(0, limit))
      if (asked.current !== key) return // a newer search took over
      setRows(body.items)
      setAlbums(body.albums)
      setMore(body.more)
      setFailed(false)
    } catch {
      if (asked.current !== key) return
      if (!quiet || rowsRef.current === null) setFailed(true)
      else showToast(t('gallery.refresh_failed'), 'error')
    }
  }

  const loadMore = async () => {
    const have = rowsRef.current
    if (!have || loadingMore) return
    const key = asked.current
    setLoadingMore(true)
    try {
      const body = await api.get<SearchResponse>(url(have.length))
      if (asked.current !== key) return
      setRows(prev => {
        const seen = new Set((prev ?? []).map(r => r.id))
        return [...(prev ?? []), ...body.items.filter(r => !seen.has(r.id))]
      })
      setMore(body.more)
    } catch {
      showToast(t('gallery.search.failed'), 'error')
    } finally {
      setLoadingMore(false)
    }
  }

  useEffect(() => {
    if (short) { asked.current = ''; setRows([]); setAlbums([]); setMore(false); return }
    void load()
  }, [query, spaceId]) // eslint-disable-line react-hooks/exhaustive-deps

  // Something changed in the gallery: re-read quietly, once for a burst.
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null
    const soon = () => {
      if (short) return
      if (timer) clearTimeout(timer)
      timer = setTimeout(() => void load(true), 400)
    }
    const offs = [
      ws.on('gallery.item_uploaded', soon),
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
  }, [query, spaceId]) // eslint-disable-line react-hooks/exhaustive-deps

  // Names for the viewer's "Added by" in space albums.
  const list = rows ?? []
  const spaceIds = [...new Set(list.map(r => r.album.space_id).filter((s): s is string => !!s))]
  useEffect(() => {
    for (const sid of spaceIds) void loadSpaceMembers(sid)
  }, [spaceIds.join(',')]) // eslint-disable-line react-hooks/exhaustive-deps

  const index = itemId ? list.findIndex(r => r.id === itemId) : -1

  // A link to a result that isn't (or no longer is) one: say so.
  useEffect(() => {
    if (!itemId || rows === null || index >= 0 || short) return
    showToast(t('gallery.item_missing'), 'info')
    locRef.current.route(searchPath(spaceId, query), true)
  }, [itemId, rows]) // eslint-disable-line react-hooks/exhaustive-deps

  // The viewer closed: focus goes back to the tile seen last.
  useEffect(() => {
    if (itemId) {
      if (backLeadsTo(searchPath(spaceId, query))) fromGrid.current = true
      if (fromGrid.current) markBackTo(searchPath(spaceId, query))
      lastItem.current = itemId
      return
    }
    fromGrid.current = false
    const seen = lastItem.current
    lastItem.current = null
    if (!seen) return
    const tile = document.querySelector<HTMLElement>(`[data-item-id="${CSS.escape(seen)}"]`)
    tile?.focus({ preventScroll: true })
    tile?.scrollIntoView?.({ block: 'nearest' })
  }, [itemId]) // eslint-disable-line react-hooks/exhaustive-deps -- the search only shapes the note

  const closeViewer = () => {
    if (fromGrid.current) history.back()
    else locRef.current.route(searchPath(spaceId, query), true)
  }
  const current = index >= 0 ? list[index] : undefined

  if (short) {
    return <p class="sh-muted sh-gallery-search__status">{t('search.keep_typing')}</p>
  }

  if (failed && rows === null) {
    return (
      <div class="sh-empty-state" role="alert">
        <div aria-hidden="true">📡</div>
        <h2>{t('gallery.search.failed')}</h2>
        <Button onClick={() => void load()}>{t('common.retry')}</Button>
      </div>
    )
  }

  if (rows === null) {
    return (
      <div class="sh-image-grid" role="status" aria-label={t('common.loading')} aria-busy="true">
        {Array.from({ length: 8 }, (_, i) => (
          <span key={i} class="sh-skeleton sh-skeleton--rect sh-gallery-skel-tile" aria-hidden="true" />
        ))}
      </div>
    )
  }

  const found = list.length
  const foundLabel = more ? `${found}+` : String(found)
  return (
    <div class="sh-gallery-search__results">
      <p class="sh-gallery-search__status" role="status">
        {found === 0 && albums.length === 0
          ? ''
          : t(isOne(found) && !more ? 'gallery.search.found_one' : 'gallery.search.found', { n: foundLabel })}
      </p>

      {albums.length > 0 && (
        <section class="sh-gallery-search__section" aria-labelledby="sh-gallery-search-albums">
          <h2 id="sh-gallery-search-albums" class="sh-gallery-search__heading">{t('gallery.albums')}</h2>
          <div class="sh-gallery-search__albums">
            {albums.map(a => (
              <a
                key={a.id}
                class="sh-gallery-search__album"
                href={addBase(galleryPath(a.space_id, a.id))}
                aria-label={t(isOne(a.item_count) ? 'gallery.open_album_aria_one' : 'gallery.open_album_aria',
                  { name: albumDisplayName(a), n: String(a.item_count) })}
              >
                {a.cover_url
                  ? <img src={a.cover_url} alt="" loading="lazy" decoding="async" />
                  : <span class="sh-gallery-search__album-empty" aria-hidden="true">🖼️</span>}
                <span class="sh-gallery-search__album-name">{albumDisplayName(a)}</span>
              </a>
            ))}
          </div>
        </section>
      )}

      {found === 0 && albums.length === 0 ? (
        <div class="sh-empty-state">
          <div aria-hidden="true">🔎</div>
          <h2>{t('gallery.search.empty_title', { query })}</h2>
          <p>{t('gallery.search.empty_body')}</p>
        </div>
      ) : found > 0 && (
        <section class="sh-gallery-search__section" aria-labelledby="sh-gallery-search-items">
          {albums.length > 0 && (
            <h2 id="sh-gallery-search-items" class="sh-gallery-search__heading">{t('gallery.search.items')}</h2>
          )}
          <div class="sh-image-grid sh-gallery-search__grid">
            {list.map(r => (
              <ResultTile
                key={r.id}
                row={r}
                href={addBase(searchPath(spaceId, query, r.id))}
                onOpen={() => { fromGrid.current = true }}
              />
            ))}
          </div>
          {more && (
            <div class="sh-gallery-more">
              <Button variant="secondary" loading={loadingMore} onClick={() => void loadMore()}>
                {t('feed.load_more')}
              </Button>
            </div>
          )}
        </section>
      )}

      {itemId && (
        <GalleryViewer
          items={list.map(r => ({ ...r, processing: r.media_status === 'processing' }))}
          index={index}
          total={list.length}
          albumName={current ? albumDisplayName(current.album) : ''}
          inSpace={!!current?.album.space_id}
          uploaderName={() => (current ? uploaderName(current) : '')}
          // Deleting stays in the album (who may, and the trash, are the
          // album's); a search is for finding.
          canDelete={() => false}
          onDelete={async () => undefined}
          reportFor={() => null}
          onGo={(i) => {
            const next = list[i]
            if (next) locRef.current.route(searchPath(spaceId, query, next.id), true)
          }}
          onClose={closeViewer}
          onDownload={async (it) => {
            const row = list.find(r => r.id === it.id)
            try {
              await downloadItems([it.id], row ? albumDisplayName(row.album) : '')
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

function ResultTile({ row, href, onOpen }: { row: SearchRow, href: string, onOpen: () => void }) {
  const kind = row.item_type === 'video' ? t('gallery.item_video') : t('gallery.item_photo')
  const caption = snippetText(row.snippet)
  const label = t('gallery.fav.tile_aria', { type: kind, album: albumDisplayName(row.album) })
    + (caption ? `: ${caption}` : '')
  return (
    <a
      href={href}
      class="sh-gallery-item sh-gallery-search__tile"
      data-item-id={row.id}
      aria-label={label}
      title={caption || undefined}
      onClick={onOpen}
    >
      <img src={row.thumbnail_url} alt="" loading="lazy" decoding="async" />
      {row.item_type === 'video' && <span class="sh-video-badge" aria-hidden="true">▶</span>}
      {row.snippet && (
        <span class="sh-gallery-search__caption" aria-hidden="true">
          {snippetParts(row.snippet).map((part, i) => (
            part.mark ? <mark key={i}>{part.text}</mark> : part.text
          ))}
        </span>
      )}
    </a>
  )
}
