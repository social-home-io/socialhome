/**
 * DrawResultPage — "Your gift draw is ready" opens Social Home's own
 * result sheet on its own address (v_78); Done goes to Apps.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'

const h = vi.hoisted(() => ({ route: vi.fn(), getDrawResult: vi.fn() }))

vi.mock('preact-iso', () => ({
  useRoute: () => ({ params: { appId: 'gift-draw', sessionId: 't1' } }),
  useLocation: () => ({ route: h.route, path: '/apps/gift-draw/draw/t1', url: '/apps/gift-draw/draw/t1', query: {} }),
}))
vi.mock('../engineBridge', () => ({ getDrawResult: h.getDrawResult }))

import DrawResultPage from './DrawResultPage'
import { DONE_ARM_MS } from './DrawResultSheet'

describe('DrawResultPage', () => {
  it('shows the sheet for its table and goes to Apps on Done', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    h.getDrawResult.mockResolvedValue({
      app_id: 'gift-draw', session_id: 't1', name: 'Ben', household: 'Familie Okafor',
      wish: '', wish_updated: false, draw_name: '', budget: '', day: null, again: 0, handed_over_by: null,
    })
    const { findByText, getByText } = render(<DrawResultPage />)
    await findByText('Ben')
    expect(h.getDrawResult).toHaveBeenCalledWith('gift-draw', 't1')
    vi.setSystemTime(Date.now() + DONE_ARM_MS)
    fireEvent.click(getByText('Done'))
    expect(h.route).toHaveBeenCalledWith('/apps')
    vi.useRealTimers()
  })
})
