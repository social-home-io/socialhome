import { describe, it, expect, afterEach } from 'vitest'

import { confirmLeave, setNavigationGuard, shouldConfirmLeave } from './navigationGuard'

let release: (() => void) | null = null
afterEach(() => { release?.(); release = null })

describe('navigationGuard', () => {
  it('never asks without a guard, and confirmLeave resolves true', async () => {
    expect(shouldConfirmLeave('/feed')).toBe(false)
    expect(await confirmLeave()).toBe(true)
  })

  it('asks only for a different path while dirty', () => {
    let dirty = false
    release = setNavigationGuard({
      path: '/settings', isDirty: () => dirty, confirm: async () => true,
    })
    expect(shouldConfirmLeave('/feed')).toBe(false)
    dirty = true
    expect(shouldConfirmLeave('/feed')).toBe(true)
    expect(shouldConfirmLeave('/settings')).toBe(false)
    expect(shouldConfirmLeave('/settings/')).toBe(false)
    expect(shouldConfirmLeave('/settings#privacy')).toBe(false)
    expect(shouldConfirmLeave('/settings?tab=x')).toBe(false)
    expect(shouldConfirmLeave('/settings-other')).toBe(true)
  })

  it('confirmLeave returns the page answer', async () => {
    release = setNavigationGuard({
      path: '/settings', isDirty: () => true, confirm: async () => false,
    })
    expect(await confirmLeave()).toBe(false)
  })

  it('a stale unregister does not remove a newer guard', () => {
    const first = setNavigationGuard({ path: '/a', isDirty: () => true, confirm: async () => true })
    release = setNavigationGuard({ path: '/b', isDirty: () => true, confirm: async () => true })
    first()
    expect(shouldConfirmLeave('/c')).toBe(true)
  })
})
