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

export async function loadInstalled(): Promise<void> {
  appsLoading.value = true
  appsError.value = null
  try {
    const data = await api.get('/api/apps') as { apps: InstalledApp[] }
    installedApps.value = data.apps ?? []
  } catch (err: unknown) {
    appsError.value = (err as Error).message ?? t('apps.load_failed')
  } finally {
    appsLoading.value = false
  }
}

export async function loadCatalog(): Promise<void> {
  catalogLoading.value = true
  catalogError.value = null
  try {
    const data = await api.get('/api/apps/catalog') as { apps: CatalogEntry[] }
    catalog.value = data.apps ?? []
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
    const data = await api.get(url) as { updates: AppUpdate[] }
    updates.value = data.updates ?? []
  } catch {
    updates.value = []
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
  householdHasProtectedMinor.value = false
}
