/**
 * Apps store — installed apps, catalog browsing, and update checks.
 *
 * Mirrors ``GET /api/apps`` (installed), ``GET /api/apps/catalog`` (every
 * member: "More apps" and "Ask an admin", D4), and ``GET /api/apps/updates``
 * (any member, ≤24 h cache; admin ``?refresh=1`` forces a live re-fetch)
 * so the Apps page and an app's detail page render without a per-render
 * fetch round-trip.
 */
import { computed, signal } from '@preact/signals'
import { api, ApiError } from '@/api'
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
  /** The plain-word permissions the household agreed to (installed
   *  manifest; ``people`` implied by ``federation``). */
  permissions?: AppPermission[]
  installed_at?: string | null
  /** Who added it ("Added by Anna"); ``null`` when that person is gone. */
  installed_by_name?: string | null
  /** Group play (SDK 1.9): how many play, whether it can be opened as a
   *  game in a space (``tables``), and whether a game being played still
   *  takes people (``late_join``). */
  players?:   { min: number, max: number }
  tables?:    boolean
  late_join?: boolean
}

/** The three plain-word permissions (D2), in the order they are listed. */
export type AppPermission = 'storage' | 'people' | 'federation'
export const APP_PERMISSIONS: readonly AppPermission[] = ['storage', 'people', 'federation']

export interface CatalogChangelogEntry {
  version: string
  date:    string | null
  notes:   Record<string, string>
}

export interface CatalogEntry {
  app_id:       string
  name:         string
  latest_version: string
  description:  string | null
  icon_url:     string | null
  capabilities: string[]
  // Catalog schema 2 — absent / null when the catalog doesn't say.
  permissions?:      AppPermission[]
  i18n?:             Record<string, { name: string | null, description: string | null }>
  bundle_size?:      number | null
  min_age?:          number | null
  languages?:        string[]
  publisher?:        string | null
  homepage?:         string | null
  min_core_version?: string | null
  changelog?:        CatalogChangelogEntry[]
  /** ``false``: needs a newer Social Home — no install offered. */
  compatible?:       boolean
  /** The caller asked an admin for it and nobody answered yet. */
  requested?:        boolean
  /** Group play: the app's players range (``null``: not a group game)
   *  and whether it can be opened as a game in a space. */
  players?:          { min: number, max: number } | null
  tables?:           boolean
}

export interface AppUpdate {
  app_id:          string
  name:            string
  current_version: string
  latest_version:  string
  /** ``false``: the new version needs a newer Social Home. */
  compatible?:     boolean
  /** What the new version asks for beyond what the household agreed to:
   *  updating needs a yes for exactly these (D7). */
  new_permissions?: AppPermission[]
  /** "What's new": the catalog's entries newer than the installed
   *  version, newest first. */
  changelog?:      CatalogChangelogEntry[]
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
/** Where the catalog comes from (D6): ``null`` = Social Home's own app
 *  list ("From Social Home"), else the host an operator set. */
export const catalogSourceHost = signal<string | null>(null)
/** Whether the catalog answered at least once in this tab. */
export const catalogLoaded   = signal(false)

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

/** Load the installed list once for a screen that only reads it (a
 *  space's games): no spinner, a failure leaves it empty. */
export function ensureInstalled(): Promise<void> {
  return installedLoaded ? Promise.resolve() : loadInstalled({ quiet: true })
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
      apps: CatalogEntry[]; enabled?: boolean; source_host?: string | null
      checked_at?: string | null; stale?: boolean
    }
    catalog.value = data.apps ?? []
    catalogEnabled.value = data.enabled !== false
    catalogSourceHost.value = data.source_host ?? null
    catalogLoaded.value = true
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

/** Update *appId* to the catalog's newest version. *acceptPermissions*
 *  are the new permissions the admin said yes to; without them a version
 *  that asks for more is refused (``409 APP_NEW_PERMISSIONS``, see
 *  :func:`newPermissionsAsked`). */
export async function updateApp(appId: string, acceptPermissions: readonly AppPermission[] = []): Promise<void> {
  const updated = await api.post(
    `/api/apps/${encodeURIComponent(appId)}/update`,
    { accept_permissions: [...acceptPermissions] },
  ) as InstalledApp
  installedApps.value = installedApps.value.map(a => a.app_id === appId ? updated : a)
  await loadInstalled()
  await loadUpdates()
}

/** The new permissions an update refusal names (``409
 *  APP_NEW_PERMISSIONS``, ``params.permissions`` "people,federation"), or
 *  ``null`` when *err* is any other failure. */
export function newPermissionsAsked(err: unknown): AppPermission[] | null {
  if (!(err instanceof ApiError) || err.code !== 'APP_NEW_PERMISSIONS') return null
  const params = (err.extra.params ?? {}) as Record<string, unknown>
  const raw = typeof params.permissions === 'string' ? params.permissions : ''
  return raw.split(',')
    .map(p => p.trim())
    .filter((p): p is AppPermission => (APP_PERMISSIONS as readonly string[]).includes(p))
}

export async function installApp(appId: string): Promise<InstalledApp> {
  const app = await api.post('/api/apps', { app_id: appId }) as InstalledApp
  installedApps.value = [...installedApps.value.filter(a => a.app_id !== appId), app]
  showToast(t('apps.added_toast', { name: app.name }), 'success')
  return app
}

/** "Ask an admin" (D4): the admins — a protected account's guardians —
 *  get a Needs-you request. The entry reads "Asked" from then on. */
export async function requestApp(appId: string): Promise<void> {
  await api.post(`/api/apps/${encodeURIComponent(appId)}/requests`)
  catalog.value = catalog.value.map(e => e.app_id === appId ? { ...e, requested: true } : e)
}

/** "Not now" (admins): every open request for the app is answered. */
export async function dismissRequests(appId: string): Promise<void> {
  await api.delete(`/api/apps/${encodeURIComponent(appId)}/requests`)
}

/** *entry*'s name and description in the viewer's language (``i18n``,
 *  base language first: ``pt-BR`` → ``pt``), else the catalog's own. */
export function localized(
  entry: Pick<CatalogEntry, 'name' | 'description' | 'i18n'>,
  lang: string,
): { name: string, description: string } {
  const texts = entry.i18n ?? {}
  const base = lang.split('-')[0]
  const hit = texts[lang] ?? texts[base]
  return {
    name: hit?.name || entry.name,
    description: hit?.description || entry.description || '',
  }
}

/**
 * Does the Apps entry belong in the nav (§2.1)? Admins: always. Anyone
 * else: when an app is usable for them, or "More apps" has one they could
 * ask for (the server already leaves out apps rated above a protected
 * account's age). ``null`` until it is known — the nav leaves it out
 * rather than flash it.
 */
export const appsNavVisible = computed<boolean | null>(() => {
  if (installedApps.value.length > 0) return true
  const installed = new Set(installedApps.value.map(a => a.app_id))
  if (catalog.value.some(e => !installed.has(e.app_id))) return true
  return navChecked.value ? false : null
})
const navChecked = signal(false)

/** Work out :data:`appsNavVisible` for a member: the installed list, and
 *  the catalog only when nothing is installed. */
export async function loadAppsNav(): Promise<void> {
  if (navChecked.value) return
  await loadInstalled({ quiet: true })
  if (installedApps.value.length === 0 && !catalogLoaded.value) {
    try {
      const data = await api.get('/api/apps/catalog') as { apps?: CatalogEntry[], source_host?: string | null }
      catalog.value = data.apps ?? []
      catalogSourceHost.value = data.source_host ?? null
      catalogLoaded.value = true
    } catch { /* no catalog: the nav entry stays out */ }
  }
  navChecked.value = true
}

/** What uninstalling an app loses (D9) — the uninstall dialog's counts.
 *  Counts and names only: never what a game holds, nor who here plays
 *  whom. ``told`` names up to three people in other households who will
 *  be told (``told_more`` the rest); ``untold`` counts games with a
 *  household on an older Social Home, which won't hear about them. */
export interface UninstallPreview {
  people: number
  games: number
  invites: number
  told: string[]
  told_more: number
  untold: number
}

export async function loadUninstallPreview(appId: string): Promise<UninstallPreview> {
  return await api.get(`/api/apps/${encodeURIComponent(appId)}/uninstall-preview`) as UninstallPreview
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
  /** What the bridge offers the app (the routes check again). */
  permissions:  string[]
  /** The app's player range (manifest ``players``), when the server
   *  says it: the group sheet's cap. Missing → the hard ceiling. */
  players?:     { min: number, max: number }
  /** Whether the app can be played as a group game (manifest
   *  ``tables``). Only an explicit ``false`` keeps the group sheet shut. */
  tables?:      boolean
}

export async function getRuntime(appId: string): Promise<AppRuntime> {
  return await api.get(`/api/apps/${encodeURIComponent(appId)}/runtime`) as AppRuntime
}

/** One person Social Home's invite sheet lists (D3). */
export interface InvitablePerson {
  instance_id:    string
  user_ref:       string
  /** "Ben · Brookside" for someone in another household. */
  display_name:   string
  person_name:    string
  /** ``null`` for your own household. */
  household_name: string | null
  is_local:       boolean
  online:         boolean
  /** Whether their household can take part in group games (v_76+). */
  groups?:        boolean
}

/** A space the person can pick everyone from in the group sheet: the
 *  invitable people with a writing seat there (indices into ``people``). */
export interface InvitableSpace {
  space_id: string
  name:     string
  people:   number[]
}

/** What the invite caps leave (server-enforced; shown before Send). */
export interface InviteCounts {
  open:        number
  max_open:    number
  today:       number
  max_per_day: number
}

/** The invite sheet's list — Social Home's own read, never the app's. */
export async function getInvitable(appId: string): Promise<{
  people: InvitablePerson[]
  invites: InviteCounts
  /** Group sheet only; older servers leave it out. */
  spaces?: InvitableSpace[]
}> {
  return await api.get(`/api/apps/${encodeURIComponent(appId)}/invitable`) as {
    people: InvitablePerson[]
    invites: InviteCounts
    spaces?: InvitableSpace[]
  }
}

/** The sheet's "Continue game with …": pick an old game up again. */
export async function continueGame(
  appId: string,
  sessionId: string,
  person: Pick<InvitablePerson, 'instance_id' | 'user_ref' | 'is_local'>,
): Promise<string> {
  const r = await api.post(
    `/api/apps/${encodeURIComponent(appId)}/sessions/${encodeURIComponent(sessionId)}/continue`,
    { target: { instance_id: person.instance_id, user_ref: person.user_ref, is_local: person.is_local } },
  ) as { session_id: string }
  return r.session_id
}

/** The sheet's Send: one invite to one picked person. */
export async function sendInvite(
  appId: string,
  person: Pick<InvitablePerson, 'instance_id' | 'user_ref' | 'is_local'>,
): Promise<string> {
  const r = await api.post(`/api/apps/${encodeURIComponent(appId)}/sessions`, {
    target: { instance_id: person.instance_id, user_ref: person.user_ref, is_local: person.is_local },
  }) as { session_id: string }
  return r.session_id
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
  catalogSourceHost.value = null
  catalogLoaded.value    = false
  navChecked.value       = false
  catalogFreshness.value = { checkedAt: null, stale: false, failed: false }
  householdHasProtectedMinor.value = false
  installedLoaded        = false
}
