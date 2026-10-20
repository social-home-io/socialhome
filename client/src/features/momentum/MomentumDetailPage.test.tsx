import { describe, it, expect, vi } from 'vitest'
import { render, waitFor } from '@testing-library/preact'

vi.mock('@/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))
vi.mock('preact-iso', () => ({
  useRoute: () => ({ params: { momentId: 'm-amaka' } }),
  useLocation: () => ({ route: vi.fn(), url: '/' }),
}))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))
vi.mock('@/ws', () => ({ ws: { on: () => () => undefined } }))

import { api } from '@/api'
import type { Moment, MomentDetail } from '@/types'
import MomentumDetailPage from './MomentumDetailPage'

function moment(id: string, author: string, extra: Partial<Moment> = {}): Moment {
  return {
    id, author_user_id: author, content: `hello from ${id}`,
    media_url: null, media_type: null, duration_ms: null, parent_moment_id: null,
    origin_instance_id: 'peer-o', created_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + 86_400_000).toISOString(),
    reaction_count: 0, reply_count: 0, ...extra,
  }
}

describe('MomentumDetailPage authors', () => {
  it('names the remote author and a remote replier by the server\'s author', async () => {
    const detail: MomentDetail = {
      moment: moment('m-amaka', 'ru-amaka', {
        author: {
          display_name: 'Amaka Okafor',
          picture_url: 'api/users/ru-amaka/picture?v=1&sig=s',
        },
      }),
      replies: [moment('m-reply', 'ru-chidi', {
        parent_moment_id: 'm-amaka',
        author: { display_name: 'Chidi Okafor', picture_url: null },
      })],
      reactions: [],
    }
    vi.mocked(api.get).mockImplementation(async (path: string) =>
      path === '/api/moments/m-amaka' ? detail : [])
    const { container } = render(<MomentumDetailPage />)
    await waitFor(() => expect(container.textContent).toContain('Amaka Okafor'))
    expect(container.textContent).toContain('Chidi Okafor')
    expect(container.textContent).not.toContain('Someone')
    expect(container.textContent).not.toContain('ru-chidi')
    expect(
      [...container.querySelectorAll('img')].map(i => i.getAttribute('src')),
    ).toContain('api/users/ru-amaka/picture?v=1&sig=s')
  })

  it('shows a GFS-followed author\'s name, picture and public bio', async () => {
    const detail: MomentDetail = {
      moment: moment('m-amaka', 'u-gfs-noor', {
        received_via: 'gfs',
        received_via_gfs_id: 'g1',
        author: {
          display_name: 'Noor Haddad',
          picture_url: 'api/gfs/g1/moments/users/u-gfs-noor/picture?v=0123&sig=s',
          household: null,
          bio: 'Beekeeper. Posts the bees.',
        },
      }),
      replies: [],
      reactions: [],
    }
    vi.mocked(api.get).mockImplementation(async (path: string) =>
      path === '/api/moments/m-amaka' ? detail : [])
    const { container } = render(<MomentumDetailPage />)
    await waitFor(() => expect(container.textContent).toContain('Noor Haddad'))
    expect(
      container.querySelector('[data-testid="moment-author-bio"]')?.textContent,
    ).toBe('Beekeeper. Posts the bees.')
    expect(
      [...container.querySelectorAll('img')].map(i => i.getAttribute('src')),
    ).toContain('api/gfs/g1/moments/users/u-gfs-noor/picture?v=0123&sig=s')
  })

  it('shows no bio line for a household author', async () => {
    const detail: MomentDetail = {
      moment: moment('m-amaka', 'ru-amaka', {
        author: { display_name: 'Amaka Okafor', picture_url: null },
      }),
      replies: [],
      reactions: [],
    }
    vi.mocked(api.get).mockImplementation(async (path: string) =>
      path === '/api/moments/m-amaka' ? detail : [])
    const { container } = render(<MomentumDetailPage />)
    await waitFor(() => expect(container.textContent).toContain('Amaka Okafor'))
    expect(container.querySelector('[data-testid="moment-author-bio"]')).toBeNull()
  })
})
