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
}

export interface IceCandidate {
  call_id:   string
  candidate: unknown
}

export const active = signal<ActiveCall[]>([])
export const incoming = signal<IncomingCall | null>(null)
export const pendingIce = signal<IceCandidate[]>([])

function upsert(call: ActiveCall): void {
  const rest = active.value.filter((c) => c.call_id !== call.call_id)
  active.value = [...rest, call]
}

function drop(callId: string): void {
  active.value = active.value.filter((c) => c.call_id !== callId)
  if (incoming.value?.call_id === callId) incoming.value = null
  // Candidates for a finished call can never be applied — don't let
  // them pile up on a device that never picked up.
  if (pendingIce.value.some((c) => c.call_id === callId)) {
    pendingIce.value = pendingIce.value.filter((c) => c.call_id !== callId)
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
    }
    if (!d?.call_id || !d?.from_user) return
    incoming.value = {
      call_id:   d.call_id,
      from_user: d.from_user,
      call_type: d.call_type ?? 'audio',
      signed_sdp: d.signed_sdp,
      conversation_id: d.conversation_id,
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
    const d = e.data as unknown as { call_id: string }
    if (!d?.call_id) return
    drop(d.call_id)
  })
  ws.on('call.ended', (e) => {
    const d = e.data as unknown as { call_id: string }
    if (!d?.call_id) return
    drop(d.call_id)
  })
  ws.on('call.ice_candidate', (e) => {
    const d = e.data as unknown as IceCandidate
    if (!d?.call_id) return
    pendingIce.value = [...pendingIce.value, d]
  })
}

export function consumeIce(callId: string): IceCandidate[] {
  const taken = pendingIce.value.filter((c) => c.call_id === callId)
  // Only write when something was taken: callers drain from an effect
  // that watches ``pendingIce``, and an unconditional write would
  // re-trigger that effect forever.
  if (taken.length > 0) {
    pendingIce.value = pendingIce.value.filter((c) => c.call_id !== callId)
  }
  return taken
}
