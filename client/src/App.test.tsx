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
vi.mock('preact-iso', async (orig) => ({
  ...(await orig<typeof import('preact-iso')>()),
  useLocation: () => ({ route: vi.fn(), url: '/', path: '/' }),
}))

import { TopBar } from './App'
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
