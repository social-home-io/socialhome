import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, act } from '@testing-library/preact'

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

  it('focuses Accept once it starts ringing', () => {
    vi.useFakeTimers()
    try {
      incoming.value = { ...RINGING, signed_sdp: null }
      const { getByText } = render(<IncomingCallDialog />)
      act(() => { vi.advanceTimersByTime(50) })
      expect(document.activeElement).toBe(getByText('Accept'))
    } finally {
      vi.useRealTimers()
    }
  })

  it('Tab and Shift+Tab stay on Accept and Decline', () => {
    vi.useFakeTimers()
    try {
      incoming.value = { ...RINGING, signed_sdp: null }
      const { getByText } = render(<IncomingCallDialog />)
      act(() => { vi.advanceTimersByTime(50) })
      const accept = getByText('Accept')
      const decline = getByText('Decline')
      decline.focus()
      fireEvent.keyDown(window, { key: 'Tab' })
      expect(document.activeElement).toBe(accept)
      fireEvent.keyDown(window, { key: 'Tab', shiftKey: true })
      expect(document.activeElement).toBe(decline)
    } finally {
      vi.useRealTimers()
    }
  })

  it('focuses its own Accept, not a look-alike elsewhere on the page', () => {
    vi.useFakeTimers()
    try {
      incoming.value = { ...RINGING, signed_sdp: null }
      const { getByText } = render(
        <>
          <div class="sh-incoming-actions">
            <button class="sh-accept">Elsewhere</button>
            <button class="sh-decline">Elsewhere too</button>
          </div>
          <IncomingCallDialog />
        </>,
      )
      act(() => { vi.advanceTimersByTime(50) })
      expect(document.activeElement).toBe(getByText('Accept'))
      getByText('Decline').focus()
      fireEvent.keyDown(window, { key: 'Tab' })
      expect(document.activeElement).toBe(getByText('Accept'))
    } finally {
      vi.useRealTimers()
    }
  })

  it('an unmount before the focus tick moves no focus', () => {
    vi.useFakeTimers()
    try {
      const before = document.createElement('button')
      document.body.appendChild(before)
      before.focus()
      incoming.value = { ...RINGING, signed_sdp: null }
      const { unmount } = render(<IncomingCallDialog />)
      act(() => { unmount() })
      act(() => { vi.advanceTimersByTime(50) })
      expect(document.activeElement).toBe(before)
      before.remove()
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

  // The dialog stays mounted at the app root across calls: busy state
  // from one call must never carry over to the next ring.
  describe('a later call after the first one resolved', () => {
    const NEXT: IncomingCall = { ...RINGING, call_id: 'c2', from_user: 'bob' }
    const flush = () => act(() => new Promise<void>(r => setTimeout(r, 0)))
    const btn = (q: (t: string) => HTMLElement, label: string) =>
      q(label).closest('button') as HTMLButtonElement

    it('after declining, the next call can be accepted and declined', async () => {
      incoming.value = RINGING
      const { getByText } = render(<IncomingCallDialog />)
      fireEvent.click(getByText('Decline'))
      await flush()
      expect(apiPost).toHaveBeenCalledWith('/api/calls/c1/decline', {})
      expect(incoming.value).toBeNull()

      act(() => { incoming.value = NEXT })
      expect(getByText(/bob is calling/)).toBeTruthy()
      expect(btn(getByText, 'Accept').disabled).toBe(false)
      expect(btn(getByText, 'Decline').disabled).toBe(false)
      expect(document.querySelector('.sh-incoming-actions .sh-spinner-sm')).toBeNull()

      // Escape declines the new call.
      fireEvent.keyDown(window, { key: 'Escape' })
      await flush()
      expect(apiPost).toHaveBeenLastCalledWith('/api/calls/c2/decline', {})
      expect(incoming.value).toBeNull()
    })

    it('after declining, Enter accepts the next call', async () => {
      acceptCall.mockResolvedValueOnce(undefined)
      incoming.value = RINGING
      render(<IncomingCallDialog />)
      fireEvent.keyDown(window, { key: 'Escape' })
      await flush()
      act(() => { incoming.value = NEXT })
      fireEvent.keyDown(window, { key: 'Enter' })
      await flush()
      expect(acceptCall).toHaveBeenCalledWith(NEXT)
      expect(route).toHaveBeenCalledWith('/calls/c2')
    })

    it('after accepting, the next call rings with idle buttons', async () => {
      acceptCall.mockResolvedValueOnce(undefined).mockResolvedValueOnce(undefined)
      incoming.value = RINGING
      const { getByText } = render(<IncomingCallDialog />)
      fireEvent.click(getByText('Accept'))
      await flush()
      expect(incoming.value).toBeNull()

      act(() => { incoming.value = NEXT })
      expect(btn(getByText, 'Accept').disabled).toBe(false)
      expect(btn(getByText, 'Decline').disabled).toBe(false)
      fireEvent.click(getByText('Accept'))
      await flush()
      expect(acceptCall).toHaveBeenLastCalledWith(NEXT)
      expect(route).toHaveBeenLastCalledWith('/calls/c2')
    })

    it('a failed accept leaves both buttons usable for a retry', async () => {
      acceptCall.mockRejectedValueOnce(new Error('Microphone access is blocked.'))
        .mockResolvedValueOnce(undefined)
      incoming.value = RINGING
      const { getByText } = render(<IncomingCallDialog />)
      fireEvent.click(getByText('Accept'))
      await flush()
      expect(btn(getByText, 'Accept').disabled).toBe(false)
      expect(btn(getByText, 'Decline').disabled).toBe(false)
      fireEvent.keyDown(window, { key: 'Enter' })
      await flush()
      expect(acceptCall).toHaveBeenCalledTimes(2)
      expect(route).toHaveBeenCalledWith('/calls/c1')
    })

    it('a failed decline still stops the ring and the next call works', async () => {
      apiPost.mockRejectedValueOnce(new Error('offline'))
      incoming.value = RINGING
      const { getByText } = render(<IncomingCallDialog />)
      fireEvent.click(getByText('Decline'))
      await flush()
      expect(incoming.value).toBeNull()
      act(() => { incoming.value = NEXT })
      expect(btn(getByText, 'Accept').disabled).toBe(false)
      expect(btn(getByText, 'Decline').disabled).toBe(false)
    })

    it('a decline still in flight never dismisses a newer call', async () => {
      let resolve!: () => void
      apiPost.mockImplementationOnce(() => new Promise<void>(r => { resolve = r }))
      incoming.value = RINGING
      const { getByText } = render(<IncomingCallDialog />)
      fireEvent.click(getByText('Decline'))
      // The caller hangs up and someone else calls before the POST lands.
      act(() => { incoming.value = null })
      act(() => { incoming.value = NEXT })
      await act(async () => { resolve() })
      await flush()
      expect(incoming.value).toEqual(NEXT)
      expect(getByText(/bob is calling/)).toBeTruthy()
      expect(btn(getByText, 'Accept').disabled).toBe(false)
    })

    it('a second call replacing the first mid-decline starts idle', async () => {
      apiPost.mockImplementationOnce(() => new Promise(() => {}))
      incoming.value = RINGING
      const { getByText } = render(<IncomingCallDialog />)
      fireEvent.click(getByText('Decline'))
      act(() => { incoming.value = NEXT })
      expect(getByText(/bob is calling/)).toBeTruthy()
      expect(btn(getByText, 'Accept').disabled).toBe(false)
      expect(btn(getByText, 'Decline').disabled).toBe(false)
    })

    it('an accept that resolves after the call was replaced keeps the new ring', async () => {
      let resolve!: () => void
      acceptCall.mockImplementationOnce(() => new Promise<void>(r => { resolve = r }))
      incoming.value = RINGING
      render(<IncomingCallDialog />)
      fireEvent.keyDown(window, { key: 'Enter' })
      act(() => { incoming.value = NEXT })
      await act(async () => { resolve() })
      await flush()
      expect(incoming.value).toEqual(NEXT)
    })
  })

  it('a call replacing a ringing one keeps its own ringtone', async () => {
    // Each card stops only its own ring. Preact 11 runs the replaced
    // card's cleanup after paint; when the new card re-renders before
    // then, its effect (start ringing) runs first, and a shared "current
    // ring" slot let the old cleanup silence the new call.
    const rings: { closed: boolean }[] = []
    class FakeAudioContext {
      currentTime = 0
      destination = {}
      ring = { closed: false }
      constructor() { rings.push(this.ring) }
      createOscillator() {
        return { connect() {}, start() {}, stop() {}, type: '', frequency: { value: 0, setValueAtTime() {} } }
      }
      createGain() {
        return { connect() {}, gain: { value: 0, setValueAtTime() {}, linearRampToValueAtTime() {} } }
      }
      close() { this.ring.closed = true }
    }
    vi.stubGlobal('AudioContext', FakeAudioContext)
    try {
      const next: IncomingCall = { ...RINGING, call_id: 'c2', from_user: 'bob' }
      incoming.value = RINGING
      render(<IncomingCallDialog />)
      await act(async () => {})
      expect(rings).toHaveLength(1)
      // The new card renders, then re-renders before the first paint.
      incoming.value = next
      await Promise.resolve(); await Promise.resolve()
      incoming.value = { ...next }
      await act(async () => { await new Promise(r => setTimeout(r, 20)) })
      expect(rings).toHaveLength(2)
      expect(rings[0].closed).toBe(true)
      expect(rings[1].closed).toBe(false)
      act(() => { incoming.value = null })
      expect(rings[1].closed).toBe(true)
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('a repeated Enter answers only once', async () => {
    acceptCall.mockImplementation(() => new Promise(() => {}))
    incoming.value = RINGING
    render(<IncomingCallDialog />)
    fireEvent.keyDown(window, { key: 'Enter' })
    fireEvent.keyDown(window, { key: 'Enter' })
    await act(() => new Promise<void>(r => setTimeout(r, 0)))
    expect(acceptCall).toHaveBeenCalledTimes(1)
  })
})
