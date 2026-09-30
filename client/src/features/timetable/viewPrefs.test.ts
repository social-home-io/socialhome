import { describe, it, expect, beforeEach, vi } from 'vitest'
import { loadViewPrefs, saveViewPrefs, DEFAULT_VIEW_PREFS } from './viewPrefs'

describe('timetable view prefs', () => {
  beforeEach(() => localStorage.clear())

  it('defaults when nothing is stored', () => {
    expect(loadViewPrefs('a')).toEqual(DEFAULT_VIEW_PREFS)
  })

  it('round-trips per timetable', () => {
    saveViewPrefs('a', { layout: 'timeline', picture: true, list: false })
    expect(loadViewPrefs('a')).toEqual({ layout: 'timeline', picture: true, list: false })
    expect(loadViewPrefs('b')).toEqual(DEFAULT_VIEW_PREFS)
  })

  it('ignores garbage and survives a throwing storage', () => {
    localStorage.setItem('sh-timetable-view:a', '{"layout":"grid","picture":"yes"}')
    expect(loadViewPrefs('a')).toEqual(DEFAULT_VIEW_PREFS)
    const spy = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    const set = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    expect(loadViewPrefs('a')).toEqual(DEFAULT_VIEW_PREFS)
    expect(() => saveViewPrefs('a', DEFAULT_VIEW_PREFS)).not.toThrow()
    spy.mockRestore()
    set.mockRestore()
  })
})
