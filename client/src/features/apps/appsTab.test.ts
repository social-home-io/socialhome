import { describe, it, expect, beforeEach, vi } from 'vitest'
import { pickTab, rememberTab, rememberedTab } from './appsTab'

const none = new Set<string>()
const base = { query: null, picked: null, installedIds: none, catalogIds: none, nothingAdded: false }

beforeEach(() => localStorage.clear())

describe('pickTab', () => {
  it('?tab= deep-links a view', () => {
    rememberTab('yours')
    expect(pickTab({ ...base, query: 'store' })).toBe('store')
    expect(pickTab({ ...base, query: 'yours', nothingAdded: true })).toBe('yours')
  })

  it('ignores an unknown ?tab=', () => {
    expect(pickTab({ ...base, query: 'catalog' })).toBe('yours')
  })

  it('?app= opens the view that holds the app', () => {
    const installedIds = new Set(['chess'])
    const catalogIds = new Set(['chess', 'go'])
    expect(pickTab({ ...base, picked: 'chess', installedIds, catalogIds })).toBe('yours')
    expect(pickTab({ ...base, picked: 'go', installedIds, catalogIds })).toBe('store')
  })

  it('remembers the view picked last', () => {
    rememberTab('store')
    expect(rememberedTab()).toBe('store')
    expect(pickTab(base)).toBe('store')
  })

  it('a household with nothing added starts in the Store', () => {
    expect(pickTab({ ...base, nothingAdded: true })).toBe('store')
    expect(pickTab(base)).toBe('yours')
  })

  it('works without browser storage', () => {
    const get = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('blocked') })
    const set = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('blocked') })
    expect(() => rememberTab('store')).not.toThrow()
    expect(rememberedTab()).toBe(null)
    expect(pickTab(base)).toBe('yours')
    get.mockRestore()
    set.mockRestore()
  })
})
