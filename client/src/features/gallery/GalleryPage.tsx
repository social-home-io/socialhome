/**
 * GalleryPage — albums + items grid (§23.119).
 *
 * Two modes:
 *   • Album list (default) — grid of album cards with empty-state hero.
 *   • Album detail — items grid for one album; click opens lightbox
 *     with prev/next + keyboard nav.
 *
 * Used both for household-level (no space_id) and per-space galleries.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { useTitle } from '@/store/pageTitle'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { ws } from '@/ws'
import { Button } from '@/components/Button'
import { Spinner } from '@/components/Spinner'
import { showToast } from '@/components/Toast'
import { openLightbox, type LightboxItem } from '@/components/ImageLightbox'
import { openReport } from '@/components/ReportDialog'
import { currentUser } from '@/store/auth'
import { describeUploadError } from '@/utils/uploadErrors'

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
  /** Background-transcode state for ``video`` items — ``'processing'``
   *  while the worker encodes. Absent on photos and on older payloads
   *  (treated as ready). A processing item can't play yet, so the tile
   *  shows a "Processing…" overlay and doesn't open the lightbox. */
  media_status?: 'processing' | 'failed' | 'ready'
}

const albums      = signal<Album[]>([])
const items       = signal<Item[]>([])
const activeAlbum = signal<Album | null>(null)
const loading     = signal(true)
const showCreate  = signal(false)

export interface GalleryPageProps {
  spaceId?: string
}

export default function GalleryPage({ spaceId }: GalleryPageProps) {
  useTitle('Gallery')
  useEffect(() => { void loadAlbums(spaceId) }, [spaceId])

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
        void loadItems(activeAlbum.value.id)
      }
    }
    const onAlbumDeleted = (e: GalleryFrame) => {
      if (!inScope(e)) return
      if (activeAlbum.value && e.data.album_id === activeAlbum.value.id) {
        activeAlbum.value = null
        items.value = []
        showToast('This album was deleted.', 'info')
      }
      void loadAlbums(spaceId, { quiet: true })
    }
    const offs = [
      ws.on('gallery.album_created', handle),
      ws.on('gallery.album_updated', handle),
      ws.on('gallery.album_deleted', onAlbumDeleted),
      ws.on('gallery.item_uploaded', handle),
      ws.on('gallery.item_deleted', handle),
    ]
    return () => { offs.forEach((off) => off()) }
  }, [spaceId])

  if (loading.value) return <Spinner />

  if (activeAlbum.value) {
    return (
      <AlbumDetail
        album={activeAlbum.value}
        onBack={() => {
          activeAlbum.value = null
          items.value = []
          // Re-fetch the album list so a freshly-uploaded cover +
          // bumped item_count show up immediately. Without this,
          // returning from an upload session still rendered the
          // pre-upload zero-item placeholder until the user
          // hard-reloaded.
          void loadAlbums(spaceId)
        }}
      />
    )
  }

  const openAlbum = async (a: Album) => {
    activeAlbum.value = a
    await loadItems(a.id)
  }

  // Counts for the hero — system "Posts" album is folded into the
  // total because it is real content from the household's perspective
  // (every shared photo lives there); the "+ how many are custom"
  // breakdown isn't useful for the user, but the totals are.
  const albumCount = albums.value.length
  const itemCount = albums.value.reduce((acc, a) => acc + a.item_count, 0)

  return (
    <div class="sh-gallery">
      <header class="sh-gallery-hero">
        <div class="sh-gallery-hero-headline">
          <strong>{albumCount}</strong>{' '}
          {albumCount === 1 ? 'album' : 'albums'} ·{' '}
          <strong>{itemCount}</strong>{' '}
          {itemCount === 1 ? 'photo or video' : 'photos and videos'}
        </div>
        <div class="sh-gallery-hero-actions">
          <Button onClick={() => (showCreate.value = true)}>+ New album</Button>
        </div>
      </header>

      {showCreate.value && (
        <CreateAlbumForm
          spaceId={spaceId}
          onClose={() => (showCreate.value = false)}
          onCreated={() => { showCreate.value = false; void loadAlbums(spaceId) }}
        />
      )}

      {albums.value.length === 0 ? (
        <div class="sh-empty-state">
          <div aria-hidden="true">📸</div>
          <h3>No albums yet</h3>
          <p>Albums are shared photo collections — holidays, birthdays,
             pet updates, anything you want {spaceId ? 'this space' : 'the household'} to see.</p>
          <Button onClick={() => (showCreate.value = true)}>
            + Create your first album
          </Button>
        </div>
      ) : (
        <div class="sh-album-grid">
          {albums.value.map(a => (
            <button
              key={a.id}
              type="button"
              class={`sh-album-card${a.is_system ? ' sh-album-card--system' : ''}`}
              aria-label={`Open album ${a.name} — ${a.item_count} items`}
              onClick={() => void openAlbum(a)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault()
                  void openAlbum(a)
                }
              }}
            >
              <AlbumCover album={a} />
              <div class="sh-album-info">
                <strong>
                  {a.name}
                  {a.is_system && (
                    <span class="sh-album-system-badge"
                          title="Auto-managed: contains every photo and video shared via the feed">
                      <span aria-hidden="true">🔒</span> Auto
                    </span>
                  )}
                </strong>
                <span class="sh-muted">
                  {a.item_count} {a.item_count === 1 ? 'item' : 'items'}
                </span>
                {a.retention_exempt && !a.is_system && (
                  <span class="sh-badge">Kept</span>
                )}
              </div>
            </button>
          ))}
        </div>
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

function AlbumDetail({ album, onBack }: { album: Album, onBack: () => void }) {
  const [uploadPct, setUploadPct] = useState<number | null>(null)
  // Once the bytes are sent the request blocks on server-side transcoding
  // (video → VP9/WebM). Flip to a "Processing…" label so the bar doesn't
  // sit at "Uploading… 100%" looking stalled — matching the feed composer.
  const [processing, setProcessing] = useState(false)
  const [dragOver, setDragOver] = useState(false)
  const inputRef = useRef<HTMLInputElement | null>(null)

  const uploadOne = (file: File): Promise<void> => {
    return new Promise((resolve, reject) => {
      // 100 MB guard, server enforces separately.
      if (file.size > 100 * 1024 * 1024) {
        reject(new Error('File too large (>100 MB).'))
        return
      }
      const fd = new FormData()
      fd.append('file', file)
      const xhr = new XMLHttpRequest()
      // Raw XHR (not the ``api`` client) so we can wire
      // ``xhr.upload.onprogress`` for the progress bar — ``fetch``
      // doesn't expose upload-stream progress events. The URL is
      // relative (no leading slash) so it resolves against
      // ``<base href>`` — under HA Supervisor ingress that's the
      // ``/api/hassio_ingress/<token>/`` prefix; an absolute
      // ``/api/...`` would bypass it and 404 (#303).
      xhr.open('POST', `api/gallery/albums/${album.id}/items`, true)
      xhr.withCredentials = true
      const tok = localStorage.getItem('sh_token')
      if (tok) xhr.setRequestHeader('Authorization', `Bearer ${tok}`)
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) setUploadPct((e.loaded / e.total) * 100)
      }
      // Bytes fully sent — the server is now transcoding before it responds.
      xhr.upload.onload = () => setProcessing(true)
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) resolve()
        else reject(new Error(`Upload failed (${xhr.status}): ${xhr.responseText}`))
      }
      xhr.onerror = () => reject(new Error('Network error'))
      xhr.send(fd)
    })
  }

  const handleFiles = async (fileList: FileList | null) => {
    if (!fileList || fileList.length === 0) return
    const files = Array.from(fileList)
    for (let i = 0; i < files.length; i++) {
      setUploadPct(0)
      setProcessing(false)
      try {
        await uploadOne(files[i])
      } catch (err: unknown) {
        showToast(describeUploadError(err, { file: files[i] }), 'error')
        setUploadPct(null)
        setProcessing(false)
        continue
      }
    }
    setUploadPct(null)
    setProcessing(false)
    showToast(
      files.length === 1 ? 'Uploaded' : `Uploaded ${files.length} items`,
      'success',
    )
    await loadItems(album.id)
  }

  const lightboxItems: LightboxItem[] = items.value.map(i => ({
    id:            i.id,
    item_type:     i.item_type,
    url:           i.url,
    thumbnail_url: i.thumbnail_url,
    caption:       i.caption,
    taken_at:      i.taken_at,
    width:         i.width,
    height:        i.height,
    // A space item someone else uploaded goes to the space's moderators.
    onReport: album.space_id && i.uploaded_by !== currentUser.value?.user_id
      ? () => openReport('gallery_item', i.id, album.space_id)
      : undefined,
  }))

  // Drag-drop is a noop on the system album — the album rejects
  // direct uploads server-side too, but handling the UI ourselves
  // avoids a needless 403 round-trip.
  const dragHandlers = album.is_system ? {} : {
    onDragOver: (e: DragEvent) => { e.preventDefault(); setDragOver(true) },
    onDragLeave: () => setDragOver(false),
    onDrop: (e: DragEvent) => {
      e.preventDefault()
      setDragOver(false)
      void handleFiles(e.dataTransfer?.files ?? null)
    },
  }

  return (
    <div
      class={`sh-album-detail ${dragOver ? 'sh-album-detail--drag' : ''}`}
      {...dragHandlers}
    >
      <header class="sh-page-header">
        <Button variant="secondary" onClick={onBack}>← Albums</Button>
        <h1 style={{ margin: 0 }}>
          {album.name}
          {album.is_system && (
            <span class="sh-album-system-badge"
                  style={{ marginLeft: 'var(--sh-space-xs)' }}>
              <span aria-hidden="true">🔒</span> Auto
            </span>
          )}
        </h1>
        {!album.is_system && (
          <div class="sh-row">
            <Button onClick={() => inputRef.current?.click()}>+ Upload</Button>
            <input
              ref={inputRef}
              type="file"
              accept="image/jpeg,image/png,image/webp,image/gif,image/heic,video/mp4,video/webm,video/quicktime"
              multiple
              onChange={(e) => {
                void handleFiles((e.target as HTMLInputElement).files)
                ;(e.target as HTMLInputElement).value = ''
              }}
              class="sr-only"
            />
          </div>
        )}
      </header>

      {album.description && <p class="sh-muted">{album.description}</p>}

      {album.is_system && (
        <div class="sh-album-system-hint">
          <span aria-hidden="true">📸</span>{' '}
          Photos and videos shared to the feed appear here automatically.
          Post a photo to add one — items are removed when their source
          post is deleted.
        </div>
      )}

      {uploadPct !== null && (
        <div class="sh-upload-progress" role="progressbar"
             aria-valuenow={processing ? 100 : Math.round(uploadPct)}
             aria-valuemin={0} aria-valuemax={100}>
          <div class="sh-upload-progress-bar"
               style={{ width: processing ? '100%' : `${uploadPct.toFixed(0)}%` }} />
          <span>{processing ? 'Processing…' : `Uploading… ${uploadPct.toFixed(0)}%`}</span>
        </div>
      )}

      {dragOver && !album.is_system && (
        <div class="sh-drop-overlay" aria-hidden="true">
          Drop to upload
        </div>
      )}

      {items.value.length === 0 ? (
        <div class="sh-empty-state">
          <div aria-hidden="true">🖼️</div>
          {album.is_system ? (
            <>
              <h3>No posts with photos or videos yet</h3>
              <p>Share a photo or video in the feed to see it here.</p>
            </>
          ) : (
            <>
              <h3>This album is empty</h3>
              <p>Drop photos or videos here, or click <strong>+ Upload</strong>.</p>
            </>
          )}
        </div>
      ) : (
        <div class="sh-image-grid">
          {items.value.map((item, idx) => {
            // A video still transcoding can't play, so the tile shows a
            // "Processing…" overlay and doesn't open the lightbox.
            const processing =
              item.item_type === 'video' && item.media_status === 'processing'
            return (
              <button
                key={item.id}
                type="button"
                class={
                  'sh-gallery-item' +
                  (processing ? ' sh-gallery-item--processing' : '')
                }
                aria-label={
                  processing
                    ? `${item.item_type} item — processing`
                    : item.caption
                      ? `${item.item_type}: ${item.caption}`
                      : `${item.item_type} item`
                }
                aria-disabled={processing ? 'true' : undefined}
                onClick={() => {
                  if (processing) return
                  openLightbox({ items: lightboxItems, index: idx })
                }}
              >
                <img
                  src={item.thumbnail_url}
                  alt={item.caption || ''}
                  loading="lazy"
                  decoding="async"
                />
                {item.item_type === 'video' && !processing && (
                  <span class="sh-video-badge" aria-hidden="true">▶</span>
                )}
                {processing && (
                  <span class="sh-gallery-item-processing">
                    <Spinner label="Processing video" />
                    <span class="sh-gallery-item-processing-msg">Processing…</span>
                  </span>
                )}
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
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
      showToast('Album created', 'success')
      onCreated()
    } catch (err: unknown) {
      showToast(`Create failed: ${(err as Error)?.message ?? err}`, 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={submit} class="sh-card" style={{ marginBottom: '1rem' }}>
      <label>
        Name
        <input
          type="text"
          maxLength={80}
          value={name}
          onInput={(e) => setName((e.target as HTMLInputElement).value)}
          placeholder="e.g. Summer 2026"
          required
        />
      </label>
      <label>
        Description (optional)
        <textarea
          maxLength={500}
          value={description}
          onInput={(e) => setDescription((e.target as HTMLTextAreaElement).value)}
        />
      </label>
      <div class="sh-form-actions">
        <Button variant="secondary" type="button" onClick={onClose}>Cancel</Button>
        <Button type="submit" loading={busy} disabled={!name.trim()}>Create</Button>
      </div>
    </form>
  )
}

/** Fetch the album list. ``quiet`` is the live-refresh path: no spinner
 *  and no error toast (a failed refresh keeps the list on screen). The
 *  open album's header (name / cover) follows the fresh row either way. */
async function loadAlbums(spaceId?: string, { quiet = false } = {}) {
  if (!quiet) loading.value = true
  try {
    const url = spaceId
      ? `/api/spaces/${spaceId}/gallery/albums`
      : '/api/gallery/albums'
    const rows = await api.get(url) as Album[]
    albums.value = rows
    const open = activeAlbum.value
    const fresh = open ? rows.find(a => a.id === open.id) : undefined
    if (fresh) activeAlbum.value = fresh
  } catch (err: unknown) {
    if (quiet) return
    showToast(
      `Could not load albums: ${(err as Error)?.message ?? err}`,
      'error',
    )
    albums.value = []
  } finally {
    if (!quiet) loading.value = false
  }
}

async function loadItems(albumId: string) {
  try {
    items.value = await api.get(
      `/api/gallery/albums/${albumId}/items`,
    ) as Item[]
  } catch (err: unknown) {
    showToast(
      `Could not load items: ${(err as Error)?.message ?? err}`,
      'error',
    )
    items.value = []
  }
}
