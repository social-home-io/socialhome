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
import { Button } from '@/components/Button'
import { Spinner } from '@/components/Spinner'
import { showToast } from '@/components/Toast'
import { openLightbox, type LightboxItem } from '@/components/ImageLightbox'

interface Album {
  id: string
  space_id: string | null
  owner_user_id: string
  name: string
  description?: string | null
  cover_url?: string | null
  item_count: number
  retention_exempt: boolean
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

  if (loading.value) return <Spinner />

  if (activeAlbum.value) {
    return (
      <AlbumDetail
        album={activeAlbum.value}
        onBack={() => { activeAlbum.value = null; items.value = [] }}
      />
    )
  }

  const openAlbum = async (a: Album) => {
    activeAlbum.value = a
    await loadItems(a.id)
  }

  return (
    <div class="sh-gallery">
      <header class="sh-page-header">
        <Button onClick={() => (showCreate.value = true)}>+ New album</Button>
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
          <div style={{ fontSize: '2.5rem' }}>📸</div>
          <h3>No albums yet</h3>
          <p>Albums are shared photo collections — holidays, birthdays,
             pet updates, anything you want the household to see.</p>
          <div style={{ marginTop: '0.75rem' }}>
            <Button onClick={() => (showCreate.value = true)}>
              + Create your first album
            </Button>
          </div>
        </div>
      ) : (
        <div class="sh-album-grid">
          {albums.value.map(a => (
            <button
              key={a.id}
              type="button"
              class="sh-album-card"
              aria-label={`Open album ${a.name} — ${a.item_count} items`}
              onClick={() => void openAlbum(a)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault()
                  void openAlbum(a)
                }
              }}
            >
              {a.cover_url ? (
                <img
                  src={a.cover_url}
                  class="sh-album-cover"
                  alt=""
                  loading="lazy"
                />
              ) : (
                <div class="sh-album-cover sh-album-cover--placeholder">
                  <span>🖼️</span>
                </div>
              )}
              <div class="sh-album-info">
                <strong>{a.name}</strong>
                <span class="sh-muted">
                  {a.item_count} {a.item_count === 1 ? 'item' : 'items'}
                </span>
                {a.retention_exempt && <span class="sh-badge">Kept</span>}
              </div>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

function AlbumDetail({ album, onBack }: { album: Album, onBack: () => void }) {
  const [uploadPct, setUploadPct] = useState<number | null>(null)
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
      xhr.open('POST', `/api/gallery/albums/${album.id}/items`, true)
      xhr.withCredentials = true
      const tok = localStorage.getItem('sh_token')
      if (tok) xhr.setRequestHeader('Authorization', `Bearer ${tok}`)
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) setUploadPct((e.loaded / e.total) * 100)
      }
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
      try {
        await uploadOne(files[i])
      } catch (err: unknown) {
        showToast(`Upload failed: ${(err as Error)?.message ?? err}`, 'error')
        setUploadPct(null)
        continue
      }
    }
    setUploadPct(null)
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
  }))

  return (
    <div
      class={`sh-album-detail ${dragOver ? 'sh-album-detail--drag' : ''}`}
      onDragOver={(e) => { e.preventDefault(); setDragOver(true) }}
      onDragLeave={() => setDragOver(false)}
      onDrop={(e) => {
        e.preventDefault()
        setDragOver(false)
        void handleFiles(e.dataTransfer?.files ?? null)
      }}
    >
      <header class="sh-page-header">
        <Button variant="secondary" onClick={onBack}>← Albums</Button>
        <h1 style={{ margin: 0 }}>{album.name}</h1>
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
            hidden
          />
        </div>
      </header>

      {album.description && <p class="sh-muted">{album.description}</p>}

      {uploadPct !== null && (
        <div class="sh-upload-progress" role="progressbar"
             aria-valuenow={Math.round(uploadPct)} aria-valuemin={0} aria-valuemax={100}>
          <div class="sh-upload-progress-bar"
               style={{ width: `${uploadPct.toFixed(0)}%` }} />
          <span>Uploading… {uploadPct.toFixed(0)}%</span>
        </div>
      )}

      {dragOver && (
        <div class="sh-drop-overlay" aria-hidden="true">
          Drop to upload
        </div>
      )}

      {items.value.length === 0 ? (
        <div class="sh-empty-state">
          <div style={{ fontSize: '2.5rem' }}>🖼️</div>
          <h3>This album is empty</h3>
          <p>Drop photos or videos here, or click <strong>+ Upload</strong>.</p>
        </div>
      ) : (
        <div class="sh-image-grid">
          {items.value.map((item, idx) => (
            <button
              key={item.id}
              type="button"
              class="sh-gallery-item"
              aria-label={
                item.caption
                  ? `${item.item_type}: ${item.caption}`
                  : `${item.item_type} item`
              }
              onClick={() => openLightbox({ items: lightboxItems, index: idx })}
            >
              <img
                src={item.thumbnail_url}
                alt={item.caption || ''}
                loading="lazy"
                decoding="async"
              />
              {item.item_type === 'video' && (
                <span class="sh-video-badge" aria-hidden="true">▶</span>
              )}
            </button>
          ))}
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

async function loadAlbums(spaceId?: string) {
  loading.value = true
  try {
    const url = spaceId
      ? `/api/spaces/${spaceId}/gallery/albums`
      : '/api/gallery/albums'
    albums.value = await api.get(url) as Album[]
  } catch (err: unknown) {
    showToast(
      `Could not load albums: ${(err as Error)?.message ?? err}`,
      'error',
    )
    albums.value = []
  } finally {
    loading.value = false
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
