import { describe, it, expect, vi } from 'vitest'
import { render, waitFor, fireEvent } from '@testing-library/preact'

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
import { showToast } from '@/components/Toast'
import { currentUser } from '@/store/auth'
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

describe('MomentumDetailPage share link', () => {
  function asAuthor(extra: Partial<Moment>) {
    currentUser.value = { user_id: 'u-me' } as never
    const detail: MomentDetail = {
      moment: moment('m-amaka', 'u-me', extra),
      replies: [],
      reactions: [],
    }
    vi.mocked(api.get).mockImplementation(async (path: string) =>
      path === '/api/moments/m-amaka' ? detail : [])
  }

  it('the author of a public moment copies its share link', async () => {
    asAuthor({ is_public: true })
    vi.mocked(api.post).mockResolvedValue({ url: 'https://viewer.example/v/x/#m=m-amaka' })
    const writeText = vi.fn(async () => undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    const { container, findByText } = render(<MomentumDetailPage />)
    const button = await findByText(/Copy share link/)
    button.click()
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('https://viewer.example/v/x/#m=m-amaka'))
    expect(api.post).toHaveBeenCalledWith('/api/moments/m-amaka/share-link', {})
    expect(container.textContent).toContain('Copy share link')
  })

  it('a household-only moment has no share link', async () => {
    asAuthor({ is_public: false })
    const { container } = render(<MomentumDetailPage />)
    await waitFor(() => expect(container.textContent).toContain('hello from m-amaka'))
    expect(container.textContent).not.toContain('Copy share link')
  })

  it('a failed request says so and copies nothing', async () => {
    asAuthor({ is_public: true })
    vi.mocked(api.post).mockRejectedValue(new Error('nope'))
    const writeText = vi.fn(async () => undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    const { findByText } = render(<MomentumDetailPage />)
    ;(await findByText(/Copy share link/)).click()
    await waitFor(() => expect(showToast).toHaveBeenCalled())
    expect(writeText).not.toHaveBeenCalled()
  })
})

describe('MomentumDetailPage gone vs failed', () => {
  const notFound = () => Object.assign(new Error('moment not found'), { status: 404 })

  it('a moment that is gone says so and leads back, without a toast', async () => {
    vi.mocked(showToast).mockClear()
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path === '/api/moments/m-amaka') throw notFound()
      return []
    })
    const { findByRole, container } = render(<MomentumDetailPage />)
    const state = await findByRole('status')
    expect(state.textContent).toContain("This moment isn't here any more")
    expect(container.querySelector('a[href$="/momentum"]')?.textContent).toBe('Back to Momentum')
    expect(container.querySelector('[role="alert"]')).toBeNull()
    expect(showToast).not.toHaveBeenCalled()
  })

  it('a failed load is not "gone": it says so and offers Try again', async () => {
    let fail = true
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path !== '/api/moments/m-amaka') return []
      if (fail) throw new Error('offline')
      return {
        moment: moment('m-amaka', 'ru-amaka', { author: { display_name: 'Amaka Okafor', picture_url: null } }),
        replies: [], reactions: [],
      } satisfies MomentDetail
    })
    const { findByRole, container } = render(<MomentumDetailPage />)
    expect((await findByRole('alert')).textContent).toContain("Couldn't load this moment")
    expect(container.textContent).not.toContain("isn't here any more")
    fail = false
    fireEvent.click(await findByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(container.textContent).toContain('Amaka Okafor'))
  })
})

describe('MomentumDetailPage for your archived moment', () => {
  it('says only you see it now, and offers no reply or share link', async () => {
    currentUser.value = { user_id: 'u-me' } as never
    const past = Date.now() - 20 * 86_400_000
    vi.mocked(api.get).mockImplementation(async (path: string) =>
      path === '/api/moments/m-amaka'
        ? {
          moment: moment('m-amaka', 'u-me', {
            created_at: new Date(past).toISOString(),
            expires_at: new Date(past + 7 * 86_400_000).toISOString(),
            is_public: true,
          }),
          replies: [], reactions: [],
        } satisfies MomentDetail
        : [])
    const { findByText, queryByRole, getByRole } = render(<MomentumDetailPage />)
    await findByText('Only you see this now. It stays under Yours.')
    expect(queryByRole('button', { name: /Reply/ })).toBeNull()
    expect(queryByRole('button', { name: /Copy link/ })).toBeNull()
    expect(getByRole('button', { name: 'Delete' })).toBeTruthy()
  })
})

describe('MomentumDetailPage save', () => {
  it('offers Save on someone else’s moment and says a saved one stays after it ended', async () => {
    const detail: MomentDetail = {
      moment: moment('m-amaka', 'ru-amaka', {
        expires_at: new Date(Date.now() - 3_600_000).toISOString(),
        saved_by_me: true,
        can_save: true,
        author: { display_name: 'Amaka Okafor', picture_url: null },
      }),
      replies: [],
      reactions: [],
    }
    vi.mocked(api.get).mockImplementation(async (path: string) =>
      path === '/api/moments/m-amaka' ? detail : [])
    const { findByRole, container } = render(<MomentumDetailPage />)
    const btn = await findByRole('button', { name: /Saved/ })
    expect(btn.getAttribute('aria-pressed')).toBe('true')
    expect(container.textContent).toContain('You saved this. It stays with you until the author deletes it.')
    expect(container.textContent).not.toContain('Only you see this now')
  })

  it('never offers Save on your own moment', async () => {
    currentUser.value = { user_id: 'u-me', username: 'me' } as never
    const detail: MomentDetail = {
      moment: moment('m-amaka', 'u-me', { can_save: false }),
      replies: [],
      reactions: [],
    }
    vi.mocked(api.get).mockImplementation(async (path: string) =>
      path === '/api/moments/m-amaka' ? detail : [])
    const { findByText, container } = render(<MomentumDetailPage />)
    await findByText('hello from m-amaka')
    expect(container.querySelector('button[aria-pressed]')).toBeNull()
    currentUser.value = null
  })
})
