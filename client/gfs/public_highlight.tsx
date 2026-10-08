/* Public-highlight viewer for the GFS landing page (§highlights_public).
 *
 * Preact bootstrap. The GFS-side SSR landing renders an empty
 * ``<div id="root">`` and a ``<script id="boot">`` JSON payload with
 * ``{instanceId, highlightId, token}``; this module mounts the viewer
 * into that root.
 *
 * Wire framing must stay in lockstep with
 * ``socialhome/services/highlight_public_framing.py`` — see the
 * golden-bytes test in ``tests/protocol/test_highlight_public_framing.py``.
 *
 * No SPA dependencies (signals, router, design tokens) — keeps the
 * bundle small enough to inline in the SSR page if we ever want to.
 */
import { render } from 'preact'
import { useEffect, useMemo, useState } from 'preact/hooks'
import { humanizeViewerError } from './viewer_errors'
import { RetryHost, ViewerError, type RetryControl } from './viewer_retry'

const CHANNEL_LABEL = 'highlight-public-v1'
const FRAME_DURATION_MS = 6000
const POLL_INTERVAL_MS = 1000
const POLL_MAX_ATTEMPTS = 30


interface BootPayload {
  instanceId: string
  highlightId: string
  token: string
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
}

interface FramingHeader {
  kind: 'highlight_meta' | 'frame_chunk' | 'stream_end' | 'error'
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


function PublicHighlightViewer({ boot, retry }: { boot: BootPayload; retry: RetryControl }) {
  const [state, setState] = useState<ViewerState>({
    status: 'connecting',
    message: 'Connecting…',
    manifest: [],
    highlight: null,
    frameUrls: {},
    currentIndex: 0,
  })

  // Per-frame chunk buffer — kept in a ref-like store so the message
  // handler captured at mount keeps appending into the same Map.
  const buffers = useMemo(() => new Map<string, Uint8Array[]>(), [])

  useEffect(() => {
    let pc: RTCPeerConnection | null = null
    let pollTimer: ReturnType<typeof setInterval> | null = null
    let cancelled = false

    function setStatus(status: ViewerState['status'], message: string | null = null) {
      if (cancelled) return
      setState((s) => ({ ...s, status, message }))
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

    function handleHeader(header: FramingHeader, payload: Uint8Array) {
      if (header.kind === 'highlight_meta') {
        setState((s) => ({
          ...s,
          status: 'playing',
          message: null,
          manifest: header.frames ?? [],
          highlight: header.highlight ?? null,
        }))
      } else if (header.kind === 'frame_chunk' && header.frame_id) {
        const meta = state.manifest.find((f) => f.frame_id === header.frame_id)
        const ct = meta?.content_type ?? 'application/octet-stream'
        appendChunk(header.frame_id, payload, !!header.is_last_chunk, ct)
      } else if (header.kind === 'stream_end') {
        // Streaming complete. Playback continues from already-buffered urls.
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
      setStatus('loading', 'Connecting via server…')
      try {
        const url =
          `/gfs/highlight_rtc/relay/${encodeURIComponent(boot.instanceId)}` +
          `/${encodeURIComponent(boot.highlightId)}?token=${encodeURIComponent(boot.token)}`
        const r = await fetch(_rel(url))
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
            handleHeader(taken.header, taken.payload)
          }
        }
      } catch (err) {
        setStatus('error', humanizeError((err as Error)?.message))
      }
    }

    async function main() {
      try {
        const ice = await fetchJson<{ servers: IceServer[] }>('/gfs/highlights/ice-servers')
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
          void postJson('/gfs/highlight_rtc/ice/viewer', {
            session_id: sessionId,
            candidate: ev.candidate.toJSON ? ev.candidate.toJSON() : {
              candidate: ev.candidate.candidate,
              sdpMid: ev.candidate.sdpMid,
            },
          })
        })

        const ch = pc.createDataChannel(CHANNEL_LABEL, { ordered: true })
        ch.binaryType = 'arraybuffer'
        ch.onopen = () => { channelOpened = true; setStatus('loading', 'Loading highlight…') }
        ch.onmessage = (ev) => {
          const buf = new Uint8Array(ev.data instanceof ArrayBuffer ? ev.data : new ArrayBuffer(0))
          const decoded = decodeFrame(buf)
          if (decoded) handleHeader(decoded.header, decoded.payload)
        }

        const offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        const offerResp = await postJson<{ session_id: string }>(
          '/gfs/highlight_rtc/offer',
          {
            instance_id: boot.instanceId,
            highlight_id: boot.highlightId,
            token: boot.token,
            sdp: offer.sdp,
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
            const session = await fetchJson<{
              answer_sdp: string | null
              ice_candidates: RTCIceCandidateInit[]
            }>(`/gfs/highlight_rtc/session/${encodeURIComponent(sessionId!)}`)
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
    }
  }, [boot.instanceId, boot.highlightId, boot.token])

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
        className="highlight-error"
        message={state.message || 'Couldn’t connect.'}
        retry={retry}
      />
    )
  }

  if (state.status === 'ended') {
    return <div class="highlight-end">Highlight finished.</div>
  }

  const total = state.manifest.length
  const cur = total > 0 ? state.manifest[state.currentIndex] : null
  const url = cur ? state.frameUrls[cur.frame_id] : null

  return (
    <div class="highlight-viewer">
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
        {!url && <div class="status">{state.message ?? 'Buffering…'}</div>}
        {cur?.caption_text && (
          <div class="caption">{cur.caption_text}</div>
        )}
      </div>
    </div>
  )
}


function humanizeError(msg: string | null | undefined): string {
  // ``expired`` only arrives inside a stream the author's own household
  // sent — the GFS itself never says it (see ``viewer_errors.ts``).
  if (msg && /expired/i.test(msg)) return 'This highlight has ended.'
  return humanizeViewerError(msg)
}


// Strip a leading ``/`` so caller paths like ``/gfs/highlights/...`` look
// natural at the call site while the actual fetch resolves against
// ``<base href>`` — keeping the public viewer portable to any
// path-prefixed deployment instead of pinning the request to the
// document origin.
const _rel = (p: string): string => p.replace(/^\/+/, '')


async function fetchJson<T>(url: string): Promise<T> {
  const r = await fetch(_rel(url))
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return r.json() as Promise<T>
}


async function postJson<T>(url: string, body: unknown): Promise<T> {
  const r = await fetch(_rel(url), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return r.json() as Promise<T>
}


// Bootstrap entry — read ``<script id="boot">`` and mount.
(function bootstrap(): void {
  const bootEl = document.getElementById('boot')
  const root = document.getElementById('root')
  if (!bootEl || !root) return
  let boot: BootPayload
  try {
    boot = JSON.parse(bootEl.textContent || '{}')
  } catch {
    root.textContent = 'Couldn’t parse boot payload.'
    return
  }
  if (!boot.instanceId || !boot.highlightId || !boot.token) {
    root.textContent = 'Missing boot context.'
    return
  }
  render(
    <RetryHost
      render={(retry, attempt) => (
        <PublicHighlightViewer key={attempt} boot={boot} retry={retry} />
      )}
    />,
    root,
  )
})()
