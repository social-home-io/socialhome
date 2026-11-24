import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'
import { LinkPreviewCard } from './LinkPreviewCard'
import type { LinkPreview } from '@/types'

const CARD: LinkPreview = {
  url: 'https://www.example.com/story',
  title: 'A <b>title</b>',
  description: 'What it is about',
  site_name: 'Example News',
  thumbnail_url: 'api/media/lp.webp?exp=1&sig=x',
}

describe('LinkPreviewCard', () => {
  it('renders title, description, site and domain as text, linking out safely', () => {
    const { container, getByText } = render(<LinkPreviewCard preview={CARD} />)
    // Escaped text, never parsed as markup.
    expect(getByText('A <b>title</b>')).toBeTruthy()
    expect(container.querySelector('b')).toBeNull()
    expect(getByText('What it is about')).toBeTruthy()
    expect(getByText('Example News')).toBeTruthy()
    expect(getByText('example.com')).toBeTruthy()
    const a = container.querySelector('a') as HTMLAnchorElement
    expect(a.href).toBe('https://www.example.com/story')
    expect(a.target).toBe('_blank')
    // The same rel as inline links in the post text.
    expect(a.rel).toBe('noopener noreferrer nofollow ugc')
    // UX review: the card's name says what it opens and how.
    expect(a.getAttribute('aria-label')).toBe(
      'A <b>title</b> – Example News, opens in a new tab',
    )
  })

  it('names a card without a title by its site', () => {
    const { container } = render(
      <LinkPreviewCard preview={{ ...CARD, title: null, site_name: null }} />,
    )
    expect(container.querySelector('a')!.getAttribute('aria-label'))
      .toBe('Open example.com in a new tab')
  })

  it.each([
    [1200, 630, true],
    [1280, 720, true],
    [400, 400, false],
    [300, 500, false],
  ])('a %ix%i picture is landscape: %s', (w, h, landscape) => {
    const { container } = render(<LinkPreviewCard preview={CARD} />)
    const img = container.querySelector('img')!
    Object.defineProperty(img, 'naturalWidth', { value: w })
    Object.defineProperty(img, 'naturalHeight', { value: h })
    fireEvent.load(img)
    expect(!!container.querySelector('.sh-link-preview--landscape')).toBe(landscape)
  })

  it('is compact with neither picture nor description', () => {
    const { container } = render(
      <LinkPreviewCard preview={{ ...CARD, thumbnail_url: null, description: null }} />,
    )
    expect(container.querySelector('.sh-link-preview--compact')).toBeTruthy()
    const full = render(<LinkPreviewCard preview={{ ...CARD, thumbnail_url: null }} />)
    expect(full.container.querySelector('.sh-link-preview--compact')).toBeNull()
  })

  it('loads the image from local media relative to the document base', () => {
    const { container } = render(<LinkPreviewCard preview={CARD} />)
    const img = container.querySelector('img') as HTMLImageElement
    expect(img.getAttribute('src')).toBe('api/media/lp.webp?exp=1&sig=x')
    expect(img.getAttribute('alt')).toBe('')
  })

  it('hides a broken image and keeps the text', () => {
    const { container } = render(<LinkPreviewCard preview={CARD} />)
    fireEvent.error(container.querySelector('img')!)
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('.sh-link-preview--no-image')).toBeTruthy()
  })

  it('renders no link for a non-web URL', () => {
    const { container, getByText } = render(
      <LinkPreviewCard preview={{ ...CARD, url: 'javascript:alert(1)' }} />,
    )
    expect(container.querySelector('a')).toBeNull()
    expect(getByText('A <b>title</b>')).toBeTruthy()
  })

  it('falls back to the domain when there is no site name or image', () => {
    const { getByText, container } = render(
      <LinkPreviewCard
        preview={{ ...CARD, site_name: null, thumbnail_url: null, description: null }}
      />,
    )
    expect(getByText('example.com')).toBeTruthy()
    expect(container.querySelector('img')).toBeNull()
  })

  it('offers a remove button in composer mode', () => {
    const onRemove = vi.fn()
    const { getByLabelText } = render(
      <LinkPreviewCard preview={CARD} onRemove={onRemove} />,
    )
    fireEvent.click(getByLabelText('Remove link preview'))
    expect(onRemove).toHaveBeenCalledOnce()
  })

  it('has no remove button on a feed card', () => {
    const { queryByLabelText } = render(<LinkPreviewCard preview={CARD} />)
    expect(queryByLabelText('Remove link preview')).toBeNull()
  })
})
