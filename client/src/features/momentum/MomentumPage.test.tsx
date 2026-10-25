import { describe, it, expect } from 'vitest'
import { momentumViewFromUrl } from './MomentumPage'

describe('MomentumPage module', () => {
  it('exports a default component', async () => {
    const m = await import('./MomentumPage')
    expect(typeof m.default).toBe('function')
  })
})

describe('momentumViewFromUrl', () => {
  it('Yours is addressable', () => {
    expect(momentumViewFromUrl('/momentum?tab=yours')).toEqual({ tab: 'yours', tag: null })
  })

  it('a hashtag link shows that tag under Moments', () => {
    expect(momentumViewFromUrl('/momentum?tag=walk')).toEqual({ tab: 'inbox', tag: 'walk' })
  })

  it('old Archive links still land: with a tag on the tag view, without on Yours', () => {
    expect(momentumViewFromUrl('/momentum?tab=archive&tag=walk')).toEqual({ tab: 'inbox', tag: 'walk' })
    expect(momentumViewFromUrl('/momentum?tab=archive')).toEqual({ tab: 'yours', tag: null })
  })

  it('anything else is the Moments list', () => {
    expect(momentumViewFromUrl('/momentum')).toEqual({ tab: 'inbox', tag: null })
    expect(momentumViewFromUrl('/momentum?tab=nope')).toEqual({ tab: 'inbox', tag: null })
  })
})
