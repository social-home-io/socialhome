/**
 * The app a notification row is about, named in the viewer's language.
 *
 * Every app row (invite, continue, waiting, declined, ended, turn, added,
 * asked, update) stores the app's name as its place (``params.space_name``),
 * written in the reader's *saved* language when the row was made — English
 * for someone who never saved one (their German may come from the browser
 * alone). The sentence is built here in the language on screen, so the
 * place is named here too: the catalog's ``i18n`` name for the viewer's
 * language, else the stored name.
 */
import { locale } from '@/i18n/i18n'
import { catalog, catalogLoaded, catalogLoading, loadCatalog, localized, type CatalogEntry } from '@/store/apps'
import type { Notification } from '@/types'

/** The app id behind *n*'s subject — ``app_session:<app>/<session>``
 *  (a game) or ``app:<app>`` (added / asked for / update) — else ``null``. */
export function appIdOf(n: Pick<Notification, 'object_key'>): string | null {
  const key = n.object_key ?? ''
  if (key.startsWith('app_session:')) {
    const id = key.slice('app_session:'.length)
    const cut = id.lastIndexOf('/')
    return cut > 0 ? id.slice(0, cut) : null
  }
  if (key.startsWith('app:')) return key.slice('app:'.length) || null
  return null
}

/** *n*'s app as the viewer's catalog lists it, else ``null``. */
export function appEntryOf(n: Pick<Notification, 'object_key'>): CatalogEntry | null {
  const appId = appIdOf(n)
  if (!appId) return null
  return catalog.value.find(e => e.app_id === appId) ?? null
}

/** *n*'s app name in the viewer's language — else the name the row was
 *  written with, else its id. */
export function appNameOf(n: Notification): string {
  const entry = appEntryOf(n)
  if (entry) return localized(entry, locale.value || 'en').name
  return n.params?.space_name || appIdOf(n) || ''
}

/** *n* with its app named in the viewer's language (the sentence's place);
 *  *n* itself for any other row or when nothing changes. */
export function withAppName(n: Notification): Notification {
  if (!n.params || !appEntryOf(n)) return n
  const name = appNameOf(n)
  return name === n.params.space_name ? n : { ...n, params: { ...n.params, space_name: name } }
}

/** Load the catalog once for app rows to read names from — unless it is
 *  already here or on its way (rows mounting together ask once). */
export function ensureAppCatalog(): void {
  if (!catalogLoaded.value && !catalogLoading.value) void loadCatalog()
}
