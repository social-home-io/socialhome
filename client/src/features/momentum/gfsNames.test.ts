import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('@/api', () => ({ api: { get: vi.fn() } }))

import { api } from '@/api'
import type { MomentPublicRegistration } from '@/types'
import { gfsNames, loadGfsNames, publicOnLabel, _resetGfsNamesForTests } from './gfsNames'

const reg = (gfs_id: string) => ({ gfs_id, default_share: false }) as MomentPublicRegistration

describe('gfsNames', () => {
  beforeEach(() => {
    _resetGfsNamesForTests()
    vi.mocked(api.get).mockReset()
  })

  it('reads the connection names once', async () => {
    vi.mocked(api.get).mockResolvedValue([{ id: 'g1', display_name: 'Demo GFS' }])
    await loadGfsNames()
    await loadGfsNames()
    expect(api.get).toHaveBeenCalledTimes(1)
    expect(gfsNames.value).toEqual({ g1: 'Demo GFS' })
  })

  it('a failed read can be tried again', async () => {
    vi.mocked(api.get).mockRejectedValueOnce(new Error('offline'))
    await loadGfsNames()
    vi.mocked(api.get).mockResolvedValue([])
    await loadGfsNames()
    expect(api.get).toHaveBeenCalledTimes(2)
  })

  it('names the GFS a moment is public on', () => {
    gfsNames.value = { g1: 'Demo GFS' }
    expect(publicOnLabel([reg('g1')])).toBe('Public on Demo GFS')
    expect(publicOnLabel([reg('g1')], true)).toBe('Also public on Demo GFS')
    expect(publicOnLabel([reg('g9')])).toBe('Public on your GFS')
    expect(publicOnLabel([])).toBe('Public on your GFS')
    expect(publicOnLabel([reg('g1'), reg('g2')])).toBe('Public on your 2 GFS servers')
  })
})
