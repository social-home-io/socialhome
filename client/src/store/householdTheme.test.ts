import { describe, it, expect, vi, beforeEach } from 'vitest'

const get = vi.fn()
vi.mock('@/api', () => ({ api: { get: (...a: unknown[]) => get(...a) } }))

import { householdFont, householdFontStack, loadHouseholdTheme } from './householdTheme'

const root = document.documentElement

beforeEach(() => {
  get.mockReset()
  householdFont.value = 'system'
})

describe('householdTheme store', () => {
  it('loads the household font and paints it as --hh-font app-wide', async () => {
    get.mockResolvedValue({ font_family: 'serif' })
    await loadHouseholdTheme()
    expect(get).toHaveBeenCalledWith('/api/theme')
    expect(householdFont.value).toBe('serif')
    expect(root.style.getPropertyValue('--hh-font')).toContain('Georgia')
    expect(householdFontStack.value).toContain('Georgia')
  })

  it('"system" is no override: --hh-font falls back to the app font', async () => {
    householdFont.value = 'mono'
    expect(root.style.getPropertyValue('--hh-font')).toContain('monospace')
    get.mockResolvedValue({ font_family: 'system' })
    await loadHouseholdTheme()
    expect(root.style.getPropertyValue('--hh-font')).toBe('')
    expect(householdFontStack.value).toBe('var(--sh-font-family)')
  })

  it('ignores an unknown stored value and a failed fetch', async () => {
    get.mockResolvedValue({ font_family: 'Comic Sans' })
    await loadHouseholdTheme()
    expect(householdFont.value).toBe('system')
    householdFont.value = 'rounded'
    get.mockRejectedValue(new Error('offline'))
    await loadHouseholdTheme()
    expect(householdFont.value).toBe('rounded')
  })
})
