import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render } from '@testing-library/preact'

// Mock the API module before importing the page
vi.mock('@/api', () => ({
  api: {
    get: vi.fn().mockResolvedValue([]),
    post: vi.fn().mockResolvedValue({}),
    patch: vi.fn().mockResolvedValue({}),
    delete: vi.fn().mockResolvedValue(undefined),
  },
}))

// Mock auth store
vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u1', username: 'admin', display_name: 'Admin', is_admin: true, picture_url: null, bio: null, is_new_member: false } },
  token: { value: 'test-tok' },
  isAuthed: { value: true },
  setToken: vi.fn(),
  logout: vi.fn(),
}))

import { pageTitle } from '@/store/pageTitle'
import { instanceConfig } from '@/store/instance'

describe('FeedPage', () => {
  beforeEach(() => {
    pageTitle.value = ''
    instanceConfig.value = null
  })

  it('module exports a default component', async () => {
    const mod = await import('./FeedPage')
    expect(mod.default).toBeTruthy()
    expect(typeof mod.default).toBe('function')
  })

  it('falls back to "Home" while the instance config is loading', async () => {
    instanceConfig.value = null
    const mod = await import('./FeedPage')
    const FeedPage = mod.default
    render(<FeedPage />)
    // useTitle runs in a useEffect; wait one tick for it to populate.
    await new Promise(r => setTimeout(r, 0))
    expect(pageTitle.value).toBe('Home')
  })

  it('uses the federated instance_name from instanceConfig when available', async () => {
    instanceConfig.value = {
      mode: 'standalone',
      instance_name: 'Casa Vizeli',
      instance_id: 'abc123',
      capabilities: [],
      setup_required: false,
    }
    const mod = await import('./FeedPage')
    const FeedPage = mod.default
    render(<FeedPage />)
    await new Promise(r => setTimeout(r, 0))
    expect(pageTitle.value).toBe('Casa Vizeli')
  })

  it('shows the Home lead (the "today" card) above the composer', async () => {
    const mod = await import('./FeedPage')
    const FeedPage = mod.default
    const { container, findByTestId } = render(<FeedPage lead={<section data-testid="lead" />} />)
    const lead = await findByTestId('lead')
    await new Promise(r => setTimeout(r, 0))
    const body = container.querySelector('.sh-feed, .sh-post--skeleton')
    expect(body).not.toBeNull()
    expect(lead.compareDocumentPosition(body!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })
})
