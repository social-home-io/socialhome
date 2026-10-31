import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'

const nav = vi.hoisted(() => ({ route: vi.fn() }))
const auth = vi.hoisted(() => ({ currentUser: { value: { is_admin: false } as { is_admin: boolean, protected?: boolean } } }))
vi.mock('preact-iso', async (orig) => ({
  ...(await orig<object>()),
  useLocation: () => ({ route: nav.route, path: '/apps', url: '/apps', query: {} }),
}))
vi.mock('@/store/auth', () => ({ currentUser: auth.currentUser }))

import { StoreCard } from './StoreCard'
import type { CatalogEntry, InstalledApp } from '@/store/apps'

const entry = (over: Partial<any> = {}): CatalogEntry => ({
  app_id: 'chess', name: 'Chess', latest_version: '2025.3.1',
  description: 'Play chess.', icon_url: null,
  capabilities: ['storage', 'federation'], permissions: ['storage', 'people', 'federation'],
  bundle_size: 212 * 1024, min_age: 0, compatible: true, requested: false,
  ...over,
})
const installed = (over: Partial<any> = {}): InstalledApp => ({
  app_id: 'chess', name: 'Chess', version: '2025.3.1', enabled: true,
  capabilities: [], icon: null, min_age: 0, ...over,
})

beforeEach(() => vi.clearAllMocks())

describe('StoreCard', () => {
  it('names the app, where it comes from and what it can do, in Social Home\'s words', () => {
    const { getByRole } = render(<StoreCard entry={entry()} installed={null} isAdmin lang="en" />)
    const card = getByRole('article', { name: 'Chess' })
    expect(card.textContent).toContain('From Social Home')
    expect(card.textContent).toContain('Sends invites and moves to other households')
    // A version from another year carries the year.
    expect(card.querySelector('.sh-store-card__meta')!.textContent).toMatch(/2025/)
  })

  it('a member asks an admin instead of installing', () => {
    const { getByRole, queryByRole } = render(<StoreCard entry={entry()} installed={null} isAdmin={false} lang="en" />)
    expect(queryByRole('button', { name: 'Install Chess' })).toBeNull()
    expect(getByRole('button', { name: 'Ask an admin for Chess' })).toBeTruthy()
  })

  it('an installed app opens; Details leads to the detail page otherwise', () => {
    const a = render(<StoreCard entry={entry()} installed={installed()} isAdmin lang="en" />)
    fireEvent.click(a.getByRole('button', { name: 'Open Chess' }))
    expect(nav.route).toHaveBeenCalledWith('/apps/chess')
    a.unmount()
    const b = render(<StoreCard entry={entry()} installed={null} isAdmin lang="en" />)
    fireEvent.click(b.getByRole('button', { name: 'Details about Chess' }))
    expect(nav.route).toHaveBeenCalledWith('/apps/chess/about')
  })

  it('a rated app shows its age on the chip with a title', () => {
    const { getByText } = render(<StoreCard entry={entry({ min_age: 16 })} installed={null} isAdmin lang="en" />)
    expect(getByText('16+').getAttribute('title')).toBe('Minimum age 16')
  })
})
