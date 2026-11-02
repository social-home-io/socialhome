/**
 * GalleryPage — albums + items grid (§23.119).
 *
 * Two modes:
 *   • Album list (default) — grid of album cards with empty-state hero.
 *   • Album detail — items grid for one album; click opens lightbox
 *     with prev/next + keyboard nav.
 *
 * Used both for household-level (no space_id) and per-space galleries.
 *
 * Both lists page: albums come 30 at a time (the auto "Posts" album
 * pinned first), an album's items 50 at a time, newest first. The next
 * page is asked for with the last row's ``created_at`` + ``id`` cursor,
 * from a "Load more" button that also fires by itself when it scrolls
 * into view — so every album and every upload can be reached.
 *
 * Every album and every photo has its own address (``galleryRoutes``):
 * ``/gallery/albums/:albumId[/:itemId]``, mirrored under a space's
 * Gallery section. Albums and tiles are real links; the viewer
 * (``GalleryViewer``) is open while the address names an item, so Back
 * closes it, a refresh keeps it and a shared link opens it.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { useLocation, useRoute } from 'preact-iso'
import { t, isOne } from '@/i18n/i18n'
import { useTitle } from '@/store/pageTitle'
import { signal } from '@preact/signals'
import { api, ApiError } from '@/api'
import { ws } from '@/ws'
import { addBase } from '@/baseUrl'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'
import { openReport } from '@/components/ReportDialog'
import { GalleryViewer, type ViewerItem } from './GalleryViewer'
import { backLeadsTo, galleryPath, markBackTo, parseGallerySub } from './galleryRoutes'
import { currentUser } from '@/store/auth'
import { householdUsers, loadHouseholdUsers } from '@/store/householdUsers'
import { spaceMembers, loadSpaceMembers } from '@/store/spaceMembers'
import { failedMedia, mediaFilename, readyMedia } from '@/store/mediaReady'
import { personLabel } from '@/utils/names'
import { formatDayKey } from '@/utils/calendar'
import { UploadSheet } from './UploadSheet'
import { counts, type UploadTarget } from './uploadQueue'
import { UploadButtons, useUploadControls } from './GalleryUpload'

interface Album {
  id: string
  space_id: string | null
  /** ``null`` for the auto-managed system album, which has no
   *  human owner. */
  owner_user_id: string | null
  name: string
  description?: string | null
  cover_url?: string | null
  item_count: number
  retention_exempt: boolean
  /** Auto-managed "Posts" album. Cannot be deleted or uploaded to;
   *  contents track every photo or video shared via a feed post. */
  is_system?: boolean
  created_at?: string
}

interface Item {
  id: string
  album_id: string
  uploaded_by: string
  item_type: 'photo' | 'video'
  url: string
  thumbnail_url: string
  width: number
  height: number
  caption?: string | null
  taken_at?: string | null
  created_at?: string
  duration_s?: number | null
  /** Background-transcode state for ``video`` items — ``'processing'``
   *  while the worker encodes. Absent on photos and on older payloads
   *  (treated as ready). A processing item can't play yet, so the tile
   *  shows a "Processing…" overlay and doesn't open the lightbox. */
  media_status?: 'processing' | 'failed' | 'ready'
}

/** Page sizes — match the server defaults (``routes/gallery.py``). */
export const ALBUM_PAGE = 30
export const ITEM_PAGE = 50
/** The server's largest page — a quiet refresh re-reads what is loaded,
 *  up to this many rows. */
const ALBUM_MAX = 200
const ITEM_MAX = 500
/** While a video tile still says "Processing…", re-read the album this
 *  often — another person's upload sends this browser no ready frame. */
const PROCESSING_POLL_MS = 10_000

const albums       = signal<Album[]>([])
const albumsMore   = signal(false)
const albumsError  = signal(false)
const items        = signal<Item[]>([])
const itemsMore    = signal(false)
const itemsLoading = signal(false)
const activeAlbum  = signal<Album | null>(null)
/** The album in the address could not be read (offline) — not "gone". */
const albumError   = signal(false)
const loading      = signal(true)
const showCreate   = signal(false)
/** The scope (space id, or ``null`` for the household) the signals above
 *  hold. The page is one module shared by every gallery, so moving from a
 *  space gallery to ``/gallery`` must drop the open album and its items. */
let loadedScope: string | null | undefined
/** The album the address asks for — a slower answer for another one is
 *  dropped. */
let wantedAlbum: string | null = null
/** How we got here, so the in-app Back / Close step back in history
 *  (and the phone's Back button agrees) instead of piling up entries:
 *  an album opened from the list, a photo opened from its grid. A cold
 *  link has neither — closing then goes to the parent address. */
let albumFromList = false
let viewerFromGrid = false
/** A link to an item past the loaded pages reads at most this many more
 *  pages looking for it (50 each) before saying it isn't there. */
const ITEM_SEARCH_PAGES = 40

export interface GalleryPageProps {
  spaceId?: string
  /** A read-only follower of the space: they look at the albums but
   *  neither create one nor upload (the server answers 403
   *  ``SUBSCRIBER_READ_ONLY``), so neither control is offered. */
  readOnly?: boolean
  /** A space owner / admin / moderator: may delete others' space items
   *  (the server checks again). */
  canModerate?: boolean
  /** TopBar title — a space passes "Gallery · <space>". */
  title?: string
}

/** The household gallery, or a space's: delete rules differ (#1271). */
function canDeleteItem(album: Album, item: { uploaded_by: string }, canModerate: boolean): boolean {
  const me = currentUser.value
  if (!me || album.is_system) return false
  if (item.uploaded_by === me.user_id) return true
  // A protected account (a child) deletes only what it added.
  if (me.protected) return false
  if (album.space_id) return canModerate
  return album.owner_user_id === me.user_id || me.is_admin
}

/** An album's shown name — the auto "Posts" album in the reader's
 *  language (the server stores one English name for it). */
function albumName(a: Album): string {
  return a.is_system ? t('gallery.system_name') : a.name
}

export default function GalleryPage({
  spaceId, readOnly = false, canModerate = false, title,
}: GalleryPageProps) {
  useTitle(title ?? t('nav.gallery'))
  const { params } = useRoute()
  const loc = useLocation()
  const locRef = useRef(loc)
  locRef.current = loc
  const where = parseGallerySub(params?.sub)
  const albumId = where.albumId
  const itemId = where.itemId

  useEffect(() => {
    const scope = spaceId ?? null
    if (loadedScope !== scope) {
      // Another gallery's album must not stay open here.
      activeAlbum.value = null
      items.value = []
      itemsMore.value = false
      albums.value = []
      albumsMore.value = false
      showCreate.value = false
      loadedScope = scope
    }
    void loadAlbums(spaceId)
    // Names for the tiles' "added by" (screen readers).
    if (spaceId) void loadSpaceMembers(spaceId)
    else void loadHouseholdUsers()
  }, [spaceId])

  // An address the gallery doesn't know → the albums.
  useEffect(() => {
    if (!where.valid) locRef.current.route(galleryPath(spaceId), true)
  }, [where.valid, spaceId])

  // The album follows the address: opened from the list, from a link or
  // a refresh; closed by Back.
  useEffect(() => {
    wantedAlbum = albumId
    if (!albumId) {
      albumFromList = false
      albumError.value = false
      if (activeAlbum.value) {
        activeAlbum.value = null
        items.value = []
        itemsMore.value = false
        // Re-fetch the album list so a freshly-uploaded cover + bumped
        // item_count show up immediately — quietly, so the list already
        // on screen stays there if the network is gone.
        void loadAlbums(spaceId, { quiet: true, toastOnFail: true })
      }
      return
    }
    // Remember (across a reload) that the list is one step back.
    if (albumFromList && !itemId) markBackTo(galleryPath(spaceId))
    if (activeAlbum.value?.id === albumId) return
    void openAlbum(albumId, spaceId).then((found) => {
      if (found || wantedAlbum !== albumId) return
      showToast(t('gallery.album_missing'), 'info')
      locRef.current.route(galleryPath(spaceId), true)
    })
  }, [spaceId, albumId]) // eslint-disable-line react-hooks/exhaustive-deps -- itemId only shapes the note

  // Live cross-device updates: refetch on gallery WS frames, scoped to
  // this page (a space gallery vs the household gallery). Thin frames →
  // refetch the canonical GET shape quietly (no spinner, so an open album
  // isn't swapped for a loader) and follow the open album: its items on
  // an upload / delete, its header on a rename or cover change, and back
  // to the list when another device or household deletes it.
  useEffect(() => {
    type GalleryFrame = { data: { space_id?: string | null; album_id?: string } }
    const inScope = (e: GalleryFrame) =>
      (e.data.space_id ?? null) === (spaceId ?? null)
    const handle = (e: GalleryFrame) => {
      if (!inScope(e)) return
      void loadAlbums(spaceId, { quiet: true })
      if (activeAlbum.value && e.data.album_id === activeAlbum.value.id) {
        void loadItems(activeAlbum.value.id, { quiet: true })
      }
    }
    const onAlbumDeleted = (e: GalleryFrame) => {
      if (!inScope(e)) return
      if (activeAlbum.value && e.data.album_id === activeAlbum.value.id) {
        activeAlbum.value = null
        items.value = []
        showToast(t('gallery.album_deleted'), 'info')
        locRef.current.route(galleryPath(spaceId), true)
      }
      void loadAlbums(spaceId, { quiet: true })
    }
    // A video this person uploaded finished (or failed) transcoding:
    // re-read the open album so its tile swaps "Processing…" for the
    // poster without a reload.
    const onMediaDone = (e: { data: { output_filename?: string } }) => {
      const fn = e.data.output_filename
      const open = activeAlbum.value
      if (!fn || !open) return
      if (items.value.some(i => mediaFilename(i.url) === fn)) {
        void loadItems(open.id, { quiet: true })
      }
    }
    const offs = [
      ws.on('gallery.album_created', handle),
      ws.on('gallery.album_updated', handle),
      ws.on('gallery.album_deleted', onAlbumDeleted),
      ws.on('gallery.item_uploaded', handle),
      ws.on('gallery.item_deleted', handle),
      ws.on('media.ready', onMediaDone),
      ws.on('media.failed', onMediaDone),
    ]
    return () => { offs.forEach((off) => off()) }
  }, [spaceId])

  // Uploads finished: quietly re-read the list (a new default album, a
  // cover, counts) and the open album — once a second at most, not per
  // file (live frames cover others' uploads).
  const doneCount = counts.value.done
  useEffect(() => {
    if (doneCount === 0) return
    const timer = setTimeout(() => {
      void loadAlbums(spaceId, { quiet: true })
      const open = activeAlbum.value
      if (open) void loadItems(open.id, { quiet: true })
    }, 1000)
    return () => clearTimeout(timer)
  }, [doneCount, spaceId])

  // Upload from the album list: the household's default album (made on
  // the first upload when there is none — no album needed first); in a
  // space, its only album, else the person picks one in the sheet.
  const regular = albums.value.filter(a => !a.is_system)
  const listTarget: UploadTarget | null = spaceId
    ? (regular.length === 1
      ? { spaceId, albumId: regular[0].id, name: regular[0].name, isDefault: false }
      : null)
    : { spaceId: null, albumId: null, name: t('gallery.upload.default_album'), isDefault: true }
  const listCanUpload = !readOnly && !albumsError.value && (!spaceId || regular.length > 0)
  const listControls = useUploadControls(listTarget, listCanUpload && !albumId && !loading.value)

  const sheet = (
    <UploadSheet
      spaceId={spaceId ?? null}
      albums={albums.value}
      // "Open album" goes to the album's own address.
      onOpenAlbum={(id) => { albumFromList = false; loc.route(galleryPath(spaceId, id)) }}
      onAlbumCreated={() => void loadAlbums(spaceId, { quiet: true })}
      onCamera={albumId ? undefined : listControls.camera}
      fromCamera={listControls.fromCamera}
    />
  )

  if (albumId) {
    // Back to the list: a step back in history when the list opened
    // this album, else to the list's address.
    const onBack = () => {
      if (albumFromList || backLeadsTo(galleryPath(spaceId))) history.back()
      else loc.route(galleryPath(spaceId))
    }
    const open = activeAlbum.value
    if (albumError.value) {
      return (
        <div class="sh-album-detail">
          {sheet}
          <header class="sh-page-header">
            <Button variant="secondary" onClick={onBack}>← {t('gallery.albums')}</Button>
          </header>
          <div class="sh-empty-state" role="alert">
            <div aria-hidden="true">📡</div>
            <h1>{t('gallery.load_items_error')}</h1>
            <Button onClick={() => {
              albumError.value = false
              void openAlbum(albumId, spaceId)
            }}>{t('common.retry')}</Button>
          </div>
        </div>
      )
    }
    if (!open || open.id !== albumId) {
      return (
        <>
          {sheet}
          <AlbumSkeleton
            onBack={onBack}
            withViewer={!!itemId}
            onCloseViewer={() => loc.route(galleryPath(spaceId, albumId), true)}
          />
        </>
      )
    }
    return (
      <>
      {sheet}
      <AlbumDetail
        album={open}
        spaceId={spaceId}
        itemId={itemId}
        readOnly={readOnly}
        canModerate={canModerate}
        onBack={onBack}
      />
      </>
    )
  }

  // The upload sheet stays on screen whatever loads: uploads carry on.
  if (loading.value) return <><AlbumListSkeleton />{sheet}</>

  // Counts for the hero — system "Posts" album is folded into the
  // total because it is real content from the household's perspective.
  // While more albums wait to load, the counts are "at least" ("30+").
  const albumCount = albums.value.length
  const itemCount = albums.value.reduce((acc, a) => acc + a.item_count, 0)
  const more = albumsMore.value ? '+' : ''

  return (
    <div class="sh-gallery">
      {sheet}
      {listControls.elements}
      <h1 class="sr-only">{t('nav.gallery')}</h1>
      {!albumsError.value && (
        <header class="sh-gallery-hero">
          <div class="sh-gallery-hero-headline">
            <strong>{albumCount}{more}</strong>{' '}
            {t(isOne(albumCount) && !more ? 'gallery.albums_word_one' : 'gallery.albums_word')} ·{' '}
            <strong>{itemCount}{more}</strong>{' '}
            {t(isOne(itemCount) && !more ? 'gallery.media_word_one' : 'gallery.media_word')}
          </div>
          {!readOnly && (
            <div class="sh-gallery-hero-actions sh-upload-actions">
              {listCanUpload && <UploadButtons controls={listControls} />}
              <Button
                variant={listCanUpload ? 'secondary' : 'primary'}
                onClick={() => (showCreate.value = true)}
              >
                {t('gallery.new_album_btn')}
              </Button>
            </div>
          )}
        </header>
      )}

      {showCreate.value && !readOnly && (
        <CreateAlbumForm
          spaceId={spaceId}
          onClose={() => (showCreate.value = false)}
          onCreated={() => { showCreate.value = false; void loadAlbums(spaceId) }}
        />
      )}

      {albumsError.value ? (
        // The list could not be read at all. Never the first-visit
        // "No albums yet" screen: that reads as "your photos are gone".
        <div class="sh-empty-state" role="alert">
          <div aria-hidden="true">📡</div>
          <h2>{t('gallery.load_error_title')}</h2>
          <p>{t('gallery.load_error_body')}</p>
          <Button onClick={() => void loadAlbums(spaceId)}>{t('common.retry')}</Button>
        </div>
      ) : albums.value.length === 0 ? (
        <div class="sh-empty-state">
          <div aria-hidden="true">📸</div>
          <h2>{t('gallery.empty_title')}</h2>
          <p>{spaceId ? t('gallery.empty_body_space') : t('gallery.empty_body_household')}</p>
          {!readOnly && listCanUpload && (
            <>
              <p>{t('gallery.upload.empty_hint', { name: t('gallery.upload.default_album') })}</p>
              <div class="sh-row sh-upload-actions sh-upload-actions--center">
                <UploadButtons controls={listControls} />
                <Button variant="secondary" onClick={() => (showCreate.value = true)}>
                  {t('gallery.create_first')}
                </Button>
              </div>
            </>
          )}
          {!readOnly && !listCanUpload && (
            <Button onClick={() => (showCreate.value = true)}>
              {t('gallery.create_first')}
            </Button>
          )}
        </div>
      ) : (
        <>
          <div class="sh-album-grid">
            {albums.value.map(a => (
              // A real link: its own address, opens in a new tab, and
              // the router keeps the ingress prefix.
              <a
                key={a.id}
                href={addBase(galleryPath(spaceId, a.id))}
                class={`sh-album-card${a.is_system ? ' sh-album-card--system' : ''}`}
                aria-label={t(isOne(a.item_count) ? 'gallery.open_album_aria_one' : 'gallery.open_album_aria', { name: albumName(a), n: String(a.item_count) })}
                onClick={() => { albumFromList = true }}
              >
                <AlbumCover album={a} />
                <div class="sh-album-info">
                  <strong>
                    {albumName(a)}
                    {a.is_system && (
                      <span class="sh-album-system-badge"
                            title={t('gallery.auto_title')}>
                        <span aria-hidden="true">🔒</span> {t('gallery.auto')}
                      </span>
                    )}
                  </strong>
                  <span class="sh-muted">
                    {t(isOne(a.item_count) ? 'gallery.items_one' : 'gallery.items', { n: String(a.item_count) })}
                  </span>
                  {a.retention_exempt && !a.is_system && (
                    <span class="sh-badge">{t('gallery.kept')}</span>
                  )}
                </div>
              </a>
            ))}
          </div>
          {albumsMore.value && (
            <LoadMore onMore={() => loadMoreAlbums(spaceId)} />
          )}
        </>
      )}
    </div>
  )
}

/** "Load more" for a paged list: a real button (keyboard, screen
 *  reader, no IntersectionObserver), which also presses itself when it
 *  scrolls into view so scrolling keeps going. */
function LoadMore({ onMore }: { onMore: () => Promise<void> }) {
  const [busy, setBusy] = useState(false)
  const ref = useRef<HTMLDivElement | null>(null)
  const busyRef = useRef(false)
  const run = async () => {
    if (busyRef.current) return
    busyRef.current = true
    setBusy(true)
    try { await onMore() } finally {
      busyRef.current = false
      setBusy(false)
    }
  }
  // The observer outlives renders: it calls the latest ``run``.
  const runRef = useRef(run)
  runRef.current = run
  useEffect(() => {
    const el = ref.current
    if (!el || typeof IntersectionObserver === 'undefined') return
    const io = new IntersectionObserver((entries) => {
      if (entries.some(e => e.isIntersecting)) void runRef.current()
    }, { rootMargin: '400px 0px' })
    io.observe(el)
    return () => io.disconnect()
  }, [])
  return (
    <div class="sh-gallery-more" ref={ref}>
      <Button variant="secondary" loading={busy} onClick={() => void run()}>
        {t('feed.load_more')}
      </Button>
    </div>
  )
}

/** Album cards in their final shape while the list loads. One status for
 *  screen readers, not one per block. */
function AlbumListSkeleton() {
  return (
    <div class="sh-gallery">
      <h1 class="sr-only">{t('nav.gallery')}</h1>
      <div class="sh-album-grid" role="status" aria-label={t('common.loading')} aria-busy="true">
        {Array.from({ length: 6 }, (_, i) => (
          <div key={i} class="sh-album-card sh-album-card--skeleton" aria-hidden="true">
            <span class="sh-skeleton sh-skeleton--rect sh-album-skel-cover" />
            <div class="sh-album-info">
              <span class="sh-skeleton sh-skeleton--line" style={{ width: '60%' }} />
              <span class="sh-skeleton sh-skeleton--line" style={{ width: '30%' }} />
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

/** Square tiles while an album's first page loads. */
function TileSkeleton({ count = 12 }: { count?: number }) {
  return (
    <div class="sh-image-grid" role="status" aria-label={t('common.loading')} aria-busy="true">
      {Array.from({ length: count }, (_, i) => (
        <span key={i} class="sh-skeleton sh-skeleton--rect sh-gallery-skel-tile" aria-hidden="true" />
      ))}
    </div>
  )
}

/** An album opened from a link, before its name has arrived. */
function AlbumSkeleton({ onBack, withViewer, onCloseViewer }: {
  onBack: () => void
  withViewer: boolean
  onCloseViewer: () => void
}) {
  return (
    <div class="sh-album-detail">
      <header class="sh-page-header">
        <Button variant="secondary" onClick={onBack}>← {t('gallery.albums')}</Button>
        <span class="sh-skeleton sh-skeleton--line" style={{ width: '12rem', height: '1.5rem' }} aria-hidden="true" />
      </header>
      <TileSkeleton />
      {withViewer && (
        <GalleryViewer
          items={[]} index={-1} total={0} albumName="" inSpace={false}
          uploaderName={() => ''} canDelete={() => false} reportFor={() => null}
          onGo={() => undefined} onClose={onCloseViewer} onDelete={async () => undefined}
        />
      )}
    </div>
  )
}

/** Album cover — renders the signed cover URL with an onError swap to
 *  the 🖼️ placeholder.  The previous implementation only showed the
 *  placeholder when ``cover_url === null``, so a 404 / signature-
 *  expired URL left a blank white card.  Tracking ``failed`` per
 *  cover keeps the swap local to the affected album. */
function AlbumCover({ album }: { album: Album }) {
  const [failed, setFailed] = useState(false)
  if (!album.cover_url || failed) {
    return (
      <div class="sh-album-cover sh-album-cover--placeholder">
        <span aria-hidden="true">{album.is_system ? '📸' : '🖼️'}</span>
      </div>
    )
  }
  return (
    <img
      src={album.cover_url}
      class="sh-album-cover"
      alt=""
      loading="lazy"
      onError={() => setFailed(true)}
    />
  )
}

/** Who added an item, by name — or ``''`` when this browser doesn't know
 *  them (never an id). */
function uploaderName(spaceId: string | null, userId: string): string {
  if (spaceId) {
    const m = spaceMembers.value[spaceId]?.get(userId)
    return m ? personLabel(m.space_display_name || m.display_name, null) : ''
  }
  const u = householdUsers.value.get(userId)
  return u ? personLabel(u.display_name, u.username) : ''
}

/** A tile's accessible name: the caption, else "Photo from <day>", plus
 *  who added it — so ten tiles are not ten identical "Photo"s. */
function tileLabel(item: Item, spaceId: string | null): string {
  const type = itemTypeLabel(item.item_type)
  const day = item.taken_at && /^\d{4}-\d{2}-\d{2}$/.test(item.taken_at)
    ? formatDayKey(item.taken_at, { year: 'numeric', month: 'short', day: 'numeric' })
    : ''
  const label = item.caption
    ? t('gallery.item_caption_aria', { type, caption: item.caption })
    : day ? t('gallery.tile_dated', { type, date: day }) : type
  const name = uploaderName(spaceId, item.uploaded_by)
  return name ? t('gallery.tile_by', { label, name }) : label
}

/** Is this video still being transcoded? The list says so; a
 *  ``media.ready`` / ``media.failed`` frame this browser got since
 *  overrides it at once. */
function isProcessing(item: Item): boolean {
  if (item.item_type !== 'video' || item.media_status !== 'processing') return false
  const fn = mediaFilename(item.url)
  return !readyMedia.value.has(fn) && !failedMedia.value.has(fn)
}

function AlbumDetail({
  album, spaceId, itemId, onBack, readOnly = false, canModerate = false,
}: {
  album: Album
  spaceId?: string
  /** The item the address names — the viewer is open on it. */
  itemId: string | null
  onBack: () => void
  readOnly?: boolean
  canModerate?: boolean
}) {
  const viewer = useAlbumViewer(album, spaceId, itemId, canModerate)

  const anyProcessing = items.value.some(isProcessing)
  // Another person's video sends this browser no ready frame: re-read
  // the album now and then while a tile still says "Processing…".
  useEffect(() => {
    if (!anyProcessing) return
    const timer = setInterval(() => {
      void loadItems(album.id, { quiet: true })
    }, PROCESSING_POLL_MS)
    return () => clearInterval(timer)
  }, [anyProcessing, album.id])

  const loaded = items.value
  // No uploads into the system album (the server refuses them too).
  const canUpload = !album.is_system && !readOnly
  const controls = useUploadControls({
    spaceId: album.space_id,
    albumId: album.id,
    name: albumName(album),
    isDefault: false,
  }, canUpload)

  return (
    <div class="sh-album-detail">
      <header class="sh-page-header">
        <Button variant="secondary" onClick={onBack}>← {t('gallery.albums')}</Button>
        <h1 style={{ margin: 0 }}>
          {albumName(album)}
          {album.is_system && (
            <span class="sh-album-system-badge"
                  style={{ marginLeft: 'var(--sh-space-xs)' }}>
              <span aria-hidden="true">🔒</span> {t('gallery.auto')}
            </span>
          )}
        </h1>
        {canUpload && (
          <div class="sh-row sh-upload-actions">
            <UploadButtons controls={controls} />
          </div>
        )}
      </header>
      {controls.elements}

      {/* The system album's description is the server's English constant;
        * its translated hint below says the same thing. */}
      {album.description && !album.is_system && <p class="sh-muted">{album.description}</p>}

      {album.is_system && (
        <div class="sh-album-system-hint">
          <span aria-hidden="true">📸</span>{' '}
          {t('gallery.system_hint')}
        </div>
      )}

      {loaded.length === 0 && itemsLoading.value ? (
        <TileSkeleton count={Math.min(12, Math.max(4, album.item_count))} />
      ) : loaded.length === 0 ? (
        <div class="sh-empty-state">
          <div aria-hidden="true">🖼️</div>
          {album.is_system ? (
            <>
              <h2>{t('gallery.system_empty_title')}</h2>
              <p>{t('gallery.system_empty_body')}</p>
            </>
          ) : (
            <>
              <h2>{t('gallery.album_empty_title')}</h2>
              {canUpload && (
                <p>{t('gallery.album_empty_body_before')}<strong>{t('gallery.upload_btn')}</strong>{t('gallery.album_empty_body_after')}</p>
              )}
            </>
          )}
        </div>
      ) : (
        <>
          <div class="sh-image-grid">
            {loaded.map((item) => {
              // A video still transcoding can't play and has no poster
              // yet, so the tile shows a "Processing…" placeholder (no
              // broken image) and opens nothing.
              if (isProcessing(item)) {
                return (
                  <div
                    key={item.id}
                    class="sh-gallery-item sh-gallery-item--processing"
                    role="img"
                    data-item-id={item.id}
                    aria-label={t('gallery.item_processing_aria', { type: itemTypeLabel(item.item_type) })}
                  >
                    <span class="sh-gallery-item-processing" aria-hidden="true">
                      <span class="sh-upload-progress__spinner" />
                      <span class="sh-gallery-item-processing-msg">{t('media.processing')}</span>
                    </span>
                  </div>
                )
              }
              // A real link to the photo's own address: the viewer opens
              // on it, and it can be shared or opened in a new tab.
              return (
                <a
                  key={item.id}
                  href={addBase(galleryPath(spaceId, album.id, item.id))}
                  class="sh-gallery-item"
                  data-item-id={item.id}
                  aria-label={tileLabel(item, album.space_id)}
                  onClick={() => { viewerFromGrid = true }}
                >
                  <img
                    src={item.thumbnail_url}
                    alt=""
                    loading="lazy"
                    decoding="async"
                  />
                  {item.item_type === 'video' && (
                    <span class="sh-video-badge" aria-hidden="true">▶</span>
                  )}
                </a>
              )
            })}
          </div>
          {itemsMore.value && (
            <LoadMore onMore={() => loadMoreItems(album.id)} />
          )}
        </>
      )}
      {viewer}
    </div>
  )
}

/** The viewer for the item the address names, wired to the album: going
 *  to a neighbour replaces the address (Back closes the viewer, it never
 *  steps through every photo seen), a link past the loaded pages reads
 *  on until it finds the item, and closing returns focus to its tile. */
function useAlbumViewer(
  album: Album, spaceId: string | undefined, itemId: string | null, canModerate: boolean,
) {
  const loc = useLocation()
  const locRef = useRef(loc)
  locRef.current = loc
  const searched = useRef(0)
  const fetching = useRef(false)
  const lastItem = useRef<string | null>(null)
  const loaded = items.value
  const index = itemId ? loaded.findIndex(i => i.id === itemId) : -1
  const albumPath = galleryPath(spaceId, album.id)

  // Closing by history.back() lands a moment later (popstate): until
  // then the address still names the item, which may be gone already.
  const closing = useRef(false)
  const close = () => {
    closing.current = true
    if (viewerFromGrid) history.back()
    else locRef.current.route(albumPath, true)
  }

  const fetchMore = () => {
    if (fetching.current) return
    fetching.current = true
    searched.current += 1
    void loadMoreItems(album.id).finally(() => { fetching.current = false })
  }

  // A link to an item not loaded yet: read on; not in the album → say so.
  useEffect(() => {
    if (!itemId) { searched.current = 0; return }
    // Read the signals now, not the render's copy: the effect runs after
    // paint, and the first page may have landed in between.
    if (items.value.some(i => i.id === itemId)) return
    if (itemsLoading.value || fetching.current || closing.current) return
    if (itemsMore.value && searched.current < ITEM_SEARCH_PAGES) {
      fetchMore()
      return
    }
    showToast(t('gallery.item_missing'), 'info')
    locRef.current.route(albumPath, true)
  }, [itemId, index, itemsLoading.value, itemsMore.value, loaded.length]) // eslint-disable-line react-hooks/exhaustive-deps

  // Near the end of what is loaded: the next page comes in ahead.
  useEffect(() => {
    if (itemId && index >= 0 && index >= loaded.length - 3 && itemsMore.value) fetchMore()
  }, [itemId, index, loaded.length, itemsMore.value]) // eslint-disable-line react-hooks/exhaustive-deps

  // Closed: focus goes to the tile of the photo seen last, in view.
  useEffect(() => {
    if (itemId) {
      // Opened from the grid — now, or before a reload (history.state
      // remembers): the album is one step back. A replace (the next
      // photo) drops the note, so it is written again each time.
      if (backLeadsTo(albumPath)) viewerFromGrid = true
      if (viewerFromGrid) markBackTo(albumPath)
      lastItem.current = itemId
      return
    }
    viewerFromGrid = false
    closing.current = false
    const seen = lastItem.current
    lastItem.current = null
    if (!seen) return
    const tile = document.querySelector<HTMLElement>(`[data-item-id="${CSS.escape(seen)}"]`)
    if (!tile) return
    tile.focus({ preventScroll: true })
    tile.scrollIntoView?.({ block: 'nearest' })
  }, [itemId]) // eslint-disable-line react-hooks/exhaustive-deps -- albumPath follows itemId's album

  if (!itemId) return null

  const viewerItems: ViewerItem[] = loaded.map(i => ({ ...i, processing: isProcessing(i) }))
  const me = currentUser.value?.user_id
  return (
    <GalleryViewer
      items={viewerItems}
      index={index}
      total={album.item_count}
      albumName={albumName(album)}
      inSpace={!!album.space_id}
      uploaderName={(uid) => uploaderName(album.space_id, uid)}
      canDelete={(it) => canDeleteItem(album, it, canModerate)}
      reportFor={(it) =>
        // A space item someone else uploaded goes to the space's moderators.
        album.space_id && it.uploaded_by !== me
          ? () => openReport('gallery_item', it.id, album.space_id)
          : null}
      onGo={(i) => {
        const next = loaded[i]
        if (next) locRef.current.route(galleryPath(spaceId, album.id, next.id), true)
      }}
      onClose={close}
      onDelete={async (it) => {
        await api.delete(`/api/gallery/items/${it.id}`)
        const pos = items.value.findIndex(i => i.id === it.id)
        const rest = items.value.filter(i => i.id !== it.id)
        const next = rest[pos] ?? rest[pos - 1]
        // Move on first, then drop the row — the viewer never sees an
        // address whose item is gone.
        if (next) locRef.current.route(galleryPath(spaceId, album.id, next.id), true)
        else {
          close()
          showToast(t('gallery.deleted'), 'success')
        }
        items.value = rest
        if (activeAlbum.value?.id === album.id) {
          activeAlbum.value = { ...album, item_count: Math.max(0, album.item_count - 1) }
        }
      }}
    />
  )
}

/** Spoken name of a gallery item kind, for screen-reader labels. */
function itemTypeLabel(kind: 'photo' | 'video'): string {
  return kind === 'video' ? t('gallery.item_video') : t('gallery.item_photo')
}

function CreateAlbumForm({
  spaceId, onClose, onCreated,
}: {
  spaceId?: string,
  onClose: () => void,
  onCreated: () => void,
}) {
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async (e: Event) => {
    e.preventDefault()
    if (!name.trim() || busy) return
    setBusy(true)
    const url = spaceId
      ? `/api/spaces/${spaceId}/gallery/albums`
      : '/api/gallery/albums'
    try {
      await api.post(url, {
        name: name.trim(),
        description: description.trim() || null,
      })
      showToast(t('gallery.created'), 'success')
      onCreated()
    } catch (err: unknown) {
      showToast(t('gallery.create_failed', { error: String((err as Error)?.message ?? err) }), 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={submit} class="sh-card" style={{ marginBottom: '1rem' }}>
      <label>
        {t('gallery.form.name')}
        <input
          type="text"
          maxLength={80}
          value={name}
          onInput={(e) => setName((e.target as HTMLInputElement).value)}
          placeholder={t('gallery.form.name_placeholder')}
          required
        />
      </label>
      <label>
        {t('gallery.form.description')}
        <textarea
          maxLength={500}
          value={description}
          onInput={(e) => setDescription((e.target as HTMLTextAreaElement).value)}
        />
      </label>
      <div class="sh-form-actions">
        <Button variant="secondary" type="button" onClick={onClose}>{t('common.cancel')}</Button>
        <Button type="submit" loading={busy} disabled={!name.trim()}>{t('gallery.form.create')}</Button>
      </div>
    </form>
  )
}

function albumsUrl(spaceId?: string): string {
  return spaceId ? `/api/spaces/${spaceId}/gallery/albums` : '/api/gallery/albums'
}

/** ``?before=&before_id=`` for the page after ``row``. */
function cursor(row: { created_at?: string; id: string }): string {
  return `&before=${encodeURIComponent(row.created_at ?? '')}&before_id=${encodeURIComponent(row.id)}`
}

/** Append ``next`` to ``rows``, skipping ids already there (a row can
 *  arrive twice when something was added between two pages). */
function appendNew<T extends { id: string }>(rows: T[], next: T[]): T[] {
  const seen = new Set(rows.map(r => r.id))
  return [...rows, ...next.filter(r => !seen.has(r.id))]
}

/** Fetch the album list's first page (or, ``quiet``, re-read as many as
 *  are loaded). ``quiet`` is the live-refresh path: no spinner, and a
 *  failure keeps the list on screen (``toastOnFail`` says so). A failed
 *  first load shows the error state with Retry — never "No albums yet".
 *  The open album's header (name / cover) follows the fresh row. */
async function loadAlbums(
  spaceId?: string, { quiet = false, toastOnFail = false } = {},
) {
  if (!quiet) loading.value = true
  const scope = spaceId ?? null
  const limit = quiet
    ? Math.min(ALBUM_MAX, Math.max(ALBUM_PAGE, albums.value.length))
    : ALBUM_PAGE
  try {
    const rows = await api.get(`${albumsUrl(spaceId)}?limit=${limit}`) as Album[]
    if (loadedScope !== scope) return  // the page moved to another gallery
    albums.value = rows
    albumsMore.value = rows.length >= limit
    albumsError.value = false
    const open = activeAlbum.value
    const fresh = open ? rows.find(a => a.id === open.id) : undefined
    if (fresh) activeAlbum.value = fresh
  } catch {
    if (loadedScope !== scope) return
    if (albums.value.length === 0) albumsError.value = true
    else if (!quiet || toastOnFail) showToast(t('gallery.refresh_failed'), 'error')
  } finally {
    if (!quiet) loading.value = false
  }
}

async function loadMoreAlbums(spaceId?: string): Promise<void> {
  const scope = spaceId ?? null
  // The cursor is the last *regular* album: the system album is pinned
  // to the first page whatever its age.
  const last = [...albums.value].reverse().find(a => !a.is_system)
  if (!last) { albumsMore.value = false; return }
  try {
    const rows = await api.get(
      `${albumsUrl(spaceId)}?limit=${ALBUM_PAGE}${cursor(last)}`,
    ) as Album[]
    if (loadedScope !== scope) return
    albums.value = appendNew(albums.value, rows)
    albumsMore.value = rows.length >= ALBUM_PAGE
  } catch {
    showToast(t('gallery.refresh_failed'), 'error')
  }
}

/** Open the album the address names: from the list when it is loaded,
 *  else read on its own (a link, a refresh). ``false`` when it doesn't
 *  exist here (gone, or another gallery's); a network failure is the
 *  album's error state with Retry, never "gone". */
async function openAlbum(id: string, spaceId?: string): Promise<boolean> {
  const scope = spaceId ?? null
  items.value = []
  itemsMore.value = false
  albumError.value = false
  let album = albums.value.find(a => a.id === id)
  if (!album) {
    activeAlbum.value = null
    try {
      album = await api.get(`/api/gallery/albums/${encodeURIComponent(id)}`) as Album
    } catch (err) {
      if (wantedAlbum !== id || loadedScope !== scope) return true
      if (err instanceof ApiError && err.status >= 400 && err.status < 500) return false
      albumError.value = true
      return true
    }
    if (wantedAlbum !== id || loadedScope !== scope) return true
    // A household link must not open a space's album, or the reverse.
    if ((album.space_id ?? null) !== scope) return false
  }
  activeAlbum.value = album
  await loadItems(id)
  return true
}

/** Fetch the open album's first page (or, ``quiet``, re-read as many as
 *  are loaded — an upload or a ready video shows up without losing the
 *  pages already scrolled through). */
async function loadItems(albumId: string, { quiet = false } = {}) {
  const limit = quiet
    ? Math.min(ITEM_MAX, Math.max(ITEM_PAGE, items.value.length))
    : ITEM_PAGE
  if (!quiet) itemsLoading.value = true
  try {
    const rows = await api.get(
      `/api/gallery/albums/${albumId}/items?limit=${limit}`,
    ) as Item[]
    if (activeAlbum.value?.id !== albumId) return  // another album is open now
    items.value = rows
    itemsMore.value = rows.length >= limit
  } catch {
    if (activeAlbum.value?.id !== albumId) return
    showToast(
      items.value.length ? t('gallery.refresh_failed') : t('gallery.load_items_error'),
      'error',
    )
  } finally {
    if (!quiet) itemsLoading.value = false
  }
}

async function loadMoreItems(albumId: string): Promise<void> {
  const last = items.value[items.value.length - 1]
  if (!last) { itemsMore.value = false; return }
  try {
    const rows = await api.get(
      `/api/gallery/albums/${albumId}/items?limit=${ITEM_PAGE}${cursor(last)}`,
    ) as Item[]
    if (activeAlbum.value?.id !== albumId) return
    items.value = appendNew(items.value, rows)
    itemsMore.value = rows.length >= ITEM_PAGE
  } catch {
    showToast(t('gallery.load_items_error'), 'error')
  }
}
