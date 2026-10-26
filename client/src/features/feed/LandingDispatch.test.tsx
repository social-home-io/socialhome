import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render } from '@testing-library/preact'
import type { ComponentChildren } from 'preact'

const landing = vi.hoisted(() => ({ path: '/feed' }))
vi.mock('preact-iso', () => ({ useLocation: () => ({ route: vi.fn(), url: '/' }) }))
vi.mock('@/utils/preferences', () => ({ getLandingPath: () => landing.path }))
vi.mock('./FeedPage', () => ({
  default: ({ lead }: { lead?: ComponentChildren }) => <main data-testid="feed">{lead}</main>,
  feedTabFromUrl: () => 'feed',
}))
vi.mock('@/features/welcome/WelcomePage', () => ({ default: () => <main data-testid="welcome" /> }))
vi.mock('@/features/welcome/HomeTodayCard', () => ({ HomeTodayCard: () => <section data-testid="today" /> }))

import LandingDispatch from './LandingDispatch'

describe('LandingDispatch', () => {
  beforeEach(() => { landing.path = '/feed' })

  it('a feed landing opens with the "today" card above the feed', () => {
    const { getByTestId } = render(<LandingDispatch />)
    expect(getByTestId('feed').querySelector('[data-testid="today"]')).not.toBeNull()
  })

  it('the welcome landing carries its own card', () => {
    landing.path = '/'
    const { getByTestId, queryByTestId } = render(<LandingDispatch />)
    expect(getByTestId('welcome')).toBeTruthy()
    expect(queryByTestId('feed')).toBeNull()
  })
})
