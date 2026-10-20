import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor } from '@testing-library/preact'

vi.mock('@/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))
vi.mock('preact-iso', () => ({ useLocation: () => ({ route: vi.fn(), url: '/' }) }))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))
vi.mock('@/ws', () => ({ ws: { on: () => () => undefined } }))

import { api } from '@/api'
import type { Moment } from '@/types'
import MomentumInboxTab from './MomentumInboxTab'

function moment(id: string, author: string, extra: Partial<Moment> = {}): Moment {
  return {
    id, author_user_id: author, content: `hello from ${id}`,
    media_url: null, media_type: null, duration_ms: null, parent_moment_id: null,
    origin_instance_id: 'peer-o', created_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + 86_400_000).toISOString(),
    reaction_count: 0, reply_count: 0, ...extra,
  }
}

describe('MomentumInboxTab authors', () => {
  beforeEach(() => {
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path === '/api/moments') {
        return [
          moment('m-amaka', 'ru-amaka', {
            author: {
              display_name: 'Amaka Okafor',
              picture_url: 'api/users/ru-amaka/picture?v=1&sig=s',
            },
          }),
          moment('m-kid', 'uid-kid', {
            author: { display_name: 'Kid', picture_url: null },
          }),
          moment('m-ghost', 'ru-forgotten', {
            author: { display_name: null, picture_url: null },
          }),
        ]
      }
      return []
    })
  })

  it('names a friend from another household by the server\'s author', async () => {
    const { container } = render(<MomentumInboxTab />)
    await waitFor(() => expect(container.textContent).toContain('Amaka Okafor'))
    const authors = [...container.querySelectorAll('.sh-momentum-row-author')]
      .map(e => e.textContent)
    expect(authors).toEqual(['Amaka Okafor', 'Kid', 'Someone'])
    expect(container.textContent).not.toContain('ru-amaka')
    expect(container.textContent).not.toContain('ru-forgotten')
    const pictures = [...container.querySelectorAll('.sh-momentum-row > img')]
      .map(i => i.getAttribute('src'))
    // Only Amaka's picture: the guardian-blocked kid's is withheld.
    expect(pictures).toEqual(['api/users/ru-amaka/picture?v=1&sig=s'])
  })
})
