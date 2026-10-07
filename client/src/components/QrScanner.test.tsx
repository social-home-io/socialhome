import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'
import { QrScanner } from './QrScanner'
import en from '@/i18n/locales/en.json'

// Stable references so the inline detector mock decides yes/no.
let detectReturn: string | null = null
class MockBarcodeDetector {
  constructor(_opts: { formats: string[] }) {}
  detect = vi.fn(async () =>
    detectReturn === null ? [] : [{ rawValue: detectReturn }],
  )
}

const originalDetector = (window as unknown as { BarcodeDetector?: unknown })
  .BarcodeDetector
const originalCreateImageBitmap = (window as unknown as {
  createImageBitmap?: unknown
}).createImageBitmap

beforeEach(() => {
  detectReturn = null
  ;(window as unknown as { BarcodeDetector?: unknown })
    .BarcodeDetector = MockBarcodeDetector
  ;(window as unknown as {
    createImageBitmap?: (file: File) => Promise<{ close?: () => void }>
  }).createImageBitmap = vi.fn(async () => ({ close: vi.fn() }))
  // ``getUserMedia`` blocks under JSDOM — make the camera startup
  // bail with a permission denial so the loop tears down quickly.
  ;(navigator as unknown as { mediaDevices: MediaDevices }).mediaDevices = {
    getUserMedia: vi.fn(async () => {
      const err = new Error('denied')
      ;(err as { name?: string }).name = 'NotAllowedError'
      throw err
    }),
  } as unknown as MediaDevices
})

afterEach(() => {
  if (originalDetector === undefined) {
    delete (window as unknown as { BarcodeDetector?: unknown }).BarcodeDetector
  } else {
    (window as unknown as { BarcodeDetector?: unknown }).BarcodeDetector =
      originalDetector
  }
  if (originalCreateImageBitmap === undefined) {
    delete (window as unknown as { createImageBitmap?: unknown })
      .createImageBitmap
  } else {
    (window as unknown as { createImageBitmap?: unknown }).createImageBitmap =
      originalCreateImageBitmap
  }
  vi.clearAllMocks()
})

describe('QrScanner', () => {
  it('uploads an image and emits the decoded payload', async () => {
    detectReturn = 'socialhome://invite#xyz'
    const onPayload = vi.fn()
    const { container } = render(<QrScanner onPayload={onPayload} />)

    const input = container.querySelector('input[type=file]') as HTMLInputElement
    const file = new File(['fake'], 'qr.png', { type: 'image/png' })
    Object.defineProperty(input, 'files', { value: [file], configurable: true })
    fireEvent.change(input)

    await waitFor(() => {
      expect(onPayload).toHaveBeenCalledWith('socialhome://invite#xyz')
    })
  })

  it('shows an inline error and calls onError when no QR is found in the image', async () => {
    detectReturn = null  // detector returns []
    const onPayload = vi.fn()
    const onError = vi.fn()
    const { container } = render(
      <QrScanner onPayload={onPayload} onError={onError} />,
    )

    const input = container.querySelector('input[type=file]') as HTMLInputElement
    const file = new File(['x'], 'blank.png', { type: 'image/png' })
    Object.defineProperty(input, 'files', { value: [file], configurable: true })
    fireEvent.change(input)

    await waitFor(() => {
      expect(container.querySelector('.sh-scan-error-inline')).not.toBeNull()
      expect(onError).toHaveBeenCalled()
      expect(onPayload).not.toHaveBeenCalled()
    })
  })

  // ── Camera errors live on the camera card only ───────────────────
  // ``QrCameraScanner`` renders its own error card; the parent's inline
  // line is for upload / decode errors. Before the split the camera
  // error reached both surfaces and the text showed twice.
  function rejectCameraWith(name: string) {
    ;(navigator as unknown as { mediaDevices: MediaDevices }).mediaDevices = {
      getUserMedia: vi.fn(async () => {
        const err = new Error(name)
        ;(err as { name?: string }).name = name
        throw err
      }),
    } as unknown as MediaDevices
  }
  const flush = () => new Promise(r => setTimeout(r, 0))

  it('shows the no-camera message exactly once when no camera is found', async () => {
    rejectCameraWith('NotFoundError')
    const { container } = render(<QrScanner onPayload={vi.fn()} />)
    await waitFor(() => {
      expect(container.querySelector('.sh-scan-error')).not.toBeNull()
    })
    await flush()
    const text = container.textContent ?? ''
    const needle = en['pairing.scan_no_camera']
    expect(text.split(needle).length - 1).toBe(1)
    // The parent inline line is reserved for upload / decode errors.
    expect(container.querySelector('.sh-scan-error-inline')).toBeNull()
  })

  it('a later camera error never replaces a more recent upload error', async () => {
    detectReturn = null
    const onPayload = vi.fn()
    const { container, rerender } = render(<QrScanner onPayload={onPayload} />)
    await waitFor(() => {
      expect(container.querySelector('.sh-scan-error')).not.toBeNull()
    })

    const input = container.querySelector('input[type=file]') as HTMLInputElement
    const file = new File(['x'], 'blank.png', { type: 'image/png' })
    Object.defineProperty(input, 'files', { value: [file], configurable: true })
    fireEvent.change(input)
    const uploadError = en['pairing.scan_no_code_in_image']
    await waitFor(() => {
      expect(container.querySelector('.sh-scan-error-inline')?.textContent)
        .toBe(uploadError)
    })

    // A parent re-render (new props) must not restart the camera and
    // overwrite the upload verdict with the camera text.
    rerender(<QrScanner onPayload={onPayload} onCancel={vi.fn()} />)
    await flush()
    await flush()
    expect(container.querySelector('.sh-scan-error-inline')?.textContent)
      .toBe(uploadError)
  })

  it('starts the camera once per mount, not once per parent render', async () => {
    const { container, rerender } = render(<QrScanner onPayload={vi.fn()} />)
    await waitFor(() => {
      expect(container.querySelector('.sh-scan-error')).not.toBeNull()
    })
    rerender(<QrScanner onPayload={vi.fn()} onCancel={vi.fn()} />)
    await flush()
    rerender(<QrScanner onPayload={vi.fn()} onError={vi.fn()} />)
    await flush()
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
  })

  it('renders the no-camera hint when BarcodeDetector is unavailable', () => {
    delete (window as unknown as { BarcodeDetector?: unknown }).BarcodeDetector
    const { container } = render(<QrScanner onPayload={vi.fn()} />)
    expect(container.querySelector('.sh-scan-no-camera')).not.toBeNull()
  })

  it('renders a Cancel button only when onCancel is provided', () => {
    const onCancel = vi.fn()
    const { container, rerender } = render(<QrScanner onPayload={vi.fn()} />)
    expect(container.querySelector('.sh-pairing-actions')).toBeNull()
    rerender(<QrScanner onPayload={vi.fn()} onCancel={onCancel} />)
    const btn = container.querySelector('.sh-pairing-actions button')
    expect(btn).not.toBeNull()
    fireEvent.click(btn!)
    expect(onCancel).toHaveBeenCalled()
  })
})
