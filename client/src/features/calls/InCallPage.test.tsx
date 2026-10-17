import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor, act, cleanup } from '@testing-library/preact'

const apiPost = vi.fn().mockResolvedValue({})
vi.mock('@/api', () => ({
  api: { get: vi.fn(), post: (...a: unknown[]) => apiPost(...a) },
}))
const showToast = vi.fn()
vi.mock('@/components/Toast', () => ({
  showToast: (...a: unknown[]) => showToast(...a),
}))
const route = vi.fn()
// Every test gets its own call id. A page left over from the previous
// test may run its unmount cleanup late (Preact 11 runs effect cleanups
// after paint): with a shared id it would see the *next* test's live
// session as its own and hang it up. A fresh id makes the page's own
// ``sessionCallId === callId`` guard keep it off the new session.
const ids = vi.hoisted(() => ({ n: 0, callId: 'call-0' }))
vi.mock('preact-iso', () => ({
  useRoute: () => ({ params: { callId: ids.callId } }),
  useLocation: () => ({ route, url: `/calls/${ids.callId}` }),
}))

// A controllable stand-in for the session: the page only renders it.
const s = vi.hoisted(() => ({
  live: false,
  hangupCall: null as unknown as ReturnType<typeof vi.fn<() => Promise<void>>>,
  resetCall: null as unknown as ReturnType<typeof vi.fn<() => void>>,
}))
vi.mock('./callSession', async () => {
  const { signal } = await import('@preact/signals')
  const callPhase = signal<string>('idle')
  const callId = signal<string | null>(null)
  s.hangupCall = vi.fn(async () => { s.live = false; callPhase.value = 'ended' })
  s.resetCall = vi.fn(() => { callPhase.value = 'idle'; callId.value = null })
  return {
    callPhase, callId,
    callConversation: signal<string | null>('conv-1'),
    callEndReason: signal<string | null>(null),
    localStream: signal<MediaStream | null>(null),
    callPeers: signal<Array<{ userId: string, name: string, state: string, stream: null }>>([]),
    hasCamera: signal<boolean>(true),
    callType: signal<'audio' | 'video'>('video'),
    getPeerConnection: () => null,
    hangupCall: () => s.hangupCall(),
    resetCall: () => s.resetCall(),
    isCallLive: () => s.live,
  }
})

import InCallPage from './InCallPage'
import {
  callConversation, callEndReason, callId, callPeers, callPhase, callType, hasCamera,
  localStream, type CallPhase,
} from './callSession'

/** Put every mocked session signal back to its initial value. */
function resetSession() {
  callEndReason.value = null
  callPhase.value = 'idle'
  callId.value = null
  callConversation.value = 'conv-1'
  localStream.value = null
  callPeers.value = [{ userId: 'uid-bob', name: 'Bob', state: 'connecting', stream: null }]
  hasCamera.value = true
  callType.value = 'video'
  s.live = false
}

function session(phase: CallPhase) {
  callId.value = ids.callId
  callConversation.value = 'conv-1'
  callPhase.value = phase
  s.live = !['idle', 'ended', 'failed'].includes(phase)
}

describe('InCallPage', () => {
  beforeEach(() => {
    ids.n += 1
    ids.callId = `call-${ids.n}`
    route.mockReset()
    apiPost.mockClear()
    showToast.mockReset()
    s.hangupCall.mockClear()
    s.resetCall.mockClear()
    resetSession()
  })

  afterEach(() => {
    // A failed assertion mid-test must not leave a live session behind.
    resetSession()
  })

  it('says the call is not connected on this device when there is no session', () => {
    const { getByText } = render(<InCallPage />)
    expect(getByText('Call not connected')).toBeTruthy()
    fireEvent.click(getByText('Back to chats'))
    expect(route).toHaveBeenCalledWith('/dms')
  })

  it('"Back to chats" after a reload hangs the call up on the backend', () => {
    // No live session on this device (reload / stale link), but the call
    // may still ring or wait on the other side — leaving must end it.
    const { getByText } = render(<InCallPage />)
    fireEvent.click(getByText('Back to chats'))
    expect(apiPost).toHaveBeenCalledWith(`/api/calls/${ids.callId}/hangup`, {})
    expect(apiPost).toHaveBeenCalledTimes(1)
  })

  it('"Back to chats" from a failed call does not post a second hangup', () => {
    // The session already posted the hangup when ICE failed.
    session('failed')
    const { getByText } = render(<InCallPage />)
    fireEvent.click(getByText('Back to chats'))
    expect(apiPost).not.toHaveBeenCalled()
    expect(route).toHaveBeenCalledWith('/dms/conv-1')
  })

  it('shows "Calling…" while the outbound offer rings', () => {
    session('ringing')
    const { getByRole, queryByLabelText } = render(<InCallPage />)
    expect(getByRole('status').textContent).toBe('Calling…')
    // No duration / quality chip until media flows.
    expect(queryByLabelText('Connection quality')).toBeNull()
  })

  it('drops the status line and shows the quality chip once connected', () => {
    session('connected')
    const { queryByRole, getByLabelText } = render(<InCallPage />)
    expect(queryByRole('status')).toBeNull()
    expect(getByLabelText('Connection quality')).toBeTruthy()
  })

  it('Hang up ends the session and returns to the conversation', async () => {
    session('connected')
    const { getByLabelText } = render(<InCallPage />)
    fireEvent.click(getByLabelText('Hang up'))
    await new Promise(r => setTimeout(r, 0))
    expect(s.hangupCall).toHaveBeenCalledTimes(1)
    expect(route).toHaveBeenCalledTimes(1)
    expect(route).toHaveBeenCalledWith('/dms/conv-1')
  })

  it('the other side ending the call toasts the reason and leaves', async () => {
    session('connected')
    render(<InCallPage />)
    callEndReason.value = 'The call was declined.'
    callPhase.value = 'ended'
    await waitFor(() => expect(showToast).toHaveBeenCalledWith('The call was declined.', 'info'))
    expect(route).toHaveBeenCalledWith('/dms/conv-1')
  })

  it('a failed call stays on screen with the reason', () => {
    session('failed')
    callEndReason.value = "Couldn't connect the call."
    const { getByText } = render(<InCallPage />)
    expect(getByText('Call failed')).toBeTruthy()
    expect(getByText("Couldn't connect the call.")).toBeTruthy()
    expect(route).not.toHaveBeenCalled()
  })

  it('disables the camera toggle when the device has no camera', () => {
    session('connected')
    hasCamera.value = false
    const { getByLabelText } = render(<InCallPage />)
    expect((getByLabelText('No camera available') as HTMLButtonElement).disabled).toBe(true)
  })

  it('an audio call has no camera to switch on — the toggle says why', () => {
    session('connected')
    callType.value = 'audio'
    hasCamera.value = false
    const { getByLabelText } = render(<InCallPage />)
    expect((getByLabelText('Camera is off in audio calls') as HTMLButtonElement).disabled)
      .toBe(true)
  })

  it('leaving the page mid-call hangs up', () => {
    session('connected')
    const { unmount } = render(<InCallPage />)
    act(() => { unmount() })
    expect(s.hangupCall).toHaveBeenCalledTimes(1)
  })

  it('a page left over from an earlier call does not hang up the next one', () => {
    session('connected')
    render(<InCallPage />)
    // Plain cleanup(), no act(): Preact 11 defers the unmount cleanup, so
    // it runs inside the next render's act() — after the new call started.
    cleanup()
    ids.n += 1
    ids.callId = `call-${ids.n}`
    session('connected')
    const { getByLabelText } = render(<InCallPage />)
    expect(s.hangupCall).not.toHaveBeenCalled()
    expect(getByLabelText('Connection quality')).toBeTruthy()
  })

  it('a group call shows one tile per participant with its own state', () => {
    session('connected')
    callPeers.value = [
      { userId: 'uid-bob', name: 'Bob', state: 'connected', stream: null },
      { userId: 'uid-carol', name: 'Carol', state: 'ringing', stream: null },
    ]
    const { getByRole, getAllByRole, queryByRole } = render(<InCallPage />)
    expect(getByRole('list', { name: 'Call participants' })).toBeTruthy()
    const tiles = getAllByRole('listitem').map(t => t.textContent)
    expect(tiles).toEqual(['BBob', 'CCarol · Ringing…'])
    // Per-tile states replace the call-wide status line.
    expect(queryByRole('status')).toBeNull()
  })

  it('a ringing group call says "Ringing…" on each tile, not across them', () => {
    session('ringing')
    callPeers.value = [
      { userId: 'uid-bob', name: 'Bob', state: 'ringing', stream: null },
      { userId: 'uid-carol', name: 'Carol', state: 'ringing', stream: null },
    ]
    const { getAllByRole, queryByRole } = render(<InCallPage />)
    expect(getAllByRole('listitem').map(t => t.textContent))
      .toEqual(['BBob · Ringing…', 'CCarol · Ringing…'])
    expect(queryByRole('status')).toBeNull()
  })
})
