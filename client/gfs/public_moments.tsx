/* Public-moments index viewer for the GFS landing page (§Momentum-public).
 *
 * Mirrors the public-highlight viewer's transport: the guest's browser
 * opens a WebRTC DataChannel directly to the author's SH and reads the
 * author's CURRENT PUBLIC moments live; if WebRTC can't connect (NAT /
 * TURN / P2P blocked) but the author is online, it falls back to a
 * chunked GET proxied through the GFS. The GFS stores no moment bytes.
 *
 * Wire framing must stay in lockstep with
 * ``socialhome/services/highlight_public_framing.py`` (shared by
 * highlights + moments) — same length-prefixed records, with a
 * ``moment_index_meta`` header carrying the moment manifest, followed by
 * ``frame_chunk`` records for each moment that has media.
 *
 * Renders a simple scrollable list of moment cards (text + optional
 * image/video) — not a story player. No SPA dependencies.
 */
import { render } from 'preact'
import { useEffect, useMemo, useState } from 'preact/hooks'
import { humanizeViewerError as humanizeError } from './viewer_errors'
import { RetryHost, ViewerError, type RetryControl } from './viewer_retry'

const CHANNEL_LABEL = 'moment-public-v1'
const POLL_INTERVAL_MS = 1000
const POLL_MAX_ATTEMPTS = 30


interface BootPayload {
  userId: string
  instanceId: string
}

interface IceServer { urls: string[] | string; username?: string; credential?: string }

interface MomentMeta {
  id: string
  content: string
  created_at: string
  media_type?: string | null
  has_media?: boolean
  media_frame_id?: string
  byte_length?: number
  content_type?: string
}

interface ViewerState {
  status: 'connecting' | 'loading' | 'ready' | 'error'
  message: string | null
  moments: MomentMeta[]
  mediaUrls: Record<string, string>
}

interface FramingHeader {
  kind: 'moment_index_meta' | 'frame_chunk' | 'stream_end' | 'error'
  moments?: MomentMeta[]
  frame_id?: string
  sequence?: number
  chunk_index?: number
  is_last_chunk?: boolean
  byte_length?: number
  error?: string
}


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


/* Pop one complete frame off an accumulating byte buffer (the relay
 * fallback reads a chunked stream whose chunk boundaries don't align to
 * frames). Returns the decoded frame + the unconsumed tail, or null. */
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


function PublicMomentsViewer({ boot, retry }: { boot: BootPayload; retry: RetryControl }) {
  const [state, setState] = useState<ViewerState>({
    status: 'connecting',
    message: 'Connecting…',
    moments: [],
    mediaUrls: {},
  })

  const buffers = useMemo(() => new Map<string, Uint8Array[]>(), [])
  // Manifest mirror the message handler can read synchronously (the
  // effect closure captures the initial state otherwise).
  const manifest = useMemo(() => new Map<string, MomentMeta>(), [])

  useEffect(() => {
    let pc: RTCPeerConnection | null = null
    let pollTimer: ReturnType<typeof setInterval> | null = null
    let cancelled = false
    let channelOpened = false
    let relayStarted = false

    function setStatus(status: ViewerState['status'], message: string | null = null) {
      if (cancelled) return
      setState((s) => ({ ...s, status, message }))
    }

    function appendChunk(frameId: string, payload: Uint8Array, isLast: boolean, contentType: string) {
      const bucket = buffers.get(frameId) ?? []
      bucket.push(payload.slice())
      buffers.set(frameId, bucket)
      if (!isLast) return
      const blob = new Blob(bucket as unknown as BlobPart[], { type: contentType })
      const url = URL.createObjectURL(blob)
      buffers.delete(frameId)
      setState((s) => ({ ...s, mediaUrls: { ...s.mediaUrls, [frameId]: url } }))
    }

    function handleHeader(header: FramingHeader, payload: Uint8Array) {
      if (header.kind === 'moment_index_meta') {
        const moments = header.moments ?? []
        moments.forEach((m) => manifest.set(m.media_frame_id ?? m.id, m))
        setState((s) => ({ ...s, status: 'ready', message: null, moments }))
      } else if (header.kind === 'frame_chunk' && header.frame_id) {
        const meta = manifest.get(header.frame_id)
        const ct = meta?.content_type ?? 'application/octet-stream'
        appendChunk(header.frame_id, payload, !!header.is_last_chunk, ct)
      } else if (header.kind === 'stream_end') {
        // Index fully streamed.
      } else if (header.kind === 'error') {
        setStatus('error', humanizeError(header.error ?? 'unknown'))
      }
    }

    async function startRelayFallback() {
      if (relayStarted || cancelled || channelOpened) return
      relayStarted = true
      if (pollTimer) clearInterval(pollTimer)
      if (pc) { try { pc.close() } catch { /* tolerated */ } }
      setStatus('loading', 'Connecting via server…')
      try {
        const url = `/gfs/moment_rtc/relay/${encodeURIComponent(boot.userId)}`
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

        pc.addEventListener('iceconnectionstatechange', () => {
          const st = pc?.iceConnectionState
          if ((st === 'failed' || st === 'closed') && !channelOpened) {
            void startRelayFallback()
          }
        })

        pc.addEventListener('icecandidate', (ev) => {
          if (!ev.candidate || !sessionId) return
          void postJson('/gfs/moment_rtc/ice/viewer', {
            session_id: sessionId,
            candidate: ev.candidate.toJSON ? ev.candidate.toJSON() : {
              candidate: ev.candidate.candidate,
              sdpMid: ev.candidate.sdpMid,
            },
          })
        })

        const ch = pc.createDataChannel(CHANNEL_LABEL, { ordered: true })
        ch.binaryType = 'arraybuffer'
        ch.onopen = () => { channelOpened = true; setStatus('loading', 'Loading moments…') }
        ch.onmessage = (ev) => {
          const buf = new Uint8Array(ev.data instanceof ArrayBuffer ? ev.data : new ArrayBuffer(0))
          const decoded = decodeFrame(buf)
          if (decoded) handleHeader(decoded.header, decoded.payload)
        }

        const offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        const offerResp = await postJson<{ session_id: string }>(
          '/gfs/moment_rtc/offer',
          { user_id: boot.userId, sdp: offer.sdp },
        )
        sessionId = offerResp.session_id

        let attempts = 0
        pollTimer = setInterval(async () => {
          attempts += 1
          if (attempts > POLL_MAX_ATTEMPTS) {
            if (pollTimer) clearInterval(pollTimer)
            if (!channelOpened) void startRelayFallback()
            return
          }
          try {
            const session = await fetchJson<{
              answer_sdp: string | null
              ice_candidates: RTCIceCandidateInit[]
            }>(`/gfs/moment_rtc/session/${encodeURIComponent(sessionId!)}`)
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
      Object.values(state.mediaUrls).forEach((u) => URL.revokeObjectURL(u))
    }
  }, [boot.userId, boot.instanceId])

  if (state.status === 'error') {
    return (
      <ViewerError
        className="moments-error"
        message={state.message || 'Couldn’t connect.'}
        retry={retry}
      />
    )
  }
  if (state.status !== 'ready') {
    return <div class="moments-status">{state.message ?? 'Loading…'}</div>
  }
  if (state.moments.length === 0) {
    return <div class="moments-empty">No public moments right now.</div>
  }

  return (
    <ul class="moments-list">
      {state.moments.map((m) => {
        const url = m.media_frame_id ? state.mediaUrls[m.media_frame_id] : undefined
        const isVideo = (m.media_type ?? m.content_type ?? '').includes('video')
        return (
          <li key={m.id} class="moment-card">
            {m.content && <p class="moment-text">{m.content}</p>}
            {m.has_media && !url && <div class="moment-media-loading">Loading media…</div>}
            {url && isVideo && <video class="moment-media" src={url} controls playsInline />}
            {url && !isVideo && <img class="moment-media" src={url} alt="" />}
            <time class="moment-time" dateTime={m.created_at}>{m.created_at}</time>
          </li>
        )
      })}
    </ul>
  )
}


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


// Bootstrap — read ``<script id="moments-boot">`` and mount into
// ``#moments-root`` (the SSR user-detail page renders both).
(function bootstrap(): void {
  const bootEl = document.getElementById('moments-boot')
  const root = document.getElementById('moments-root')
  if (!bootEl || !root) return
  let boot: BootPayload
  try {
    boot = JSON.parse(bootEl.textContent || '{}')
  } catch {
    root.textContent = 'Couldn’t parse boot payload.'
    return
  }
  if (!boot.userId || !boot.instanceId) {
    root.textContent = 'Missing boot context.'
    return
  }
  render(
    <RetryHost
      render={(retry, attempt) => (
        <PublicMomentsViewer key={attempt} boot={boot} retry={retry} />
      )}
    />,
    root,
  )
})()
