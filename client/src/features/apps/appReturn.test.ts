import { describe, it, expect, beforeEach } from 'vitest'
import {
  _resetAppReturnForTests, appOpenedFrom, isAppHostPath, noteAppRoute,
} from './appReturn'

describe('appReturn', () => {
  beforeEach(() => _resetAppReturnForTests())

  it('knows the app host path from the Apps page', () => {
    expect(isAppHostPath('/apps/chess')).toBe(true)
    expect(isAppHostPath('/apps/chess?session=s1')).toBe(true)
    expect(isAppHostPath('/apps')).toBe(false)
    expect(isAppHostPath('/apps/chess/more')).toBe(false)
  })

  it('remembers the page an app was opened from', () => {
    noteAppRoute('/notifications')
    noteAppRoute('/apps/chess')
    expect(appOpenedFrom()).toBe('/notifications')
  })

  it('a deep link (the app is the first page) has nowhere to go back to', () => {
    noteAppRoute('/apps/chess')
    expect(appOpenedFrom()).toBeNull()
  })

  it('says nothing once the app is left', () => {
    noteAppRoute('/apps')
    noteAppRoute('/apps/chess')
    noteAppRoute('/apps')
    expect(appOpenedFrom()).toBeNull()
  })

  it('a repeated report of the same URL keeps the origin', () => {
    noteAppRoute('/apps')
    noteAppRoute('/apps/chess')
    noteAppRoute('/apps/chess')
    expect(appOpenedFrom()).toBe('/apps')
  })
})
