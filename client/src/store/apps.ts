/**
 * Apps store — installed apps, catalog browsing, and update checks.
 *
 * Mirrors ``GET /api/apps`` (installed), ``GET /api/apps/catalog``
 * (admin-only browse surface), and ``GET /api/apps/updates`` (any member,
 * ≤24 h cache; admin ``?refresh=1`` forces a live re-fetch) so the AppsPage
 * can render without a per-render fetch round-trip.
 */
import { signal } from '@preact/signals'
import { api } from '@/api'
import { ws } from '@/ws'
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'

export interface InstalledApp {
  app_id:       string
  name:         string
  version:      string
  enabled:      boolean
  capabilities: string[]
  icon:         string | null
  min_age:      number
}

export interface CatalogEntry {
  app_id:       string
  name:         string
  latest_version: string
  description:  string | null
  icon_url:     string | null
  capabilities: string[]
}

export interface AppUpdate {
  app_id:          string
  name:            string
  current_version: string
  latest_version:  string
}

export const installedApps   = signal<InstalledApp[]>([])
export const catalog         = signal<CatalogEntry[]>([])
export const updates         = signal<AppUpdate[]>([])
export const appsLoading     = signal(false)
export const catalogLoading  = signal(false)
export const updatesChecking = signal(false)
export const appsError       = signal<string | null>(null)
export const catalogError    = signal<string | null>(null)
/** ``false`` when the operator turned the catalog off (empty
 *  ``apps_catalog_url``) — the Catalog tab says so instead of "no apps". */
export const catalogEnabled  = signal(true)

/**
 * How current the app list we hold is — from ``checked_at`` / ``stale`` on
 * ``/api/apps/updates`` and ``/api/apps/catalog``. ``stale``: the latest
 * try to reach the app list failed, so what we show is from ``checkedAt``
 * (``null``: it never answered since the server started). ``failed``: the
 * check itself errored (nothing held to fall back on). Either way the page
 * must not say "All apps are up to date".
 */
export interface CatalogFreshness {
  checkedAt: string | null
  stale:     boolean
  failed:    boolean
}
export const catalogFreshness = signal<CatalogFreshness>({ checkedAt: null, stale: false, failed: false })

/** Did the last check reach the app list? */
export function catalogReached(f: CatalogFreshness = catalogFreshness.value): boolean {
  return !f.stale && !f.failed
}

function freshnessFrom(data: { checked_at?: string | null, stale?: boolean }): CatalogFreshness {
  return { checkedAt: data.checked_at ?? null, stale: data.stale === true, failed: false }
}
/** True when at least one household member has child protection enabled —
 *  gates the per-app age-gate admin control (no point configuring a gate
 *  nobody is subject to). Loaded by an admin via ``GET /api/cp/protection``. */
export const householdHasProtectedMinor = signal(false)

export async function loadProtectionStatus(): Promise<void> {
  try {
    const data = await api.get('/api/cp/protection') as {
      users: Array<{ is_minor: boolean }>
    }
    householdHasProtectedMinor.value = (data.users ?? []).some(u => u.is_minor)
  } catch {
    // Non-admins 403 here; the control is admin-only anyway, so fail closed
    // (hide the age setting) rather than surface an error.
    householdHasProtectedMinor.value = false
  }
}

/** Whether this tab loaded the installed list yet — a live change only
 *  refreshes a list someone is looking at (the Apps page, the dashboard). */
let installedLoaded = false

/**
 * Load ``GET /api/apps``. *quiet*: a live refresh (``app.*`` frames) —
 * the list stays on screen while it loads and a failed refresh keeps it.
 */
export async function loadInstalled({ quiet = false }: { quiet?: boolean } = {}): Promise<void> {
  if (!quiet) {
    appsLoading.value = true
    appsError.value = null
  }
  try {
    const data = await api.get('/api/apps') as { apps: InstalledApp[] }
    installedApps.value = data.apps ?? []
    installedLoaded = true
    if (quiet) appsError.value = null
  } catch (err: unknown) {
    if (!quiet) appsError.value = (err as Error).message ?? t('apps.load_failed')
  } finally {
    if (!quiet) appsLoading.value = false
  }
}

/** The live app frames (``app.installed`` / ``app.updated`` /
 *  ``app.uninstalled`` / ``app.enabled`` / ``app.disabled``, each
 *  ``{app_id}``) — the server sends them to whoever ``GET /api/apps``
 *  would show the app. */
export const APP_FRAMES = [
  'app.installed', 'app.updated', 'app.uninstalled', 'app.enabled', 'app.disabled',
] as const

/**
 * Keep open Apps pages and the dashboard's Apps card current: any app
 * change refetches the installed list (quietly, and only once this tab
 * loaded it); an updated or uninstalled app leaves the "update available"
 * list.
 */
export function wireAppsWs(): () => void {
  const offs = APP_FRAMES.map(type => ws.on(type, (e) => {
    const appId = String(e.data.app_id ?? '')
    if (type === 'app.updated' || type === 'app.uninstalled') {
      updates.value = updates.value.filter(u => u.app_id !== appId)
    }
    if (installedLoaded) void loadInstalled({ quiet: true })
  }))
  return () => offs.forEach(off => off())
}

export async function loadCatalog(force = false): Promise<void> {
  catalogLoading.value = true
  catalogError.value = null
  try {
    const data = await api.get('/api/apps/catalog' + (force ? '?refresh=1' : '')) as {
      apps: CatalogEntry[]; enabled?: boolean; checked_at?: string | null; stale?: boolean
    }
    catalog.value = data.apps ?? []
    catalogEnabled.value = data.enabled !== false
    catalogFreshness.value = freshnessFrom(data)
  } catch (err: unknown) {
    catalogError.value = (err as Error).message ?? t('apps.catalog_failed')
  } finally {
    catalogLoading.value = false
  }
}

export async function loadUpdates(force = false): Promise<void> {
  updatesChecking.value = true
  try {
    const url = '/api/apps/updates' + (force ? '?refresh=1' : '')
    const data = await api.get(url) as {
      updates: AppUpdate[]; checked_at?: string | null; stale?: boolean
    }
    updates.value = data.updates ?? []
    catalogFreshness.value = freshnessFrom(data)
  } catch {
    // The check didn't answer and nothing is held: no list, and never
    // "up to date".
    updates.value = []
    catalogFreshness.value = { ...catalogFreshness.value, failed: true }
  } finally {
    updatesChecking.value = false
  }
}

export async function updateApp(appId: string): Promise<void> {
  const updated = await api.post(`/api/apps/${encodeURIComponent(appId)}/update`) as InstalledApp
  installedApps.value = installedApps.value.map(a => a.app_id === appId ? updated : a)
  await loadInstalled()
  await loadUpdates()
}

export async function installApp(appId: string): Promise<void> {
  const app = await api.post('/api/apps', { app_id: appId }) as InstalledApp
  installedApps.value = [...installedApps.value, app]
  showToast(t('apps.installed_toast', { name: app.name }), 'success')
}

export async function uninstallApp(appId: string): Promise<void> {
  await api.delete(`/api/apps/${encodeURIComponent(appId)}`)
  installedApps.value = installedApps.value.filter(a => a.app_id !== appId)
}

export async function setEnabled(appId: string, enabled: boolean): Promise<void> {
  const updated = await api.patch(`/api/apps/${encodeURIComponent(appId)}`, { enabled }) as InstalledApp
  installedApps.value = installedApps.value.map(a => a.app_id === appId ? updated : a)
}

export async function setMinAge(appId: string, minAge: number): Promise<void> {
  await api.patch(`/api/apps/${encodeURIComponent(appId)}`, { min_age: minAge })
  await loadInstalled()
}

export interface AppRuntime {
  app_id:       string
  name:         string
  entry_url:    string
  self_user_id: string
  capabilities: string[]
}

export async function getRuntime(appId: string): Promise<AppRuntime> {
  return await api.get(`/api/apps/${encodeURIComponent(appId)}/runtime`) as AppRuntime
}

/** Test helper — reset signals without hitting the API. */
export function _resetAppsForTest(): void {
  installedApps.value    = []
  catalog.value          = []
  updates.value          = []
  appsLoading.value      = false
  catalogLoading.value   = false
  updatesChecking.value  = false
  appsError.value        = null
  catalogError.value     = null
  catalogEnabled.value   = true
  catalogFreshness.value = { checkedAt: null, stale: false, failed: false }
  householdHasProtectedMinor.value = false
  installedLoaded        = false
}
