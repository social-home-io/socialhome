import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor } from '@testing-library/preact'

const apiGet = vi.fn()
vi.mock('@/api', () => ({
  api: {
    get: (...args: unknown[]) => apiGet(...args),
    post: vi.fn(), patch: vi.fn(), delete: vi.fn(),
  },
}))

type Handler = (e: { type: string; data: Record<string, unknown> }) => void
const handlers = new Map<string, Set<Handler>>()
vi.mock('@/ws', () => ({
  ws: {
    on: (type: string, h: Handler) => {
      if (!handlers.has(type)) handlers.set(type, new Set())
      handlers.get(type)!.add(h)
      return () => { handlers.get(type)?.delete(h) }
    },
  },
}))
function emit(type: string, data: Record<string, unknown>) {
  handlers.get(type)?.forEach(h => h({ type, data: { type, ...data } }))
}

const showToast = vi.fn()
vi.mock('@/components/Toast', () => ({ showToast: (...a: unknown[]) => showToast(...a) }))

function bot(id: string, name: string) {
  return {
    bot_id: id, space_id: 'sp-1', scope: 'space', slug: id, name,
    icon: '🔔', created_by: 'u1', created_at: '2026-09-01T00:00:00Z',
  }
}

let rows: Array<Record<string, unknown>>
beforeEach(() => {
  handlers.clear()
  showToast.mockReset()
  apiGet.mockReset()
  rows = [bot('b1', 'Doorbell')]
  apiGet.mockImplementation(async () => rows)
})

async function renderTab() {
  const { SpaceBotsTab } = await import('./SpaceBotsTab')
  const view = render(
    <SpaceBotsTab spaceId="sp-1" canAdmin currentUserId="u1"
                  botEnabled onBotEnabledChange={() => {}} />,
  )
  await view.findByText('Doorbell')
  return view
}

describe('SpaceBotsTab live bot frames', () => {
  it.each([
    'space.bot.created', 'space.bot.updated',
    'space.bot.deleted', 'space.bot.token_rotated',
  ])('%s for this space refetches the bot list', async (type) => {
    const view = await renderTab()
    rows = [bot('b2', 'Laundry')]
    emit(type, { space_id: 'sp-1', bot_id: 'b2' })
    expect(await view.findByText('Laundry')).toBeTruthy()
    expect(view.queryByText('Doorbell')).toBeNull()
    expect(apiGet).toHaveBeenLastCalledWith('/api/spaces/sp-1/bots')
  })

  it('ignores a bot frame for another space', async () => {
    await renderTab()
    emit('space.bot.created', { space_id: 'sp-2', bot_id: 'bx' })
    expect(apiGet).toHaveBeenCalledTimes(1)
  })

  it('a failed live refresh keeps the list and shows no error toast', async () => {
    const view = await renderTab()
    apiGet.mockRejectedValueOnce(new Error('offline'))
    emit('space.bot.deleted', { space_id: 'sp-1', bot_id: 'b1' })
    await waitFor(() => expect(apiGet).toHaveBeenCalledTimes(2))
    expect(view.getByText('Doorbell')).toBeTruthy()
    expect(showToast).not.toHaveBeenCalled()
  })

  it('stops listening after unmount', async () => {
    const view = await renderTab()
    view.unmount()
    emit('space.bot.created', { space_id: 'sp-1', bot_id: 'b2' })
    expect(apiGet).toHaveBeenCalledTimes(1)
  })
})
