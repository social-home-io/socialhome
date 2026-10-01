import { signal } from '@preact/signals'
import en from './locales/en.json'
import meta from './locales/_meta.json'

type Translations = Record<string, string>

/** Source-of-truth English catalog. Frozen so missing translations
 *  in the active locale gracefully fall back to the English string
 *  rather than a raw dotted key — §30 fallback semantics. */
const EN_SOURCE: Translations = en

const translations = signal<Translations>(en)
export const locale = signal('en')

export function t(key: string, params?: Record<string, string>): string {
  // Fallback chain: active locale → English source → raw key.
  // Raw-key fallback is the dev-time "missing translation" signal;
  // in production, `EN_SOURCE[key]` covers every key we ship so end
  // users never see a dotted key.
  let text = translations.value[key] || EN_SOURCE[key] || key
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      text = text.replace(`{${k}}`, v)
    }
  }
  return text
}

/** localStorage cache of the last chosen locale, so a cold start (and
 *  the login screen) paints in it before ``/api/me`` answers. The
 *  durable copy is the user's ``locale`` preference
 *  (``store/localePreference.ts``). */
const STORAGE_KEY = 'sh_locale'

type LocaleInfo = { rtl?: boolean }
const LOCALES = meta.locales as Record<string, LocaleInfo>

/** ``true`` for a locale we ship. Guards every dynamic import so a
 *  stored or server-supplied value can never name an arbitrary path. */
export function isKnownLocale(lang: unknown): lang is string {
  return typeof lang === 'string' && Object.hasOwn(LOCALES, lang)
}

export async function setLocale(lang: string) {
  if (!isKnownLocale(lang)) {
    console.warn(`Locale ${lang} not found, keeping ${locale.value}`)
    return
  }
  try {
    const mod = await import(`./locales/${lang}.json`)
    translations.value = mod.default
    locale.value = lang
  } catch {
    console.warn(`Locale ${lang} failed to load, keeping ${locale.value}`)
    return
  }
  // Screen readers pick the voice from <html lang>; RTL locales flip dir.
  document.documentElement.lang = lang
  document.documentElement.dir = LOCALES[lang].rtl ? 'rtl' : 'ltr'
  try { localStorage.setItem(STORAGE_KEY, lang) } catch { /* storage blocked */ }
}

/** Apply the cached locale on cold start (no-op when none / unknown). */
export async function initLocale(): Promise<void> {
  let cached: string | null = null
  try { cached = localStorage.getItem(STORAGE_KEY) } catch { /* storage blocked */ }
  if (isKnownLocale(cached) && cached !== locale.value) await setLocale(cached)
}
