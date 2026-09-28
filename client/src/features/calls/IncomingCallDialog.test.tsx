import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'

const apiPost = vi.fn().mockResolvedValue({})
vi.mock('@/api', () => ({
  api: { post: (...a: unknown[]) => apiPost(...a), get: vi.fn().mockResolvedValue([]) },
}))
const showToast = vi.fn()
vi.mock('@/components/Toast', () => ({
  showToast: (...a: unknown[]) => showToast(...a),
}))
// The SDP answer / media handshake is covered by callSession.test.ts.
const acceptCall = vi.fn()
vi.mock('./callSession', () => ({
  acceptCall: (...a: unknown[]) => acceptCall(...a),
}))
const route = vi.fn()
vi.mock('preact-iso', () => ({ useLocation: () => ({ route, url: '/' }) }))

import IncomingCallDialog from './IncomingCallDialog'
import { CallEmbedBlockedDialog } from './CallEmbedBlockedDialog'
import { CallEmbedBlockedError } from './embedPolicy'
import { incoming, type IncomingCall } from '@/store/calls'

const RINGING: IncomingCall = {
  call_id: 'c1', from_user: 'alice', call_type: 'video',
  conversation_id: 'conv-1',
  signed_sdp: { sdp: 'OFFER', sdp_type: 'offer', signature: 'sig' },
}

describe('IncomingCallDialog', () => {
  beforeEach(() => {
    incoming.value = null
    acceptCall.mockReset()
    apiPost.mockClear()
    showToast.mockReset()
    route.mockReset()
  })

  it('renders nothing when no incoming call is set', () => {
    const { container } = render(<IncomingCallDialog />)
    expect(container.textContent).toBe('')
  })

  it('renders the caller display + accept/decline when ringing', () => {
    incoming.value = { ...RINGING, signed_sdp: null }
    const { getByText } = render(<IncomingCallDialog />)
    expect(getByText(/alice is calling/)).toBeTruthy()
    expect(getByText('Accept')).toBeTruthy()
    expect(getByText('Decline')).toBeTruthy()
  })

  it('Accept answers the offer through the call session, then opens the call', async () => {
    acceptCall.mockResolvedValueOnce(undefined)
    incoming.value = RINGING
    const { getByText } = render(<IncomingCallDialog />)
    fireEvent.click(getByText('Accept'))
    await new Promise(r => setTimeout(r, 0))
    expect(acceptCall).toHaveBeenCalledWith(RINGING)
    // No placeholder SDP is posted from here any more.
    expect(apiPost).not.toHaveBeenCalled()
    expect(route).toHaveBeenCalledWith('/calls/c1')
    expect(incoming.value).toBeNull()
  })

  it('keeps ringing and explains why when answering fails', async () => {
    acceptCall.mockRejectedValueOnce(new Error('Microphone access is blocked.'))
    incoming.value = RINGING
    const { getByText } = render(<IncomingCallDialog />)
    fireEvent.click(getByText('Accept'))
    await new Promise(r => setTimeout(r, 0))
    expect(showToast).toHaveBeenCalledWith(
      "Couldn't answer the call: Microphone access is blocked.", 'error',
    )
    expect(route).not.toHaveBeenCalled()
    expect(incoming.value).toEqual(RINGING)
    expect(getByText('Decline')).toBeTruthy()
  })

  it('an unmount before the focus tick cancels it', () => {
    vi.useFakeTimers()
    try {
      incoming.value = { ...RINGING, signed_sdp: null }
      const { unmount } = render(<IncomingCallDialog />)
      unmount()
      const query = vi.spyOn(document, 'querySelector')
      vi.advanceTimersByTime(50)
      expect(query).not.toHaveBeenCalled()
      query.mockRestore()
    } finally {
      vi.useRealTimers()
    }
  })

  it('an embed that denies the mic stops the ring here and points to its own tab', async () => {
    acceptCall.mockRejectedValueOnce(new CallEmbedBlockedError())
    incoming.value = RINGING
    const { getByText, queryByText } = render(
      <><IncomingCallDialog /><CallEmbedBlockedDialog /></>,
    )
    fireEvent.click(getByText('Accept'))
    await new Promise(r => setTimeout(r, 0))
    // Not a decline: another device of this user may still answer.
    expect(apiPost).not.toHaveBeenCalled()
    expect(incoming.value).toBeNull()
    expect(showToast).not.toHaveBeenCalled()
    expect(queryByText('Accept')).toBeNull()
    const link = getByText('Open in a new tab') as HTMLAnchorElement
    expect(link.target).toBe('_blank')
    expect(link.href).toBe(window.location.href)
    fireEvent.click(getByText('Not now'))
    expect(queryByText('Open in a new tab')).toBeNull()
  })
})
