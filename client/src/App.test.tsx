/**
 * App shell wiring. The full ``<App>`` needs the whole boot sequence, so
 * these tests render the exported ``TopBar`` with its neighbours stubbed.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, cleanup } from '@testing-library/preact'

vi.mock('@/components/TopBarHeading', () => ({
  TopBarHeading: () => <h1 data-testid="heading">Feed</h1>,
}))
vi.mock('@/components/SearchBar', () => ({
  SearchBar: () => <div data-testid="search" />,
  openSearchResult: vi.fn(),
}))
vi.mock('@/components/NotificationBell', () => ({
  NotificationBell: () => <div data-testid="bell" />,
  startNotificationPolling: vi.fn(),
  stopNotificationPolling: vi.fn(),
}))
vi.mock('@/components/SideNav', () => ({
  SideNav: () => <nav class="sh-sidenav" data-testid="sidenav"><a href="/">Feed</a></nav>,
}))
vi.mock('@/components/MobileNav', () => ({
  MobileNav: () => <nav class="sh-mobile-nav" data-testid="mobilenav"><a href="/">Home</a></nav>,
  mobileSidebarOpen: { value: false },
}))
vi.mock('preact-iso', async (orig) => ({
  ...(await orig<typeof import('preact-iso')>()),
  useLocation: () => ({ route: vi.fn(), url: '/', path: '/' }),
}))

import { AppLayout, TopBar } from './App'
import { currentUser } from '@/store/auth'

describe('TopBar', () => {
  beforeEach(() => {
    cleanup()
    currentUser.value = {
      user_id: 'u1', username: 'anna', display_name: 'Anna', is_admin: false,
      picture_url: null, picture_hash: null, bio: null, is_new_member: false,
    }
  })

  it('ends with the "You" menu, right after the notification bell', () => {
    const { container } = render(<TopBar />)
    const bar = container.querySelector('header.sh-topbar')!
    const kids = [...bar.children]
    const bell = kids.findIndex(el => el.getAttribute('data-testid') === 'bell')
    expect(bell).toBeGreaterThan(-1)
    expect(kids[bell + 1]?.querySelector('button.sh-me-btn')?.getAttribute('aria-label')).toBe('You: Anna')
    expect(kids.length).toBe(bell + 2)
  })

  it('shows no "You" button before the user is loaded', () => {
    currentUser.value = null
    const { container } = render(<TopBar />)
    expect(container.querySelector('.sh-me-btn')).toBeNull()
    expect(container.querySelector('[data-testid="bell"]')).not.toBeNull()
  })
})

describe('AppLayout', () => {
  beforeEach(() => { cleanup() })

  it('puts the top bar and the page before the navigation (focus order)', () => {
    // Phones: the bottom tab bar came first in the DOM, so Tab walked it
    // before the top bar. Desktop: ~20 sidebar stops came before a
    // space's section row. The grid / fixed bar keep the visual layout.
    const { container } = render(
      <AppLayout overlays={<div data-testid="overlay" />}>
        <a href="/x" data-testid="page-link">Page</a>
      </AppLayout>,
    )
    const order = [...container.querySelectorAll(
      'header.sh-topbar, main#main, [data-testid="sidenav"], [data-testid="mobilenav"], [data-testid="overlay"]',
    )].map(el => el.getAttribute('data-testid') ?? el.tagName.toLowerCase())
    expect(order).toEqual(['header', 'main', 'sidenav', 'mobilenav', 'overlay'])
    expect(container.querySelector('main#main [data-testid="page-link"]')).not.toBeNull()
    expect(container.querySelector('main#main')?.getAttribute('tabindex')).toBe('-1')
  })
})
