/* Per-moment share link viewer, on the pinned page.
 *
 * A moment link is ``{viewer}/v/<hash>/#g=<GFS>&a=<author>&m=<moment>&b=
 * <share blob>&k=<item key>`` (``moment_link.ts``). The page fetches the
 * moment's share copy and its file from the GFS — ciphertext addressed by
 * SHA-256, readable cross-origin without credentials — checks each address,
 * opens them with the moment's item key (``moment_seal.ts``) and shows the
 * moment. The GFS never sees the key: it is in the fragment. Nothing here
 * talks to the author's household, so the moment opens while it is
 * offline, and the home uplink never carries a viewer's copy.
 *
 * Nothing in the clear is ever shown: text, name and picture all come out
 * of the sealed copy. A missing or expired copy reads "no longer
 * available"; anything that doesn't open reads "incomplete or damaged".
 */
import { useEffect, useState } from 'preact/hooks'
import type { MomentBoot } from './moment_link'
import { CONTENT_TYPES, openBlob, openShare, type ShareBody } from './moment_seal'
import { pinnedEndpoint, pinnedRequestInit } from './pinned_link'

type State =
  | { status: 'loading' }
  | { status: 'gone' }
  | { status: 'broken' }
  | { status: 'ready'; body: ShareBody; mediaUrl: string | null; posterUrl: string | null }

class Gone extends Error {}

async function fetchBlob(gfs: string, blobId: string): Promise<Uint8Array<ArrayBuffer>> {
  const r = await fetch(pinnedEndpoint(gfs, `/gfs/moments/media/${blobId}`), pinnedRequestInit())
  if (r.status === 404) throw new Gone('gone')
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return new Uint8Array(await r.arrayBuffer())
}

export async function loadMoment(boot: MomentBoot): Promise<{
  body: ShareBody
  media: Blob | null
  poster: Blob | null
}> {
  const keys = { itemKey: boot.itemKey, author: boot.author, momentId: boot.momentId }
  const body = await openShare({
    ...keys,
    blob: await fetchBlob(boot.gfs, boot.shareId),
    blobId: boot.shareId,
  })
  let media: Blob | null = null
  let poster: Blob | null = null
  if (body.media) {
    const type = body.media.content_type
    const data = await openBlob({
      ...keys,
      blob: await fetchBlob(boot.gfs, body.media.blob_id),
      blobId: body.media.blob_id,
      role: 'media',
    })
    media = new Blob([data], { type })
    if (CONTENT_TYPES[type] === 'video' && body.media.poster_blob_id) {
      try {
        const p = await openBlob({
          ...keys,
          blob: await fetchBlob(boot.gfs, body.media.poster_blob_id),
          blobId: body.media.poster_blob_id,
          role: 'poster',
        })
        poster = new Blob([p], { type: 'image/webp' })
      } catch {
        poster = null // the video plays without its still
      }
    }
  }
  return { body, media, poster }
}

function when(iso: string | undefined): string {
  if (!iso) return ''
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleString(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  })
}

export function PublicMomentViewer({ boot }: { boot: MomentBoot }) {
  const [state, setState] = useState<State>({ status: 'loading' })

  useEffect(() => {
    let cancelled = false
    const urls: string[] = []
    loadMoment(boot)
      .then(({ body, media, poster }) => {
        if (cancelled) return
        const mediaUrl = media ? URL.createObjectURL(media) : null
        const posterUrl = poster ? URL.createObjectURL(poster) : null
        for (const u of [mediaUrl, posterUrl]) if (u) urls.push(u)
        setState({ status: 'ready', body, mediaUrl, posterUrl })
      })
      .catch((err: unknown) => {
        if (!cancelled) setState({ status: err instanceof Gone ? 'gone' : 'broken' })
      })
    return () => {
      cancelled = true
      for (const u of urls) URL.revokeObjectURL(u)
    }
  }, [boot])

  if (state.status === 'loading') {
    return <p class="status" role="status">Opening this moment…</p>
  }
  if (state.status === 'gone') {
    return (
      <div class="highlight-error" role="alert">
        <p>This moment is no longer available.</p>
        <p>Moments end after a while, and the person who shared it may have deleted it.</p>
      </div>
    )
  }
  if (state.status === 'broken') {
    return (
      <div class="highlight-error" role="alert">
        <p>This link can’t be opened.</p>
        <p>This link is incomplete or damaged. Ask whoever shared it for a new link.</p>
      </div>
    )
  }
  const { body, mediaUrl, posterUrl } = state
  const kind = body.media ? CONTENT_TYPES[body.media.content_type] : undefined
  const name = typeof body.author_name === 'string' && body.author_name.trim()
    ? body.author_name.trim()
    : 'Someone'
  return (
    <article class="highlight-viewer moment-viewer">
      <header class="highlight-author">
        <div class="highlight-author-text">
          <p class="highlight-author-name">{name}</p>
          <p class="highlight-author-bio">{when(body.created_at)}</p>
        </div>
      </header>
      {mediaUrl && kind === 'image' && (
        <div class="stage"><img src={mediaUrl} alt="" /></div>
      )}
      {mediaUrl && kind === 'video' && (
        <div class="stage">
          <video src={mediaUrl} poster={posterUrl ?? undefined} controls playsInline />
        </div>
      )}
      {body.content && <p class="moment-text">{body.content}</p>}
    </article>
  )
}
