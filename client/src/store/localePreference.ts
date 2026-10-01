/**
 * Keeps the UI language in step with the signed-in user's ``locale``
 * preference (``users.preferences_json``), so a language chosen on one
 * device follows the user to the next. The Settings language picker
 * writes the preference; this applies it whenever ``currentUser``
 * (re)loads.
 */
import { effect } from '@preact/signals'
import { isKnownLocale, locale, setLocale } from '@/i18n/i18n'
import { currentUser } from '@/store/auth'
import { getPreferences } from '@/utils/preferences'

let disposer: (() => void) | null = null

export function wireLocalePreference(): void {
  disposer?.()
  disposer = effect(() => {
    if (!currentUser.value) return
    const saved = getPreferences().locale
    if (isKnownLocale(saved) && saved !== locale.peek()) void setLocale(saved)
  })
}
