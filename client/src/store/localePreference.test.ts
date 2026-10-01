import { describe, it, expect, afterEach } from 'vitest'
import { waitFor } from '@testing-library/preact'
import { currentUser } from '@/store/auth'
import { locale, setLocale } from '@/i18n/i18n'
import { wireLocalePreference } from './localePreference'
import type { User } from '@/types'

function userWith(prefs: Record<string, unknown>): User {
  return { user_id: 'u1', username: 'a', preferences_json: JSON.stringify(prefs) } as unknown as User
}

describe('wireLocalePreference', () => {
  afterEach(async () => {
    currentUser.value = null
    await setLocale('en')
  })

  it('applies the signed-in user\'s saved locale', async () => {
    wireLocalePreference()
    currentUser.value = userWith({ locale: 'de' })
    await waitFor(() => expect(locale.value).toBe('de'))
  })

  it('leaves the locale alone when the user has none saved', async () => {
    wireLocalePreference()
    await setLocale('fr')
    currentUser.value = userWith({})
    await new Promise(r => setTimeout(r, 0))
    expect(locale.value).toBe('fr')
  })

  it('ignores an unknown saved locale', async () => {
    wireLocalePreference()
    currentUser.value = userWith({ locale: 'klingon' })
    await new Promise(r => setTimeout(r, 0))
    expect(locale.value).toBe('en')
  })
})
