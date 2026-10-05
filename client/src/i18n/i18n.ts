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
      // split/join, not replace(): a value is literal text — ``$&`` or
      // ``$'`` in a name or search term must not act as a pattern — and
      // every occurrence of the placeholder is filled, not just the first.
      text = text.split(`{${k}}`).join(v)
    }
  }
  return text
}

/** Label for a raw server value (a status, mode, quality…): its
 *  translation under ``prefix.value``, or the raw value itself when the
 *  catalog has no such key — never a dotted key on screen. */
export function tValue(prefix: string, value: string): string {
  const key = `${prefix}.${value}`
  const text = t(key)
  return text === key ? value : text
}

/** The locale for dates, times and numbers: the UI language with the
 *  browser's region. A browser tag in the UI language wins as-is
 *  (``en-GB``, ``de-CH``); otherwise the first browser region is borrowed
 *  (an English UI in a ``de-DE`` browser formats as ``en-DE``: English
 *  words, ``18:00`` and day-first dates). With no region, the UI language. */
export function formatLocale(): string {
  const ui = (locale.value || 'en').split('-')[0].toLowerCase()
  const prefs = typeof navigator === 'undefined' ? [] : (navigator.languages ?? [navigator.language])
  for (const tag of prefs) {
    if (tag && tag.split('-')[0].toLowerCase() === ui) return tag
  }
  for (const tag of prefs) {
    const region = tag?.split('-').find((part, i) => i > 0 && /^[A-Za-z]{2}$/.test(part))
    if (!region) continue
    try {
      return Intl.getCanonicalLocales(`${ui}-${region}`)[0]
    } catch {
      // A malformed browser tag: try the next one.
    }
  }
  return ui
}

/** The UI language's "one" plural category (French counts 0 as one).
 *  Pick a ``_one`` key with it: ``t(isOne(n) ? 'k_one' : 'k', …)``. */
export function isOne(n: number): boolean {
  try {
    return new Intl.PluralRules(locale.value || undefined).select(n) === 'one'
  } catch {
    return n === 1
  }
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
