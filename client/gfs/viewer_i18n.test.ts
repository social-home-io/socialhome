/**
 * The viewer's bundled message tables: every language has every English
 * key with the same placeholders, the glossary's fixed words hold, and the
 * language comes from the browser's list without a network request.
 */
import { afterEach, describe, expect, it } from 'vitest'
import { LOCALES, locale, pickLocale, setLocale, t, tn, type MessageKey } from './viewer_i18n'

const placeholders = (s: string) => [...s.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort()

afterEach(() => setLocale('en'))

describe('message tables', () => {
  const keys = Object.keys(LOCALES.en) as MessageKey[]

  it.each(Object.keys(LOCALES))('%s has every key with the same placeholders', (lang) => {
    const table = LOCALES[lang as keyof typeof LOCALES]
    expect(Object.keys(table).sort()).toEqual([...keys].sort())
    for (const key of keys) {
      expect(table[key].trim(), `${lang} ${key}`).not.toBe('')
      expect(placeholders(table[key]), `${lang} ${key}`).toEqual(placeholders(LOCALES.en[key]))
    }
  })

  it('never says "end-to-end" in any language (glossary)', () => {
    for (const table of Object.values(LOCALES)) {
      for (const text of Object.values(table)) {
        expect(text.toLowerCase()).not.toMatch(/end-to-end|ende-zu-ende|de bout en bout|de extremo a extremo/)
      }
    }
  })

  it('keeps the names and the glossary words', () => {
    for (const table of Object.values(LOCALES)) {
      expect(table['space.cta']).toContain('Social Home')
      expect(table['title.person']).toContain('Momentum')
    }
    expect(LOCALES.de['space.cta_hint']).toContain('Space')
    expect(LOCALES.de['space.cta_hint']).not.toMatch(/Raum/)
    expect(LOCALES.nl['space.cta_hint']).not.toMatch(/ruimte/)
    expect(LOCALES.de['person.follow']).toMatch(/^Mit deinem/)
    expect(LOCALES.fr['person.follow']).toContain('votre')
  })
})

describe('pickLocale', () => {
  it('takes the first language it has, by primary subtag', () => {
    expect(pickLocale(['de-CH', 'en'])).toBe('de')
    expect(pickLocale(['ja', 'nl-BE'])).toBe('nl')
    expect(pickLocale(['FR'])).toBe('fr')
    expect(pickLocale(['es-419'])).toBe('es')
  })

  it('falls back to English', () => {
    expect(pickLocale([])).toBe('en')
    expect(pickLocale(['ja', 'zh-Hant'])).toBe('en')
    expect(pickLocale(['__proto__', 'constructor'])).toBe('en')
  })
})

describe('t / tn', () => {
  it('fills placeholders and leaves unknown ones', () => {
    expect(t('page.back', { server: 'Bern' })).toBe('Back to Bern')
    expect(t('page.back')).toBe('Back to {server}')
  })

  it('uses the singular for exactly one', () => {
    expect(tn('space.members', 1)).toBe('1 member')
    expect(tn('space.members', 0)).toBe('0 members')
    expect(tn('space.members', 12)).toBe('12 members')
  })

  it('speaks the chosen language', () => {
    setLocale('de')
    expect(locale()).toBe('de')
    expect(tn('person.followers', 1)).toBe('1 Follower')
    expect(t('join.title', { space: 'Gartenclub' })).toBe('Du bist zu Gartenclub eingeladen')
  })

  it('does not interpret a placeholder value', () => {
    expect(t('page.back', { server: '{server} <b>' })).toBe('Back to {server} <b>')
  })
})
