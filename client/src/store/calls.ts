/**
 * Calls store — driven by `call.ringing`, `call.answered`,
 * `call.declined`, `call.ended`, `call.ice_candidate` WS frames (§26).
 *
 * CallsPage reads :data:`active` (in-progress + ringing) and
 * :data:`incoming` (current inbound offer). Updates land without
 * polling thanks to the WS subscription wired below.
 */
import { signal } from '@preact/signals'
import { ws } from '@/ws'

export interface ActiveCall {
  call_id:    string
  status:     'ringing' | 'in_progress' | 'ended'
  caller:     string
  callee:     string | null
  call_type:  'audio' | 'video'
  created_at: number
  conversation_id?: string
}

export interface IncomingCall {
  call_id:    string
  from_user:  string
  call_type:  'audio' | 'video'
  /** The caller's SDP offer, signed by the relaying household (§26.8). */
  signed_sdp?: { sdp: string, sdp_type: string, signature: string } | null
  conversation_id?: string
  /** Everyone invited, caller included (group calls; absent from older
   *  households, where the call is 1:1). */
  participants?: string[]
}

export interface IceCandidate {
  call_id:   string
  /** Sender — picks the mesh leg the candidate belongs to. */
  from_user?: string
  candidate: unknown
}

/** A mesh-leg offer from another participant of a group call
 *  (``call.peer_join``) — answered once this device is in the call. */
export interface PeerOffer {
  call_id: string
  from_user: string
  sdp: string
}

export const active = signal<ActiveCall[]>([])
export const incoming = signal<IncomingCall | null>(null)
export const pendingIce = signal<IceCandidate[]>([])
export const pendingPeerOffers = signal<PeerOffer[]>([])

function upsert(call: ActiveCall): void {
  const rest = active.value.filter((c) => c.call_id !== call.call_id)
  active.value = [...rest, call]
}

function drop(callId: string): void {
  active.value = active.value.filter((c) => c.call_id !== callId)
  if (incoming.value?.call_id === callId) incoming.value = null
  // Candidates / offers for a finished call can never be applied — don't
  // let them pile up on a device that never picked up.
  if (pendingIce.value.some((c) => c.call_id === callId)) {
    pendingIce.value = pendingIce.value.filter((c) => c.call_id !== callId)
  }
  if (pendingPeerOffers.value.some((o) => o.call_id === callId)) {
    pendingPeerOffers.value = pendingPeerOffers.value.filter((o) => o.call_id !== callId)
  }
}

/** ``call.ended`` / ``call.declined``. With ``by`` and not ``over`` it is
 *  one participant leaving a group call: the ring only stops when that
 *  was the caller (their invite is withdrawn); otherwise just forget that
 *  participant's leg offers. */
function onLeave(d: { call_id: string, by?: string, over?: boolean }): void {
  const ring = incoming.value?.call_id === d.call_id ? incoming.value : null
  if (!d.by || d.over || (ring !== null && ring.from_user === d.by)) {
    drop(d.call_id)
    return
  }
  const by = d.by
  if (pendingPeerOffers.value.some((o) => o.call_id === d.call_id && o.from_user === by)) {
    pendingPeerOffers.value = pendingPeerOffers.value.filter(
      (o) => !(o.call_id === d.call_id && o.from_user === by))
  }
}

export function wireCallsWs(): void {
  ws.on('call.ringing', (e) => {
    const d = e.data as unknown as {
      call_id: string
      from_user: string
      call_type?: 'audio' | 'video'
      signed_sdp?: IncomingCall['signed_sdp']
      conversation_id?: string
      participants?: string[]
    }
    if (!d?.call_id || !d?.from_user) return
    incoming.value = {
      call_id:   d.call_id,
      from_user: d.from_user,
      call_type: d.call_type ?? 'audio',
      signed_sdp: d.signed_sdp,
      conversation_id: d.conversation_id,
      participants: Array.isArray(d.participants) ? d.participants : undefined,
    }
    upsert({
      call_id:    d.call_id,
      status:     'ringing',
      caller:     d.from_user,
      callee:     null,
      call_type:  d.call_type ?? 'audio',
      created_at: Date.now(),
      conversation_id: d.conversation_id,
    })
  })
  ws.on('call.answered', (e) => {
    const d = e.data as unknown as { call_id: string }
    if (!d?.call_id) return
    const existing = active.value.find((c) => c.call_id === d.call_id)
    if (existing) upsert({ ...existing, status: 'in_progress' })
    if (incoming.value?.call_id === d.call_id) incoming.value = null
  })
  ws.on('call.declined', (e) => {
    const d = e.data as unknown as { call_id: string, by?: string, over?: boolean }
    if (!d?.call_id) return
    onLeave(d)
  })
  ws.on('call.ended', (e) => {
    const d = e.data as unknown as { call_id: string, by?: string, over?: boolean }
    if (!d?.call_id) return
    onLeave(d)
  })
  ws.on('call.peer_join', (e) => {
    const d = e.data as unknown as {
      call_id?: string, joiner_user_id?: string, signed_sdp?: { sdp?: unknown },
    }
    const sdp = d?.signed_sdp?.sdp
    if (!d?.call_id || !d.joiner_user_id || typeof sdp !== 'string') return
    // Kept until the call session answers it — the offer may land while
    // this device is still ringing.
    pendingPeerOffers.value = [
      ...pendingPeerOffers.value.filter(
        (o) => !(o.call_id === d.call_id && o.from_user === d.joiner_user_id)),
      { call_id: d.call_id, from_user: d.joiner_user_id, sdp },
    ]
  })
  ws.on('call.ice_candidate', (e) => {
    const d = e.data as unknown as IceCandidate
    if (!d?.call_id) return
    pendingIce.value = [...pendingIce.value, d]
  })
}

/** Take the queued candidates of *callId* that *accept* claims (all of
 *  them by default). */
export function consumeIce(
  callId: string,
  accept: (c: IceCandidate) => boolean = () => true,
): IceCandidate[] {
  const taken = pendingIce.value.filter((c) => c.call_id === callId && accept(c))
  // Only write when something was taken: callers drain from an effect
  // that watches ``pendingIce``, and an unconditional write would
  // re-trigger that effect forever.
  if (taken.length > 0) {
    pendingIce.value = pendingIce.value.filter((c) => !taken.includes(c))
  }
  return taken
}

/** Take the queued mesh-leg offers of *callId*. */
export function consumePeerOffers(callId: string): PeerOffer[] {
  const taken = pendingPeerOffers.value.filter((o) => o.call_id === callId)
  if (taken.length > 0) {
    pendingPeerOffers.value = pendingPeerOffers.value.filter((o) => o.call_id !== callId)
  }
  return taken
}
