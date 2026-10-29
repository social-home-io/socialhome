/**
 * Live app changes (``app.installed`` / ``app.updated`` / ``app.uninstalled``
 * / ``app.enabled`` / ``app.disabled``): an open Apps page and the
 * dashboard's Apps card refresh their list without a reload, quietly (no
 * "Loading…" flash), and an updated or removed app leaves "update
 * available".
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const handlers: Record<string, (e: { type: string, data: Record<string, unknown> }) => void> = {}

vi.mock('@/ws', () => ({
  ws: {
    on: (type: string, h: (e: { type: string, data: Record<string, unknown> }) => void) => {
      handlers[type] = h
      return () => { delete handlers[type] }
    },
  },
}))

import {
  APP_FRAMES,
  appsLoading,
  installedApps,
  loadInstalled,
  updates,
  wireAppsWs,
  _resetAppsForTest,
} from './apps'

const fetchMock = vi.fn()
const CHESS = {
  app_id: 'chess', name: 'Chess', version: '1', enabled: true,
  capabilities: [], icon: null, min_age: 0,
}
const GO = { ...CHESS, app_id: 'go', name: 'Go' }

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), { headers: { 'content-type': 'application/json' } })
}

function frame(type: string, appId: string) {
  handlers[type]({ type, data: { type, app_id: appId } })
}

let off: () => void = () => {}

beforeEach(() => {
  _resetAppsForTest()
  fetchMock.mockReset()
  globalThis.fetch = fetchMock as unknown as typeof fetch
  off = wireAppsWs()
})

afterEach(() => {
  off()
  _resetAppsForTest()
})

describe('wireAppsWs', () => {
  it('listens to every app frame', () => {
    expect(Object.keys(handlers).sort()).toEqual([...APP_FRAMES].sort())
  })

  it('a new app shows up in a list this tab already loaded, without a loading flash', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ apps: [CHESS] }))
    await loadInstalled()
    expect(installedApps.value.map(a => a.app_id)).toEqual(['chess'])

    let resolve: (r: Response) => void = () => {}
    fetchMock.mockReturnValueOnce(new Promise<Response>(r => { resolve = r }))
    frame('app.installed', 'go')
    expect(appsLoading.value).toBe(false)
    resolve(jsonResponse({ apps: [CHESS, GO] }))
    await vi.waitFor(() => expect(installedApps.value.map(a => a.app_id)).toEqual(['chess', 'go']))
  })

  it.each(['app.uninstalled', 'app.disabled', 'app.enabled', 'app.updated'])(
    '%s refetches the installed list',
    async (type) => {
      fetchMock.mockResolvedValueOnce(jsonResponse({ apps: [CHESS, GO] }))
      await loadInstalled()
      fetchMock.mockResolvedValueOnce(jsonResponse({ apps: [CHESS] }))
      frame(type, 'go')
      await vi.waitFor(() => expect(installedApps.value.map(a => a.app_id)).toEqual(['chess']))
    },
  )

  it('does nothing before this tab loaded the list (nobody is looking)', () => {
    frame('app.installed', 'go')
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('a failed live refresh keeps the list on screen', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ apps: [CHESS] }))
    await loadInstalled()
    fetchMock.mockRejectedValueOnce(new Error('offline'))
    frame('app.installed', 'go')
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
    expect(installedApps.value.map(a => a.app_id)).toEqual(['chess'])
  })

  it.each(['app.updated', 'app.uninstalled'])('%s drops the app from "update available"', (type) => {
    updates.value = [
      { app_id: 'chess', name: 'Chess', current_version: '1', latest_version: '2' },
      { app_id: 'go', name: 'Go', current_version: '1', latest_version: '2' },
    ]
    frame(type, 'chess')
    expect(updates.value.map(u => u.app_id)).toEqual(['go'])
  })
})
