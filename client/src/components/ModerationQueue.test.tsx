import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render } from '@testing-library/preact'

describe('ModerationQueue', () => {
  beforeEach(() => {
    vi.resetModules()
  })

  it('fetches /api/spaces/{id}/moderation and lists pending items', async () => {
    vi.doMock('@/api', () => ({
      api: {
        get: vi.fn(async () => ([{
          id: 'item-1', space_id: 'sp-1',
          feature: 'posts', action: 'create',
          submitted_by: 'uid-bob', status: 'pending',
          payload: { content: 'hi' },
          submitted_at: '2026-04-18T00:00:00Z',
          expires_at:   '2026-04-25T00:00:00Z',
        }])),
        post: vi.fn(),
      },
    }))
    const { ModerationQueue } = await import('./ModerationQueue')
    const { findByText } = render(<ModerationQueue spaceId="sp-1" />)
    expect(await findByText('Approve')).toBeTruthy()
    // The submitter falls back to the user_id when the household
    // roster doesn't have a display name for the user.
    expect(await findByText(/uid-bob/)).toBeTruthy()
  })

  it('renders the empty state when the queue returns []', async () => {
    vi.doMock('@/api', () => ({
      api: {
        get: vi.fn(async () => ([])),
        post: vi.fn(),
      },
    }))
    const { ModerationQueue } = await import('./ModerationQueue')
    const { findByText } = render(<ModerationQueue spaceId="sp-1" />)
    expect(await findByText(/Nothing pending/)).toBeTruthy()
  })

  it('shows an alert region when the fetch fails', async () => {
    vi.doMock('@/api', () => ({
      api: {
        get: vi.fn(async () => {
          throw new Error('boom')
        }),
        post: vi.fn(),
      },
    }))
    const { ModerationQueue } = await import('./ModerationQueue')
    const { findByRole } = render(<ModerationQueue spaceId="sp-1" />)
    const alert = await findByRole('alert')
    expect(alert.textContent).toContain('boom')
  })

  describe('live space.moderation.* frames', () => {
    type Handler = (e: { type: string; data: Record<string, unknown> }) => void
    const handlers = new Map<string, Set<Handler>>()
    const emit = (type: string, data: Record<string, unknown>) =>
      handlers.get(type)?.forEach(h => h({ type, data: { type, ...data } }))
    const row = (id: string, content: string) => ({
      id, space_id: 'sp-1', feature: 'posts', action: 'create',
      submitted_by: 'uid-bob', status: 'pending', payload: { content },
      submitted_at: '2026-04-18T00:00:00Z', expires_at: '2026-04-25T00:00:00Z',
    })

    beforeEach(() => {
      handlers.clear()
      vi.doMock('@/ws', () => ({
        ws: {
          on: (type: string, h: Handler) => {
            if (!handlers.has(type)) handlers.set(type, new Set())
            handlers.get(type)!.add(h)
            return () => { handlers.get(type)?.delete(h) }
          },
        },
      }))
    })

    it.each([
      'space.moderation.queued',
      'space.moderation.approved',
      'space.moderation.rejected',
    ])('%s for this space refetches the queue', async (type) => {
      let rows = [row('item-1', 'first post')]
      const get = vi.fn(async () => rows)
      vi.doMock('@/api', () => ({ api: { get, post: vi.fn() } }))
      const { ModerationQueue } = await import('./ModerationQueue')
      const view = render(<ModerationQueue spaceId="sp-1" />)
      await view.findByText('first post')
      rows = [row('item-2', 'second post')]
      emit(type, { space_id: 'sp-1', item: { id: 'item-2' } })
      expect(await view.findByText('second post')).toBeTruthy()
      expect(view.queryByText('first post')).toBeNull()
      expect(get).toHaveBeenLastCalledWith('/api/spaces/sp-1/moderation')
    })

    it('ignores a frame for another space and keeps rows on a failed refresh', async () => {
      const get = vi.fn(async () => [row('item-1', 'first post')])
      const queueGets = () =>
        get.mock.calls.filter(c => (c as unknown[])[0] === '/api/spaces/sp-1/moderation').length
      vi.doMock('@/api', () => ({ api: { get, post: vi.fn() } }))
      const { ModerationQueue } = await import('./ModerationQueue')
      const view = render(<ModerationQueue spaceId="sp-1" />)
      await view.findByText('first post')
      emit('space.moderation.queued', { space_id: 'sp-2' })
      expect(queueGets()).toBe(1)
      get.mockRejectedValueOnce(new Error('offline'))
      emit('space.moderation.queued', { space_id: 'sp-1' })
      await new Promise(r => setTimeout(r, 0))
      expect(queueGets()).toBe(2)
      expect(view.getByText('first post')).toBeTruthy()
      expect(view.queryByRole('alert')).toBeNull()
    })
  })
})
