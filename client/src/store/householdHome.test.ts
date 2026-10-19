import { describe, it, expect, vi, beforeEach } from 'vitest'

const { get } = vi.hoisted(() => ({ get: vi.fn() }))
vi.mock('@/api', () => ({ api: { get } }))

import { ensureHouseholdHome, householdHome, resetHouseholdHome } from './householdHome'
import { selfLat } from './connections'

beforeEach(() => {
  get.mockReset()
  resetHouseholdHome()
})

describe('householdHome', () => {
  it('is unknown until read, then the household home', async () => {
    get.mockResolvedValue({ instance: { home_lat: 47.3769, home_lon: 8.5417 } })
    expect(householdHome.value).toBeUndefined()
    await ensureHouseholdHome()
    expect(get).toHaveBeenCalledWith('/api/friends')
    expect(householdHome.value).toEqual({ lat: 47.3769, lon: 8.5417 })
  })

  it('no home set → null', async () => {
    get.mockResolvedValue({ instance: { home_lat: null, home_lon: null } })
    await ensureHouseholdHome()
    expect(householdHome.value).toBeNull()
  })

  it('a failed read counts as no home known', async () => {
    get.mockRejectedValue(new Error('offline'))
    await ensureHouseholdHome()
    expect(householdHome.value).toBeNull()
  })

  it('reads once; callers share it', async () => {
    get.mockResolvedValue({ instance: { home_lat: 1, home_lon: 2 } })
    await Promise.all([ensureHouseholdHome(), ensureHouseholdHome()])
    await ensureHouseholdHome()
    expect(get).toHaveBeenCalledTimes(1)
  })

  it('follows the live home the Connections map keeps (local.home_changed)', async () => {
    get.mockResolvedValue({ instance: {} })
    await ensureHouseholdHome()
    expect(householdHome.value).toBeNull()
    selfLat.value = 3
    expect(householdHome.value).toBeNull() // lon still missing
  })
})
