/**
 * Tests for renderHashtagged — keep the regex in sync with the
 * server-side extractor in ``socialhome/domain/moment.py``.
 */
import { describe, expect, it, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'
import { normalizeHashtag, renderHashtagged } from './hashtags'

describe('renderHashtagged', () => {
  it('passes through content with no hashtag', () => {
    const out = renderHashtagged('plain text', () => {})
    const { container } = render(<>{out}</>)
    expect(container.textContent).toBe('plain text')
    expect(container.querySelector('a')).toBeNull()
  })

  it('linkifies a hashtag and lowercases the slug', () => {
    // Mirror what real callers do — ``MomentumInboxTab`` /
    // ``MomentumDetailPage`` both ``preventDefault()`` and route in-app.
    // ``renderHashtagged`` deliberately leaves that to the caller, so a
    // bare ``vi.fn()`` let the click reach jsdom's native anchor
    // activation, which it cannot implement: it logs "Not implemented:
    // navigation to another Document" asynchronously through its virtual
    // console. Landing after the file finished, that surfaced as a
    // vitest unhandled error and failed the whole run with every test
    // green — a flake that cost a CI re-run on #658.
    const onClick = vi.fn((_tag: string, ev: MouseEvent) => ev.preventDefault())
    const out = renderHashtagged('Trip to #Berlin tomorrow', onClick)
    const { container } = render(<>{out}</>)
    const link = container.querySelector('a.sh-hashtag') as HTMLAnchorElement
    expect(link).not.toBeNull()
    expect(link.textContent).toBe('#Berlin')
    expect(link.getAttribute('href')).toBe('/momentum?tag=berlin')
    fireEvent.click(link)
    expect(onClick).toHaveBeenCalledWith('berlin', expect.anything())
  })

  it('does not match mid-word "#"', () => {
    const out = renderHashtagged('issue#42 is unrelated', () => {})
    const { container } = render(<>{out}</>)
    expect(container.querySelector('a.sh-hashtag')).toBeNull()
    expect(container.textContent).toBe('issue#42 is unrelated')
  })

  it('renders multiple tags and preserves surrounding text', () => {
    const out = renderHashtagged('#one and #two', () => {})
    const { container } = render(<>{out}</>)
    const links = container.querySelectorAll('a.sh-hashtag')
    expect(links.length).toBe(2)
    expect(links[0].textContent).toBe('#one')
    expect(links[1].textContent).toBe('#two')
    expect(container.textContent).toBe('#one and #two')
  })

  // Same cases as tests/domain/test_moment.py — the server's stored tag.
  const tags = (content: string) => {
    const { container } = render(<>{renderHashtagged(content, () => {})}</>)
    return [...container.querySelectorAll('a.sh-hashtag')].map(a =>
      new URL(a.getAttribute('href')!, 'http://x').searchParams.get('tag'))
  }

  it('keeps letters beyond ASCII in the tag ("#Kürbisfest" was "#k")', () => {
    expect(tags('Bestes Wochenende!! #Kürbisfest #Fußball #herbst2026'))
      .toEqual(['kürbisfest', 'fußball', 'herbst2026'])
    expect(tags('#Ελλάδα #Москва #東京 #café_au_lait'))
      .toEqual(['ελλάδα', 'москва', '東京', 'café_au_lait'])
  })

  it('composes a decomposed letter and keeps marks (Devanagari)', () => {
    expect(tags('#Kürbisfest'.normalize('NFD'))).toEqual(['kürbisfest'])
    expect(tags('#हिन्दी')).toEqual(['हिन्दी'])
  })

  it('ends a tag at punctuation or emoji and ignores a mid-word #', () => {
    expect(tags('#fest! #fest2🎃 #a-b #x.y')).toEqual(['fest', 'fest2', 'a', 'x'])
    expect(tags('issue#42 straße#nein ##ok')).toEqual(['ok'])
  })

  it('drops a run longer than 32 code points instead of cutting it', () => {
    const ok = 'ü'.repeat(32)
    const { container } = render(<>{renderHashtagged(`#${ok} #${ok}x`, () => {})}</>)
    expect(tags(`#${ok} #${ok}x`)).toEqual([ok])
    expect(container.textContent).toBe(`#${ok} #${ok}x`)
  })

  it('normalizes a ?tag= like the server', () => {
    expect(normalizeHashtag(' #Kürbisfest ')).toBe('kürbisfest')
    expect(normalizeHashtag('Kürbisfest'.normalize('NFD'))).toBe('kürbisfest')
  })
})
