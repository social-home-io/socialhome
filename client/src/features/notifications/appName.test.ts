import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

const apiGet = vi.fn()
vi.mock('@/api', () => ({
  api: { get: (...a: unknown[]) => apiGet(...a), post: vi.fn() },
  ApiError: class extends Error {},
}))

import type { Notification } from '@/types'
import { setLocale } from '@/i18n/i18n'
import { catalog, catalogLoaded, catalogLoading } from '@/store/apps'
import { appEntryOf, appIdOf, appNameOf, ensureAppCatalog, withAppName } from './appName'

const CHESS = {
  app_id: 'chess', name: 'Chess', latest_version: '1', description: null,
  icon_url: null, capabilities: [], i18n: { de: { name: 'Schach', description: null } },
}

function row(object_key: string | null, space_name: string | null = 'Chess'): Notification {
  return {
    id: 'n1', type: 'app_turn', title: 't', body: null, link_url: '/apps/chess', read_at: null,
    created_at: new Date().toISOString(), object_key, space_id: null,
    params: { actors: [], actor_count: 0, new: 1, total: 1, space_name },
  }
}

beforeEach(() => {
  apiGet.mockReset()
  catalog.value = [CHESS]
  catalogLoaded.value = true
  catalogLoading.value = false
})
afterEach(async () => {
  catalog.value = []
  catalogLoaded.value = false
  await setLocale('en')
})

describe('appIdOf', () => {
  it('reads a game, an app, and nothing else', () => {
    expect(appIdOf(row('app_session:chess/s1'))).toBe('chess')
    expect(appIdOf(row('app:chess'))).toBe('chess')
    expect(appIdOf(row('space_post:p1'))).toBeNull()
    expect(appIdOf(row(null))).toBeNull()
    expect(appIdOf(row('app:'))).toBeNull()
  })
})

describe('appNameOf / withAppName', () => {
  it('names the app in the language on screen, not the stored one', async () => {
    await setLocale('de')
    expect(appNameOf(row('app_session:chess/s1'))).toBe('Schach')
    expect(withAppName(row('app:chess')).params!.space_name).toBe('Schach')
  })

  it('English reads the catalog name and leaves the row as it is', () => {
    const n = row('app_session:chess/s1')
    expect(appNameOf(n)).toBe('Chess')
    expect(withAppName(n)).toBe(n)
  })

  it('an app the catalog lacks keeps the stored name, else its id', async () => {
    await setLocale('de')
    const go = row('app_session:go/s1', 'Go')
    expect(appEntryOf(go)).toBeNull()
    expect(appNameOf(go)).toBe('Go')
    expect(withAppName(go)).toBe(go)
    expect(appNameOf(row('app_session:go/s1', null))).toBe('go')
  })

  it('a space row is never touched', async () => {
    await setLocale('de')
    const n = row('space_post:p1', 'chess')
    expect(withAppName(n)).toBe(n)
  })
})

describe('ensureAppCatalog', () => {
  it('asks once while the catalog is on its way, never when it is here', async () => {
    catalogLoaded.value = false
    let done!: (v: unknown) => void
    apiGet.mockReturnValue(new Promise(r => { done = r }))
    ensureAppCatalog()
    ensureAppCatalog()
    expect(apiGet).toHaveBeenCalledTimes(1)
    done({ apps: [CHESS] })
    await vi.waitFor(() => expect(catalogLoaded.value).toBe(true))
    ensureAppCatalog()
    expect(apiGet).toHaveBeenCalledTimes(1)
  })
})
