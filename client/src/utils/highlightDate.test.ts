import { describe, it, expect, vi, afterEach } from 'vitest'

const loc = vi.hoisted(() => ({ value: 'en' }))
vi.mock('@/i18n/i18n', async (orig) => ({
  ...(await orig<typeof import('@/i18n/i18n')>()),
  formatLocale: () => loc.value,
}))

import { highlightDateLabel } from './highlightDate'

afterEach(() => { loc.value = 'en' })

describe('highlightDateLabel', () => {
  it('spells the day out in the UI locale', () => {
    expect(highlightDateLabel('2026-10-19')).toBe('October 19, 2026')
    loc.value = 'de'
    expect(highlightDateLabel('2026-10-19')).toBe('19. Oktober 2026')
  })

  it('reads the key in UTC: the 1st stays the 1st west of UTC', () => {
    expect(highlightDateLabel('2026-11-01')).toBe('November 1, 2026')
  })

  it('anything else is shown as it came', () => {
    expect(highlightDateLabel('')).toBe('')
    expect(highlightDateLabel('soon')).toBe('soon')
  })
})
