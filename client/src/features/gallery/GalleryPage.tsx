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
 * pinned first, then newest activity first — the album someone last
 * added to on top), an album's items 50 at a time by the day they were
 * taken (newest first, or oldest first — remembered per album, see
 * ``albumOrder``), under month and day headings. The next page is asked
 * for with the last row's cursor, from a "Load more" button that also
 * fires by itself when it scrolls into view — so every album and every
 * upload can be reached.
 *
 * The household list opens with a "♡ Favourites" chip — your own
 * favourites across every album (``FavouritesPage``; private, D12) — and
 * the "On this day" card (``OnThisDay``),
 * a first-run screen when the household has no photos yet, and an
 * honest offline banner that keeps what was loaded on screen.
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
import { householdDisplayName, householdUsers, loadHouseholdUsers } from '@/store/householdUsers'
import { spaceMembers, loadSpaceMembers } from '@/store/spaceMembers'
import { failedMedia, mediaFilename, readyMedia } from '@/store/mediaReady'
import { personLabel } from '@/utils/names'
import { formatDayKey } from '@/utils/calendar'
import { UploadSheet } from './UploadSheet'
import { counts, type UploadTarget } from './uploadQueue'
import { UploadButtons, useUploadControls } from './GalleryUpload'
import { albumDisplayName } from './albumName'
import {
  groupByMonthDay, readAlbumOrder, sortParam, takenCursor, writeAlbumOrder,
  type AlbumOrder,
} from './albumOrder'
import { OnThisDayCardView, OnThisDayNote, loadOnThisDay } from './OnThisDay'
import { noteAlbumSeen, withoutDot } from './albumSeen'
import { OverflowMenu } from '@/components/OverflowMenu'
import { confirmDialog } from '@/components/confirm'
import { pendingDeletes } from '@/utils/undoableDelete'
import TrashPage from './TrashPage'
import FavouritesPage, { openedFavouritesFromList } from './FavouritesPage'
import { HeartIcon } from './HeartIcon'
import { SelectionBar, type SelectionAction } from './SelectionBar'
import { useSelection, type Selection } from './useSelection'
import { MoveToAlbumDialog } from './MoveToAlbumDialog'
import { ShareSheet } from './ShareSheet'
import { SHARE_MAX, type ShareItem } from './gallerySharing'
import { EditItemDialog } from './EditItemDialog'
import { AlbumMenu } from './AlbumMenu'
import {
  bulk, deleteLaterWithUndo, downloadError, downloadItems, itemsWord, othersLine, setFavourite,
  trashWithUndo,
} from './galleryManage'
import { TRASH_PATH, favouritesPath } from './galleryRoutes'
import { AudienceChooser, AudienceSheet, type AudienceChoice } from './AudienceSheet'
import { loadGallerySettings, mayAddToGallery } from './gallerySettings'

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
  /** Newest activity: the newest photo's upload time, else
   *  ``created_at`` — the list's order and its cursor. */
  activity_at?: string
  /** The household's default album; ``default_name`` while nobody
   *  renamed it (shown in the reader's own language). */
  is_default?: boolean
  default_name?: boolean
  /** Someone else added photos since the viewer last opened it — the
   *  "neu" dot (D9; ``albumSeen``). */
  has_new?: boolean
  /** Who sees a household album (D7): ``chosen`` = "Private – only these
   *  people". */
  audience?: 'everyone' | 'chosen'
  /** A private album a household admin isn't in: a locked tile with no
   *  name or cover — they may put it in the trash, never open it. */
  locked?: boolean
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
  /** The day it sorts by — taken, else uploaded. */
  day?: string
  created_at?: string
  duration_s?: number | null
  /** Background-transcode state for ``video`` items — ``'processing'``
   *  while the worker encodes. Absent on photos and on older payloads
   *  (treated as ready). A processing item can't play yet, so the tile
   *  shows a "Processing…" overlay and doesn't open the lightbox. */
  media_status?: 'processing' | 'failed' | 'ready'
  /** While ``processing``: rough seconds until the video is ready (the
   *  household converts one video at a time). */
  media_eta_s?: number
  /** One of the viewer's own favourites (♡, D12) — only ever theirs. */
  is_favourite?: boolean
}

/** "Ready in about 4 min" for a video tile still converting, or ``null``
 *  when the server gave no estimate. */
export function readyInLabel(etaS: number | undefined): string | null {
  if (typeof etaS !== 'number' || !Number.isFinite(etaS) || etaS <= 0) return null
  if (etaS < 60) return t('gallery.item_ready_in_soon')
  return t('gallery.item_ready_in_min', { n: String(Math.ceil(etaS / 60)) })
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
/** The list could not be read at all: ``'offline'`` (no answer from
 *  home — the network, or the proxy in front of it) or ``'server'``. */
const albumsError  = signal<false | 'offline' | 'server'>(false)
const items        = signal<Item[]>([])
const itemsMore    = signal(false)
const itemsLoading = signal(false)
const activeAlbum  = signal<Album | null>(null)
/** The album in the address could not be read (offline) — not "gone". */
const albumError   = signal(false)
const loading      = signal(true)
const showCreate   = signal(false)
/** The open album's order (by date taken), as this person last left it. */
const itemOrder    = signal<AlbumOrder>('newest')
/** The albums on screen may be out of date: the last refresh failed or
 *  the browser went offline. They stay; a banner says so. */
const albumsStale  = signal(false)
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
/** The trash was opened from the albums' ⋯ (its Back steps back). */
let trashFromList = false
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
  // Household (D6): only who added it, or a household admin — the album's
  // owner deletes the album as a whole, not others' photos one by one.
  return !!me.is_admin
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
    else {
      void loadHouseholdUsers()
      // "On this day" is the household gallery's (D14), never a space's.
      void loadOnThisDay()
      // May this person add photos here (a child, D6)?
      void loadGallerySettings()
    }
  }, [spaceId])

  // Offline: what is on screen stays, a banner says it may be out of
  // date; back online, re-read quietly (that clears the banner).
  useEffect(() => {
    const offline = () => { albumsStale.value = true }
    const online = () => {
      void loadAlbums(spaceId, { quiet: true })
      if (!spaceId) void loadOnThisDay()
    }
    if (typeof navigator !== 'undefined' && navigator.onLine === false) offline()
    window.addEventListener('offline', offline)
    window.addEventListener('online', online)
    return () => {
      window.removeEventListener('offline', offline)
      window.removeEventListener('online', online)
    }
  }, [spaceId])

  // An address the gallery doesn't know → the albums. A space has no
  // trash (its deletes go out to the other households), and favourites
  // are one list across every album, under the household gallery.
  const valid = where.valid && !((where.trash || where.favourites) && spaceId)
  useEffect(() => {
    if (!valid) locRef.current.route(galleryPath(spaceId), true)
  }, [valid, spaceId])

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
    // A selection deletes, restores or moves many photos at once — one
    // frame each. Re-read once for the burst, not once per frame.
    let timer: ReturnType<typeof setTimeout> | null = null
    let albumDirty = false
    const refreshSoon = (touchesOpen: boolean) => {
      albumDirty = albumDirty || touchesOpen
      if (timer) clearTimeout(timer)
      timer = setTimeout(() => {
        timer = null
        void loadAlbums(spaceId, { quiet: true })
        if (albumDirty && activeAlbum.value) void loadItems(activeAlbum.value.id, { quiet: true })
        albumDirty = false
      }, 300)
    }
    const handle = (e: GalleryFrame & { data: { from_album_id?: string | null } }) => {
      if (!inScope(e)) return
      const open = activeAlbum.value?.id
      refreshSoon(!!open && (e.data.album_id === open || e.data.from_album_id === open))
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
      // Household only (no ``space_id``): back from the trash, a new
      // caption / date, a move between albums.
      ws.on('gallery.item_restored', handle),
      ws.on('gallery.item_updated', handle),
      ws.on('media.ready', onMediaDone),
      ws.on('media.failed', onMediaDone),
    ]
    return () => {
      if (timer) clearTimeout(timer)
      offs.forEach((off) => off())
    }
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
  // A child while "Children may add photos" is off (D6) adds nothing to
  // the household gallery.
  const mayAdd = spaceId ? true : mayAddToGallery()
  const listCanUpload = !readOnly && mayAdd && !albumsError.value
    && (!spaceId || regular.length > 0)
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

  if (where.trash && !spaceId) return <TrashPage fromList={trashFromList} />
  if (where.favourites && !spaceId) return <>{sheet}<FavouritesPage itemId={itemId} /></>

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
  // First run: a gallery (the household's or a space's) with no photos and
  // no album of its own yet (the auto "Posts" album may exist, empty). One
  // screen that explains, one big button.
  const firstRun = !albumsError.value && !albumsMore.value
    && regular.length === 0 && itemCount === 0

  return (
    <div class="sh-gallery">
      {sheet}
      {listControls.elements}
      <h1 class="sr-only">{t('nav.gallery')}</h1>
      {albumsStale.value && !albumsError.value && (
        <OfflineBanner onRetry={() => void loadAlbums(spaceId, { quiet: true, toastOnFail: true })} />
      )}
      {!albumsError.value && !firstRun && (
        <header class="sh-gallery-hero">
          <div class="sh-gallery-hero-headline">
            <strong>{albumCount}{more}</strong>{' '}
            {t(isOne(albumCount) && !more ? 'gallery.albums_word_one' : 'gallery.albums_word')} ·{' '}
            <strong>{itemCount}{more}</strong>{' '}
            {t(isOne(itemCount) && !more ? 'gallery.media_word_one' : 'gallery.media_word')}
          </div>
          {!readOnly && (
            <div class="sh-gallery-hero-actions sh-upload-actions">
              {mayAdd && (
                <Button
                  variant={listCanUpload ? 'secondary' : 'primary'}
                  onClick={() => (showCreate.value = true)}
                >
                  {t('gallery.new_album_btn')}
                </Button>
              )}
              {listCanUpload && <UploadButtons controls={listControls} />}
              {!spaceId && (
                <OverflowMenu
                  label={t('gallery.manage.gallery_menu')}
                  items={[{
                    key: 'trash',
                    label: t('gallery.trash.title'),
                    onSelect: () => { trashFromList = true; loc.route(TRASH_PATH) },
                  }]}
                  menuWidth={200}
                ><span aria-hidden="true">⋯</span></OverflowMenu>
              )}
            </div>
          )}
        </header>
      )}

      {!mayAdd && !readOnly && (
        <p class="sh-gallery-note" role="note">{t('gallery.children.off_note')}</p>
      )}

      {!spaceId && !albumsError.value && !firstRun && (
        // A filter, not an album: a photo lives in exactly one album (D1),
        // and favourites are each person's own (D12).
        <nav class="sh-gallery-chips" aria-label={t('gallery.fav.chips_aria')}>
          <a
            class="sh-chip sh-gallery-chip"
            href={addBase(favouritesPath())}
            onClick={openedFavouritesFromList}
          >
            <HeartIcon class="sh-gallery-chip__heart" /> {t('gallery.fav.title')}
          </a>
        </nav>
      )}

      {!spaceId && !albumsError.value && <OnThisDayCardView />}

      {showCreate.value && !readOnly && mayAdd && (
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
          <div aria-hidden="true">{albumsError.value === 'offline' ? '⚡' : '📡'}</div>
          <h2>{albumsError.value === 'offline' ? t('gallery.offline_title') : t('gallery.load_error_title')}</h2>
          <p>{albumsError.value === 'offline' ? t('gallery.offline_first_body') : t('gallery.load_error_body')}</p>
          <Button onClick={() => void loadAlbums(spaceId)}>{t('common.retry')}</Button>
        </div>
      ) : firstRun ? (
        <FirstRun
          controls={listControls}
          inSpace={!!spaceId}
          canUpload={listCanUpload}
          canCreate={!readOnly && mayAdd}
          onCreate={() => (showCreate.value = true)}
          creating={showCreate.value}
        />
      ) : (
        <>
          <div class="sh-album-grid">
            {albums.value.filter(a => !pendingDeletes.value.has(a.id)).map(a => a.locked ? (
              <LockedAlbumCard key={a.id} album={a} onDeleted={() => void loadAlbums(spaceId, { quiet: true })} />
            ) : (
              // A real link: its own address, opens in a new tab, and
              // the router keeps the ingress prefix.
              <a
                key={a.id}
                href={addBase(galleryPath(spaceId, a.id))}
                class={`sh-album-card${a.is_system ? ' sh-album-card--system' : ''}`}
                aria-label={t(isOne(a.item_count) ? 'gallery.open_album_aria_one' : 'gallery.open_album_aria', { name: albumDisplayName(a), n: String(a.item_count) })
                  + (a.has_new ? `, ${t('gallery.new_photos')}` : '')}
                onClick={() => { albumFromList = true }}
              >
                <AlbumCover album={a} />
                {a.has_new && (
                  <span class="sh-album-new" title={t('gallery.new_photos')}>
                    {t('gallery.new_badge')}
                  </span>
                )}
                <div class="sh-album-info">
                  <strong>
                    {albumDisplayName(a)}
                    {a.is_system && (
                      <span class="sh-album-system-badge"
                            title={t('gallery.auto_title')}>
                        <span aria-hidden="true">🔒</span> {t('gallery.auto')}
                      </span>
                    )}
                    {a.audience === 'chosen' && <PrivateBadge />}
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
      {!spaceId && !albumsError.value && <OnThisDayNote />}
    </div>
  )
}

/** The albums on screen may be out of date: home can't be reached right
 *  now. Nothing is hidden, and uploads wait (the queue resumes online). */
function OfflineBanner({ onRetry }: { onRetry: () => void }) {
  // The app's connection bar already says "offline" / "can't reach your
  // home"; this line says what it means here: nothing is gone, and
  // uploads wait.
  return (
    <div class="sh-gallery-offline" role="status">
      <span class="sh-gallery-offline__icon" aria-hidden="true">⚡</span>
      <span class="sh-gallery-offline__text">{t('gallery.offline_body')}</span>
      <button
        type="button"
        class="sh-gallery-offline__retry"
        aria-label={t('common.retry')}
        title={t('common.retry')}
        onClick={onRetry}
      >
        <span aria-hidden="true">↻</span>
      </button>
    </div>
  )
}

/** A gallery with no photos yet: what it is, who sees it, and one big
 *  button. In the household that is "Add photos" — the files go into the
 *  default album, so nobody has to make an album first. A space has no
 *  default album, so its first step is "Make an album" (a follower, who
 *  can do neither, just reads what the gallery is for). */
function FirstRun({ controls, inSpace, canUpload, canCreate, onCreate, creating }: {
  controls: ReturnType<typeof useUploadControls>
  inSpace: boolean
  canUpload: boolean
  canCreate: boolean
  onCreate: () => void
  creating: boolean
}) {
  return (
    <section class="sh-gallery-first" aria-labelledby="sh-gallery-first-title">
      <div class="sh-gallery-first__art" aria-hidden="true">
        <span>🏔️</span><span>📷</span><span>🌅</span>
      </div>
      <h2 id="sh-gallery-first-title">
        {t(inSpace ? 'gallery.first.title_space' : 'gallery.first.title')}
      </h2>
      <p class="sh-muted">{t(inSpace ? 'gallery.first.body_space' : 'gallery.first.body')}</p>
      {!inSpace && canUpload && (
        <div class="sh-gallery-first__actions">
          <Button onClick={controls.pick}>{t('gallery.first.add')}</Button>
          <span class="sh-upload-camera-btn">
            <Button
              variant="secondary"
              aria-label={t('gallery.upload.camera_aria')}
              onClick={controls.camera}
            >
              <span aria-hidden="true">📷</span> {t('gallery.upload.camera')}
            </Button>
          </span>
          <p class="sh-gallery-first__drop sh-muted">{t('gallery.first.drop')}</p>
        </div>
      )}
      {inSpace && canCreate && !creating && (
        <div class="sh-gallery-first__actions">
          <Button onClick={onCreate}>{t('gallery.first.make_first_album')}</Button>
        </div>
      )}
      {!inSpace && (
        <p class="sh-gallery-first__hint sh-muted">
          {t('gallery.first.default_album', { name: t('gallery.upload.default_album') })}{' '}
          {!creating && (
            <button type="button" class="sh-link-button" onClick={onCreate}>
              {t('gallery.first.make_album')}
            </button>
          )}
        </p>
      )}
      <p class="sh-gallery-first__hint sh-muted">
        {t(inSpace ? 'gallery.first.feed_hint_space' : 'gallery.first.feed_hint')}
      </p>
    </section>
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

  // A space delete waits 8 s for its Undo before it is sent: hidden here
  // meanwhile.
  const loaded = items.value.filter(i => !pendingDeletes.value.has(i.id))
  // No uploads into the system album (the server refuses them too), nor
  // by a child while "Children may add photos" is off (D6).
  const canUpload = !album.is_system && !readOnly && (!!album.space_id || mayAddToGallery())
  const [audienceOpen, setAudienceOpen] = useState(false)
  const controls = useUploadControls({
    spaceId: album.space_id,
    albumId: album.id,
    name: albumDisplayName(album),
    isDefault: false,
  }, canUpload)
  const manage = useAlbumManage(album, loaded, canModerate, readOnly)
  const sel = manage.sel

  return (
    <div class="sh-album-detail">
      <header class="sh-page-header">
        <Button variant="secondary" onClick={onBack}>← {t('gallery.albums')}</Button>
        <h1 style={{ margin: 0 }}>
          {albumDisplayName(album)}
          {album.is_system && (
            <span class="sh-album-system-badge"
                  style={{ marginLeft: 'var(--sh-space-xs)' }}>
              <span aria-hidden="true">🔒</span> {t('gallery.auto')}
            </span>
          )}
          {album.audience === 'chosen' && (
            <button
              type="button"
              class="sh-album-private-badge sh-album-private-badge--button"
              onClick={() => setAudienceOpen(true)}
              aria-label={t('gallery.audience.who_aria')}
            >
              <span aria-hidden="true">👥</span> {t('gallery.audience.badge')}
            </button>
          )}
        </h1>
        <div class="sh-row sh-upload-actions sh-album-actions">
          {album.item_count > 1 && !sel.active && <OrderToggle albumId={album.id} />}
          {canUpload && <UploadButtons controls={controls} />}
          {loaded.length > 0 && !sel.active && (
            <Button variant="secondary" onClick={() => sel.start()}>
              {t('gallery.manage.select')}
            </Button>
          )}
          {manage.canManageAlbum && (
            <AlbumMenu
              album={album}
              onAudience={!album.space_id && album.owner_user_id === currentUser.value?.user_id
                ? () => setAudienceOpen(true)
                : undefined}
              covers={loaded.filter(i => !isProcessing(i))}
              onChanged={() => void loadAlbums(spaceId, { quiet: true })}
              onDeleted={onBack}
              onRestored={() => void loadAlbums(spaceId, { quiet: true })}
            />
          )}
        </div>
      </header>
      {controls.elements}
      {manage.elements}
      {audienceOpen && (
        <AudienceSheet
          albumId={album.id}
          albumName={albumDisplayName(album)}
          onClose={() => setAudienceOpen(false)}
          onSaved={() => void loadAlbums(spaceId, { quiet: true })}
        />
      )}

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
          {groupByMonthDay(loaded).map(m => (
            <section key={m.month} class="sh-album-month" aria-labelledby={`sh-month-${m.month}`}>
              <h2 id={`sh-month-${m.month}`} class="sh-album-month__title">{monthLabel(m.month)}</h2>
              {m.days.map(d => (
                <div key={d.day} class="sh-album-day">
                  <h3 class="sh-album-day__title">
                    {dayLabel(d.day)}
                    {byLine(d.items, album.space_id)}
                  </h3>
                  <div class="sh-image-grid">
                    {d.items.map(item => (
                      <Tile
                        key={item.id} item={item} albumId={album.id} spaceId={spaceId} scope={album.space_id}
                        selecting={sel.active} picked={sel.ids.has(item.id)} handlers={sel.tileHandlers(item.id)}
                      />
                    ))}
                  </div>
                </div>
              ))}
            </section>
          ))}
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
  const [editing, setEditing] = useState<Item | null>(null)
  const [sharing, setSharing] = useState<Item | null>(null)
  const lastItem = useRef<string | null>(null)
  const loaded = items.value.filter(i => !pendingDeletes.value.has(i.id))
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
  // TODO(gallery PR 13, space parity, proto v_79): caption / date edits
  // in a space need a federation event — household photos only for now.
  const household = !album.space_id && !album.is_system
  return (
    <>
    {editing && (
      <EditItemDialog
        item={editing}
        onClose={() => setEditing(null)}
        onSaved={(it) => { items.value = items.value.map(i => i.id === it.id ? { ...i, ...it } : i) }}
      />
    )}
    {sharing && (
      <ShareSheet
        items={[asShareItem(sharing)]}
        nameOf={(uid) => uploaderName(album.space_id, uid)}
        onClose={() => setSharing(null)}
        onShared={(message, result) => showToast(message, result.shared > 0 ? 'success' : 'info')}
      />
    )}
    <GalleryViewer
      items={viewerItems}
      index={index}
      total={album.item_count}
      albumName={albumDisplayName(album)}
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
      canEdit={(it) => household && canDeleteItem(album, it, canModerate)}
      onEdit={(it) => {
        const row = items.value.find(i => i.id === it.id)
        if (row) setEditing(row)
      }}
      onShare={album.space_id ? undefined : (it) => {
        const row = items.value.find(i => i.id === it.id)
        if (row) setSharing(row)
      }}
      onDownload={async (it) => {
        try {
          await downloadItems([it.id], albumDisplayName(album))
        } catch (err) {
          showToast(downloadError(err), 'error')
        }
      }}
      isFavourite={(it) => !!items.value.find(i => i.id === it.id)?.is_favourite}
      onFavourite={async (it, on) => {
        await setFavourite(it.id, on)
        items.value = items.value.map(i => i.id === it.id ? { ...i, is_favourite: on } : i)
      }}
      onDelete={async (it) => {
        const pos = loaded.findIndex(i => i.id === it.id)
        const rest = loaded.filter(i => i.id !== it.id)
        const next = rest[pos] ?? rest[pos - 1]
        const what = itemsWord([it])
        const refresh = () => {
          void loadItems(album.id, { quiet: true })
          void loadAlbums(spaceId, { quiet: true })
        }
        // Move on first — the viewer never sees an address whose item
        // is gone.
        const moveOn = () => {
          if (next) locRef.current.route(galleryPath(spaceId, album.id, next.id), true)
          else close()
        }
        if (album.space_id) {
          // No trash in a space: held for 8 s, sent when Undo runs out.
          moveOn()
          deleteLaterWithUndo([it.id], t('gallery.manage.deleted_space', { what }), refresh)
        } else {
          const res = await trashWithUndo([it.id], () => t('gallery.manage.trashed', { what }), refresh)
          if (!res.done.length) throw new Error('refused')
          // The re-read drops the tile once the address has moved on.
          moveOn()
          refresh()
        }
        if (activeAlbum.value?.id === album.id) {
          activeAlbum.value = { ...album, item_count: Math.max(0, album.item_count - 1) }
        }
      }}
    />
    </>
  )
}

/** One tile: a real link to the photo's own address (the viewer opens on
 *  it, and it can be shared or opened in a new tab) — or, for a video
 *  still transcoding, a "Processing…" placeholder that opens nothing. */
function Tile({ item, albumId, spaceId, scope, selecting, picked, handlers }: {
  item: Item
  albumId: string
  spaceId?: string
  scope: string | null
  /** Selecting: the tile is a checkbox (Space / Enter toggles). */
  selecting: boolean
  picked: boolean
  /** ``useSelection``: a long press (touch) or Ctrl / ⌘ / Shift + click
   *  starts selecting; while selecting a click toggles. */
  handlers: ReturnType<Selection['tileHandlers']>
}) {
  if (isProcessing(item)) {
    const readyIn = readyInLabel(item.media_eta_s)
    return (
      <div
        class="sh-gallery-item sh-gallery-item--processing"
        role="img"
        data-item-id={item.id}
        aria-label={
          t('gallery.item_processing_aria', { type: itemTypeLabel(item.item_type) })
          + (readyIn ? ` · ${readyIn}` : '')
        }
      >
        <span class="sh-gallery-item-processing" aria-hidden="true">
          <span class="sh-upload-progress__spinner" />
          <span class="sh-gallery-item-processing-msg">{t('media.processing')}</span>
          {readyIn && (
            <span class="sh-gallery-item-processing-eta">{readyIn}</span>
          )}
        </span>
      </div>
    )
  }
  const media = (
    <>
      <img src={item.thumbnail_url} alt="" loading="lazy" decoding="async" />
      {item.item_type === 'video' && (
        <span class="sh-video-badge" aria-hidden="true">▶</span>
      )}
      {item.is_favourite && <span class="sh-fav-badge" aria-hidden="true"><HeartIcon size={14} /></span>}
    </>
  )
  if (selecting) {
    return (
      <button
        type="button"
        role="checkbox"
        aria-checked={picked}
        class={'sh-gallery-item sh-gallery-item--selectable' + (picked ? ' sh-gallery-item--picked' : '')}
        data-item-id={item.id}
        aria-label={tileLabel(item, scope)}
        {...handlers}
      >
        {media}
        <span class="sh-select-check" aria-hidden="true" />
      </button>
    )
  }
  return (
    <a
      href={addBase(galleryPath(spaceId, albumId, item.id))}
      class="sh-gallery-item"
      data-item-id={item.id}
      aria-label={tileLabel(item, scope)}
      {...handlers}
      onClick={(e: MouseEvent) => {
        handlers.onClick(e)
        if (!e.defaultPrevented) viewerFromGrid = true
      }}
    >
      {media}
    </a>
  )
}

/** "Newest first ⇅" / "Oldest first ⇅": shows the order and flips it.
 *  Remembered for this album and this person; the album reloads from
 *  the first page in the new order. */
function OrderToggle({ albumId }: { albumId: string }) {
  const order = itemOrder.value
  const next: AlbumOrder = order === 'newest' ? 'oldest' : 'newest'
  const flip = () => {
    itemOrder.value = next
    writeAlbumOrder(currentUser.value?.user_id, albumId, next)
    void loadItems(albumId)
  }
  return (
    <Button
      variant="ghost"
      onClick={flip}
      aria-label={t(order === 'newest' ? 'gallery.order.to_oldest_aria' : 'gallery.order.to_newest_aria')}
    >
      {t(order === 'newest' ? 'gallery.order.newest' : 'gallery.order.oldest')}{' '}
      <span aria-hidden="true">⇅</span>
    </Button>
  )
}

/** "November 2026" for ``YYYY-MM``. */
function monthLabel(month: string): string {
  if (!/^\d{4}-\d{2}$/.test(month)) return t('gallery.day_unknown')
  return formatDayKey(`${month}-01`, { month: 'long', year: 'numeric' })
}

/** "Sun, 1 Nov" for ``YYYY-MM-DD``. */
function dayLabel(day: string): string {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return t('gallery.day_unknown')
  return formatDayKey(day, { weekday: 'short', day: 'numeric', month: 'short' })
}

/** " · by Anna, Lena" — who added the day's photos, as far as this
 *  browser knows their names (never an id). */
function byLine(dayItems: Item[], scope: string | null) {
  const names: string[] = []
  for (const i of dayItems) {
    const n = uploaderName(scope, i.uploaded_by)
    if (n && !names.includes(n)) names.push(n)
  }
  if (names.length === 0) return null
  const shown = names.length > 3
    ? t('gallery.by_more', { names: names.slice(0, 3).join(', '), n: String(names.length - 3) })
    : names.join(', ')
  return <span class="sh-album-day__by"> · {t('gallery.by', { names: shown })}</span>
}

/** May this person rename, re-cover or delete the album? The owner, a
 *  household admin (household) or a space moderator (space) — the server
 *  checks again. Never the feed's "Posts" album. */
function canManageAlbum(album: Album, canModerate: boolean, readOnly: boolean): boolean {
  const me = currentUser.value
  if (!me || album.is_system || readOnly) return false
  if (album.owner_user_id === me.user_id) return true
  if (me.protected) return false
  return album.space_id ? canModerate : !!me.is_admin
}

/** Selection in an album: the bar, its actions, and their dialogs. */
function useAlbumManage(album: Album, loaded: Item[], canModerate: boolean, readOnly: boolean) {
  const sel = useSelection(loaded.map(i => i.id))
  const [moving, setMoving] = useState(false)
  const [sharing, setSharing] = useState(false)
  const [busy, setBusy] = useState<'save' | 'delete' | 'favourite' | null>(null)
  const spaceId = album.space_id ?? undefined
  const picked = loaded.filter(i => sel.ids.has(i.id))
  const refresh = () => {
    void loadItems(album.id, { quiet: true })
    void loadAlbums(spaceId, { quiet: true })
  }
  const nameOf = (uid: string) => uploaderName(album.space_id, uid)

  const remove = async () => {
    if (!picked.length) return
    const mine = picked.filter(i => canDeleteItem(album, i, canModerate))
    const others = picked.filter(i => !canDeleteItem(album, i, canModerate))
    if (!mine.length) {
      showToast(t('gallery.manage.none_deletable', { names: othersLine(others, nameOf) }), 'error')
      return
    }
    if (others.length) {
      const ok = await confirmDialog(
        t(isOne(others.length) ? 'gallery.manage.partial_body_one' : 'gallery.manage.partial_body', {
          n: String(others.length), names: othersLine(others, nameOf),
        }),
        {
          title: t('gallery.manage.partial_title', { n: String(mine.length), total: String(picked.length) }),
          confirmLabel: t('gallery.manage.partial_confirm', { n: String(mine.length) }),
          destructive: true,
        },
      )
      if (!ok) return
    }
    const ids = mine.map(i => i.id)
    const what = itemsWord(mine)
    if (album.space_id) {
      deleteLaterWithUndo(ids, t('gallery.manage.deleted_space', { what }), refresh)
      sel.keep(others.map(i => i.id))
      return
    }
    setBusy('delete')
    try {
      const res = await trashWithUndo(ids, (n) => t('gallery.manage.trashed', {
        what: itemsWord(mine.slice(0, n)),
      }), refresh)
      items.value = items.value.filter(i => !res.done.includes(i.id))
      sel.keep([...others.map(i => i.id), ...res.refused])
      refresh()
    } catch {
      showToast(t('gallery.delete_failed'), 'error')
    } finally {
      setBusy(null)
    }
  }

  const save = async () => {
    if (!picked.length) return
    setBusy('save')
    try {
      await downloadItems(picked.map(i => i.id), albumDisplayName(album))
    } catch (err) {
      showToast(downloadError(err), 'error')
    } finally {
      setBusy(null)
    }
  }

  // A space photo can't change album yet: the other member households
  // would never hear of it (no federation event for a move).
  // TODO(gallery PR 13, space parity, proto v_79): move within a space.
  const movable = !album.space_id && !album.is_system
    && picked.some(i => canDeleteItem(album, i, canModerate))
  const move = async (toId: string, toName: string) => {
    const mine = picked.filter(i => canDeleteItem(album, i, canModerate))
    try {
      const res = await bulk('move', mine.map(i => i.id), toId)
      items.value = items.value.filter(i => !res.done.includes(i.id))
      refresh()
      if (res.done.length) {
        showToast(t('gallery.manage.moved', {
          what: itemsWord(mine.filter(i => res.done.includes(i.id))), name: toName,
        }), 'success')
      }
      const left = picked.length - res.done.length
      if (left > 0) showToast(t('gallery.manage.move_partial', { n: String(left) }), 'info')
      sel.keep(picked.filter(i => !res.done.includes(i.id)).map(i => i.id))
    } catch (err) {
      showToast(String((err as Error)?.message ?? t('gallery.manage.move_failed')), 'error')
      throw err
    }
  }

  // ♡ the selection — or, when every picked photo is a favourite already,
  // take them off. Private to this person (D12).
  const allFavourite = picked.length > 0 && picked.every(i => i.is_favourite)
  const favourite = async () => {
    if (!picked.length) return
    const on = !allFavourite
    setBusy('favourite')
    try {
      const res = await bulk(on ? 'favourite' : 'unfavourite', picked.map(i => i.id))
      const done = new Set(res.done)
      items.value = items.value.map(i => done.has(i.id) ? { ...i, is_favourite: on } : i)
      const what = itemsWord(picked.filter(i => done.has(i.id)))
      if (done.size) showToast(t(on ? 'gallery.fav.added' : 'gallery.fav.removed', { what }), 'success')
      sel.clear()
    } catch {
      showToast(t('gallery.fav.failed'), 'error')
    } finally {
      setBusy(null)
    }
  }

  const deletable = !readOnly && !album.is_system
  // D8: a copy of household photos goes to the feed, a space or a chat.
  // A space's own gallery shares nothing out from here.
  const shareable = !album.space_id
  const share = () => {
    if (picked.length > SHARE_MAX) {
      showToast(t('gallery.share.too_many', { n: String(SHARE_MAX) }), 'error')
      return
    }
    setSharing(true)
  }
  const actions: SelectionAction[] = [
    {
      key: 'favourite', icon: 'favourite',
      label: t(allFavourite ? 'gallery.fav.remove' : 'gallery.fav.add'),
      onClick: () => void favourite(), busy: busy === 'favourite',
    },
    ...(shareable ? [{ key: 'share', icon: 'share', label: t('gallery.share.action'), onClick: share }] : []),
    ...(movable ? [{ key: 'move', icon: 'move', label: t('gallery.manage.move'), onClick: () => setMoving(true) }] : []),
    { key: 'save', icon: 'save', label: t('gallery.manage.save'), onClick: () => void save(), busy: busy === 'save' },
    ...(deletable ? [{
      key: 'delete', icon: 'delete', label: t('gallery.viewer_delete'), danger: true,
      onClick: () => void remove(), busy: busy === 'delete',
    }] : []),
  ]

  const elements = (
    <>
      {sel.active && (
        <SelectionBar count={sel.ids.size} onClose={sel.clear} actions={actions} />
      )}
      {sharing && (
        <ShareSheet
          items={picked.map(asShareItem)}
          nameOf={nameOf}
          onClose={() => setSharing(false)}
          onShared={(message, result) => {
            showToast(message, result.shared > 0 ? 'success' : 'info')
            if (result.shared > 0) sel.clear()
          }}
        />
      )}
      {moving && (
        <MoveToAlbumDialog
          count={picked.filter(i => canDeleteItem(album, i, canModerate)).length}
          currentAlbumId={album.id}
          onClose={() => setMoving(false)}
          onMove={move}
        />
      )}
    </>
  )
  return { sel, elements, canManageAlbum: canManageAlbum(album, canModerate, readOnly) }
}

/** A tile as the share sheet takes it. */
function asShareItem(i: Item): ShareItem {
  return {
    id: i.id,
    item_type: i.item_type,
    uploaded_by: i.uploaded_by,
    thumbnail_url: i.thumbnail_url,
    caption: i.caption,
    duration_s: i.duration_s,
    processing: isProcessing(i),
  }
}

/** Spoken name of a gallery item kind, for screen-reader labels. */
/** "Private" on an album's card (D7). */
function PrivateBadge() {
  return (
    <span class="sh-album-private-badge" title={t('gallery.audience.private')}>
      <span aria-hidden="true">👥</span> {t('gallery.audience.badge')}
    </span>
  )
}

/** A private album a household admin isn't in (D7): whose and how many,
 *  never its name or a preview. The admin may put it in the trash — its
 *  owner is told and can bring it back for 30 days. */
function LockedAlbumCard({ album, onDeleted }: { album: Album; onDeleted: () => void }) {
  const owner = householdDisplayName(album.owner_user_id)
  const count = t(isOne(album.item_count) ? 'gallery.items_one' : 'gallery.items', { n: String(album.item_count) })
  const remove = async () => {
    const ok = await confirmDialog(t('gallery.locked.delete_body', { name: owner }), {
      title: t('gallery.locked.delete_title', { name: owner }),
      confirmLabel: t('gallery.manage.album_delete_confirm'),
      destructive: true,
    })
    if (!ok) return
    try {
      await api.delete(`/api/gallery/albums/${encodeURIComponent(album.id)}`)
      showToast(t('gallery.locked.deleted', { name: owner }), 'info')
      onDeleted()
    } catch (err) {
      showToast(String((err as Error)?.message ?? err), 'error')
    }
  }
  return (
    <div class="sh-album-card sh-album-card--locked" role="group"
         aria-label={t('gallery.locked.aria', { name: owner, count })}>
      <div class="sh-album-cover sh-album-cover--locked" aria-hidden="true">🔒</div>
      <div class="sh-album-info">
        <strong>{t('gallery.locked.title')}</strong>
        <span class="sh-muted">{t('gallery.locked.by', { name: owner })} · {count}</span>
        <Button variant="secondary" onClick={() => void remove()}>
          {t('gallery.locked.delete')}
        </Button>
      </div>
    </div>
  )
}

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
  const [who, setWho] = useState<AudienceChoice>({ audience: 'everyone', userIds: [] })

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
        // Only a household album can be private (D7).
        ...(spaceId ? {} : {
          audience: who.audience,
          user_ids: who.audience === 'chosen' ? who.userIds : [],
        }),
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
      {!spaceId && (
        <div class="sh-create-audience">
          <span class="sh-create-audience__label">{t('gallery.audience.create_label')}</span>
          <AudienceChooser
            value={who}
            onChange={setWho}
            ownerId={currentUser.value?.user_id ?? null}
            guardianIds={[]}
            idPrefix="sh-create"
          />
        </div>
      )}
      <div class="sh-form-actions">
        <Button variant="secondary" type="button" onClick={onClose}>{t('common.cancel')}</Button>
        <Button type="submit" loading={busy} disabled={!name.trim()}>{t('gallery.form.create')}</Button>
      </div>
    </form>
  )
}

/** No answer from home: the network is gone, or the proxy in front of
 *  home (ingress, a reverse proxy) could not reach it. */
function isOffline(err: unknown): boolean {
  if (typeof navigator !== 'undefined' && navigator.onLine === false) return true
  return !(err instanceof ApiError) || err.status >= 502
}

function albumsUrl(spaceId?: string): string {
  return spaceId ? `/api/spaces/${spaceId}/gallery/albums` : '/api/gallery/albums'
}

/** ``?before=&before_id=`` for the page after ``row`` (albums: its
 *  ``activity_at``, the list's order). */
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
    albumsStale.value = false
    const open = activeAlbum.value
    const fresh = open ? rows.find(a => a.id === open.id) : undefined
    if (fresh) activeAlbum.value = fresh
  } catch (err) {
    if (loadedScope !== scope) return
    if (albums.value.length === 0) albumsError.value = isOffline(err) ? 'offline' : 'server'
    else {
      // What is on screen stays; the banner says it may be out of date.
      albumsStale.value = true
      if (!quiet || toastOnFail) showToast(t('gallery.refresh_failed'), 'error')
    }
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
      `${albumsUrl(spaceId)}?limit=${ALBUM_PAGE}${cursor({ id: last.id, created_at: last.activity_at ?? last.created_at })}`,
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
  itemOrder.value = readAlbumOrder(currentUser.value?.user_id, id)
  if (await loadItems(id) && wantedAlbum === id && loadedScope === scope) {
    // On screen now: its "neu" dot and "new photos" rows go (D9).
    void noteAlbumSeen(id, scope)
    albums.value = withoutDot(albums.value, id)
  }
  return true
}

/** Fetch the open album's first page (or, ``quiet``, re-read as many as
 *  are loaded — an upload or a ready video shows up without losing the
 *  pages already scrolled through). ``true`` once they are on screen. */
async function loadItems(albumId: string, { quiet = false } = {}): Promise<boolean> {
  const limit = quiet
    ? Math.min(ITEM_MAX, Math.max(ITEM_PAGE, items.value.length))
    : ITEM_PAGE
  if (!quiet) itemsLoading.value = true
  try {
    const rows = await api.get(
      `/api/gallery/albums/${albumId}/items?limit=${limit}&sort=${sortParam(itemOrder.value)}`,
    ) as Item[]
    if (activeAlbum.value?.id !== albumId) return false  // another album is open now
    items.value = rows
    itemsMore.value = rows.length >= limit
    return true
  } catch {
    if (activeAlbum.value?.id !== albumId) return false
    showToast(
      items.value.length ? t('gallery.refresh_failed') : t('gallery.load_items_error'),
      'error',
    )
  } finally {
    if (!quiet) itemsLoading.value = false
  }
  return false
}

async function loadMoreItems(albumId: string): Promise<void> {
  const last = items.value[items.value.length - 1]
  if (!last) { itemsMore.value = false; return }
  try {
    const order = itemOrder.value
    const rows = await api.get(
      `/api/gallery/albums/${albumId}/items?limit=${ITEM_PAGE}&sort=${sortParam(order)}${takenCursor(last)}`,
    ) as Item[]
    // The order flipped while this page was on its way: it belongs to the
    // other order.
    if (itemOrder.value !== order) return
    if (activeAlbum.value?.id !== albumId) return
    items.value = appendNew(items.value, rows)
    itemsMore.value = rows.length >= ITEM_PAGE
  } catch {
    showToast(t('gallery.load_items_error'), 'error')
  }
}
