/**
 * Which view of the Apps page shows: **Your apps** or the **Store**.
 *
 * ``/apps?tab=store`` / ``/apps?tab=yours`` deep-link a view. ``?app=<id>``
 * (a "new app" notification) opens the view that holds that app: your
 * apps when the household has it, else the Store. Otherwise the view a
 * person picked last is remembered on this device (per viewer, browser
 * storage — a convenience, never shared), and a household with nothing
 * added yet starts in the Store.
 */

export type AppsTab = 'yours' | 'store'

export const APPS_TABS: readonly AppsTab[] = ['yours', 'store']

const KEY = 'sh_apps_tab'

export function isAppsTab(value: unknown): value is AppsTab {
  return value === 'yours' || value === 'store'
}

/** The view picked last on this device, or ``null``. */
export function rememberedTab(): AppsTab | null {
  try {
    const v = localStorage.getItem(KEY)
    return isAppsTab(v) ? v : null
  } catch {
    return null
  }
}

/** Remember the picked view (fails quietly without storage). */
export function rememberTab(tab: AppsTab): void {
  try {
    localStorage.setItem(KEY, tab)
  } catch {
    // Private window / blocked storage: the page still works.
  }
}

/** The view to show, from the most to the least specific signal. */
export function pickTab({ query, picked, installedIds, catalogIds, nothingAdded }: {
  /** ``?tab=`` */
  query: string | null | undefined
  /** ``?app=`` */
  picked: string | null | undefined
  installedIds: ReadonlySet<string>
  catalogIds: ReadonlySet<string>
  /** The household has no apps yet (and the list answered). */
  nothingAdded: boolean
}): AppsTab {
  if (isAppsTab(query)) return query
  if (picked) {
    if (installedIds.has(picked)) return 'yours'
    if (catalogIds.has(picked)) return 'store'
  }
  return rememberedTab() ?? (nothingAdded ? 'store' : 'yours')
}
