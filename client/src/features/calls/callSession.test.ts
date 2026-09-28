/**
 * callSession — the browser side of the §26 SDP / trickle-ICE handshake.
 *
 * ``RTCPeerConnection`` and ``getUserMedia`` are scripted fakes that write
 * every call into one ordered ``log``; the REST client and the WS manager
 * are mocked at the module boundary, while the real ``store/calls`` queues
 * incoming ICE exactly as it does in the app.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// ─── module-boundary mocks ──────────────────────────────────────────────

type Frame = Record<string, unknown>
const wsHandlers = new Map<string, Set<(e: { type: string, data: Frame }) => void>>()
function emit(type: string, data: Frame): void {
  wsHandlers.get(type)?.forEach(h => h({ type, data: { type, ...data } }))
}
vi.mock('@/ws', () => ({
  ws: {
    on(type: string, h: (e: { type: string, data: Frame }) => void) {
      if (!wsHandlers.has(type)) wsHandlers.set(type, new Set())
      wsHandlers.get(type)!.add(h)
      return () => { wsHandlers.get(type)?.delete(h) }
    },
  },
}))

const apiGet = vi.fn()
const apiPost = vi.fn()
// Our own user id decides who offers on a callee ↔ callee mesh leg.
const auth = vi.hoisted(() => ({ me: 'uid-me' }))
vi.mock('@/store/auth', () => ({
  currentUser: { get value() { return { user_id: auth.me } } },
}))
vi.mock('@/api', () => ({
  api: {
    get: (...a: unknown[]) => apiGet(...a),
    post: (...a: unknown[]) => apiPost(...a),
  },
}))

// ─── WebRTC / media fakes ───────────────────────────────────────────────

let log: string[] = []
const pcs: FakePC[] = []

class FakeTrack {
  enabled = true
  stopped = false
  constructor(public kind: 'audio' | 'video') {}
  stop() { this.stopped = true }
}
class FakeStream {
  constructor(public tracks: FakeTrack[]) {}
  getTracks() { return this.tracks }
  getAudioTracks() { return this.tracks.filter(t => t.kind === 'audio') }
  getVideoTracks() { return this.tracks.filter(t => t.kind === 'video') }
}

let pcSeq = 0
class FakePC {
  /** Numbered so each leg's offer / answer is told apart in the log. */
  n = ++pcSeq
  signalingState: RTCSignalingState = 'stable'
  connectionState: RTCPeerConnectionState = 'new'
  localDescription: RTCSessionDescriptionInit | null = null
  remoteDescription: RTCSessionDescriptionInit | null = null
  onicecandidate: ((e: { candidate: unknown }) => void) | null = null
  ontrack: ((e: unknown) => void) | null = null
  onconnectionstatechange: (() => void) | null = null
  /** Candidate the fake "gathers" as soon as a local description is set. */
  static gather: RTCIceCandidateInit | null = { candidate: 'local-1', sdpMid: '0' }
  constructor(public config: RTCConfiguration) {
    log.push(`new PC ${JSON.stringify(config.iceServers)}`)
    pcs.push(this)
  }
  addTrack(t: FakeTrack) { log.push(`addTrack ${t.kind}`) }
  async createOffer() {
    log.push('createOffer')
    return { type: 'offer', sdp: this.n === 1 ? 'OFFER-SDP' : `OFFER-SDP-${this.n}` }
  }
  async createAnswer() {
    log.push('createAnswer')
    return { type: 'answer', sdp: this.n === 1 ? 'ANSWER-SDP' : `ANSWER-SDP-${this.n}` }
  }
  async setLocalDescription(d: RTCSessionDescriptionInit) {
    log.push(`setLocal ${d.type}`)
    this.localDescription = d
    this.signalingState = d.type === 'offer' ? 'have-local-offer' : 'stable'
    const c = FakePC.gather
    if (c) this.onicecandidate?.({ candidate: { toJSON: () => c } })
  }
  async setRemoteDescription(d: RTCSessionDescriptionInit) {
    log.push(`setRemote ${d.type} ${d.sdp}`)
    this.remoteDescription = d
    this.signalingState = d.type === 'offer' ? 'have-remote-offer' : 'stable'
  }
  async addIceCandidate(c: RTCIceCandidateInit) { log.push(`addIce ${c.candidate}`) }
  close() { log.push('close'); this.connectionState = 'closed' }
  setConnection(s: RTCPeerConnectionState) {
    this.connectionState = s
    this.onconnectionstatechange?.()
  }
}

let getUserMedia: ReturnType<typeof vi.fn>
let lastStream: FakeStream | null = null

function mediaOk(kinds: Array<'audio' | 'video'>) {
  return async (c: MediaStreamConstraints) => {
    log.push(`getUserMedia ${c.video ? 'audio+video' : 'audio'}`)
    // Like the browser: only the requested kinds come back.
    lastStream = new FakeStream(
      kinds.filter(k => k === 'audio' || c.video).map(k => new FakeTrack(k)))
    return lastStream
  }
}

const flush = () => new Promise(r => setTimeout(r, 0))

interface Member { user_id: string, display_name?: string, is_self?: boolean }
const SELF: Member = { user_id: 'uid-me', display_name: 'Me', is_self: true }
let members: Member[] = []

type Session = typeof import('./callSession')
type Store = typeof import('@/store/calls')
let S: Session
let store: Store
let CallEmbedBlockedError: typeof import('./embedPolicy').CallEmbedBlockedError

beforeEach(async () => {
  vi.resetModules()
  wsHandlers.clear()
  log = []
  pcs.length = 0
  pcSeq = 0
  auth.me = 'uid-me'
  members = [SELF, { user_id: 'uid-bob', display_name: 'Bob' }]
  lastStream = null
  FakePC.gather = { candidate: 'local-1', sdpMid: '0' }
  apiGet.mockReset().mockImplementation(async (path: string) => {
    log.push(`GET ${path}`)
    if (path.endsWith('/members')) return members
    return { ice_servers: [{ urls: ['turn:turn.example'], username: 'u', credential: 'c' }] }
  })
  apiPost.mockReset().mockImplementation(async (path: string, body: Frame) => {
    log.push(`POST ${path} ${JSON.stringify(body)}`)
    if (path === '/api/calls') return { call_id: 'call-1', status: 'ringing' }
    return {}
  })
  getUserMedia = vi.fn(mediaOk(['audio', 'video']))
  vi.stubGlobal('RTCPeerConnection', FakePC)
  vi.stubGlobal('navigator', { mediaDevices: { getUserMedia } })
  store = await import('@/store/calls')
  store.wireCallsWs()
  S = await import('./callSession')
  CallEmbedBlockedError = (await import('./embedPolicy')).CallEmbedBlockedError
})

afterEach(() => {
  vi.unstubAllGlobals()
})

const TURN = '[{"urls":["turn:turn.example"],"username":"u","credential":"c"}]'

// ─── caller ─────────────────────────────────────────────────────────────

describe('startCall (caller)', () => {
  it('offers after local media, posts the real SDP, then trickles ICE', async () => {
    const id = await S.startCall('conv-1', 'video')
    expect(id).toBe('call-1')
    expect(log).toEqual([
      'GET /api/calls/ice-servers',
      'getUserMedia audio+video',
      'GET /api/conversations/conv-1/members',
      `new PC ${TURN}`,
      'addTrack audio',
      'addTrack video',
      'createOffer',
      'setLocal offer',
      // 1:1 keeps the single-offer body.
      'POST /api/calls {"conversation_id":"conv-1","call_type":"video","sdp_offer":"OFFER-SDP"}',
      // Gathered before call_id existed → held, then flushed to the leg.
      'POST /api/calls/call-1/ice {"candidate":{"candidate":"local-1","sdpMid":"0"},"to_user":"uid-bob"}',
    ])
    expect(S.callPhase.value).toBe('ringing')
    expect(S.callId.value).toBe('call-1')
    expect(S.localStream.value).toBe(lastStream)
  })

  it('queues remote ICE until the answer lands, then applies it in order', async () => {
    await S.startCall('conv-1', 'audio')
    log = []
    emit('call.ice_candidate', { call_id: 'call-1', candidate: { candidate: 'remote-1' } })
    await flush()
    expect(log).toEqual([])  // no remote description yet → held

    emit('call.answered', {
      call_id: 'call-1',
      signed_sdp: { sdp: 'ANSWER-SDP', sdp_type: 'answer', signature: 'sig' },
    })
    await flush()
    emit('call.ice_candidate', { call_id: 'call-1', candidate: { candidate: 'remote-2' } })
    await flush()
    expect(log).toEqual([
      'setRemote answer ANSWER-SDP',
      'addIce remote-1',
      'addIce remote-2',
    ])
    expect(store.pendingIce.value).toEqual([])
    expect(S.callPhase.value).toBe('connecting')

    pcs[0].setConnection('connected')
    expect(S.callPhase.value).toBe('connected')
  })

  it('ignores answers and candidates for other calls', async () => {
    await S.startCall('conv-1', 'audio')
    log = []
    emit('call.answered', { call_id: 'call-other', signed_sdp: { sdp: 'X' } })
    await flush()
    expect(log).toEqual([])
    expect(pcs[0].signalingState).toBe('have-local-offer')
  })

  it('applies an answer that raced ahead of the POST /api/calls response', async () => {
    let release!: () => void
    apiPost.mockImplementation(async (path: string, body: Frame) => {
      log.push(`POST ${path} ${JSON.stringify(body)}`)
      if (path === '/api/calls') {
        await new Promise<void>(r => { release = r })
        return { call_id: 'call-1' }
      }
      return {}
    })
    const started = S.startCall('conv-1', 'audio')
    await flush(); await flush()
    emit('call.answered', { call_id: 'call-1', signed_sdp: { sdp: 'EARLY-ANSWER' } })
    release()
    await started
    await flush()
    expect(log).toContain('setRemote answer EARLY-ANSWER')
  })

  it('audio calls ask for the microphone only — the camera is never opened', async () => {
    await S.startCall('conv-1', 'audio')
    expect(getUserMedia).toHaveBeenCalledTimes(1)
    expect(getUserMedia).toHaveBeenCalledWith({ audio: true, video: false })
    expect(log).toContain('addTrack audio')
    expect(log).not.toContain('addTrack video')
    expect(S.hasCamera.value).toBe(false)
  })

  it('video calls ask for the microphone and the camera', async () => {
    await S.startCall('conv-1', 'video')
    expect(getUserMedia).toHaveBeenCalledTimes(1)
    expect(getUserMedia).toHaveBeenCalledWith({ audio: true, video: true })
    expect(S.hasCamera.value).toBe(true)
  })

  it('explains an embed that denies the microphone instead of prompting', async () => {
    // A cross-origin iframe with allow="fullscreen" (HA Webpage dashboard):
    // Chromium reports the policy, and getUserMedia is never tried.
    vi.stubGlobal('top', {})
    Object.defineProperty(document, 'permissionsPolicy', {
      configurable: true,
      value: { allowsFeature: (f: string) => f !== 'microphone' && f !== 'camera' },
    })
    try {
      const err = await S.startCall('conv-1', 'audio').catch(e => e)
      expect(err).toBeInstanceOf(CallEmbedBlockedError)
      expect(err.certain).toBe(true)
      expect(err.message).toMatch(/its own tab/)
      expect(getUserMedia).not.toHaveBeenCalled()
      expect(apiPost).not.toHaveBeenCalled()
      expect(S.isCallLive()).toBe(false)
    } finally {
      delete (document as { permissionsPolicy?: unknown }).permissionsPolicy
    }
  })

  it('a framed NotAllowedError without a policy API reads as a possible embed denial', async () => {
    vi.stubGlobal('top', {})
    getUserMedia.mockRejectedValue(Object.assign(new Error('denied'), { name: 'NotAllowedError' }))
    const err = await S.startCall('conv-1', 'audio').catch(e => e)
    expect(err).toBeInstanceOf(CallEmbedBlockedError)
    expect(err.certain).toBe(false)
  })

  it('an unframed page never reports an embed denial', async () => {
    Object.defineProperty(document, 'permissionsPolicy', {
      configurable: true,
      value: { allowsFeature: () => false },
    })
    try {
      getUserMedia.mockRejectedValue(Object.assign(new Error('denied'), { name: 'NotAllowedError' }))
      const err = await S.startCall('conv-1', 'audio').catch(e => e)
      expect(err).not.toBeInstanceOf(CallEmbedBlockedError)
      expect(err.message).toMatch(/Microphone access is blocked/)
    } finally {
      delete (document as { permissionsPolicy?: unknown }).permissionsPolicy
    }
  })

  it('falls back to audio-only when there is no usable camera', async () => {
    getUserMedia.mockImplementationOnce(async () => {
      log.push('getUserMedia audio+video')
      throw Object.assign(new Error('no cam'), { name: 'NotFoundError' })
    }).mockImplementationOnce(mediaOk(['audio']))
    await S.startCall('conv-1', 'video')
    expect(log.slice(0, 5)).toEqual([
      'GET /api/calls/ice-servers',
      'getUserMedia audio+video',
      'GET /api/conversations/conv-1/members',
      'getUserMedia audio',
      `new PC ${TURN}`,
    ])
    expect(S.hasCamera.value).toBe(false)
  })

  it('rejects with a readable error and posts nothing when the mic is blocked', async () => {
    getUserMedia.mockRejectedValue(Object.assign(new Error('denied'), { name: 'NotAllowedError' }))
    await expect(S.startCall('conv-1', 'audio')).rejects.toThrow(/Microphone access is blocked/)
    expect(apiPost).not.toHaveBeenCalled()
    expect(pcs).toHaveLength(0)
    expect(S.isCallLive()).toBe(false)
    // A fresh attempt is possible straight away.
    getUserMedia.mockImplementation(mediaOk(['audio']))
    await expect(S.startCall('conv-1', 'audio')).resolves.toBe('call-1')
  })

  it('explains that calls need HTTPS when getUserMedia is unavailable', async () => {
    vi.stubGlobal('navigator', {})
    await expect(S.startCall('conv-1', 'audio')).rejects.toThrow(/secure \(HTTPS\)/)
    expect(S.isCallLive()).toBe(false)
  })

  it('tears down when POST /api/calls fails', async () => {
    apiPost.mockRejectedValueOnce(new Error('403 forbidden'))
    await expect(S.startCall('conv-1', 'audio')).rejects.toThrow('403 forbidden')
    expect(pcs[0].connectionState).toBe('closed')
    expect(lastStream!.getTracks().every(t => t.stopped)).toBe(true)
    expect(S.isCallLive()).toBe(false)
  })

  it('refuses to start a second call while one is live', async () => {
    await S.startCall('conv-1', 'audio')
    await expect(S.startCall('conv-2', 'audio')).rejects.toThrow(/already in a call/)
    expect(pcs).toHaveLength(1)
  })
})

// ─── callee ─────────────────────────────────────────────────────────────

describe('acceptCall (callee)', () => {
  const ringing = {
    call_id: 'call-9',
    from_user: 'uid-alice',
    call_type: 'video' as const,
    conversation_id: 'conv-1',
    signed_sdp: { sdp: 'OFFER-SDP', sdp_type: 'offer', signature: 'sig' },
  }

  it('answers the ringing offer, applying ICE that arrived while ringing', async () => {
    // Caller trickles while we are still ringing — store/calls queues it.
    emit('call.ringing', { ...ringing })
    emit('call.ice_candidate', { call_id: 'call-9', candidate: { candidate: 'remote-1' } })
    expect(store.pendingIce.value).toHaveLength(1)

    await S.acceptCall(store.incoming.value!)
    expect(log).toEqual([
      // Names for the call page — fetched alongside, never waited on.
      'GET /api/conversations/conv-1/members',
      'GET /api/calls/ice-servers',
      'getUserMedia audio+video',
      `new PC ${TURN}`,
      'addTrack audio',
      'addTrack video',
      'setRemote offer OFFER-SDP',
      'addIce remote-1',
      'createAnswer',
      'setLocal answer',
      'POST /api/calls/call-9/answer {"sdp_answer":"ANSWER-SDP"}',
      // Our own candidates only go out once the answer is posted.
      'POST /api/calls/call-9/ice {"candidate":{"candidate":"local-1","sdpMid":"0"},"to_user":"uid-alice"}',
    ])
    expect(store.pendingIce.value).toEqual([])
    expect(S.callPhase.value).toBe('connecting')
    expect(S.callConversation.value).toBe('conv-1')

    log = []
    emit('call.ice_candidate', { call_id: 'call-9', candidate: { candidate: 'remote-2' } })
    await flush()
    expect(log).toEqual(['addIce remote-2'])
  })

  it('rejects an offer without SDP and touches nothing', async () => {
    await expect(S.acceptCall({ ...ringing, signed_sdp: null })).rejects.toThrow(/no connection offer/)
    expect(getUserMedia).not.toHaveBeenCalled()
    expect(apiPost).not.toHaveBeenCalled()
    expect(S.isCallLive()).toBe(false)
  })

  it('surfaces an unusable offer and releases the media', async () => {
    FakePC.prototype.setRemoteDescription = vi.fn(async () => { throw new Error('Invalid SDP') })
    try {
      await expect(S.acceptCall(ringing)).rejects.toThrow('Invalid SDP')
    } finally {
      FakePC.prototype.setRemoteDescription = async function (this: FakePC, d) {
        log.push(`setRemote ${d.type} ${d.sdp}`)
        this.remoteDescription = d
      }
    }
    expect(lastStream!.getTracks().every(t => t.stopped)).toBe(true)
    expect(apiPost).not.toHaveBeenCalled()
    expect(S.isCallLive()).toBe(false)
  })
})

// ─── ending ─────────────────────────────────────────────────────────────

describe('ending a call', () => {
  it('hangupCall posts hangup and releases the peer connection + media', async () => {
    await S.startCall('conv-1', 'video')
    log = []
    await S.hangupCall()
    expect(log).toEqual(['close', 'POST /api/calls/call-1/hangup {}'])
    expect(lastStream!.getTracks().every(t => t.stopped)).toBe(true)
    expect(S.callPhase.value).toBe('ended')
    expect(S.localStream.value).toBeNull()
    // WS subscriptions are gone — late frames can't resurrect the call.
    emit('call.answered', { call_id: 'call-1', signed_sdp: { sdp: 'LATE' } })
    await flush()
    expect(log).toEqual(['close', 'POST /api/calls/call-1/hangup {}'])
  })

  it('the other side hanging up ends the call with a reason', async () => {
    await S.startCall('conv-1', 'audio')
    emit('call.ended', { call_id: 'call-1' })
    expect(pcs[0].connectionState).toBe('closed')
    expect(S.callPhase.value).toBe('ended')
    expect(S.callEndReason.value).toBe('The call ended.')
    S.resetCall()
    expect(S.callPhase.value).toBe('idle')
    expect(S.callId.value).toBeNull()
  })

  it('a declined call ends with its own reason', async () => {
    await S.startCall('conv-1', 'audio')
    emit('call.declined', { call_id: 'call-1', by: 'uid-bob' })
    expect(S.callPhase.value).toBe('ended')
    expect(S.callEndReason.value).toBe('The call was declined.')
  })

  it('a failed ICE / DTLS connection fails the call and tells the backend', async () => {
    await S.startCall('conv-1', 'audio')
    log = []
    pcs[0].setConnection('failed')
    await flush()
    expect(S.callPhase.value).toBe('failed')
    expect(S.callEndReason.value).toMatch(/TURN server/)
    expect(log).toEqual(['close', 'POST /api/calls/call-1/hangup {}'])
  })

  it('a dropped connection shows as reconnecting, then recovers', async () => {
    await S.startCall('conv-1', 'audio')
    pcs[0].setConnection('connected')
    pcs[0].setConnection('disconnected')
    expect(S.callPhase.value).toBe('reconnecting')
    pcs[0].setConnection('connected')
    expect(S.callPhase.value).toBe('connected')
  })
})

describe('store/calls', () => {
  it('drops queued candidates of a call that ended on this device', () => {
    emit('call.ice_candidate', { call_id: 'call-x', candidate: { candidate: 'c' } })
    emit('call.ended', { call_id: 'call-x' })
    expect(store.pendingIce.value).toEqual([])
  })

  it('consumeIce does not rewrite the queue when nothing matches', () => {
    emit('call.ice_candidate', { call_id: 'call-x', candidate: { candidate: 'c' } })
    const before = store.pendingIce.value
    expect(store.consumeIce('call-y')).toEqual([])
    expect(store.pendingIce.value).toBe(before)
  })
})

// ─── group calls: full mesh ─────────────────────────────────────────────

describe('group calls (mesh)', () => {
  const BOB = { user_id: 'uid-bob', display_name: 'Bob' }
  const CAROL = { user_id: 'uid-carol', display_name: 'Carol' }
  const answered = (from: string, sdp: string, call = 'call-1') =>
    emit('call.answered', { call_id: call, from_user: from, signed_sdp: { sdp } })

  it('the caller opens one leg per callee and posts one offer each', async () => {
    members = [SELF, BOB, CAROL]
    await S.startCall('conv-g', 'video')
    // One camera/mic for the whole call, sent on every leg.
    expect(getUserMedia).toHaveBeenCalledTimes(1)
    expect(pcs).toHaveLength(2)
    expect(log.filter(l => l.startsWith('addTrack'))).toHaveLength(4)
    expect(log).toContain('POST /api/calls {"conversation_id":"conv-g","call_type":"video",'
      + '"sdp_offers":{"uid-bob":"OFFER-SDP","uid-carol":"OFFER-SDP-2"}}')
    // Each leg's held candidates go to its own participant.
    expect(log).toContain('POST /api/calls/call-1/ice {"candidate":{"candidate":"local-1","sdpMid":"0"},"to_user":"uid-bob"}')
    expect(log).toContain('POST /api/calls/call-1/ice {"candidate":{"candidate":"local-1","sdpMid":"0"},"to_user":"uid-carol"}')
    expect(S.callPeers.value.map(p => [p.name, p.state])).toEqual([
      ['Bob', 'ringing'], ['Carol', 'ringing'],
    ])
    expect(S.callPhase.value).toBe('ringing')
  })

  it('routes each answer and each remote candidate to its own leg', async () => {
    members = [SELF, BOB, CAROL]
    await S.startCall('conv-g', 'video')
    const [bob, carol] = pcs
    log = []
    // Carol's candidate arrives first but waits for Carol's answer.
    emit('call.ice_candidate', { call_id: 'call-1', from_user: 'uid-carol', candidate: { candidate: 'c-1' } })
    answered('uid-bob', 'ANS-BOB')
    await flush()
    expect(bob.remoteDescription?.sdp).toBe('ANS-BOB')
    expect(carol.remoteDescription).toBeNull()
    expect(log).toEqual(['setRemote answer ANS-BOB'])
    answered('uid-carol', 'ANS-CAROL')
    await flush()
    expect(log.slice(1)).toEqual(['setRemote answer ANS-CAROL', 'addIce c-1'])
    expect(S.callPhase.value).toBe('connecting')
    bob.setConnection('connected')
    expect(S.callPhase.value).toBe('connected')
    expect(S.callPeers.value.map(p => p.state)).toEqual(['connected', 'connecting'])
  })

  it('refuses a group bigger than the mesh cap before touching the network', async () => {
    members = [SELF, ...Array.from({ length: S.MAX_CALL_PARTICIPANTS }, (_, i) => ({ user_id: `uid-${i}` }))]
    await expect(S.startCall('conv-big', 'audio')).rejects.toThrow(/limited to 6 people/)
    expect(apiPost).not.toHaveBeenCalled()
    expect(S.isCallLive()).toBe(false)
  })

  it('one callee leaving closes their leg only; "over" ends the call', async () => {
    members = [SELF, BOB, CAROL]
    await S.startCall('conv-g', 'video')
    emit('call.declined', { call_id: 'call-1', by: 'uid-bob', over: false })
    expect(pcs[0].connectionState).toBe('closed')
    expect(pcs[1].connectionState).toBe('new')
    expect(S.isCallLive()).toBe(true)
    expect(S.callPeers.value.map(p => p.name)).toEqual(['Carol'])
    emit('call.declined', { call_id: 'call-1', by: 'uid-carol', over: true })
    expect(S.callPhase.value).toBe('ended')
    expect(S.callEndReason.value).toBe('The call was declined.')
  })

  it('a callee opens its legs to the higher-id callees after answering the caller', async () => {
    auth.me = 'uid-b'
    emit('call.ringing', {
      call_id: 'call-9', from_user: 'uid-a', call_type: 'audio', conversation_id: 'conv-g',
      participants: ['uid-a', 'uid-b', 'uid-c'],
      signed_sdp: { sdp: 'OFFER-FROM-A', sdp_type: 'offer', signature: 's' },
    })
    await S.acceptCall(store.incoming.value!)
    await flush()
    const posts = log.filter(l => l.startsWith('POST'))
    expect(posts).toEqual([
      'POST /api/calls/call-9/answer {"sdp_answer":"ANSWER-SDP"}',
      'POST /api/calls/call-9/ice {"candidate":{"candidate":"local-1","sdpMid":"0"},"to_user":"uid-a"}',
      'POST /api/calls/call-9/join {"sdp_offers":{"uid-c":"OFFER-SDP-2"}}',
      'POST /api/calls/call-9/ice {"candidate":{"candidate":"local-1","sdpMid":"0"},"to_user":"uid-c"}',
    ])
    // Carol's answer to that leg lands on it — not on the caller's.
    answered('uid-c', 'ANS-C', 'call-9')
    await flush()
    expect(pcs[1].remoteDescription?.sdp).toBe('ANS-C')
    expect(pcs[0].remoteDescription?.sdp).toBe('OFFER-FROM-A')
  })

  it('a higher-id callee answers the leg offer that arrived while it was ringing', async () => {
    auth.me = 'uid-c'
    emit('call.ringing', {
      call_id: 'call-9', from_user: 'uid-a', call_type: 'audio', conversation_id: 'conv-g',
      participants: ['uid-a', 'uid-b', 'uid-c'],
      signed_sdp: { sdp: 'OFFER-FROM-A', sdp_type: 'offer', signature: 's' },
    })
    // uid-b accepted first and offered us a leg — we are still ringing.
    emit('call.peer_join', {
      call_id: 'call-9', joiner_user_id: 'uid-b',
      signed_sdp: { sdp: 'OFFER-FROM-B', sdp_type: 'offer', signature: 's' },
    })
    emit('call.ice_candidate', { call_id: 'call-9', from_user: 'uid-b', candidate: { candidate: 'b-1' } })
    await flush()
    expect(pcs).toHaveLength(0)
    await S.acceptCall(store.incoming.value!)
    await flush(); await flush()
    expect(pcs).toHaveLength(2)
    expect(pcs[1].remoteDescription?.sdp).toBe('OFFER-FROM-B')
    expect(log).toContain('addIce b-1')
    expect(log).toContain('POST /api/calls/call-9/answer {"sdp_answer":"ANSWER-SDP-2","to_user":"uid-b"}')
    // We never offer to a lower id.
    expect(log.some(l => l.includes('/join'))).toBe(false)
    expect(store.pendingPeerOffers.value).toEqual([])
  })

  it('an unanswered leg is dropped after the ringing TTL and a lone call ends', async () => {
    vi.useFakeTimers()
    try {
      await S.startCall('conv-1', 'audio')
      log = []
      vi.advanceTimersByTime(S.RING_TIMEOUT_MS)
      expect(S.callPhase.value).toBe('ended')
      expect(S.callEndReason.value).toBe('Nobody answered.')
      expect(log).toContain('POST /api/calls/call-1/hangup {}')
    } finally {
      vi.useRealTimers()
    }
  })

  it('one failed leg in a group keeps the call; the last one fails it', async () => {
    members = [SELF, BOB, CAROL]
    await S.startCall('conv-g', 'video')
    pcs[0].setConnection('failed')
    expect(S.isCallLive()).toBe(true)
    log = []
    pcs[1].setConnection('failed')
    await flush()
    expect(S.callPhase.value).toBe('failed')
    expect(log).toContain('POST /api/calls/call-1/hangup {}')
  })
})

describe('store/calls — group ringing', () => {
  const ring = {
    call_id: 'call-9', from_user: 'uid-a', call_type: 'audio',
    participants: ['uid-a', 'uid-b', 'uid-c'],
    signed_sdp: { sdp: 'O', sdp_type: 'offer', signature: 's' },
  }

  it('another callee declining does not stop our ring', () => {
    emit('call.ringing', ring)
    emit('call.declined', { call_id: 'call-9', by: 'uid-b', over: false })
    expect(store.incoming.value?.call_id).toBe('call-9')
    expect(store.incoming.value?.participants).toEqual(['uid-a', 'uid-b', 'uid-c'])
  })

  it('the caller leaving withdraws the invite', () => {
    emit('call.ringing', ring)
    emit('call.ended', { call_id: 'call-9', by: 'uid-a', over: false })
    expect(store.incoming.value).toBeNull()
  })

  it('a leaver\'s queued leg offer is forgotten', () => {
    emit('call.peer_join', { call_id: 'call-9', joiner_user_id: 'uid-b', signed_sdp: { sdp: 'X' } })
    expect(store.pendingPeerOffers.value).toHaveLength(1)
    emit('call.ended', { call_id: 'call-9', by: 'uid-b', over: false })
    expect(store.pendingPeerOffers.value).toEqual([])
  })
})
