import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/preact'
import { FileRenderer, VideoRenderer, ImageRenderer } from './FileRenderer'

describe('FileRenderer', () => {
  it('renders file name and download link', () => {
    const { container, getByText } = render(
      <FileRenderer file={{ url: '/f.pdf', mime_type: 'application/pdf', original_name: 'spec.pdf', size_bytes: 2048 }} />
    )
    expect(getByText('spec.pdf')).toBeTruthy()
    expect(container.querySelector('a')?.getAttribute('href')).toBe('/f.pdf')
  })

  it('drops a javascript: url instead of linking it', () => {
    const { container } = render(
      <FileRenderer file={{ url: 'javascript:alert(1)', mime_type: 'application/pdf', original_name: 'x.pdf', size_bytes: 1 }} />
    )
    expect(container.querySelector('a')?.hasAttribute('href')).toBe(false)
  })

  it('formats file size correctly', () => {
    const { getByText } = render(
      <FileRenderer file={{ url: '/f', mime_type: 'text/plain', original_name: 'x', size_bytes: 1536 }} />
    )
    expect(getByText('1.5 KB')).toBeTruthy()
  })
})

describe('VideoRenderer', () => {
  it('renders video element when ready (status absent)', () => {
    const { container } = render(<VideoRenderer src="/v.mp4" />)
    expect(container.querySelector('video')).toBeTruthy()
  })

  it('renders the processing placeholder (no <video>) while transcoding', () => {
    const { container } = render(
      <VideoRenderer src="/v.webm" mediaStatus="processing" />,
    )
    expect(container.querySelector('video')).toBeNull()
    expect(container.querySelector('.sh-video-processing')).toBeTruthy()
  })

  it('renders the player when mediaStatus is ready', () => {
    const { container } = render(
      <VideoRenderer src="/v2.webm" mediaStatus="ready" />,
    )
    expect(container.querySelector('video')).toBeTruthy()
  })
})

describe('ImageRenderer', () => {
  it('renders image element', () => {
    const { container } = render(<ImageRenderer src="/i.webp" />)
    const img = container.querySelector('img')
    expect(img?.getAttribute('src')).toBe('/i.webp')
  })
})
