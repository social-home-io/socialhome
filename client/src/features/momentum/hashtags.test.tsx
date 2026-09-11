/**
 * Tests for renderHashtagged — keep the regex in sync with the
 * server-side extractor in ``socialhome/domain/moment.py``.
 */
import { describe, expect, it, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'
import { renderHashtagged } from './hashtags'

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
    expect(link.getAttribute('href')).toBe('/momentum?tab=archive&tag=berlin')
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
})
