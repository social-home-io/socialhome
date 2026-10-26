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
vi.mock('./PublicDiscoveryPage', () => ({ default: () => <div data-testid="discover" /> }))
const me = vi.hoisted(() => ({ value: { user_id: 'u-me', restrictions: [] as string[] } as Record<string, unknown> | null }))
vi.mock('@/store/auth', () => ({ currentUser: me }))

import { api } from '@/api'
import MomentumPage, { momentumViewFromUrl } from './MomentumPage'
import { momentumBackTarget, _resetMomentumListForTests } from './backLink'
import { _resetGfsConnectionsForTests } from '@/store/momentPublic'

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

  it('Discover has its own path; the old public/discover address moves there', () => {
    expect(momentumViewFromUrl('/momentum/discover')).toEqual({ tab: 'discover', tag: null, redirect: null })
    expect(momentumViewFromUrl('/momentum/public/discover'))
      .toEqual({ tab: 'discover', tag: null, redirect: '/momentum/discover' })
  })

  it('anything else is the Moments list', () => {
    expect(momentumViewFromUrl('/momentum')).toEqual({ tab: 'moments', tag: null, redirect: null })
    expect(momentumViewFromUrl('/momentum?tab=nope')).toEqual({ tab: 'moments', tag: null, redirect: null })
  })
})

describe('MomentumPage tabs', () => {
  beforeEach(() => {
    route.mockReset()
    _resetGfsConnectionsForTests()
    me.value = { user_id: 'u-me', restrictions: [] }
    vi.mocked(api.get).mockReset().mockImplementation(async (path: string) =>
      path === '/api/gfs/connections' ? [] : { hashtags: [] })
  })

  const withGfs = (rows: unknown[]) => {
    vi.mocked(api.get).mockImplementation(async (path: string) =>
      path === '/api/gfs/connections' ? rows : { hashtags: [] })
  }
  const tabNames = (nav: HTMLElement) => [...nav.querySelectorAll('a')].map(a => a.textContent)

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

  it('Discover joins the tabs once a GFS is connected', async () => {
    withGfs([{ id: 'g1', display_name: 'Demo GFS', status: 'active' }])
    where.url = '/momentum'; where.path = '/momentum'
    const { getByRole } = render(<MomentumPage />)
    const nav = getByRole('navigation', { name: 'Momentum sections' })
    await waitFor(() => expect(tabNames(nav)).toEqual(['Moments', 'Yours', 'Discover']))
    expect(nav.querySelectorAll('a')[2].getAttribute('href')).toBe('/momentum/discover')
  })

  it('no Discover tab without an active GFS', async () => {
    withGfs([{ id: 'g1', display_name: 'Pending GFS', status: 'pending' }])
    where.url = '/momentum'; where.path = '/momentum'
    const { getByRole } = render(<MomentumPage />)
    await waitFor(() => expect(api.get).toHaveBeenCalledWith('/api/gfs/connections'))
    await Promise.resolve()
    expect(tabNames(getByRole('navigation', { name: 'Momentum sections' }))).toEqual(['Moments', 'Yours'])
  })

  it('no Discover tab for a protected account, even with a GFS', async () => {
    me.value = { user_id: 'u-kid', protected: true, restrictions: ['public_moments'] }
    withGfs([{ id: 'g1', display_name: 'Demo GFS', status: 'active' }])
    where.url = '/momentum'; where.path = '/momentum'
    const { getByRole } = render(<MomentumPage />)
    await new Promise(r => setTimeout(r, 0))
    expect(tabNames(getByRole('navigation', { name: 'Momentum sections' }))).toEqual(['Moments', 'Yours'])
  })

  it('/momentum/discover shows Discover as the open tab', async () => {
    withGfs([{ id: 'g1', display_name: 'Demo GFS', status: 'active' }])
    where.url = '/momentum/discover'; where.path = '/momentum/discover'
    const { getByRole, getByTestId } = render(<MomentumPage />)
    const nav = getByRole('navigation', { name: 'Momentum sections' })
    expect(getByTestId('discover')).toBeTruthy()
    const current = nav.querySelector('[aria-current="page"]')
    expect(current?.textContent).toBe('Discover')
  })

  it('the old /momentum/public/discover is replaced by /momentum/discover', async () => {
    where.url = '/momentum/public/discover'; where.path = '/momentum/public/discover'
    render(<MomentumPage />)
    await waitFor(() => expect(route).toHaveBeenCalledWith('/momentum/discover', true))
  })
})
