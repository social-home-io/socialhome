import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor } from '@testing-library/preact'
import { QrCodeImg } from './QrCodeImg'

vi.mock('qrcode', () => ({
  default: {
    toDataURL: vi.fn(async (data: string) =>
      `data:image/png;base64,FAKE-${data}`),
  },
}))

describe('QrCodeImg', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders the skeleton before the data URL resolves', () => {
    const { container } = render(<QrCodeImg data="socialhome://invite#abc" />)
    const skeleton = container.querySelector('.sh-qr-skeleton')
    expect(skeleton).not.toBeNull()
  })

  it('sizes the skeleton in px (Preact 11 core no longer appends px to numbers)', () => {
    const { container } = render(<QrCodeImg data="payload" size={160} />)
    const skeleton = container.querySelector('.sh-qr-skeleton') as HTMLElement
    expect(skeleton.style.width).toBe('160px')
    expect(skeleton.style.height).toBe('160px')
  })

  it('renders the img once qrcode resolves', async () => {
    const { container } = render(<QrCodeImg data="socialhome://invite#abc" />)
    await waitFor(() => {
      const img = container.querySelector('img.sh-qr-code') as HTMLImageElement
      expect(img).not.toBeNull()
      expect(img.getAttribute('src')).toContain('FAKE-socialhome://invite#abc')
    })
  })

  it('honours size and uses 2× width for retina', async () => {
    const QRCode = (await import('qrcode')).default
    render(<QrCodeImg data="payload" size={160} />)
    await waitFor(() => {
      expect(QRCode.toDataURL).toHaveBeenCalledWith(
        'payload',
        expect.objectContaining({ width: 320 }),
      )
    })
  })

  it('uses the alt prop as both the image alt and the skeleton hint', async () => {
    const { container } = render(
      <QrCodeImg data="payload" alt="Invite QR" />,
    )
    const skeleton = container.querySelector('.sh-qr-skeleton')
    expect(skeleton?.getAttribute('aria-label')).toBe('Generating Invite QR')
    await waitFor(() => {
      const img = container.querySelector('img.sh-qr-code')
      expect(img?.getAttribute('alt')).toBe('Invite QR')
    })
  })
})
