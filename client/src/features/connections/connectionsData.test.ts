import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

const apiGet = vi.fn()
vi.mock('@/api', () => ({
  api: { get: (...args: unknown[]) => apiGet(...args) },
}))

import {
  INBOX_RELOAD_GAP_MS,
  autoPairRequests,
  cancelAutoPairReload,
  loadAutoPairRequests,
  reloadAutoPairRequestsSoon,
} from './connectionsData'

const req = (id: string) => ({
  request_id: id, from_a_id: 'a', from_a_display: 'A', via_b_id: 'b',
  via_b_display: 'B', ts: '', received_at: '',
})

const INBOX = '/api/pairing/auto-pair-requests'

let clock = Date.now()

beforeEach(() => {
  vi.useFakeTimers()
  // Start every test well past the previous one's last read.
  clock += 10 * 60_000
  vi.setSystemTime(clock)
  apiGet.mockReset()
  autoPairRequests.value = []
})

afterEach(() => {
  vi.runOnlyPendingTimers()
  vi.useRealTimers()
})

describe('reloadAutoPairRequestsSoon — a burst of WS events', () => {
  it('reads at once, then folds the rest of a burst into ONE trailing read', async () => {
    // A room of households asking at once fires one
    // ``pairing.auto_pair_requested`` each; a read per event spent the
    // inbox's rate-limit bucket in one burst.
    apiGet.mockResolvedValueOnce([req('r1')]).mockResolvedValueOnce([req('r1'), req('r2')])
    for (let i = 0; i < 20; i++) reloadAutoPairRequestsSoon()
    expect(apiGet).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(INBOX_RELOAD_GAP_MS - 1)
    expect(apiGet).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(apiGet).toHaveBeenCalledTimes(2)
    expect(apiGet).toHaveBeenLastCalledWith(INBOX)
    // The list ends up current.
    expect(autoPairRequests.value.map((r) => r.request_id)).toEqual(['r1', 'r2'])
  })

  it('a steady stream reads at most once per gap — under the 30 / min bucket', async () => {
    apiGet.mockResolvedValue([])
    for (let ms = 0; ms < 60_000; ms += 250) {
      reloadAutoPairRequestsSoon()
      await vi.advanceTimersByTimeAsync(250)
    }
    expect(apiGet.mock.calls.length).toBeLessThanOrEqual(60_000 / INBOX_RELOAD_GAP_MS + 1)
    expect(apiGet.mock.calls.length).toBeLessThan(30)
  })

  it('a read that never answers does not stall later ones', async () => {
    apiGet.mockReturnValueOnce(new Promise(() => {})).mockResolvedValueOnce([req('r3')])
    reloadAutoPairRequestsSoon()
    await vi.advanceTimersByTimeAsync(INBOX_RELOAD_GAP_MS)
    reloadAutoPairRequestsSoon()
    await vi.advanceTimersByTimeAsync(0)
    expect(apiGet).toHaveBeenCalledTimes(2)
    expect(autoPairRequests.value.map((r) => r.request_id)).toEqual(['r3'])
  })
})

describe('cancelAutoPairReload', () => {
  it('drops a pending trailing read when the page leaves', async () => {
    apiGet.mockResolvedValue([])
    reloadAutoPairRequestsSoon()
    reloadAutoPairRequestsSoon()
    cancelAutoPairReload()
    await vi.advanceTimersByTimeAsync(INBOX_RELOAD_GAP_MS * 2)
    expect(apiGet).toHaveBeenCalledTimes(1)
  })
})

describe('reloadAutoPairRequestsSoon — the wall clock', () => {
  it('a clock set back never holds the inbox longer than one gap', async () => {
    apiGet.mockResolvedValue([])
    reloadAutoPairRequestsSoon()
    vi.setSystemTime(Date.now() - 3_600_000)
    reloadAutoPairRequestsSoon()
    await vi.advanceTimersByTimeAsync(INBOX_RELOAD_GAP_MS)
    expect(apiGet).toHaveBeenCalledTimes(2)
  })
})

describe('loadAutoPairRequests', () => {
  it('a failed read keeps the last inbox', async () => {
    autoPairRequests.value = [req('kept')]
    apiGet.mockRejectedValueOnce(new Error('429'))
    await loadAutoPairRequests()
    expect(autoPairRequests.value.map((r) => r.request_id)).toEqual(['kept'])
  })
})
