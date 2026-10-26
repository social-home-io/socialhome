import { describe, it, expect, beforeEach } from 'vitest'
import { _resetMomentumListForTests, backIsYours, momentumBackTarget, noteMomentumList } from './backLink'

describe('backLink', () => {
  beforeEach(() => _resetMomentumListForTests())

  it('defaults to Moments', () => {
    expect(momentumBackTarget()).toBe('/momentum')
  })

  it('keeps the tab and tag of the last list', () => {
    noteMomentumList('/momentum?tag=garten')
    expect(momentumBackTarget()).toBe('/momentum?tag=garten')
    noteMomentumList('/momentum/yours?show=saved')
    expect(momentumBackTarget()).toBe('/momentum/yours?show=saved')
    expect(backIsYours(momentumBackTarget())).toBe(true)
    expect(backIsYours('/momentum?tag=x')).toBe(false)
  })

  it('ignores anything that is not a Momentum list', () => {
    noteMomentumList('/feed')
    expect(momentumBackTarget()).toBe('/momentum')
    noteMomentumList('/momentum/m-1')
    expect(momentumBackTarget()).toBe('/momentum')
  })
})
