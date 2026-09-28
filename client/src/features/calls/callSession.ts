/**
 * callSession — the browser half of a call's WebRTC handshake (§26).
 *
 * The backend is a pure signalling relay (``services/call_service.py``):
 * it signs and forwards whatever SDP / ICE the browsers hand it, locally
 * over ``call.*`` WS frames and cross-household over ``CALL_*`` federation
 * events. Media never touches a server.
 *
 * A call is a full mesh: one ``RTCPeerConnection`` ("leg") per remote
 * participant. A 1:1 call is simply a mesh with one leg.
 *
 * Caller                                   Callee
 *   getUserMedia → addTrack (per leg)
 *   createOffer / setLocalDescription (per callee)
 *   POST /api/calls {sdp_offer | sdp_offers} ─▶ WS call.ringing {signed_sdp, participants}
 *                                          (Accept) getUserMedia → addTrack
 *                                          setRemoteDescription(offer)
 *                                          createAnswer / setLocalDescription
 *   WS call.answered {from_user, signed_sdp} ◀── POST /api/calls/{id}/answer
 *   setRemoteDescription(answer)
 *   POST /api/calls/{id}/ice {to_user} ◀─ trickle per leg ─▶ WS call.ice_candidate {from_user}
 *
 * Callee ↔ callee legs of a group call: of each pair the one with the
 * lower ``user_id`` offers, right after answering the caller, through
 * ``POST /api/calls/{id}/join {sdp_offers}``. The other side gets
 * ``call.peer_join`` (kept by ``store/calls`` while it is still ringing)
 * and answers with ``POST /answer {sdp_answer, to_user}``.
 *
 * Remote candidates are queued by ``store/calls`` (``pendingIce``) from
 * the moment the WS frame lands and applied to their leg once it has a
 * remote description. Local candidates are held until the other side can
 * address them (the caller doesn't know ``call_id`` until
 * ``POST /api/calls`` returns; an answering leg holds them until its
 * answer is posted so the peer never sees a candidate before the answer).
 *
 * The session outlives route changes (it starts in the call picker /
 * ringing dialog and is rendered by ``InCallPage``), so it lives at module
 * level rather than inside a component.
 */
import { effect, signal } from '@preact/signals'
import { api } from '@/api'
import { ws, type WsEvent } from '@/ws'
import { currentUser } from '@/store/auth'
import {
  consumeIce, consumePeerOffers, pendingIce, pendingPeerOffers,
  type IncomingCall,
} from '@/store/calls'
import { CallEmbedBlockedError, embedBlocksMicrophone, isFramed } from './embedPolicy'

export type CallType = 'audio' | 'video'
export type CallPhase =
  | 'idle'          // no call on this device
  | 'starting'      // caller: acquiring media / creating the offer
  | 'ringing'       // caller: offer delivered, nobody answered yet
  | 'connecting'    // SDP exchanged, ICE / DTLS in progress
  | 'connected'     // media flowing (with at least one participant)
  | 'reconnecting'  // ICE dropped, the browser is trying to recover
  | 'ended'         // hung up / declined (locally or by the others)
  | 'failed'        // media could not be established

/** One remote participant as the call page shows it. */
export type PeerState = 'ringing' | 'connecting' | 'connected' | 'reconnecting'
export interface CallPeer {
  userId: string
  name: string
  state: PeerState
  stream: MediaStream | null
}

/** Mirrors ``MAX_CALL_PARTICIPANTS`` in ``services/call_service.py``:
 *  every browser uploads its media once per other participant. */
export const MAX_CALL_PARTICIPANTS = 6
/** A leg nobody answers is dropped after the backend's ringing TTL. */
export const RING_TIMEOUT_MS = 90_000

export const callPhase        = signal<CallPhase>('idle')
export const callId           = signal<string | null>(null)
export const callType         = signal<CallType>('audio')
export const callConversation = signal<string | null>(null)
/** Human-readable reason for ``ended`` / ``failed`` (toast / page copy). */
export const callEndReason    = signal<string | null>(null)
export const localStream      = signal<MediaStream | null>(null)
/** Remote participants, in roster order. */
export const callPeers        = signal<CallPeer[]>([])
/** ``false`` for an audio call (the camera is never opened) or when the
 *  device gave us no camera — the page disables the camera toggle instead
 *  of offering a control that can't do anything. */
export const hasCamera        = signal<boolean>(false)

interface SignedSdp { sdp?: unknown, sdp_type?: unknown }
interface IceServersResponse { ice_servers?: RTCIceServer[] }
interface MemberRow { user_id: string, display_name?: string, username?: string, is_self?: boolean }

interface Leg {
  userId: string
  pc: RTCPeerConnection
  /** Local candidates waiting until the peer can be addressed. */
  outboundIce: RTCIceCandidateInit[]
  ready: boolean
  state: PeerState
  stream: MediaStream | null
  timer: ReturnType<typeof setTimeout> | null
}

let role: 'caller' | 'callee' | null = null
let me = ''
let legs = new Map<string, Leg>()
/** Invited participants we expect a leg with but have none yet (a
 *  callee with a lower id who hasn't accepted — they'll offer to us). */
let pending = new Set<string>()
/** Roster order for the tiles (everyone but us). */
let order: string[] = []
let names = new Map<string, string>()
let iceServers: RTCIceServer[] = []
/** ``call.answered`` frames that raced ahead of ``POST /api/calls``. */
let earlyAnswers = new Map<string, string>()
let anyAnswered = false
let unsubs: Array<() => void> = []

/** ``true`` from the moment a call starts / is accepted until teardown. */
export function isCallLive(): boolean {
  return role !== null
}

/** The first leg's connection (quality sampling reads its stats). */
export function getPeerConnection(): RTCPeerConnection | null {
  return legs.values().next().value?.pc ?? null
}

/** Start an outbound call to everyone else in the conversation. Resolves
 *  with the new ``call_id``; rejects with a user-facing ``Error`` (and
 *  leaves no half-open session behind). */
export async function startCall(conversationId: string, type: CallType): Promise<string> {
  begin('caller', type, conversationId)
  callPhase.value = 'starting'
  const session = unsubs
  try {
    const [stream, members] = await Promise.all([
      openSession(type),
      api.get(`/api/conversations/${conversationId}/members`) as Promise<MemberRow[]>,
    ])
    if (unsubs !== session) throw new Error('Call cancelled')
    const others = members.filter(m => !m.is_self && m.user_id !== me)
    if (others.length === 0) throw new Error('There is nobody else in this conversation to call.')
    if (others.length + 1 > MAX_CALL_PARTICIPANTS) {
      throw new Error(`Group calls are limited to ${MAX_CALL_PARTICIPANTS} people.`)
    }
    for (const m of others) names.set(m.user_id, m.display_name || m.username || m.user_id)
    order = others.map(m => m.user_id)
    const offers: Record<string, string> = {}
    for (const m of others) {
      const leg = openLeg(m.user_id, stream, 'ringing')
      const offer = await leg.pc.createOffer()
      await leg.pc.setLocalDescription(offer)
      offers[m.user_id] = leg.pc.localDescription?.sdp ?? offer.sdp ?? ''
    }
    publishPeers()
    if (unsubs !== session) throw new Error('Call cancelled')
    // 1:1 keeps the single-offer body older households understand.
    const body = others.length === 1
      ? { conversation_id: conversationId, call_type: type, sdp_offer: offers[others[0].user_id] }
      : { conversation_id: conversationId, call_type: type, sdp_offers: offers }
    const r = await api.post('/api/calls', body) as { call_id: string, participants?: string[] }
    if (unsubs !== session) {
      // Hung up while the POST was in flight — tell the backend too.
      void api.post(`/api/calls/${r.call_id}/hangup`, {}).catch(() => {})
      throw new Error('Call cancelled')
    }
    callId.value = r.call_id
    // A callee the backend didn't ring (no offer reached it) has no leg.
    if (Array.isArray(r.participants)) {
      for (const uid of [...legs.keys()]) if (!r.participants.includes(uid)) closeLeg(uid)
    }
    if (callPhase.value === 'starting') callPhase.value = 'ringing'
    const early = earlyAnswers
    earlyAnswers = new Map()
    for (const [from, sdp] of early) await applyAnswer(from, sdp)
    for (const leg of legs.values()) markReady(leg)
    drainRemoteIce()
    return r.call_id
  } catch (err) {
    if (role !== null && unsubs === session) teardown('failed', null)
    throw asUserError(err)
  }
}

/** Accept a ringing call: answer the caller's offer, then open the mesh
 *  legs to the other callees. Rejects with a user-facing ``Error`` (the
 *  ringing dialog stays up so the user can retry or decline). */
export async function acceptCall(call: IncomingCall): Promise<void> {
  const offerSdp = (call.signed_sdp as SignedSdp | undefined)?.sdp
  if (typeof offerSdp !== 'string' || !offerSdp.trim()) {
    throw new Error('This call has no connection offer to answer.')
  }
  begin('callee', call.call_type, call.conversation_id ?? null)
  callId.value = call.call_id
  callPhase.value = 'connecting'
  const session = unsubs
  const caller = call.from_user
  const roster = (call.participants ?? []).filter(u => u !== me && u !== caller)
  order = [caller, ...roster]
  // Of each callee pair the lower id offers; the others offer to us.
  const iOffer = roster.filter(u => me !== '' && me < u)
  pending = new Set(roster.filter(u => !iOffer.includes(u)))
  if (call.conversation_id) void loadNames(call.conversation_id, session)
  if (pending.size > 0) {
    // A callee who hasn't accepted by the ringing TTL isn't coming.
    setTimeout(() => {
      if (unsubs !== session || pending.size === 0) return
      pending = new Set()
      if (!maybeFinish('Nobody else joined.')) refresh()
    }, RING_TIMEOUT_MS)
  }
  try {
    const stream = await openSession(call.call_type)
    const leg = openLeg(caller, stream, 'connecting')
    await leg.pc.setRemoteDescription({ type: 'offer', sdp: offerSdp })
    drainRemoteIce()
    const answer = await leg.pc.createAnswer()
    await leg.pc.setLocalDescription(answer)
    if (unsubs !== session) throw new Error('Call cancelled')
    await api.post(`/api/calls/${call.call_id}/answer`, {
      sdp_answer: leg.pc.localDescription?.sdp ?? answer.sdp,
    })
    markReady(leg)
    publishPeers()
    if (iOffer.length > 0) await offerLegs(call.call_id, iOffer, stream, session)
    drainPeerOffers()
  } catch (err) {
    if (role !== null && unsubs === session) teardown('failed', null)
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
  me = currentUser.value?.user_id ?? ''
  callType.value = type
  callConversation.value = conversationId
  callId.value = null
  callEndReason.value = null
  legs = new Map()
  pending = new Set()
  order = []
  names = new Map()
  earlyAnswers = new Map()
  anyAnswered = false
  callPeers.value = []
  unsubs = [
    ws.on('call.answered', onAnswered),
    ws.on('call.ended', (e) => onRemoteLeave(e, 'The call ended.')),
    ws.on('call.declined', (e) => onRemoteLeave(e, 'The call was declined.')),
    // ``store/calls`` queues every candidate and mesh offer; apply them
    // as they land.
    effect(() => { void pendingIce.value; drainRemoteIce() }),
    effect(() => { void pendingPeerOffers.value; drainPeerOffers() }),
  ]
}

/** Media + ICE servers, shared by every leg of the call. */
async function openSession(type: CallType): Promise<MediaStream> {
  const session = unsubs
  const [servers, stream] = await Promise.all([
    (api.get('/api/calls/ice-servers') as Promise<IceServersResponse>)
      .then(r => r.ice_servers ?? [])
      .catch(() => [] as RTCIceServer[]),
    acquireMedia(type),
  ])
  if (role === null || unsubs !== session) {
    // Torn down while waiting on the permission prompt.
    stream.getTracks().forEach(t => t.stop())
    throw new Error('Call cancelled')
  }
  iceServers = servers
  // Audio calls never open the camera, so there is no track to switch on
  // mid-call (that would need a renegotiation the signalling doesn't
  // carry) — the page disables the camera toggle instead.
  hasCamera.value = stream.getVideoTracks().length > 0
  localStream.value = stream
  return stream
}

async function loadNames(conversationId: string, session: typeof unsubs): Promise<void> {
  try {
    const rows = await api.get(`/api/conversations/${conversationId}/members`) as MemberRow[]
    if (unsubs !== session) return
    for (const m of rows) names.set(m.user_id, m.display_name || m.username || m.user_id)
    publishPeers()
  } catch { /* names fall back to ids */ }
}

function openLeg(userId: string, stream: MediaStream, state: PeerState): Leg {
  const conn = new RTCPeerConnection({ iceServers })
  const leg: Leg = {
    userId, pc: conn, outboundIce: [], ready: false, state, stream: null, timer: null,
  }
  legs.set(userId, leg)
  pending.delete(userId)
  stream.getTracks().forEach(t => conn.addTrack(t, stream))

  conn.ontrack = (evt) => {
    const ms = leg.stream ?? evt.streams[0] ?? new MediaStream()
    if (!ms.getTracks().includes(evt.track)) ms.addTrack(evt.track)
    leg.stream = ms
    publishPeers()
  }
  conn.onicecandidate = (evt) => {
    if (!evt.candidate) return
    leg.outboundIce.push(evt.candidate.toJSON())
    if (leg.ready) flushOutboundIce(leg)
  }
  conn.onconnectionstatechange = () => {
    if (legs.get(userId) !== leg) return
    switch (conn.connectionState) {
      case 'connected':
        leg.state = 'connected'
        break
      case 'disconnected':
        if (leg.state === 'connected') leg.state = 'reconnecting'
        break
      case 'failed':
        legFailed(leg)
        return
    }
    refresh()
  }
  if (state === 'ringing') {
    // Nobody picked up within the ringing TTL — stop showing them.
    leg.timer = setTimeout(() => {
      if (legs.get(userId) === leg && !leg.pc.remoteDescription) closeLeg(userId)
      maybeFinish('Nobody answered.')
    }, RING_TIMEOUT_MS)
  }
  return leg
}

/** Offer mesh legs to *targets* (callees we are the lower id of). */
async function offerLegs(
  id: string, targets: string[], stream: MediaStream, session: typeof unsubs,
): Promise<void> {
  const offers: Record<string, string> = {}
  const opened: Leg[] = []
  for (const uid of targets) {
    const leg = openLeg(uid, stream, 'ringing')
    const offer = await leg.pc.createOffer()
    await leg.pc.setLocalDescription(offer)
    offers[uid] = leg.pc.localDescription?.sdp ?? offer.sdp ?? ''
    opened.push(leg)
  }
  publishPeers()
  if (unsubs !== session) return
  try {
    await api.post(`/api/calls/${id}/join`, { sdp_offers: offers })
  } catch {
    // The call to the caller works without these legs.
    opened.forEach(l => closeLeg(l.userId))
    return
  }
  opened.forEach(markReady)
}

/** Answer a queued mesh-leg offer from another participant. */
async function answerPeer(from: string, sdp: string): Promise<void> {
  const id = callId.value
  const stream = localStream.value
  if (id === null || stream === null || from === me) return
  const existing = legs.get(from)
  if (existing) {
    // Both sides offered (glare): the lower id's offer wins.
    if (me < from || existing.pc.remoteDescription) return
    closeLeg(from)
  }
  const leg = openLeg(from, stream, 'connecting')
  try {
    await leg.pc.setRemoteDescription({ type: 'offer', sdp })
    drainRemoteIce()
    const answer = await leg.pc.createAnswer()
    await leg.pc.setLocalDescription(answer)
    if (legs.get(from) !== leg) return
    await api.post(`/api/calls/${id}/answer`, {
      sdp_answer: leg.pc.localDescription?.sdp ?? answer.sdp,
      to_user: from,
    })
    markReady(leg)
  } catch {
    closeLeg(from)
    maybeFinish(null)
  }
  refresh()
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
  const d = e.data as { call_id?: string, from_user?: string, signed_sdp?: SignedSdp }
  const sdp = d.signed_sdp?.sdp
  if (role === null || !d.call_id || typeof sdp !== 'string') return
  if (callId.value === null) {
    // Answer beat our own POST /api/calls response — hold it.
    if (role === 'caller') earlyAnswers.set(d.from_user ?? '', sdp)
    return
  }
  if (d.call_id !== callId.value) return
  void applyAnswer(d.from_user ?? '', sdp)
}

async function applyAnswer(from: string, sdp: string): Promise<void> {
  // An older household names no answerer: it can only be the one leg
  // still waiting for its answer.
  let leg = legs.get(from)
  if (!leg && from === '') {
    const waiting = [...legs.values()].filter(l => l.pc.signalingState === 'have-local-offer')
    if (waiting.length === 1) leg = waiting[0]
  }
  if (!leg || leg.pc.signalingState !== 'have-local-offer') return
  const conn = leg.pc
  try {
    await conn.setRemoteDescription({ type: 'answer', sdp })
    if (legs.get(leg.userId) !== leg) return
    if (leg.timer) { clearTimeout(leg.timer); leg.timer = null }
    if (leg.state === 'ringing') leg.state = 'connecting'
    anyAnswered = true
    drainRemoteIce()
    refresh()
  } catch (err) {
    if (legs.size === 1) {
      const id = callId.value
      teardown('failed', `Couldn't connect the call: ${(err as Error)?.message ?? err}`)
      if (id) void api.post(`/api/calls/${id}/hangup`, {}).catch(() => {})
      return
    }
    closeLeg(leg.userId)
    maybeFinish(null)
  }
}

function onRemoteLeave(e: WsEvent, reason: string): void {
  const d = e.data as { call_id?: string, by?: string, over?: boolean }
  if (!d.call_id || d.call_id !== callId.value) return
  if (!d.by || d.over) {
    teardown('ended', reason)
    return
  }
  pending.delete(d.by)
  closeLeg(d.by)
  if (!maybeFinish(reason)) refresh()
}

function legFailed(leg: Leg): void {
  const id = callId.value
  const reason = "Couldn't connect the call. The network between you may be "
    + 'blocking it — a TURN server may be needed.'
  closeLeg(leg.userId)
  if (legs.size === 0 && pending.size === 0) {
    teardown('failed', reason)
    if (id) void api.post(`/api/calls/${id}/hangup`, {}).catch(() => {})
    return
  }
  refresh()
}

/** Tear down once nobody is left to talk to. Returns ``true`` if it did. */
function maybeFinish(reason: string | null): boolean {
  if (role === null || legs.size > 0 || pending.size > 0) return false
  const id = callId.value
  teardown('ended', reason)
  if (id) void api.post(`/api/calls/${id}/hangup`, {}).catch(() => {})
  return true
}

function closeLeg(userId: string): void {
  const leg = legs.get(userId)
  if (!leg) return
  legs.delete(userId)
  if (leg.timer) clearTimeout(leg.timer)
  leg.pc.onicecandidate = null
  leg.pc.ontrack = null
  leg.pc.onconnectionstatechange = null
  leg.pc.close()
  publishPeers()
}

function markReady(leg: Leg): void {
  leg.ready = true
  flushOutboundIce(leg)
}

function drainRemoteIce(): void {
  const id = callId.value
  if (id === null) return
  for (const leg of legs.values()) {
    if (!leg.pc.remoteDescription) continue
    // A candidate without a sender comes from an older household, where
    // the call is 1:1.
    const mine = consumeIce(id, c => c.from_user === leg.userId
      || (c.from_user === undefined && legs.size === 1))
    for (const c of mine) {
      if (!c.candidate) continue
      leg.pc.addIceCandidate(c.candidate as RTCIceCandidateInit).catch(() => {
        /* a stale / malformed candidate must not kill the call */
      })
    }
  }
}

function drainPeerOffers(): void {
  const id = callId.value
  // Only once this device is in the call — while ringing they stay queued.
  if (id === null || role === null || localStream.value === null || !legs.size) return
  for (const o of consumePeerOffers(id)) void answerPeer(o.from_user, o.sdp)
}

function flushOutboundIce(leg: Leg): void {
  const id = callId.value
  if (id === null || !leg.ready) return
  const batch = leg.outboundIce
  leg.outboundIce = []
  for (const candidate of batch) {
    api.post(`/api/calls/${id}/ice`, { candidate, to_user: leg.userId })
      .catch(() => { /* best-effort */ })
  }
}

/** Recompute the call-level phase + the tiles from the legs. */
function refresh(): void {
  if (role === null) return
  const states = [...legs.values()].map(l => l.state)
  if (states.includes('connected')) callPhase.value = 'connected'
  else if (states.includes('reconnecting')) callPhase.value = 'reconnecting'
  else if (role === 'caller' && !anyAnswered) {
    if (callPhase.value !== 'starting') callPhase.value = 'ringing'
  } else callPhase.value = 'connecting'
  publishPeers()
}

function publishPeers(): void {
  callPeers.value = order
    .filter(uid => legs.has(uid) || pending.has(uid))
    .map(uid => {
      const leg = legs.get(uid)
      return {
        userId: uid,
        name: names.get(uid) ?? uid,
        state: leg?.state ?? 'ringing',
        stream: leg?.stream ?? null,
      }
    })
}

function teardown(phase: 'ended' | 'failed', reason: string | null): void {
  unsubs.forEach(u => u())
  unsubs = []
  role = null
  for (const uid of [...legs.keys()]) closeLeg(uid)
  pending = new Set()
  localStream.value?.getTracks().forEach(t => t.stop())
  localStream.value = null
  callPeers.value = []
  callEndReason.value = reason
  callPhase.value = phase
}

function asUserError(err: unknown): Error {
  return err instanceof Error ? err : new Error(String(err))
}
