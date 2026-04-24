import { describe, it, expect } from 'vitest'

describe('BazaarOffersPanel', () => {
  it('module exports exist', async () => {
    const mod = await import('./BazaarOffersPanel')
    expect(mod).toBeTruthy()
    expect(typeof mod.BazaarOffersPanel).toBe('function')
  })
})
