import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, act, waitFor, fireEvent } from '@testing-library/preact'

const { handlers, apiGet, apiPost, apiDelete } = vi.hoisted(() => ({
  handlers: new Map<string, (e: { type: string; data: Record<string, unknown> }) => void>(),
  apiGet: vi.fn(),
  apiPost: vi.fn(async () => ({ retried: 1 })),
  apiDelete: vi.fn(async () => ({ discarded: 1 })),
}))
vi.mock('@/ws', () => ({
  ws: {
    on: (type: string, fn: (e: { type: string; data: Record<string, unknown> }) => void) => {
      handlers.set(type, fn)
      return () => { handlers.delete(type) }
    },
  },
}))
vi.mock('@/api', () => ({
  api: {
    get: (...a: unknown[]) => apiGet(...a),
    post: (...a: unknown[]) => apiPost(...(a as [])),
    delete: (...a: unknown[]) => apiDelete(...(a as [])),
  },
}))

import { SpaceUnsentBanner, unsentReasons, type UnsentItem } from './SpaceUnsentBanner'

const item = (over: Partial<UnsentItem> = {}): UnsentItem => ({
  item_id: 'i', event_type: 'space_chat_message_created', error: 'refused',
  created_at: '2026-11-27T10:00:00Z', retryable: true, count: 1, ...over,
})

const banner = (c: Element) => c.querySelector('[data-testid="space-unsent-banner"]')

beforeEach(() => {
  handlers.clear()
  apiGet.mockReset()
  apiPost.mockClear()
  apiDelete.mockClear()
})

describe('SpaceUnsentBanner', () => {
  it('shows nothing when nothing failed', async () => {
    apiGet.mockResolvedValue({ items: [] })
    const { container } = render(<SpaceUnsentBanner spaceId="s1" spaceName="Garden" />)
    await waitFor(() => expect(apiGet).toHaveBeenCalledWith('/api/spaces/s1/unsent'))
    expect(banner(container)).toBeNull()
  })

  it('names the space and why, and Try again sends them again', async () => {
    apiGet.mockResolvedValueOnce({ items: [item(), item({ item_id: 'j' })] })
    const { container, getByTestId } = render(<SpaceUnsentBanner spaceId="s1" spaceName="Garden" />)
    await waitFor(() => expect(banner(container)).not.toBeNull())
    expect(banner(container)?.textContent).toContain('Garden')
    expect(banner(container)?.getAttribute('role')).toBe('alert')
    apiGet.mockResolvedValueOnce({ items: [] })
    await act(async () => { fireEvent.click(getByTestId('space-unsent-retry')) })
    expect(apiPost).toHaveBeenCalledWith('/api/spaces/s1/unsent')
    await waitFor(() => expect(banner(container)).toBeNull())
  })

  it('offers only Discard when nothing kept its bytes', async () => {
    apiGet.mockResolvedValueOnce({ items: [item({ error: 'too_large', retryable: false })] })
    const { container, getByTestId, queryByTestId } = render(
      <SpaceUnsentBanner spaceId="s1" spaceName="Garden" />,
    )
    await waitFor(() => expect(banner(container)).not.toBeNull())
    expect(queryByTestId('space-unsent-retry')).toBeNull()
    apiGet.mockResolvedValueOnce({ items: [] })
    await act(async () => { fireEvent.click(getByTestId('space-unsent-discard')) })
    expect(apiDelete).toHaveBeenCalledWith('/api/spaces/s1/unsent')
  })

  it('reloads on a space.unsent frame for this space only', async () => {
    apiGet.mockResolvedValue({ items: [] })
    render(<SpaceUnsentBanner spaceId="s1" spaceName="Garden" />)
    await waitFor(() => expect(apiGet).toHaveBeenCalledTimes(1))
    act(() => { handlers.get('space.unsent')?.({ type: 'space.unsent', data: { space_id: 'other' } }) })
    expect(apiGet).toHaveBeenCalledTimes(1)
    act(() => { handlers.get('space.unsent')?.({ type: 'space.unsent', data: { space_id: 's1' } }) })
    await waitFor(() => expect(apiGet).toHaveBeenCalledTimes(2))
  })

  it('counts each reason, a full queue by its refusals', () => {
    const lines = unsentReasons([
      item(), item({ error: 'queue_full', count: 3, retryable: false }),
      item({ error: 'weird' }),
    ])
    expect(lines).toHaveLength(2)
    expect(lines[0]).toContain('2')
    expect(lines[1]).toContain('3')
  })
})
