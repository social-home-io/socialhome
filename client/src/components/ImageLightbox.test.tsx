import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

vi.mock('./Toast', () => ({ showToast: vi.fn() }))

import {
  ImageLightbox,
  copyReferenceForItem,
  openLightbox,
  closeLightbox,
} from './ImageLightbox'

describe('ImageLightbox', () => {
  it('module exports exist', async () => {
    const mod = await import('./ImageLightbox')
    expect(mod).toBeTruthy()
    expect(Object.keys(mod).length).toBeGreaterThan(0)
  })

  describe('copyReferenceForItem', () => {
    beforeEach(() => {
      Object.defineProperty(navigator, 'clipboard', {
        value: { writeText: vi.fn(async () => undefined) },
        configurable: true,
      })
    })

    it('copies a markdown image with the canonical /api/media URL', async () => {
      await copyReferenceForItem({
        url: '/api/media/abc.webp?exp=999&sig=DEADBEEF',
        caption: 'Sunrise',
      })
      const writeText = navigator.clipboard.writeText as unknown as ReturnType<
        typeof vi.fn
      >
      expect(writeText).toHaveBeenCalledOnce()
      const arg = writeText.mock.calls[0][0] as string
      expect(arg).toBe('![Sunrise](/api/media/abc.webp)')
      expect(arg).not.toContain('?exp=')
      expect(arg).not.toContain('&sig=')
    })

    it('falls back to a filename-derived alt when no caption', async () => {
      await copyReferenceForItem({
        url: '/api/media/holiday-photo.webp?exp=1&sig=2',
      })
      const writeText = navigator.clipboard.writeText as unknown as ReturnType<
        typeof vi.fn
      >
      expect(writeText.mock.calls[0][0]).toBe(
        '![holiday-photo](/api/media/holiday-photo.webp)',
      )
    })
  })

  describe('video item', () => {
    beforeEach(() => { closeLightbox() })

    it('renders a <video> with native controls and the thumbnail as poster', async () => {
      openLightbox({
        items: [
          {
            url: '/api/media/clip.webm?exp=1&sig=2',
            thumbnail_url: '/api/media/clip-thumb.webp',
            item_type: 'video',
          },
        ],
        index: 0,
      })
      const { container } = render(<ImageLightbox />)
      const video = container.querySelector('video') as HTMLVideoElement
      expect(video).toBeTruthy()
      // Native controls so the viewer can scrub / mute / fullscreen.
      expect(video.hasAttribute('controls')).toBe(true)
      // Poster = thumbnail so a browser that blocks the unmuted autoplay
      // shows a clean frame instead of a black box.
      expect(video.getAttribute('poster')).toBe('/api/media/clip-thumb.webp')
      // Muted so an opened clip doesn't blast audio — and so the autoPlay
      // is actually allowed to fire (browsers block unmuted autoplay).
      expect(video.muted).toBe(true)
    })
  })

  describe('Copy reference button', () => {
    beforeEach(() => {
      closeLightbox()
      Object.defineProperty(navigator, 'clipboard', {
        value: { writeText: vi.fn(async () => undefined) },
        configurable: true,
      })
    })

    it('renders in the lightbox toolbar and copies on click', async () => {
      openLightbox({
        items: [
          {
            url: '/api/media/x.webp?exp=1&sig=2',
            caption: 'Lunch',
            item_type: 'photo',
          },
        ],
        index: 0,
      })
      const { findByRole } = render(<ImageLightbox />)
      const btn = await findByRole('button', { name: /copy.*reference/i })
      fireEvent.click(btn)
      await new Promise(r => setTimeout(r, 0))
      const writeText = navigator.clipboard.writeText as unknown as ReturnType<
        typeof vi.fn
      >
      expect(writeText).toHaveBeenCalledOnce()
      expect(writeText.mock.calls[0][0]).toBe('![Lunch](/api/media/x.webp)')
    })
  })

  it('an item with onReport shows Report, which closes the viewer first', async () => {
    closeLightbox()
    const onReport = vi.fn()
    const r = render(<ImageLightbox />)
    openLightbox({ items: [{ id: 'g1', url: '/api/media/a.webp', onReport }] })
    fireEvent.click(await r.findByRole('button', { name: /🚩/ }))
    expect(onReport).toHaveBeenCalled()
    await waitFor(() => expect(r.queryByRole('button', { name: /🚩/ })).toBeNull())
  })

  it('an item without onReport shows no Report', async () => {
    closeLightbox()
    const r = render(<ImageLightbox />)
    openLightbox({ items: [{ id: 'g1', url: '/api/media/a.webp' }] })
    await r.findByRole('button', { name: /Copy/ })
    expect(r.queryByRole('button', { name: /🚩/ })).toBeNull()
    closeLightbox()
  })
})
