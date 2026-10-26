import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor, fireEvent } from '@testing-library/preact'

const where = vi.hoisted(() => ({ url: '/momentum', path: '/momentum' }))
const route = vi.hoisted(() => vi.fn())
vi.mock('preact-iso', () => ({
  useLocation: () => ({ route, url: where.url, path: where.path }),
}))
vi.mock('@/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))
vi.mock('@/ws', () => ({ ws: { on: () => () => undefined } }))
vi.mock('./MomentumInboxTab', () => ({
  default: ({ tag }: { tag: string | null }) => <div data-testid="moments">{tag ?? 'all'}</div>,
}))
vi.mock('./MomentumYoursTab', () => ({ default: () => <div data-testid="yours" /> }))

import { api } from '@/api'
import MomentumPage, { momentumViewFromUrl } from './MomentumPage'
import { momentumBackTarget, _resetMomentumListForTests } from './backLink'

describe('momentumViewFromUrl', () => {
  it('Yours has its own path', () => {
    expect(momentumViewFromUrl('/momentum/yours')).toEqual({ tab: 'yours', tag: null, redirect: null })
    expect(momentumViewFromUrl('/momentum/yours?show=saved'))
      .toEqual({ tab: 'yours', tag: null, redirect: null })
  })

  it('a hashtag link filters the Moments list', () => {
    expect(momentumViewFromUrl('/momentum?tag=walk')).toEqual({ tab: 'moments', tag: 'walk', redirect: null })
  })

  it('old ?tab= links redirect to the path URLs', () => {
    expect(momentumViewFromUrl('/momentum?tab=yours').redirect).toBe('/momentum/yours')
    expect(momentumViewFromUrl('/momentum?tab=yours&show=saved').redirect).toBe('/momentum/yours?show=saved')
    expect(momentumViewFromUrl('/momentum?tab=archive').redirect).toBe('/momentum/yours')
    expect(momentumViewFromUrl('/momentum?tab=archive&tag=walk').redirect).toBe('/momentum?tag=walk')
  })

  it('anything else is the Moments list', () => {
    expect(momentumViewFromUrl('/momentum')).toEqual({ tab: 'moments', tag: null, redirect: null })
    expect(momentumViewFromUrl('/momentum?tab=nope')).toEqual({ tab: 'moments', tag: null, redirect: null })
  })
})

describe('MomentumPage tabs', () => {
  beforeEach(() => {
    route.mockReset()
    vi.mocked(api.get).mockReset().mockResolvedValue({ hashtags: [] })
  })

  it('are links in a nav, the open one marked aria-current', () => {
    where.url = '/momentum/yours'; where.path = '/momentum/yours'
    const { getByRole, getByTestId } = render(<MomentumPage />)
    const nav = getByRole('navigation', { name: 'Momentum sections' })
    const links = [...nav.querySelectorAll('a')]
    expect(links.map(a => [a.textContent, a.getAttribute('href')])).toEqual([
      ['Moments', '/momentum'],
      ['Yours', '/momentum/yours'],
    ])
    expect(links[1].getAttribute('aria-current')).toBe('page')
    expect(links[0].getAttribute('aria-current')).toBeNull()
    expect(nav.querySelector('[role="tab"]')).toBeNull()
    expect(getByTestId('yours')).toBeTruthy()
  })

  it('remembers the tab and tag for the moment page\'s back link', () => {
    _resetMomentumListForTests()
    where.url = '/momentum?tag=herbst'; where.path = '/momentum'
    render(<MomentumPage />)
    expect(momentumBackTarget()).toBe('/momentum?tag=herbst')
    where.url = '/momentum?tab=yours'; where.path = '/momentum'
    render(<MomentumPage />)
    expect(momentumBackTarget()).toBe('/momentum/yours')
  })

  it('an old ?tab=yours link is replaced by /momentum/yours', async () => {
    where.url = '/momentum?tab=yours'; where.path = '/momentum'
    render(<MomentumPage />)
    await waitFor(() => expect(route).toHaveBeenCalledWith('/momentum/yours', true))
  })

  it('the # button opens trending tags as links; an active tag is one removable chip', async () => {
    where.url = '/momentum?tag=herbst'; where.path = '/momentum'
    vi.mocked(api.get).mockResolvedValue({ hashtags: [{ tag: 'herbst', count: 3 }, { tag: 'küche', count: 1 }] })
    const { getByRole, findByRole, getByTestId, queryByRole } = render(<MomentumPage />)
    expect(getByTestId('moments').textContent).toBe('herbst')
    const chip = getByRole('link', { name: 'Remove filter #herbst' })
    expect(chip.getAttribute('href')).toBe('/momentum')
    const btn = getByRole('button', { name: 'Filter by hashtag' })
    expect(btn.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(btn)
    const kueche = await findByRole('link', { name: /#küche/ })
    expect(kueche.getAttribute('href')).toBe('/momentum?tag=k%C3%BCche')
    expect(api.get).toHaveBeenCalledWith('/api/moments/hashtags', { limit: '12' })
    fireEvent.keyDown(kueche, { key: 'Escape' })
    await waitFor(() => expect(queryByRole('link', { name: /#küche/ })).toBeNull())
    expect(document.activeElement).toBe(btn)
  })
})
