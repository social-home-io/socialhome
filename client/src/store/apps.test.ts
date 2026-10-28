import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  installedApps,
  catalog,
  updates,
  updatesChecking,
  loadInstalled,
  loadCatalog,
  loadUpdates,
  updateApp,
  installApp,
  uninstallApp,
  setEnabled,
  setMinAge,
  loadProtectionStatus,
  householdHasProtectedMinor,
  catalogEnabled,
  _resetAppsForTest,
} from './apps'

const fetchMock = vi.fn()

beforeEach(() => {
  _resetAppsForTest()
  fetchMock.mockReset()
  globalThis.fetch = fetchMock as unknown as typeof fetch
})

afterEach(() => {
  _resetAppsForTest()
})

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    ...init,
    headers: { 'content-type': 'application/json' },
  })
}

describe('apps store', () => {
  // ── loadProtectionStatus ────────────────────────────────────────

  it('loadProtectionStatus sets the signal true when any user is a minor', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        users: [
          { user_id: 'u1', username: 'a', is_minor: false, declared_age: 0 },
          { user_id: 'u2', username: 'b', is_minor: true, declared_age: 8 },
        ],
      }),
    )
    await loadProtectionStatus()
    expect(householdHasProtectedMinor.value).toBe(true)
  })

  it('loadProtectionStatus stays false when no minors (and fails closed on error)', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        users: [{ user_id: 'u1', username: 'a', is_minor: false, declared_age: 0 }],
      }),
    )
    await loadProtectionStatus()
    expect(householdHasProtectedMinor.value).toBe(false)

    // A 403 (non-admin) → fail closed (hide the age setting).
    fetchMock.mockResolvedValueOnce(jsonResponse({}, { status: 403 }))
    householdHasProtectedMinor.value = true
    await loadProtectionStatus()
    expect(householdHasProtectedMinor.value).toBe(false)
  })

  // ── loadInstalled ───────────────────────────────────────────────

  it('loadInstalled calls GET /api/apps and populates installedApps', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        apps: [
          { app_id: 'a1', name: 'Alpha', version: '1.0', enabled: true, capabilities: [], icon: null, min_age: 0 },
          { app_id: 'a2', name: 'Beta',  version: '2.0', enabled: false, capabilities: ['notify'], icon: null, min_age: 13 },
        ],
      }),
    )
    await loadInstalled()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url] = fetchMock.mock.calls[0] as [string]
    expect(url).toBe('api/apps')
    expect(installedApps.value).toHaveLength(2)
    expect(installedApps.value[0].app_id).toBe('a1')
  })

  it('loadInstalled sets appsError on failure', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ error: { code: 'SERVER_ERROR', detail: 'boom' } }, { status: 500 }),
    )
    const { appsError } = await import('./apps')
    await loadInstalled()
    expect(appsError.value).toBeTruthy()
    expect(installedApps.value).toHaveLength(0)
  })

  // ── loadCatalog ─────────────────────────────────────────────────

  it('loadCatalog calls GET /api/apps/catalog and populates catalog', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        apps: [
          { app_id: 'c1', name: 'CoolApp', latest_version: '3.0', description: 'desc', icon_url: null, capabilities: ['storage'] },
        ],
      }),
    )
    await loadCatalog()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url] = fetchMock.mock.calls[0] as [string]
    expect(url).toBe('api/apps/catalog')
    expect(catalog.value).toHaveLength(1)
    expect(catalog.value[0].app_id).toBe('c1')
    expect(catalogEnabled.value).toBe(true)
  })

  it('loadCatalog marks a turned-off catalog (enabled: false)', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ apps: [], enabled: false }))
    await loadCatalog()
    expect(catalogEnabled.value).toBe(false)
    fetchMock.mockResolvedValueOnce(jsonResponse({ apps: [], enabled: true }))
    await loadCatalog()
    expect(catalogEnabled.value).toBe(true)
  })

  // ── installApp ──────────────────────────────────────────────────

  it('installApp calls POST /api/apps with {app_id} and appends to installedApps', async () => {
    const newApp = { app_id: 'x1', name: 'Xapp', version: '0.1', enabled: true, capabilities: [], icon: null, min_age: 0 }
    fetchMock.mockResolvedValueOnce(jsonResponse(newApp, { status: 201 }))
    await installApp('x1')
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, opts] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('api/apps')
    expect(opts.method).toBe('POST')
    expect(JSON.parse(opts.body as string)).toEqual({ app_id: 'x1' })
    expect(installedApps.value).toHaveLength(1)
    expect(installedApps.value[0].app_id).toBe('x1')
  })

  // ── uninstallApp ────────────────────────────────────────────────

  it('uninstallApp calls DELETE /api/apps/{id} and removes from installedApps', async () => {
    installedApps.value = [
      { app_id: 'a1', name: 'Alpha', version: '1.0', enabled: true, capabilities: [], icon: null, min_age: 0 },
      { app_id: 'a2', name: 'Beta',  version: '2.0', enabled: true, capabilities: [], icon: null, min_age: 0 },
    ]
    fetchMock.mockResolvedValueOnce(new Response('{"status":"ok"}', { status: 200, headers: { 'content-type': 'application/json' } }))
    await uninstallApp('a1')
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, opts] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('api/apps/a1')
    expect(opts.method).toBe('DELETE')
    expect(installedApps.value).toHaveLength(1)
    expect(installedApps.value[0].app_id).toBe('a2')
  })

  // ── setEnabled ──────────────────────────────────────────────────

  it('setEnabled calls PATCH /api/apps/{id} with {enabled} and updates signal', async () => {
    installedApps.value = [
      { app_id: 'a1', name: 'Alpha', version: '1.0', enabled: true, capabilities: [], icon: null, min_age: 0 },
    ]
    const updated = { app_id: 'a1', name: 'Alpha', version: '1.0', enabled: false, capabilities: [], icon: null, min_age: 0 }
    fetchMock.mockResolvedValueOnce(jsonResponse(updated))
    await setEnabled('a1', false)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, opts] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('api/apps/a1')
    expect(opts.method).toBe('PATCH')
    expect(JSON.parse(opts.body as string)).toEqual({ enabled: false })
    expect(installedApps.value[0].enabled).toBe(false)
  })

  // ── loadUpdates ─────────────────────────────────────────────────

  it('loadUpdates calls GET /api/apps/updates and populates updates signal', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        updates: [
          { app_id: 'a1', name: 'Alpha', current_version: '1.0', latest_version: '1.1' },
        ],
      }),
    )
    await loadUpdates()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url] = fetchMock.mock.calls[0] as [string]
    expect(url).toBe('api/apps/updates')
    expect(updates.value).toHaveLength(1)
    expect(updates.value[0].app_id).toBe('a1')
    expect(updates.value[0].latest_version).toBe('1.1')
  })

  it('loadUpdates(true) calls GET /api/apps/updates?refresh=1', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ updates: [] }))
    await loadUpdates(true)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url] = fetchMock.mock.calls[0] as [string]
    expect(url).toBe('api/apps/updates?refresh=1')
  })

  it('loadUpdates sets updates to [] on API error and does not throw', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ error: { code: 'SERVER_ERROR', detail: 'boom' } }, { status: 500 }),
    )
    await expect(loadUpdates()).resolves.toBeUndefined()
    expect(updates.value).toHaveLength(0)
  })

  it('loadUpdates clears updatesChecking after completion', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ updates: [] }))
    await loadUpdates()
    expect(updatesChecking.value).toBe(false)
  })

  // ── updateApp ───────────────────────────────────────────────────

  it('updateApp calls POST /api/apps/{id}/update then refreshes installed + updates', async () => {
    const orig = { app_id: 'a1', name: 'Alpha', version: '1.0', enabled: true, capabilities: [], icon: null, min_age: 0 }
    const patched = { ...orig, version: '1.1' }
    installedApps.value = [orig]

    // POST /api/apps/a1/update → updated app
    fetchMock.mockResolvedValueOnce(jsonResponse(patched))
    // loadInstalled
    fetchMock.mockResolvedValueOnce(jsonResponse({ apps: [patched] }))
    // loadUpdates
    fetchMock.mockResolvedValueOnce(jsonResponse({ updates: [] }))

    await updateApp('a1')

    expect(fetchMock).toHaveBeenCalledTimes(3)
    const [url, opts] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('api/apps/a1/update')
    expect(opts.method).toBe('POST')
    expect(installedApps.value[0].version).toBe('1.1')
    expect(updates.value).toHaveLength(0)
  })

  it('updateApp rethrows ApiError for the caller to handle', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ error: { code: 'NOT_FOUND', detail: 'app not found' } }, { status: 404 }),
    )
    await expect(updateApp('missing')).rejects.toThrow()
  })

  // ── setMinAge ───────────────────────────────────────────────────

  it('setMinAge calls PATCH /api/apps/{id} with {min_age} and refreshes installed', async () => {
    installedApps.value = [
      { app_id: 'a1', name: 'Alpha', version: '1.0', enabled: true, capabilities: [], icon: null, min_age: 0 },
    ]
    // PATCH response (empty body / 200)
    fetchMock.mockResolvedValueOnce(
      new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } }),
    )
    // loadInstalled refresh — returns app with updated min_age
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        apps: [{ app_id: 'a1', name: 'Alpha', version: '1.0', enabled: true, capabilities: [], icon: null, min_age: 13 }],
      }),
    )

    await setMinAge('a1', 13)

    expect(fetchMock).toHaveBeenCalledTimes(2)
    const [patchUrl, patchOpts] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(patchUrl).toBe('api/apps/a1')
    expect(patchOpts.method).toBe('PATCH')
    expect(JSON.parse(patchOpts.body as string)).toEqual({ min_age: 13 })
    // After the loadInstalled refresh the signal reflects the new value.
    expect(installedApps.value[0].min_age).toBe(13)
  })

  it('setMinAge rethrows ApiError for the caller to handle', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ error: { code: 'UNPROCESSABLE', detail: 'Invalid min_age' } }, { status: 422 }),
    )
    await expect(setMinAge('a1', 99)).rejects.toThrow()
  })
})
