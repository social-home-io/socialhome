import { describe, it, expect, vi } from 'vitest'
import { render, waitFor } from '@testing-library/preact'

const { get } = vi.hoisted(() => ({ get: vi.fn() }))
vi.mock('@/api', () => ({ api: { get } }))
vi.mock('@/ws', () => ({ ws: { on: () => () => {} } }))
vi.mock('preact-iso', () => ({
  useRoute: () => ({ params: { id: 'conv-1' } }),
  useLocation: () => ({ route: vi.fn() }),
}))

describe('CallHistoryPane', () => {
  it('exports a default component', async () => {
    get.mockResolvedValue({ calls: [] })
    const mod = await import('./CallHistoryPane')
    expect(mod.default).toBeTruthy()
    expect(typeof mod.default).toBe('function')
  })

  it('names a caller from another household from the server, else "Someone"', async () => {
    const row = (id: string, over: Record<string, unknown>) => ({
      call_id: id, conversation_id: 'conv-1', initiator_user_id: `ru-${id}`,
      callee_user_id: null, call_type: 'audio', status: 'ended',
      participant_user_ids: [], started_at: '2026-04-18T10:00:00Z',
      connected_at: null, ended_at: null, duration_seconds: null,
      avg_rtt_ms: null, avg_loss_pct: null, ...over,
    })
    get.mockImplementation(async (url: string) => (url === '/api/conversations/conv-1/calls'
      ? {
        calls: [
          row('a', { initiator_display_name: 'Remote Rita' }),
          row('b', { initiator_display_name: null }),
        ],
      }
      : []))
    const { default: CallHistoryPane } = await import('./CallHistoryPane')
    const view = render(<CallHistoryPane />)
    await waitFor(() => expect(view.container.querySelectorAll('.sh-call-who').length).toBe(2))
    const names = [...view.container.querySelectorAll('.sh-call-who')].map(n => n.textContent)
    expect(names).toEqual(['Remote Rita', 'Someone'])
    expect(view.container.textContent).not.toContain('ru-b')
  })
})
