import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'

// The SDP offer / media handshake lives in callSession (covered by its own
// test); the picker only has to start the chosen call type and route.
const startCall = vi.fn()
vi.mock('@/features/calls/callSession', () => ({
  startCall: (...a: unknown[]) => startCall(...a),
}))
const showToast = vi.fn()
vi.mock('./Toast', () => ({ showToast: (...a: unknown[]) => showToast(...a) }))

const routeSpy = vi.fn()
vi.mock('preact-iso', () => ({
  useLocation: () => ({ route: routeSpy, url: '/' }),
}))

import { CallTypePickerDialog, openCallTypePicker } from './CallTypePickerDialog'
import { CallEmbedBlockedDialog } from '@/features/calls/CallEmbedBlockedDialog'
import { CallEmbedBlockedError } from '@/features/calls/embedPolicy'

describe('CallTypePickerDialog', () => {
  beforeEach(() => {
    startCall.mockReset()
    showToast.mockReset()
    routeSpy.mockReset()
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
  })

  it('renders nothing when closed', () => {
    const { container } = render(<CallTypePickerDialog />)
    expect(container.querySelector('.sh-call-picker')).toBeNull()
  })

  it('renders one audio + one video tile when opened', async () => {
    openCallTypePicker('conv-1')
    const { findByLabelText } = render(<CallTypePickerDialog />)
    expect(await findByLabelText('Start audio call')).toBeTruthy()
    expect(await findByLabelText('Start video call')).toBeTruthy()
  })

  it('starts an audio call when the Audio tile is clicked', async () => {
    startCall.mockResolvedValueOnce('call-aud')
    openCallTypePicker('conv-aud')
    const { findByLabelText } = render(<CallTypePickerDialog />)
    fireEvent.click(await findByLabelText('Start audio call'))
    await new Promise(r => setTimeout(r, 0))
    expect(startCall).toHaveBeenCalledWith('conv-aud', 'audio')
    expect(routeSpy).toHaveBeenCalledWith('/calls/call-aud')
  })

  it('starts a video call when the Video tile is clicked', async () => {
    startCall.mockResolvedValueOnce('call-vid')
    openCallTypePicker('conv-vid')
    const { findByLabelText } = render(<CallTypePickerDialog />)
    fireEvent.click(await findByLabelText('Start video call'))
    await new Promise(r => setTimeout(r, 0))
    expect(startCall).toHaveBeenCalledWith('conv-vid', 'video')
    expect(routeSpy).toHaveBeenCalledWith('/calls/call-vid')
  })

  it('stays open and explains why when the call cannot start', async () => {
    startCall.mockRejectedValueOnce(new Error('Microphone access is blocked.'))
    openCallTypePicker('conv-err')
    const { findByLabelText } = render(<CallTypePickerDialog />)
    const tile = await findByLabelText('Start audio call') as HTMLButtonElement
    fireEvent.click(tile)
    await new Promise(r => setTimeout(r, 0))
    expect(routeSpy).not.toHaveBeenCalled()
    expect(showToast).toHaveBeenCalledWith(
      "Couldn't start the call: Microphone access is blocked.", 'error',
    )
    expect(tile.disabled).toBe(false)
  })

  it('swaps itself for the "open in its own tab" dialog when an embed blocks the mic', async () => {
    startCall.mockRejectedValueOnce(new CallEmbedBlockedError())
    openCallTypePicker('conv-1')
    const { findByLabelText, getByText, queryByLabelText } = render(
      <><CallTypePickerDialog /><CallEmbedBlockedDialog /></>,
    )
    fireEvent.click(await findByLabelText('Start audio call'))
    await new Promise(r => setTimeout(r, 0))
    expect(showToast).not.toHaveBeenCalled()
    expect(queryByLabelText('Start audio call')).toBeNull()
    expect(getByText(/inside this embedded view/)).toBeTruthy()
    expect(getByText('Open in a new tab')).toBeTruthy()
    fireEvent.click(getByText('Not now'))
  })
})
