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
 * of the sealed copy. Three ways it can fail, told apart because only the
 * GFS is ever asked (the household isn't, so this is no presence oracle):
 * a missing or expired copy (404 / 410) reads "no longer available"; the
 * GFS not answering (no network, 5xx, 408, 429) reads "Can't reach <GFS>"
 * with Try again; anything that doesn't open — or a request the GFS
 * refuses outright — reads "incomplete or damaged".
 *
 * A video starts muted, so the browser lets it autoplay, under the same
 * control bar as a highlight (pause / play, mute / unmute, Space / K / M).
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import type { MomentBoot } from './moment_link'
import { CONTENT_TYPES, openBlob, openShare, type ShareBody } from './moment_seal'
import { pinnedEndpoint, pinnedRequestInit } from './pinned_link'
import { PlaybackControls, usePlaybackKeys } from './viewer_playback'
import { ViewerError } from './viewer_retry'
import { t } from './viewer_i18n'

type State =
  | { status: 'loading' }
  | { status: 'gone' }
  | { status: 'broken' }
  | { status: 'unreachable' }
  | { status: 'ready'; body: ShareBody; mediaUrl: string | null; posterUrl: string | null }

class Gone extends Error {}
class Unreachable extends Error {}

/** Whether a GFS answer means "not now" rather than "not there" or "not
 *  a valid request": a server error, a timeout or a rate limit. */
function transient(status: number): boolean {
  return status >= 500 || status === 408 || status === 429
}

async function fetchBlob(gfs: string, blobId: string): Promise<Uint8Array<ArrayBuffer>> {
  let r: Response
  try {
    r = await fetch(pinnedEndpoint(gfs, `/gfs/moments/media/${blobId}`), pinnedRequestInit())
  } catch {
    throw new Unreachable('network')
  }
  if (r.status === 404 || r.status === 410) throw new Gone('gone')
  if (transient(r.status)) throw new Unreachable(`HTTP ${r.status}`)
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  try {
    return new Uint8Array(await r.arrayBuffer())
  } catch {
    throw new Unreachable('body') // the connection dropped mid-file
  }
}

/** The GFS as the link names it, for "Can't reach …": its host. */
function gfsName(gfs: string): string {
  try {
    return new URL(gfs).host
  } catch {
    return gfs
  }
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

/* The moment's video: muted autoplay (what browsers allow), no native
 * controls, the shared control bar. The button follows the element's own
 * play / pause / ended events, so it never claims a state the video isn't
 * in (autoplay refused → Play). */
export function MomentVideo({ src, poster }: { src: string; poster: string | null }) {
  const video = useRef<HTMLVideoElement | null>(null)
  const [paused, setPaused] = useState(false)
  const [muted, setMuted] = useState(true)

  useEffect(() => {
    const v = video.current
    if (!v) return
    v.defaultMuted = true // the ``muted`` attribute, for autoplay policies
    v.muted = true
    void Promise.resolve(v.play()).catch(() => setPaused(true))
  }, [src])

  useEffect(() => {
    if (video.current) video.current.muted = muted
  }, [muted])

  const togglePause = () => {
    const v = video.current
    if (!v) return
    if (paused) {
      setPaused(false)
      void Promise.resolve(v.play()).catch(() => setPaused(true))
    } else {
      setPaused(true)
      v.pause()
    }
  }
  const toggleMute = () => setMuted((m) => !m)
  usePlaybackKeys({ togglePause, toggleMute })

  return (
    <>
      <div class="stage">
        <video
          ref={video}
          src={src}
          poster={poster ?? undefined}
          autoPlay
          muted={muted}
          playsInline
          onPlay={() => setPaused(false)}
          onPause={() => setPaused(true)}
          onEnded={() => setPaused(true)}
        />
      </div>
      <PlaybackControls
        paused={paused}
        muted={muted}
        onTogglePause={togglePause}
        onToggleMute={toggleMute}
      />
    </>
  )
}

export function PublicMomentViewer({ boot }: { boot: MomentBoot }) {
  const [state, setState] = useState<State>({ status: 'loading' })
  // Try again re-runs the fetch: a new attempt, same link.
  const [attempt, setAttempt] = useState(0)

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
        if (cancelled) return
        setState({
          status: err instanceof Gone ? 'gone' : err instanceof Unreachable ? 'unreachable' : 'broken',
        })
      })
    return () => {
      cancelled = true
      for (const u of urls) URL.revokeObjectURL(u)
    }
  }, [boot, attempt])

  if (state.status === 'loading') {
    return <p class="status" role="status">{t('moment.opening')}</p>
  }
  if (state.status === 'gone') {
    return (
      <div class="highlight-error" role="alert">
        <p class="viewer-title">{t('moment.gone')}</p>
        <p>{t('moment.gone_body')}</p>
      </div>
    )
  }
  if (state.status === 'broken') {
    return (
      <div class="highlight-error" role="alert">
        <p class="viewer-title">{t('link.broken')}</p>
        <p>{t('link.broken_body')}</p>
      </div>
    )
  }
  if (state.status === 'unreachable') {
    // Manual only: the visitor knows when their connection is back.
    const retry = {
      manual: () => {
        setState({ status: 'loading' })
        setAttempt((a) => a + 1)
      },
      auto: () => {},
      nextAutoDelayMs: () => null,
    }
    return (
      <ViewerError
        class="highlight-error"
        message={t('moment.unreachable', { server: gfsName(boot.gfs) })}
        detail={t('moment.unreachable_body')}
        retry={retry}
      />
    )
  }
  const { body, mediaUrl, posterUrl } = state
  const kind = body.media ? CONTENT_TYPES[body.media.content_type] : undefined
  const name = typeof body.author_name === 'string' && body.author_name.trim()
    ? body.author_name.trim()
    : t('moment.someone')
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
      {mediaUrl && kind === 'video' && <MomentVideo src={mediaUrl} poster={posterUrl} />}
      {body.content && <p class="moment-text">{body.content}</p>}
    </article>
  )
}
