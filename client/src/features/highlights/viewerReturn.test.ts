import { describe, it, expect, beforeEach } from 'vitest'
import {
  _resetViewerReturnForTests, isViewerPath, noteViewerRoute, viewerOpenedFrom,
} from './viewerReturn'

describe('viewerReturn — close goes back where you came from (C8)', () => {
  beforeEach(() => _resetViewerReturnForTests())

  it('knows the viewer from the other Highlights pages', () => {
    expect(isViewerPath('/highlights/h1')).toBe(true)
    expect(isViewerPath('/highlights/h1?via_post=p1')).toBe(true)
    expect(isViewerPath('/highlights')).toBe(false)
    expect(isViewerPath('/highlights/new')).toBe(false)
    expect(isViewerPath('/highlights/archive')).toBe(false)
  })

  it('remembers the page before the viewer, across people', () => {
    noteViewerRoute('/spaces/s1')
    noteViewerRoute('/highlights/h1?via_post=p1')
    expect(viewerOpenedFrom()).toBe('/spaces/s1')
    noteViewerRoute('/highlights/h2')
    expect(viewerOpenedFrom()).toBe('/spaces/s1')
  })

  it('a deep link opened from nowhere has nothing to go back to', () => {
    noteViewerRoute('/highlights/h1')
    expect(viewerOpenedFrom()).toBeNull()
  })

  it('outside the viewer there is no return target', () => {
    noteViewerRoute('/')
    noteViewerRoute('/highlights/h1')
    noteViewerRoute('/feed')
    expect(viewerOpenedFrom()).toBeNull()
    noteViewerRoute('/highlights/h3')
    expect(viewerOpenedFrom()).toBe('/feed')
  })
})
