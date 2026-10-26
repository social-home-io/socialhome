import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, within } from '@testing-library/preact'

const loc = vi.hoisted(() => ({ url: '/highlights', path: '/highlights', route: vi.fn() }))
vi.mock('preact-iso', () => ({ useLocation: () => loc }))
vi.mock('./HighlightsInboxTab', () => ({ default: () => <p>today-tab</p> }))
vi.mock('./HighlightArchiveTab', () => ({ default: () => <p>archive-tab</p> }))

import HighlightsPage, { highlightsTabFromUrl } from './HighlightsPage'

function at(url: string) {
  loc.url = url
  loc.path = url.split('?')[0]
}

beforeEach(() => { loc.route.mockReset(); at('/highlights') })

describe('highlightsTabFromUrl', () => {
  it('reads the tab from the path', () => {
    expect(highlightsTabFromUrl('/highlights')).toBe('today')
    expect(highlightsTabFromUrl('/highlights/archive')).toBe('archive')
    expect(highlightsTabFromUrl('/highlights/archive?month=2026-10')).toBe('archive')
  })
})

describe('HighlightsPage', () => {
  it('tabs are links with aria-current on the open one', () => {
    const { getByRole, getByText } = render(<HighlightsPage />)
    const nav = getByRole('navigation', { name: 'Highlights sections' })
    const today = within(nav).getByRole('link', { name: 'Today' })
    const archive = within(nav).getByRole('link', { name: 'Archive' })
    expect(today.getAttribute('href')).toBe('/highlights')
    expect(today.getAttribute('aria-current')).toBe('page')
    expect(archive.getAttribute('href')).toBe('/highlights/archive')
    expect(archive.getAttribute('aria-current')).toBeNull()
    expect(getByText('today-tab')).toBeTruthy()
  })

  it('/highlights/archive opens the Archive', () => {
    at('/highlights/archive')
    const { getByRole, getByText } = render(<HighlightsPage />)
    expect(getByRole('link', { name: 'Archive' }).getAttribute('aria-current')).toBe('page')
    expect(getByText('archive-tab')).toBeTruthy()
  })

  it('an old ?tab=archive link lands on /highlights/archive', () => {
    at('/highlights?tab=archive')
    render(<HighlightsPage />)
    expect(loc.route).toHaveBeenCalledWith('/highlights/archive', true)
  })

  it('New opens the composer and the gear opens Highlights settings', () => {
    const { getByRole } = render(<HighlightsPage />)
    expect(getByRole('link', { name: 'New' }).getAttribute('href')).toBe('/highlights/new')
    expect(getByRole('link', { name: 'Highlights settings' }).getAttribute('href'))
      .toBe('/settings#highlights')
  })
})
