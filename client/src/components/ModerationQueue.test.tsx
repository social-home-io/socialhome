import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

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

describe('ModerationQueue — ADMIN_ONLY posts (§4.3)', () => {
  beforeEach(() => { vi.resetModules() })

  it('a moderator gets Reject and a note instead of Approve', async () => {
    vi.doMock('@/api', () => ({
      api: {
        get: vi.fn(async () => ([{
          id: 'item-1', space_id: 'sp-1', feature: 'posts', action: 'create',
          submitted_by: 'uid-bob', status: 'pending', payload: { content: 'hi' },
          submitted_at: '2026-04-18T00:00:00Z', expires_at: '2026-04-25T00:00:00Z',
        }])),
        post: vi.fn(),
      },
    }))
    const { ModerationQueue } = await import('./ModerationQueue')
    const { findByText, queryByText } = render(
      <ModerationQueue spaceId="sp-1" canApprove={false} />,
    )
    expect(await findByText('Reject')).toBeTruthy()
    expect(queryByText('Approve')).toBeNull()
    expect(queryByText('Only admins can approve posts here — you can still reject them.')).toBeTruthy()
  })
})

describe('ModerationQueue — every feature (§4.3 Reviewed)', () => {
  beforeEach(() => { vi.resetModules() })

  const item = (over: Record<string, unknown>) => ({
    id: 'item-1', space_id: 'sp-1', feature: 'tasks', action: 'edit', entity: 'task', op: null,
    target_id: 't1', submitted_by: 'uid-bob', submitted_by_display: 'Bob',
    status: 'pending', submitted_at: '2026-10-01T00:00:00Z', expires_at: '2099-01-01T00:00:00Z',
    preview: {}, snapshot: null, current: null, payload: {},
    ...over,
  })

  it('a task edit shows a field table old → new and flags a field changed since', async () => {
    vi.doMock('@/api', () => ({
      api: {
        get: vi.fn(async () => [item({
          preview: { title: 'Paint the fence', status: 'done' },
          snapshot: { title: 'Paint fence', status: 'todo' },
          current: { title: 'Paint fence', status: 'in_progress' },
        })]),
        post: vi.fn(),
      },
    }))
    const { ModerationQueue } = await import('./ModerationQueue')
    const view = render(<ModerationQueue spaceId="sp-1" />)
    expect(await view.findByText(/Edit to task/)).toBeTruthy()
    const table = view.container.querySelector('table.sh-moderation-table')!
    expect(table).toBeTruthy()
    const rows = Array.from(table.querySelectorAll('tbody tr'))
    expect(rows.map(r => r.querySelector('th')!.textContent)).toEqual(['Title', 'Status'])
    expect(rows[0].querySelector('.sh-moderation-table__old')!.textContent).toBe('Paint fence')
    expect(rows[0].querySelector('.sh-moderation-table__new')!.textContent).toBe('Paint the fence')
    expect(rows[1].classList.contains('sh-moderation-table__row--stale')).toBe(true)
    expect(rows[1].textContent).toContain('Changed since it was submitted — now: In progress')
    expect(view.getByText('Bob')).toBeTruthy()
    expect(view.getByText(/^Expires /)).toBeTruthy()
  })

  it('a page edit shows a line diff of the body', async () => {
    vi.doMock('@/api', () => ({
      api: {
        get: vi.fn(async () => [item({
          feature: 'pages', entity: 'page', target_id: 'pg1',
          preview: { content: 'Intro\nNew line\nOutro' },
          snapshot: { content: 'Intro\nOld line\nOutro' },
          current: { content: 'Intro\nOld line\nOutro' },
        })]),
        post: vi.fn(),
      },
    }))
    const { ModerationQueue } = await import('./ModerationQueue')
    const view = render(<ModerationQueue spaceId="sp-1" />)
    expect(await view.findByText(/Edit to page/)).toBeTruthy()
    const added = view.container.querySelectorAll('.sh-moderation-diff__line--add')
    const removed = view.container.querySelectorAll('.sh-moderation-diff__line--del')
    expect(Array.from(added).map(l => l.textContent)).toEqual(['+Added:New line'])
    expect(Array.from(removed).map(l => l.textContent)).toEqual(['−Removed:Old line'])
    // The body isn't repeated as a table row.
    expect(view.container.querySelector('table.sh-moderation-table')).toBeNull()
  })

  it('a delete shows the row that would go', async () => {
    vi.doMock('@/api', () => ({
      api: {
        get: vi.fn(async () => [item({
          feature: 'stickies', entity: 'sticky', action: 'delete',
          preview: { content: 'Milk' }, snapshot: { content: 'Milk', color: '#FFF9B1' },
        })]),
        post: vi.fn(),
      },
    }))
    const { ModerationQueue } = await import('./ModerationQueue')
    const view = render(<ModerationQueue spaceId="sp-1" />)
    expect(await view.findByText(/Delete sticky note/)).toBeTruthy()
    expect(view.getByText('This would be deleted:')).toBeTruthy()
    expect(view.getByText('Milk')).toBeTruthy()
  })

  it('STALE → "Approve anyway" resends the approve with force: true', async () => {
    const { ApiError } = await vi.importActual<typeof import('@/api')>('@/api')
    const stale = new ApiError(409, '/approve', {
      code: 'STALE', detail: 'changed',
      current: { content: 'Someone else' }, proposed: { content: 'Mine' }, base: { content: 'Base' },
    })
    const post = vi.fn()
      .mockRejectedValueOnce(stale)
      .mockResolvedValueOnce({ item_id: 'item-1', status: 'approved', target_id: 'pg1', post_id: null })
    vi.doMock('@/api', () => ({
      ApiError,
      api: {
        get: vi.fn(async () => [item({
          feature: 'pages', entity: 'page', preview: { content: 'Mine' }, snapshot: { content: 'Base' },
        })]),
        post,
      },
    }))
    const { ModerationQueue } = await import('./ModerationQueue')
    const view = render(<ModerationQueue spaceId="sp-1" />)
    fireEvent.click(await view.findByText('Approve'))
    expect(await view.findByText('This page changed meanwhile')).toBeTruthy()
    expect(post).toHaveBeenNthCalledWith(1, '/api/spaces/sp-1/moderation/item-1/approve', {})
    fireEvent.click(view.getByText('Approve anyway'))
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2))
    expect(post).toHaveBeenNthCalledWith(2, '/api/spaces/sp-1/moderation/item-1/approve', { force: true })
  })

  it('an incomplete approve (published, attachment failed) retries once to finish it', async () => {
    const { ApiError } = await vi.importActual<typeof import('@/api')>('@/api')
    const get = vi.fn(async () => [item({ preview: { title: 'x' }, snapshot: { title: 'y' } })])
    const post = vi.fn()
      .mockResolvedValueOnce({ status: 'approved', complete: false })
      .mockResolvedValueOnce({ status: 'approved', complete: true })
    const toast = vi.fn()
    vi.doMock('./Toast', () => ({ showToast: toast }))
    vi.doMock('@/api', () => ({ ApiError, api: { get, post } }))
    const { ModerationQueue } = await import('./ModerationQueue')
    const view = render(<ModerationQueue spaceId="sp-1" />)
    fireEvent.click(await view.findByText('Approve'))
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2))
    expect((post.mock.calls[1] as unknown[])[0]).toBe((post.mock.calls[0] as unknown[])[0])
    await waitFor(() => expect(toast).toHaveBeenCalledWith('Content approved', 'success'))
  })

  it('an approve that stays incomplete warns', async () => {
    const { ApiError } = await vi.importActual<typeof import('@/api')>('@/api')
    const get = vi.fn(async () => [item({ preview: { title: 'x' }, snapshot: { title: 'y' } })])
    const post = vi.fn().mockResolvedValue({ status: 'approved', complete: false })
    const toast = vi.fn()
    vi.doMock('./Toast', () => ({ showToast: toast }))
    vi.doMock('@/api', () => ({ ApiError, api: { get, post } }))
    const { ModerationQueue } = await import('./ModerationQueue')
    const view = render(<ModerationQueue spaceId="sp-1" />)
    fireEvent.click(await view.findByText('Approve'))
    await waitFor(() => expect(toast).toHaveBeenCalledWith(
      expect.stringContaining("didn't save"), 'error',
    ))
  })

  it('409 IN_PROGRESS says someone is approving it', async () => {
    const { ApiError } = await vi.importActual<typeof import('@/api')>('@/api')
    const get = vi.fn(async () => [item({ preview: { title: 'x' }, snapshot: { title: 'y' } })])
    const post = vi.fn().mockRejectedValueOnce(new ApiError(409, '/approve', { code: 'IN_PROGRESS' }))
    const toast = vi.fn()
    vi.doMock('./Toast', () => ({ showToast: toast }))
    vi.doMock('@/api', () => ({ ApiError, api: { get, post } }))
    const { ModerationQueue } = await import('./ModerationQueue')
    const view = render(<ModerationQueue spaceId="sp-1" />)
    fireEvent.click(await view.findByText('Approve'))
    await waitFor(() => expect(toast).toHaveBeenCalledWith(
      'Someone is approving this right now.', 'info',
    ))
  })

  it('410 EXPIRED says the submission expired', async () => {
    const { ApiError } = await vi.importActual<typeof import('@/api')>('@/api')
    const get = vi.fn(async () => [item({ preview: { title: 'x' }, snapshot: { title: 'y' } })])
    const post = vi.fn().mockRejectedValueOnce(new ApiError(410, '/approve', { code: 'EXPIRED' }))
    const toast = vi.fn()
    vi.doMock('./Toast', () => ({ showToast: toast }))
    vi.doMock('@/api', () => ({ ApiError, api: { get, post } }))
    const { ModerationQueue } = await import('./ModerationQueue')
    const view = render(<ModerationQueue spaceId="sp-1" />)
    fireEvent.click(await view.findByText('Approve'))
    await waitFor(() => expect(toast).toHaveBeenCalledWith(
      'This submission expired before it was reviewed.', 'info',
    ))
  })

  it('410 TARGET_GONE toasts and refreshes the queue', async () => {
    const { ApiError } = await vi.importActual<typeof import('@/api')>('@/api')
    const get = vi.fn(async () => [item({ preview: { title: 'x' }, snapshot: { title: 'y' } })])
    const post = vi.fn().mockRejectedValueOnce(new ApiError(410, '/approve', { code: 'TARGET_GONE' }))
    const toast = vi.fn()
    vi.doMock('./Toast', () => ({ showToast: toast }))
    vi.doMock('@/api', () => ({ ApiError, api: { get, post } }))
    const { ModerationQueue } = await import('./ModerationQueue')
    const view = render(<ModerationQueue spaceId="sp-1" />)
    fireEvent.click(await view.findByText('Approve'))
    await waitFor(() => expect(toast).toHaveBeenCalledWith(
      'The item was deleted meanwhile — this change can’t be applied any more.', 'info',
    ))
    const queueGets = () =>
      get.mock.calls.filter(c => (c as unknown[])[0] === '/api/spaces/sp-1/moderation').length
    await waitFor(() => expect(queueGets()).toBe(2))
  })

  it('canApprove per feature: Approve only where the viewer may', async () => {
    vi.doMock('@/api', () => ({
      api: {
        get: vi.fn(async () => [
          item({ id: 'a', preview: { title: 'T' }, snapshot: { title: 'S' } }),
          item({ id: 'b', feature: 'posts', entity: 'post', action: 'create', preview: { content: 'hello' } }),
        ]),
        post: vi.fn(),
      },
    }))
    const { ModerationQueue } = await import('./ModerationQueue')
    const view = render(<ModerationQueue spaceId="sp-1" canApprove={f => f !== 'tasks'} />)
    await view.findByText('hello')
    expect(view.getAllByText('Approve')).toHaveLength(1)
    expect(view.getAllByText('Reject')).toHaveLength(2)
    expect(view.getByText('Only admins can approve this here — you can still reject it.')).toBeTruthy()
  })
})
