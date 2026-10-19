import { describe, it, expect, vi, beforeEach } from 'vitest'

const confirmMock = vi.hoisted(() => vi.fn())
vi.mock('./confirm', () => ({ confirmDialog: (...a: unknown[]) => confirmMock(...a) }))
const home = vi.hoisted(() => ({ value: null as { lat: number; lon: number } | null }))
vi.mock('@/store/householdHome', () => ({
  ensureHouseholdHome: async () => {},
  householdHome: home,
}))

const { askLocationConsent, isApiCode, locationConsentText } = await import('./localConsent')

beforeEach(() => { confirmMock.mockReset() })

describe('askLocationConsent', () => {
  it('names the household that receives the approximate location', async () => {
    home.value = { lat: 47.39, lon: 8.56 }
    confirmMock.mockResolvedValueOnce(true)
    expect(await askLocationConsent('Keller household')).toBe(true)
    const [message, opts] = confirmMock.mock.calls[0]
    expect(message).toBe(
      'To check you live in the area, your approximate location (about 1 km) is sent to '
      + "Keller household with this request. Keller household only uses it to decide and doesn't keep it.",
    )
    expect(opts.confirmLabel).toBe('Send and ask to join')
  })

  it('a No is a no', async () => {
    home.value = { lat: 47.39, lon: 8.56 }
    confirmMock.mockResolvedValueOnce(false)
    expect(await askLocationConsent(null)).toBe(false)
    expect(confirmMock.mock.calls[0][0]).toContain("the space's household")
  })

  it('no household home: explains where it comes from and never says yes', async () => {
    home.value = null
    confirmMock.mockResolvedValueOnce(true)
    expect(await askLocationConsent('Keller household')).toBe(false)
    expect(confirmMock.mock.calls[0][0]).toContain('Settings → System → General')
  })
})

describe('helpers', () => {
  it('the consent line has a generic form for an unnamed household', () => {
    expect(locationConsentText(null)).toContain("the space's household")
  })

  it('recognises an API error by its code', () => {
    expect(isApiCode(Object.assign(new Error('x'), { code: 'LOCATION_REQUIRED' }), 'LOCATION_REQUIRED')).toBe(true)
    expect(isApiCode(new Error('x'), 'LOCATION_REQUIRED')).toBe(false)
    expect(isApiCode(null, 'LOCATION_REQUIRED')).toBe(false)
  })
})
