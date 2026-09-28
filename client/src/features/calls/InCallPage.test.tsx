import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

const apiPost = vi.fn().mockResolvedValue({})
vi.mock('@/api', () => ({
  api: { get: vi.fn(), post: (...a: unknown[]) => apiPost(...a) },
}))
const showToast = vi.fn()
vi.mock('@/components/Toast', () => ({
  showToast: (...a: unknown[]) => showToast(...a),
}))
const route = vi.fn()
vi.mock('preact-iso', () => ({
  useRoute: () => ({ params: { callId: 'call-1' } }),
  useLocation: () => ({ route, url: '/calls/call-1' }),
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
    remoteStream: signal<MediaStream | null>(null),
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
  callConversation, callEndReason, callId, callPhase, callType, hasCamera,
  type CallPhase,
} from './callSession'

function session(phase: CallPhase) {
  callId.value = 'call-1'
  callConversation.value = 'conv-1'
  callPhase.value = phase
  s.live = !['idle', 'ended', 'failed'].includes(phase)
}

describe('InCallPage', () => {
  beforeEach(() => {
    route.mockReset()
    apiPost.mockClear()
    showToast.mockReset()
    s.hangupCall.mockClear()
    s.resetCall.mockClear()
    callEndReason.value = null
    callPhase.value = 'idle'
    callId.value = null
    s.live = false
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
    expect(apiPost).toHaveBeenCalledWith('/api/calls/call-1/hangup', {})
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
    hasCamera.value = true
  })

  it('an audio call has no camera to switch on — the toggle says why', () => {
    session('connected')
    callType.value = 'audio'
    hasCamera.value = false
    const { getByLabelText } = render(<InCallPage />)
    expect((getByLabelText('Camera is off in audio calls') as HTMLButtonElement).disabled)
      .toBe(true)
    hasCamera.value = true
    callType.value = 'video'
  })

  it('leaving the page mid-call hangs up', () => {
    session('connected')
    const { unmount } = render(<InCallPage />)
    unmount()
    expect(s.hangupCall).toHaveBeenCalledTimes(1)
  })
})
