import { describe, it, expect, afterEach, vi } from 'vitest'
import { t, tValue, locale, formatLocale } from './i18n'
import en from './locales/en.json'
import de from './locales/de.json'
import nl from './locales/nl.json'
import fr from './locales/fr.json'
import es from './locales/es.json'

describe('i18n', () => {
  it('returns the key for known translations', () => {
    expect(t('app.title')).toBe('Social Home')
  })

  it('returns the key itself for unknown translations', () => {
    expect(t('unknown.key')).toBe('unknown.key')
  })

  it('interpolates parameters', () => {
    // No parametrized keys in en.json yet, but the function should pass through
    expect(t('app.title', { unused: 'x' })).toBe('Social Home')
  })

  it('default locale is en', () => {
    expect(locale.value).toBe('en')
  })
})

describe('setLocale / initLocale', () => {
  afterEach(async () => {
    const { setLocale } = await import('./i18n')
    await setLocale('en')
    try { localStorage.removeItem('sh_locale') } catch { /* ignore */ }
  })

  it('switches translations and sets <html lang> and dir', async () => {
    const { setLocale, t: tt, locale: loc } = await import('./i18n')
    await setLocale('de')
    expect(loc.value).toBe('de')
    expect(document.documentElement.lang).toBe('de')
    expect(document.documentElement.dir).toBe('ltr')
    expect(tt('settings.week_start.monday')).toBe('Montag')
  })

  it('caches the choice so the next cold start uses it', async () => {
    const { setLocale } = await import('./i18n')
    await setLocale('fr')
    expect(localStorage.getItem('sh_locale')).toBe('fr')
  })

  it('initLocale applies a cached locale', async () => {
    const { initLocale, locale: loc } = await import('./i18n')
    localStorage.setItem('sh_locale', 'nl')
    await initLocale()
    expect(loc.value).toBe('nl')
    expect(document.documentElement.lang).toBe('nl')
  })

  it('ignores unknown locale codes (never imports an arbitrary path)', async () => {
    const { initLocale, setLocale, locale: loc } = await import('./i18n')
    localStorage.setItem('sh_locale', '../../secrets')
    await initLocale()
    expect(loc.value).toBe('en')
    await setLocale('xx')
    expect(loc.value).toBe('en')
  })

  it('survives storage that throws', async () => {
    const { initLocale, setLocale, locale: loc } = await import('./i18n')
    const spy = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('blocked') })
    const spy2 = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('blocked') })
    await initLocale()
    await setLocale('es')
    expect(loc.value).toBe('es')
    spy.mockRestore(); spy2.mockRestore()
  })
})

describe('formatLocale', () => {
  const setLangs = (langs: string[]) =>
    vi.spyOn(navigator, 'languages', 'get').mockReturnValue(langs)
  afterEach(() => {
    vi.restoreAllMocks()
    locale.value = 'en'
  })

  it('keeps an English user in the UK on day-first dates (en-GB)', () => {
    setLangs(['en-GB', 'en'])
    locale.value = 'en'
    expect(formatLocale()).toBe('en-GB')
    expect(new Date(Date.UTC(2026, 9, 4)).toLocaleDateString(formatLocale(), { timeZone: 'UTC' }))
      .toBe('04/10/2026')
  })

  it('uses the browser region only when it matches the UI language', () => {
    setLangs(['en-GB', 'de-CH'])
    locale.value = 'de'
    expect(formatLocale()).toBe('de-CH')
  })

  it('borrows the browser region when no browser language matches', () => {
    setLangs(['en-US'])
    locale.value = 'fr'
    expect(formatLocale()).toBe('fr-US')
  })

  it('keeps a German browser on 18:00 under the default English UI (en-DE)', () => {
    setLangs(['de-DE', 'de'])
    locale.value = 'en'
    expect(formatLocale()).toBe('en-DE')
    const d = new Date(Date.UTC(2026, 9, 5, 18, 0))
    expect(d.toLocaleTimeString(formatLocale(), { timeZone: 'UTC', timeStyle: 'short' })).toBe('18:00')
  })

  it('falls back to the UI language when the browser gives no region', () => {
    setLangs(['de'])
    locale.value = 'en'
    expect(formatLocale()).toBe('en')
  })
})

describe('t() parameters', () => {
  it('inserts values literally, even with $ patterns', () => {
    // 'feed.empty.title' has no params; use a raw key so the key is the text.
    expect(t('Hi {name}!', { name: "x$'y" })).toBe("Hi x$'y!")
    expect(t('{q} and {q}', { q: 'a$$b' })).toBe('a$$b and a$$b')
  })
})

describe('tValue', () => {
  it('translates a known raw value and shows an unknown one as is', () => {
    expect(tValue('calls.state', 'ended')).not.toBe('calls.state.ended')
    expect(tValue('calls.state', 'some_future_state')).toBe('some_future_state')
  })
})

describe('glossary: Apps is a name', () => {
  // ../translations GUIDE: "Apps" (the section) is never translated —
  // French had "Applis" in the side nav and its error lines.
  const locales = { en, de, nl, fr, es } as Record<string, Record<string, string>>

  it.each(Object.keys(locales))('%s keeps the Apps section name', (code) => {
    const strings = locales[code]
    expect(strings['nav.apps']).toBe('Apps')
    expect(strings['apps.host.back_to_apps']).toMatch(/\bApps\b/)
    expect(strings['dashboard.open_apps']).toMatch(/\bApps\b/)
    const translated = Object.entries(strings)
      .filter(([, v]) => /\b(applis?|aplis?)\b/i.test(v))
      .map(([k]) => k)
    expect(translated).toEqual([])
  })
})

describe('an app name is never glued into a compound', () => {
  // A German or Dutch compound ("{space}-Partie") only works for a
  // one-word name: "eure Schiffe versenken-Partie" broke. Any app name
  // must read right, so the name stands on its own ("eure Partie {space}").
  const locales = { en, de, nl, fr, es } as Record<string, Record<string, string>>
  const appKey = /^(apps\.|notifications\.row\.app_)/

  it.each(Object.keys(locales))('%s', (code) => {
    const glued = Object.entries(locales[code])
      .filter(([k, v]) => appKey.test(k) && /\{(name|space|app)\}-\p{L}/u.test(v))
      .map(([k]) => k)
    expect(glued).toEqual([])
  })
})
