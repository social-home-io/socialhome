/* Public-highlight viewer (§highlights_public).
 *
 * Preact bootstrap, for two pages:
 *
 * - The **pinned** page (``v/<hash>/``, built by
 *   ``scripts/build-pinned-viewer.mjs``): a static, content-addressed page
 *   on an origin no GFS runs, with this script pinned by SRI. There is no
 *   boot block; the whole context — GFS URL, ids, token, secret — is in
 *   the fragment (``pinned_link.ts``) and every request goes, absolute and
 *   without credentials, to the GFS it names. A GFS can't rewrite this
 *   page's script, so it can't read the secret.
 * - The GFS-served landing (older links, and the self-hosted fallback):
 *   an empty ``<div id="root">`` and a ``<script id="boot">`` JSON payload
 *   with ``{instanceId, highlightId, token}``; requests stay relative.
 *
 * The pinned page also opens a shared moment (``moment_link.ts``) and a
 * GFS's public pages — space, invite, profile (``page_link.ts``,
 * ``public_pages.tsx``); the GFS-served page never does.
 *
 * Wire framing must stay in lockstep with
 * ``socialhome/services/highlight_public_framing.py`` — see the
 * golden-bytes test in ``tests/protocol/test_highlight_public_framing.py``.
 *
 * End to end: the link's secret is in its fragment (``#k=…``), which the
 * browser never sends to the GFS. The author's household seals every frame
 * to this view's key and that secret (``stream_seal.ts``); only content-free
 * ``error`` frames arrive in the clear, and a plaintext content frame is
 * never shown. A link without the secret (made before this) opens nothing
 * and contacts nobody — the page asks for a new link.
 *
 * No SPA dependencies (signals, router, design tokens) — keeps the
 * bundle small enough to inline in the SSR page if we ever want to.
 */
import { render } from 'preact'
import { useEffect, useMemo, useState } from 'preact/hooks'
import { humanizeViewerError } from './viewer_errors'
import { RetryHost, ViewerError, type RetryControl } from './viewer_retry'
import {
  makeViewerKey,
  openProfile,
  pictureUrl,
  type AuthorProfile,
  type SealedHeader,
  type ViewerKey,
} from './sealed_profile'
import { linkSecretFromHash, makeStreamOpener, type StreamOpener } from './stream_seal'
import { pinnedBootFromHash, pinnedEndpoint, pinnedRequestInit } from './pinned_link'
import { isMomentHash, momentBootFromHash } from './moment_link'
import { PublicMomentViewer } from './public_moment'
import { isPageHash, pageBootFromHash } from './page_link'
import { PublicPage } from './public_pages'
import { locale, t } from './viewer_i18n'

const CHANNEL_LABEL = 'highlight-public-v1'
const FRAME_DURATION_MS = 6000
const POLL_INTERVAL_MS = 1000
const POLL_MAX_ATTEMPTS = 30


interface BootPayload {
  instanceId: string
  highlightId: string
  token: string
  /** The GFS base URL on the pinned page; absent on the GFS-served page,
   *  where every request stays relative to it. */
  gfs?: string
}

interface IceServer { urls: string[] | string; username?: string; credential?: string }

interface FrameMeta {
  frame_id: string
  sequence: number
  content_type: string
  byte_length: number
  caption_text?: string | null
  caption_emoji?: string | null
  duration_ms?: number | null
}

interface HighlightMeta {
  id: string
  author_user_id: string
  highlight_date: string
  expires_at: string
}

interface ViewerState {
  status: 'connecting' | 'loading' | 'playing' | 'ended' | 'error'
  message: string | null
  manifest: FrameMeta[]
  highlight: HighlightMeta | null
  frameUrls: Record<string, string>
  currentIndex: number
  /** Who shared it — opened from the sealed ``author_profile`` frame. */
  author: AuthorProfile | null
  authorPicture: string | null
  /** The channel closed before ``stream_end``: some frames never came. */
  incomplete: boolean
}

/** Whether a signalling-session poll answered "this session is gone": the
 *  GFS answers 404 for a session it does not hold (its server restarted, or
 *  a load balancer sent the poll to another server), and a 5xx means the
 *  server holding it is down. Either way it will never be answered. */
export function sessionLost(status: number): boolean {
  return status === 404 || status >= 500
}

/** What a viewer shows when the channel or relay closes:
 *  ``complete`` — ``stream_end`` came (or an error is already shown);
 *  ``cant_open`` — nothing sealed for this link ever arrived;
 *  ``nothing_arrived`` — sealed, but no highlight before the close;
 *  ``incomplete`` — some frames came, the rest didn't (no ``stream_end``). */
export function closedStreamVerdict(s: {
  sawSeal: boolean
  sawEnd: boolean
  failed: boolean
  frames: number
}): 'complete' | 'cant_open' | 'nothing_arrived' | 'incomplete' {
  if (s.sawEnd || s.failed) return 'complete'
  if (!s.sawSeal) return 'cant_open'
  if (s.frames === 0) return 'nothing_arrived'
  return 'incomplete'
}

/** Shown when the stream can't be opened: not sealed for this link. */
const cantOpen = (): string => t('hl.cant_open')

interface FramingHeader extends SealedHeader {
  kind:
    | 'highlight_meta' | 'author_profile' | 'frame_chunk' | 'stream_end' | 'error'
    | 'stream_seal' | 'sealed'
  highlight?: HighlightMeta
  frames?: FrameMeta[]
  frame_id?: string
  sequence?: number
  chunk_index?: number
  is_last_chunk?: boolean
  byte_length?: number
  error?: string
}


/* Decode one length-prefixed framing record. Returns header + payload bytes. */
function decodeFrame(buf: Uint8Array): { header: FramingHeader; payload: Uint8Array } | null {
  if (buf.length < 8) return null
  const view = new DataView(buf.buffer, buf.byteOffset, buf.byteLength)
  const headerLen = view.getUint32(0)
  if (buf.length < 4 + headerLen + 4) return null
  const headerJson = new TextDecoder('utf-8').decode(buf.subarray(4, 4 + headerLen))
  let header: FramingHeader
  try { header = JSON.parse(headerJson) } catch { return null }
  const payloadLen = view.getUint32(4 + headerLen)
  const payload = buf.subarray(4 + headerLen + 4, 4 + headerLen + 4 + payloadLen)
  return { header, payload }
}


/* Pop one complete frame off the front of an accumulating byte buffer
 * (the GFS-relay fallback reads a chunked stream whose chunk boundaries
 * don't align to frames). Returns the decoded frame + the unconsumed
 * tail, or null when a whole frame isn't buffered yet. */
function takeFrame(
  buf: Uint8Array,
): { header: FramingHeader; payload: Uint8Array; rest: Uint8Array } | null {
  const decoded = decodeFrame(buf)
  if (!decoded) return null
  const view = new DataView(buf.buffer, buf.byteOffset, buf.byteLength)
  const headerLen = view.getUint32(0)
  const payloadLen = view.getUint32(4 + headerLen)
  const consumed = 4 + headerLen + 4 + payloadLen
  return { header: decoded.header, payload: decoded.payload, rest: buf.subarray(consumed) }
}


function PublicHighlightViewer(
  { boot, secret, retry }: { boot: BootPayload; secret: Uint8Array<ArrayBuffer>; retry: RetryControl },
) {
  const [state, setState] = useState<ViewerState>({
    status: 'connecting',
    message: t('hl.connecting'),
    manifest: [],
    highlight: null,
    frameUrls: {},
    currentIndex: 0,
    author: null,
    authorPicture: null,
    incomplete: false,
  })

  // Per-frame chunk buffer — kept in a ref-like store so the message
  // handler captured at mount keeps appending into the same Map.
  const buffers = useMemo(() => new Map<string, Uint8Array[]>(), [])

  useEffect(() => {
    let pc: RTCPeerConnection | null = null
    let pollTimer: ReturnType<typeof setInterval> | null = null
    let cancelled = false
    // This view's ephemeral key: the author seals the whole stream to it
    // (and to the link secret).
    let viewerKey: ViewerKey | null = null
    let opener: StreamOpener | null = null
    // The manifest as it arrives (the mount-time ``state`` is stale here).
    let manifest: FrameMeta[] = []
    // Sealed frames must open in arrival order: one promise chain.
    let queue: Promise<void> = Promise.resolve()
    // How far the stream got — for a channel that closes early.
    let sawSeal = false
    let sawEnd = false
    let failed = false

    function setStatus(status: ViewerState['status'], message: string | null = null) {
      if (cancelled) return
      if (status === 'error') failed = true
      setState((s) => ({ ...s, status, message }))
    }

    // The channel or relay closed. Once every frame that did arrive is
    // opened: nothing sealed for this link came at all → can't be opened;
    // no highlight yet → error; otherwise show what came, and say the rest
    // didn't (a stream cut short has no ``stream_end``).
    function onTransportClosed() {
      queue = queue.then(() => {
        if (cancelled) return
        const verdict = closedStreamVerdict({
          sawSeal, sawEnd, failed, frames: manifest.length,
        })
        if (verdict === 'cant_open') {
          setStatus('error', cantOpen())
        } else if (verdict === 'nothing_arrived') {
          setStatus('error', t('hl.closed_early'))
        } else if (verdict === 'incomplete') {
          setState((s) => ({ ...s, incomplete: true }))
        }
      })
    }

    function appendChunk(frameId: string, payload: Uint8Array, isLast: boolean, contentType: string) {
      const slice = payload.slice() // copy out of the WS read buffer
      const bucket = buffers.get(frameId) ?? []
      bucket.push(slice)
      buffers.set(frameId, bucket)
      if (!isLast) return
      // Cast to ``BlobPart[]`` — TS 5.7 narrowed `Uint8Array<ArrayBufferLike>`
      // away from `BlobPart`, but each element here is in fact a normal
      // ``Uint8Array<ArrayBuffer>`` (we built it via ``.slice()``).
      const blob = new Blob(bucket as unknown as BlobPart[], { type: contentType })
      const url = URL.createObjectURL(blob)
      buffers.delete(frameId)
      setState((s) => ({ ...s, frameUrls: { ...s.frameUrls, [frameId]: url } }))
    }

    function onWireFrame(header: FramingHeader, payload: Uint8Array) {
      const bytes = payload.slice()
      queue = queue.then(() => openWireFrame(header, bytes)).catch(() => {
        // A frame that doesn't open: tampered, reordered or not ours.
        opener = null
        setStatus('error', cantOpen())
      })
    }

    async function openWireFrame(header: FramingHeader, payload: Uint8Array) {
      if (cancelled) return
      if (header.kind === 'error') {
        setStatus('error', humanizeError(header.error ?? 'unknown'))
      } else if (header.kind === 'stream_seal' && viewerKey && !opener) {
        opener = await makeStreamOpener(viewerKey.privateKey, header, secret, boot.highlightId)
        if (opener) sawSeal = true
        else setStatus('error', cantOpen())
      } else if (header.kind === 'sealed' && opener) {
        const inner = decodeFrame(await opener.open(payload))
        if (inner && inner.header.kind !== 'stream_seal' && inner.header.kind !== 'sealed') {
          handleHeader(inner.header, inner.payload)
        }
      }
      // Anything else arrived in the clear: never shown.
    }

    function handleHeader(header: FramingHeader, payload: Uint8Array) {
      if (header.kind === 'highlight_meta') {
        manifest = header.frames ?? []
        setState((s) => ({
          ...s,
          status: 'playing',
          message: null,
          manifest: header.frames ?? [],
          highlight: header.highlight ?? null,
        }))
      } else if (header.kind === 'author_profile' && viewerKey) {
        const key = viewerKey
        void openProfile(key.privateKey, header, payload.slice(), boot.highlightId).then((author) => {
          if (cancelled || !author) return
          setState((s) => ({ ...s, author, authorPicture: pictureUrl(author) }))
        })
      } else if (header.kind === 'frame_chunk' && header.frame_id) {
        const meta = manifest.find((f) => f.frame_id === header.frame_id)
        const ct = meta?.content_type ?? 'application/octet-stream'
        appendChunk(header.frame_id, payload, !!header.is_last_chunk, ct)
      } else if (header.kind === 'stream_end') {
        // Streaming complete. Playback continues from already-buffered urls.
        sawEnd = true
      } else if (header.kind === 'error') {
        setStatus('error', humanizeError(header.error ?? 'unknown'))
      }
    }

    let channelOpened = false
    let relayStarted = false

    // GFS-relay fallback: direct WebRTC couldn't connect (NAT/TURN/P2P
    // blocked) but the author is online. Pull the identical framed stream
    // proxied through the GFS over a plain chunked GET.
    async function startRelayFallback() {
      if (relayStarted || cancelled || channelOpened) return
      relayStarted = true
      if (pollTimer) clearInterval(pollTimer)
      if (pc) { try { pc.close() } catch { /* tolerated */ } }
      setStatus('loading', t('hl.via_server'))
      try {
        const url =
          `/gfs/highlight_rtc/relay/${encodeURIComponent(boot.instanceId)}` +
          `/${encodeURIComponent(boot.highlightId)}?token=${encodeURIComponent(boot.token)}` +
          `&vk=${viewerKey!.vk}`
        const r = await gfsFetch(boot.gfs, url)
        if (!r.ok || !r.body) {
          setStatus('error', humanizeError(`HTTP ${r.status}`))
          return
        }
        const reader = r.body.getReader()
        let buf: Uint8Array = new Uint8Array(0)
        while (!cancelled) {
          const { done, value } = await reader.read()
          if (done) break
          if (value && value.length) {
            const next = new Uint8Array(buf.length + value.length)
            next.set(buf)
            next.set(value, buf.length)
            buf = next
          }
          for (;;) {
            const taken = takeFrame(buf)
            if (!taken) break
            buf = taken.rest
            onWireFrame(taken.header, taken.payload)
          }
        }
        onTransportClosed()
      } catch (err) {
        setStatus('error', humanizeError((err as Error)?.message))
      }
    }

    async function main() {
      try {
        viewerKey = await makeViewerKey()
        if (!viewerKey) {
          // No WebCrypto: nothing could open the sealed stream, so don't ask.
          setStatus('error', t('hl.no_crypto'))
          return
        }
        const ice = await fetchJson<{ servers: IceServer[] }>(boot.gfs, '/gfs/highlights/ice-servers')
        pc = new RTCPeerConnection({ iceServers: ice.servers })
        let sessionId: string | null = null
        let ackedCandidates = 0

        // A failed/closed ICE transport before the channel ever opened is
        // the canonical "WebRTC won't work here" signal → fall back.
        pc.addEventListener('iceconnectionstatechange', () => {
          const st = pc?.iceConnectionState
          if ((st === 'failed' || st === 'closed') && !channelOpened) {
            void startRelayFallback()
          }
        })

        pc.addEventListener('icecandidate', (ev) => {
          if (!ev.candidate || !sessionId) return
          void postJson(boot.gfs, '/gfs/highlight_rtc/ice/viewer', {
            session_id: sessionId,
            candidate: ev.candidate.toJSON ? ev.candidate.toJSON() : {
              candidate: ev.candidate.candidate,
              sdpMid: ev.candidate.sdpMid,
            },
          })
        })

        const ch = pc.createDataChannel(CHANNEL_LABEL, { ordered: true })
        ch.binaryType = 'arraybuffer'
        ch.onopen = () => { channelOpened = true; setStatus('loading', t('hl.loading')) }
        ch.onclose = () => { if (channelOpened) onTransportClosed() }
        ch.onmessage = (ev) => {
          const buf = new Uint8Array(ev.data instanceof ArrayBuffer ? ev.data : new ArrayBuffer(0))
          const decoded = decodeFrame(buf)
          if (decoded) onWireFrame(decoded.header, decoded.payload)
        }

        const offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        const offerResp = await postJson<{ session_id: string }>(
          boot.gfs,
          '/gfs/highlight_rtc/offer',
          {
            instance_id: boot.instanceId,
            highlight_id: boot.highlightId,
            token: boot.token,
            sdp: offer.sdp,
            vk: viewerKey.vk,
          },
        )
        sessionId = offerResp.session_id

        let attempts = 0
        pollTimer = setInterval(async () => {
          attempts += 1
          if (attempts > POLL_MAX_ATTEMPTS) {
            if (pollTimer) clearInterval(pollTimer)
            // Author online but no direct channel within budget → relay.
            if (!channelOpened) void startRelayFallback()
            return
          }
          try {
            const r = await gfsFetch(
              boot.gfs,
              `/gfs/highlight_rtc/session/${encodeURIComponent(sessionId!)}`,
            )
            if (sessionLost(r.status)) {
              // The GFS no longer holds this session (the server that took
              // the offer restarted, or the request reached another one): it
              // will never be answered, so go to the relay now instead of
              // polling out the whole budget.
              if (pollTimer) clearInterval(pollTimer)
              if (!channelOpened) void startRelayFallback()
              return
            }
            if (!r.ok) return // keep polling
            const session = (await r.json()) as {
              answer_sdp: string | null
              ice_candidates: RTCIceCandidateInit[]
            }
            if (session.answer_sdp && pc!.remoteDescription === null) {
              await pc!.setRemoteDescription({ type: 'answer', sdp: session.answer_sdp })
            }
            const cands = session.ice_candidates ?? []
            for (let i = ackedCandidates; i < cands.length; i++) {
              try { await pc!.addIceCandidate(cands[i]) } catch { /* tolerated */ }
            }
            ackedCandidates = cands.length
            if (session.answer_sdp && pc!.iceConnectionState === 'connected') {
              if (pollTimer) clearInterval(pollTimer)
            }
          } catch { /* keep polling */ }
        }, POLL_INTERVAL_MS)
      } catch (err) {
        setStatus('error', humanizeError((err as Error)?.message))
      }
    }

    void main()

    return () => {
      cancelled = true
      if (pollTimer) clearInterval(pollTimer)
      if (pc) try { pc.close() } catch { /* tolerated */ }
      // Release all blob URLs.
      Object.values(state.frameUrls).forEach((u) => URL.revokeObjectURL(u))
      if (state.authorPicture) URL.revokeObjectURL(state.authorPicture)
    }
  }, [boot.instanceId, boot.highlightId, boot.token, boot.gfs])

  // Auto-advance once we have a renderable frame.
  useEffect(() => {
    if (state.status !== 'playing') return
    if (state.currentIndex >= state.manifest.length) return
    const cur = state.manifest[state.currentIndex]
    if (!state.frameUrls[cur.frame_id]) return
    const dur = (cur.duration_ms && cur.duration_ms > 0)
      ? cur.duration_ms : FRAME_DURATION_MS
    const t = setTimeout(() => {
      setState((s) => {
        if (s.currentIndex < s.manifest.length - 1) {
          return { ...s, currentIndex: s.currentIndex + 1 }
        }
        return { ...s, status: 'ended' }
      })
    }, dur)
    return () => clearTimeout(t)
  }, [state.status, state.currentIndex, state.manifest, state.frameUrls])

  if (state.status === 'error') {
    return (
      <ViewerError
        class="highlight-error"
        message={state.message || t('error.connect')}
        retry={retry}
      />
    )
  }

  const incomplete = state.incomplete && (
    <div class="highlight-incomplete" role="status">
      <p>{t('hl.part_missing')}</p>
      <button type="button" class="viewer-retry" onClick={retry.manual}>{t('retry')}</button>
    </div>
  )

  if (state.status === 'ended') {
    return (
      <div class="highlight-end">
        <p>{t('hl.finished')}</p>
        {incomplete}
      </div>
    )
  }

  const total = state.manifest.length
  const cur = total > 0 ? state.manifest[state.currentIndex] : null
  const url = cur ? state.frameUrls[cur.frame_id] : null

  return (
    <div class="highlight-viewer">
      {state.author && <AuthorHeader author={state.author} picture={state.authorPicture} />}
      <div class="progress">
        {state.manifest.map((_, i) => (
          <span
            key={i}
            class={
              i < state.currentIndex ? 'seg done' :
              i === state.currentIndex ? 'seg active' : 'seg'
            }
          />
        ))}
      </div>
      <div class="stage">
        {url && cur && cur.content_type.startsWith('video/') && (
          <video src={url} autoPlay playsInline />
        )}
        {url && cur && !cur.content_type.startsWith('video/') && (
          <img src={url} alt="" />
        )}
        {!url && (
          <div class="status">
            {state.incomplete ? t('hl.this_part') : state.message ?? t('hl.buffering')}
          </div>
        )}
        {cur?.caption_text && (
          <div class="caption">{cur.caption_text}</div>
        )}
      </div>
      {incomplete}
    </div>
  )
}


/* Who shared it: picture (or initials), name and bio. Plain text only. */
function AuthorHeader({ author, picture }: { author: AuthorProfile; picture: string | null }) {
  const initials = author.display_name
    .split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]!.toUpperCase()).join('')
  return (
    <header class="highlight-author" aria-label={t('hl.shared_by', { name: author.display_name })}>
      {picture
        ? <img class="highlight-author-pic" src={picture} alt="" width={48} height={48} />
        : <span class="highlight-author-pic highlight-author-initials" aria-hidden="true">{initials}</span>}
      <div class="highlight-author-text">
        <p class="highlight-author-name">{author.display_name}</p>
        {author.bio && <p class="highlight-author-bio">{author.bio}</p>}
      </div>
    </header>
  )
}


/* A pinned link whose fragment is missing a part, too long or malformed
 * (e.g. a GFS address that isn't https). Nothing is contacted. */
export function BrokenLink() {
  return (
    <div class="highlight-error" role="alert">
      <p>{t('link.broken')}</p>
      <p>{t('link.broken_body')}</p>
    </div>
  )
}


/* The link has no secret in its fragment: an old link, or cut short. */
export function OutdatedLink() {
  return (
    <div class="highlight-error" role="alert">
      <p>{t('link.broken')}</p>
      <p>{t('link.outdated_body')}</p>
    </div>
  )
}


function humanizeError(msg: string | null | undefined): string {
  // ``expired`` only arrives inside a stream the author's own household
  // sent — the GFS itself never says it (see ``viewer_errors.ts``).
  if (msg && /expired/i.test(msg)) return t('hl.ended')
  if (msg && /unsupported/i.test(msg)) {
    return t('hl.outdated_page')
  }
  return humanizeViewerError(msg)
}


// Strip a leading ``/`` so caller paths like ``/gfs/highlights/...`` look
// natural at the call site while the actual fetch resolves against
// ``<base href>`` — keeping the public viewer portable to any
// path-prefixed deployment instead of pinning the request to the
// document origin.
const _rel = (p: string): string => p.replace(/^\/+/, '')


/* One request to the GFS. On the pinned page (``gfs`` set): absolute on
 * that GFS, cross-origin, no credentials, no referrer. On the GFS-served
 * page: relative, exactly as before. */
function gfsFetch(gfs: string | undefined, path: string, init?: RequestInit): Promise<Response> {
  if (gfs) return fetch(pinnedEndpoint(gfs, path), pinnedRequestInit(init))
  return init ? fetch(_rel(path), init) : fetch(_rel(path))
}


async function fetchJson<T>(gfs: string | undefined, url: string): Promise<T> {
  const r = await gfsFetch(gfs, url)
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return r.json() as Promise<T>
}


async function postJson<T>(gfs: string | undefined, url: string, body: unknown): Promise<T> {
  const r = await gfsFetch(gfs, url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return r.json() as Promise<T>
}


/* The boot context: the GFS page's ``<script id="boot">`` block, or — on
 * the pinned page, which has none — the link's fragment. The fragment is
 * left in the address bar: a reload (or a copy of the address) still opens
 * the highlight, and the secret is already in the browser's history from
 * the navigation itself, so dropping it now would hide nothing. */
function readBoot(root: HTMLElement): BootPayload | null {
  const bootEl = document.getElementById('boot')
  if (!bootEl) {
    // The pinned page is never meant to be framed: a framer on the same
    // origin could read its DOM. (A meta tag can't set frame-ancestors.)
    if (window.top !== window) {
      render(<BrokenLink />, root)
      return null
    }
    const pinned = pinnedBootFromHash(window.location.hash)
    if (!pinned) render(<BrokenLink />, root)
    return pinned
  }
  let boot: BootPayload
  try {
    boot = JSON.parse(bootEl.textContent || '{}')
  } catch {
    root.textContent = 'Couldn’t parse boot payload.'
    return null
  }
  if (!boot.instanceId || !boot.highlightId || !boot.token) {
    root.textContent = 'Missing boot context.'
    return null
  }
  // The GFS page never takes a GFS address from the fragment.
  return { instanceId: boot.instanceId, highlightId: boot.highlightId, token: boot.token }
}


// Bootstrap entry — read the boot context and mount.
(function bootstrap(): void {
  const root = document.getElementById('root')
  if (!root) return
  document.documentElement.lang = locale()
  // A GFS's public page (space, invite, profile): the GFS redirected its
  // own page here, everything in the fragment (``page_link.ts``). Only on
  // the pinned page — the GFS-served one (boot block) never opens one.
  if (!document.getElementById('boot') && isPageHash(window.location.hash)) {
    const page = pageBootFromHash(window.location.hash)
    document.documentElement.dataset.page = 'doc'
    if (window.top !== window || !page) {
      render(<div class="page"><main class="page-main"><BrokenLink /></main></div>, root)
      return
    }
    render(<PublicPage boot={page} />, root)
    return
  }
  // A per-moment link on the pinned page: everything — the GFS, the
  // ids, the moment's item key — is in the fragment. The GFS-served page
  // (with a boot block) never opens one.
  if (!document.getElementById('boot') && isMomentHash(window.location.hash)) {
    const moment = momentBootFromHash(window.location.hash)
    if (window.top !== window || !moment) {
      render(<BrokenLink />, root)
      return
    }
    document.title = t('title.moment')
    render(<PublicMomentViewer boot={moment} />, root)
    return
  }
  const boot = readBoot(root)
  if (!boot) return
  // The pinned page's static title names no page; the GFS page's says
  // "Highlight" already.
  if (boot.gfs) document.title = t('title.highlight')
  const secret = linkSecretFromHash(window.location.hash)
  if (!secret) {
    // Contact nobody: nothing could open the stream anyway. On the GFS
    // page, a link from before highlights were end-to-end encrypted (or
    // cut short when pasted); every pinned link had one, so it was cut.
    render(boot.gfs ? <BrokenLink /> : <OutdatedLink />, root)
    return
  }
  render(
    <RetryHost
      render={(retry, attempt) => (
        <PublicHighlightViewer key={attempt} boot={boot} secret={secret} retry={retry} />
      )}
    />,
    root,
  )
})()
