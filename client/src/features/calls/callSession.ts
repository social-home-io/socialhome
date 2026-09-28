/**
 * callSession — the browser half of a 1:1 call's WebRTC handshake (§26).
 *
 * The backend is a pure signalling relay (``services/call_service.py``):
 * it signs and forwards whatever SDP / ICE the browsers hand it, locally
 * over ``call.*`` WS frames and cross-household over ``CALL_*`` federation
 * events. Media never touches a server. This module owns the single live
 * ``RTCPeerConnection`` and drives that contract:
 *
 * Caller                                   Callee
 *   getUserMedia → addTrack
 *   createOffer / setLocalDescription
 *   POST /api/calls {sdp_offer}  ──────▶  WS call.ringing {signed_sdp}
 *                                          (Accept) getUserMedia → addTrack
 *                                          setRemoteDescription(offer)
 *                                          createAnswer / setLocalDescription
 *   WS call.answered {signed_sdp} ◀──────  POST /api/calls/{id}/answer
 *   setRemoteDescription(answer)
 *   POST /api/calls/{id}/ice  ◀─ trickle both ways ─▶  WS call.ice_candidate
 *
 * Remote candidates are queued by ``store/calls`` (``pendingIce``) from
 * the moment the WS frame lands — the callee receives the caller's
 * candidates while the call is still ringing — and are only applied once
 * a remote description exists. Local candidates are held until the other
 * side can address them (the caller doesn't know ``call_id`` until
 * ``POST /api/calls`` returns; the callee holds them until its answer is
 * posted so the caller never sees a candidate before the answer).
 *
 * The session outlives route changes (it starts in the call picker /
 * ringing dialog and is rendered by ``InCallPage``), so it lives at module
 * level rather than inside a component.
 */
import { effect, signal } from '@preact/signals'
import { api } from '@/api'
import { ws, type WsEvent } from '@/ws'
import { consumeIce, pendingIce, type IncomingCall } from '@/store/calls'
import { CallEmbedBlockedError, embedBlocksMicrophone, isFramed } from './embedPolicy'

export type CallType = 'audio' | 'video'
export type CallPhase =
  | 'idle'          // no call on this device
  | 'starting'      // caller: acquiring media / creating the offer
  | 'ringing'       // caller: offer delivered, waiting for an answer
  | 'connecting'    // SDP exchanged, ICE / DTLS in progress
  | 'connected'     // media flowing
  | 'reconnecting'  // ICE dropped, the browser is trying to recover
  | 'ended'         // hung up / declined (locally or by the peer)
  | 'failed'        // media could not be established

export const callPhase        = signal<CallPhase>('idle')
export const callId           = signal<string | null>(null)
export const callType         = signal<CallType>('audio')
export const callConversation = signal<string | null>(null)
/** Human-readable reason for ``ended`` / ``failed`` (toast / page copy). */
export const callEndReason    = signal<string | null>(null)
export const localStream      = signal<MediaStream | null>(null)
export const remoteStream     = signal<MediaStream | null>(null)
/** ``false`` for an audio call (the camera is never opened) or when the
 *  device gave us no camera — the page disables the camera toggle instead
 *  of offering a control that can't do anything. */
export const hasCamera        = signal<boolean>(false)

interface SignedSdp { sdp?: unknown, sdp_type?: unknown }
interface IceServersResponse { ice_servers?: RTCIceServer[] }

let pc: RTCPeerConnection | null = null
let role: 'caller' | 'callee' | null = null
/** Local candidates waiting until the peer can be addressed. */
let outboundIce: RTCIceCandidateInit[] = []
let outboundReady = false
/** ``call.answered`` frames that raced ahead of ``POST /api/calls``. */
let earlyAnswers = new Map<string, string>()
let unsubs: Array<() => void> = []

/** ``true`` from the moment a call starts / is accepted until teardown. */
export function isCallLive(): boolean {
  return role !== null
}

export function getPeerConnection(): RTCPeerConnection | null {
  return pc
}

/** Start an outbound call. Resolves with the new ``call_id``; rejects with
 *  a user-facing ``Error`` (and leaves no half-open session behind). */
export async function startCall(conversationId: string, type: CallType): Promise<string> {
  begin('caller', type, conversationId)
  callPhase.value = 'starting'
  try {
    const conn = await openPeerConnection(type)
    const offer = await conn.createOffer()
    await conn.setLocalDescription(offer)
    if (pc !== conn) throw new Error('Call cancelled')
    const r = await api.post('/api/calls', {
      conversation_id: conversationId,
      call_type: type,
      sdp_offer: conn.localDescription?.sdp ?? offer.sdp,
    }) as { call_id: string }
    if (pc !== conn) {
      // Hung up while the POST was in flight — tell the backend too.
      void api.post(`/api/calls/${r.call_id}/hangup`, {}).catch(() => {})
      throw new Error('Call cancelled')
    }
    callId.value = r.call_id
    if (callPhase.value === 'starting') callPhase.value = 'ringing'
    const early = earlyAnswers.get(r.call_id)
    earlyAnswers.clear()
    if (early !== undefined) await applyAnswer(early)
    flushOutboundIce()
    drainRemoteIce()
    return r.call_id
  } catch (err) {
    if (role !== null) teardown('failed', null)
    throw asUserError(err)
  }
}

/** Accept a ringing call: answer the caller's offer. Rejects with a
 *  user-facing ``Error`` (the ringing dialog stays up so the user can
 *  retry or decline). */
export async function acceptCall(call: IncomingCall): Promise<void> {
  const offerSdp = (call.signed_sdp as SignedSdp | undefined)?.sdp
  if (typeof offerSdp !== 'string' || !offerSdp.trim()) {
    throw new Error('This call has no connection offer to answer.')
  }
  begin('callee', call.call_type, call.conversation_id ?? null)
  callId.value = call.call_id
  callPhase.value = 'connecting'
  try {
    const conn = await openPeerConnection(call.call_type)
    await conn.setRemoteDescription({ type: 'offer', sdp: offerSdp })
    drainRemoteIce()
    const answer = await conn.createAnswer()
    await conn.setLocalDescription(answer)
    if (pc !== conn) throw new Error('Call cancelled')
    await api.post(`/api/calls/${call.call_id}/answer`, {
      sdp_answer: conn.localDescription?.sdp ?? answer.sdp,
    })
    flushOutboundIce()
  } catch (err) {
    if (role !== null) teardown('failed', null)
    throw asUserError(err)
  }
}

/** Hang up (or cancel a ringing outbound call) and release the media. */
export async function hangupCall(): Promise<void> {
  const id = callId.value
  if (role === null && id === null) return
  if (role !== null) teardown('ended', null)
  else callPhase.value = 'ended'
  if (id) {
    try { await api.post(`/api/calls/${id}/hangup`, {}) } catch { /* best-effort */ }
  }
}

/** Forget a finished call so the next one starts clean. */
export function resetCall(): void {
  if (role !== null) return
  callPhase.value = 'idle'
  callId.value = null
  callConversation.value = null
  callEndReason.value = null
}

// ─── internals ──────────────────────────────────────────────────────────

function begin(r: 'caller' | 'callee', type: CallType, conversationId: string | null): void {
  if (role !== null) throw new Error("You're already in a call.")
  role = r
  callType.value = type
  callConversation.value = conversationId
  callId.value = null
  callEndReason.value = null
  outboundIce = []
  outboundReady = false
  earlyAnswers = new Map()
  unsubs = [
    ws.on('call.answered', onAnswered),
    ws.on('call.ended', (e) => onRemoteEnd(e, 'The call ended.')),
    ws.on('call.declined', (e) => onRemoteEnd(e, 'The call was declined.')),
    // ``store/calls`` queues every candidate; apply them as they land.
    effect(() => { void pendingIce.value; drainRemoteIce() }),
  ]
}

async function openPeerConnection(type: CallType): Promise<RTCPeerConnection> {
  const [servers, stream] = await Promise.all([
    (api.get('/api/calls/ice-servers') as Promise<IceServersResponse>)
      .then(r => r.ice_servers ?? [])
      .catch(() => [] as RTCIceServer[]),
    acquireMedia(type),
  ])
  if (role === null) {
    // Torn down while waiting on the permission prompt.
    stream.getTracks().forEach(t => t.stop())
    throw new Error('Call cancelled')
  }
  const conn = new RTCPeerConnection({ iceServers: servers })
  pc = conn
  // Audio calls never open the camera, so there is no track to switch on
  // mid-call (that would need a renegotiation the signalling doesn't
  // carry) — the page disables the camera toggle instead.
  hasCamera.value = stream.getVideoTracks().length > 0
  stream.getTracks().forEach(t => conn.addTrack(t, stream))
  localStream.value = stream

  conn.ontrack = (evt) => {
    const ms = remoteStream.value ?? evt.streams[0] ?? new MediaStream()
    if (!ms.getTracks().includes(evt.track)) ms.addTrack(evt.track)
    // New object identity is not needed — the <video> keeps the stream.
    remoteStream.value = ms
  }
  conn.onicecandidate = (evt) => {
    if (!evt.candidate) return
    outboundIce.push(evt.candidate.toJSON())
    if (outboundReady) flushOutboundIce()
  }
  conn.onconnectionstatechange = () => {
    if (pc !== conn) return
    switch (conn.connectionState) {
      case 'connected':
        callPhase.value = 'connected'
        break
      case 'disconnected':
        if (callPhase.value === 'connected') callPhase.value = 'reconnecting'
        break
      case 'failed': {
        const id = callId.value
        teardown('failed', "Couldn't connect the call. The network between you may be "
          + 'blocking it — a TURN server may be needed.')
        if (id) void api.post(`/api/calls/${id}/hangup`, {}).catch(() => {})
        break
      }
    }
  }
  return conn
}

async function acquireMedia(type: CallType): Promise<MediaStream> {
  const md = typeof navigator !== 'undefined' ? navigator.mediaDevices : undefined
  if (!md?.getUserMedia) {
    throw new Error('Calls need a secure (HTTPS) connection to use the microphone.')
  }
  if (embedBlocksMicrophone()) throw new CallEmbedBlockedError()
  // An audio call asks for the microphone only — no camera prompt, no
  // camera light. A video call asks for both and falls back to
  // audio-only when there is no (usable) camera.
  if (type === 'video') {
    try {
      return await md.getUserMedia({ audio: true, video: true })
    } catch { /* fall through to audio-only */ }
  }
  try {
    return await md.getUserMedia({ audio: true, video: false })
  } catch (err) {
    const name = (err as DOMException)?.name
    if (name === 'NotAllowedError' || name === 'SecurityError') {
      // Inside a frame a denial can come from the embedding page's
      // ``allow`` attribute rather than from the user — Firefox and
      // Safari have no API to tell the two apart.
      if (isFramed()) throw new CallEmbedBlockedError({ cause: err, certain: false })
      throw new Error('Microphone access is blocked. Allow it in your browser\'s site '
        + 'settings, then try again.', { cause: err })
    }
    if (name === 'NotFoundError') {
      throw new Error('No microphone was found on this device.', { cause: err })
    }
    throw new Error(`Couldn't start the microphone: ${(err as Error)?.message ?? err}`,
      { cause: err })
  }
}

function onAnswered(e: WsEvent): void {
  const d = e.data as { call_id?: string, signed_sdp?: SignedSdp }
  const sdp = d.signed_sdp?.sdp
  if (role !== 'caller' || !d.call_id || typeof sdp !== 'string') return
  if (callId.value === null) {
    // Answer beat our own POST /api/calls response — hold it.
    earlyAnswers.set(d.call_id, sdp)
    return
  }
  if (d.call_id !== callId.value) return
  void applyAnswer(sdp)
}

async function applyAnswer(sdp: string): Promise<void> {
  const conn = pc
  // A group call rings several people with one offer; only the first
  // answer can be applied.
  if (conn === null || conn.signalingState !== 'have-local-offer') return
  try {
    await conn.setRemoteDescription({ type: 'answer', sdp })
    if (pc === conn && callPhase.value !== 'connected') callPhase.value = 'connecting'
    drainRemoteIce()
  } catch (err) {
    const id = callId.value
    teardown('failed', `Couldn't connect the call: ${(err as Error)?.message ?? err}`)
    if (id) void api.post(`/api/calls/${id}/hangup`, {}).catch(() => {})
  }
}

function onRemoteEnd(e: WsEvent, reason: string): void {
  const d = e.data as { call_id?: string }
  if (!d.call_id || d.call_id !== callId.value) return
  teardown('ended', reason)
}

function drainRemoteIce(): void {
  const conn = pc
  const id = callId.value
  if (conn === null || id === null || !conn.remoteDescription) return
  for (const c of consumeIce(id)) {
    if (!c.candidate) continue
    conn.addIceCandidate(c.candidate as RTCIceCandidateInit).catch(() => {
      /* a stale / malformed candidate must not kill the call */
    })
  }
}

function flushOutboundIce(): void {
  const id = callId.value
  if (id === null) return
  outboundReady = true
  const batch = outboundIce
  outboundIce = []
  for (const candidate of batch) {
    api.post(`/api/calls/${id}/ice`, { candidate }).catch(() => { /* best-effort */ })
  }
}

function teardown(phase: 'ended' | 'failed', reason: string | null): void {
  unsubs.forEach(u => u())
  unsubs = []
  const conn = pc
  pc = null
  role = null
  outboundIce = []
  outboundReady = false
  if (conn) {
    conn.onicecandidate = null
    conn.ontrack = null
    conn.onconnectionstatechange = null
    conn.close()
  }
  localStream.value?.getTracks().forEach(t => t.stop())
  localStream.value = null
  remoteStream.value = null
  callEndReason.value = reason
  callPhase.value = phase
}

function asUserError(err: unknown): Error {
  return err instanceof Error ? err : new Error(String(err))
}
