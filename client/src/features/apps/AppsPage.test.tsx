import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'

// ── Mutable signal-like holders so each test can set store state ────────────
// vi.hoisted so they exist when the (hoisted) vi.mock factories run.
const h = vi.hoisted(() => ({
  installedApps: { value: [] as any[] },
  catalog: { value: [] as any[] },
  updates: { value: [] as any[] },
  updatesChecking: { value: false },
  appsLoading: { value: false },
  catalogLoading: { value: false },
  appsError: { value: null as string | null },
  catalogError: { value: null as string | null },
  catalogEnabled: { value: true },
  catalogSourceHost: { value: null as string | null },
  householdHasProtectedMinor: { value: true },
  currentUser: { value: { is_admin: true } as { is_admin: boolean, protected?: boolean } | null },
  setEnabled: vi.fn().mockResolvedValue(undefined),
  setMinAge: vi.fn().mockResolvedValue(undefined),
  uninstallApp: vi.fn().mockResolvedValue(undefined),
  installApp: vi.fn().mockResolvedValue(undefined),
  requestApp: vi.fn().mockResolvedValue(undefined),
  updateApp: vi.fn().mockResolvedValue(undefined),
  showToast: vi.fn(),
  catalogFreshness: {
    value: { checkedAt: null, stale: false, failed: false } as {
      checkedAt: string | null, stale: boolean, failed: boolean
    },
  },
  loadUpdates: vi.fn().mockResolvedValue(undefined),
  locale: { value: 'en' },
}))
const {
  installedApps, catalog, updates, appsError,
  householdHasProtectedMinor, currentUser, setEnabled, setMinAge,
  catalogEnabled, catalogSourceHost, showToast, catalogFreshness, loadUpdates,
  installApp, requestApp, updateApp,
} = h

vi.mock('@/store/apps', async (orig) => {
  const real = await orig<typeof import('@/store/apps')>()
  return {
    installedApps: h.installedApps, catalog: h.catalog, updates: h.updates,
    updatesChecking: h.updatesChecking, appsLoading: h.appsLoading,
    catalogLoading: h.catalogLoading, appsError: h.appsError, catalogError: h.catalogError,
    catalogEnabled: h.catalogEnabled, catalogSourceHost: h.catalogSourceHost,
    householdHasProtectedMinor: h.householdHasProtectedMinor,
    catalogFreshness: h.catalogFreshness,
    catalogReached: (f = h.catalogFreshness.value) => !f.stale && !f.failed,
    loadInstalled: vi.fn(), loadCatalog: vi.fn(), loadUpdates: h.loadUpdates,
    loadProtectionStatus: vi.fn(),
    updateApp: h.updateApp, installApp: h.installApp, requestApp: h.requestApp,
    uninstallApp: h.uninstallApp, setEnabled: h.setEnabled, setMinAge: h.setMinAge,
    localized: real.localized,
  }
})
vi.mock('@/store/auth', () => ({ currentUser: h.currentUser }))
vi.mock('@/store/pageTitle', () => ({ useTitle: () => {} }))
vi.mock('@/components/Toast', () => ({ showToast: h.showToast }))
vi.mock('@/baseUrl', () => ({ addBase: (p: string) => p, basePath: '/' }))
vi.mock('@/features/notifications/NeedsYouSection', () => ({
  NeedsYouSection: ({ only }: { only?: ReadonlySet<string> }) => (
    <div data-testid="needs-you">{[...(only ?? [])].join(',')}</div>
  ),
}))
vi.mock('@/i18n/i18n', async (orig) => ({
  ...(await orig<object>()),
  locale: h.locale,
}))
const nav = vi.hoisted(() => ({ route: vi.fn(), query: {} as Record<string, string> }))
vi.mock('preact-iso', async (orig) => ({
  ...(await orig<object>()),
  useLocation: () => ({ route: nav.route, path: '/apps', url: '/apps', query: nav.query }),
}))

import AppsPage, { FILTERS_AT, SEARCH_AT } from './AppsPage'

function makeApp(over: Partial<any> = {}) {
  return {
    app_id: 'chess', name: 'Chess', version: '2026.10.29',
    enabled: true, capabilities: ['storage', 'federation'], icon: null, min_age: 0,
    permissions: ['storage', 'people', 'federation'],
    installed_at: '2026-10-12T09:00:00+00:00', installed_by_name: 'Anna',
    ...over,
  }
}

function makeEntry(over: Partial<any> = {}) {
  return {
    app_id: 'chess', name: 'Chess', latest_version: '2026.10.29',
    description: 'Play chess with another household.', icon_url: null,
    capabilities: ['storage', 'federation'], permissions: ['storage', 'people', 'federation'],
    i18n: { de: { name: 'Schach', description: 'Spiel Schach.' } },
    bundle_size: 16198, min_age: 0, compatible: true, requested: false,
    ...over,
  }
}

/** *n* catalog apps (``app0`` …), half for playing together. */
function manyEntries(n: number) {
  return Array.from({ length: n }, (_, i) => makeEntry({
    app_id: `app${i}`, name: `Game ${i}`, description: `Number ${i}`, i18n: {},
    permissions: i % 2 ? ['storage'] : ['storage', 'people', 'federation'],
  }))
}

beforeEach(() => {
  vi.clearAllMocks()
  installedApps.value = [makeApp()]
  catalog.value = [makeEntry(), makeEntry({ app_id: 'notes', name: 'Notes', description: 'Take notes', i18n: {}, permissions: ['storage'], min_age: 13 })]
  updates.value = []
  appsError.value = null
  catalogEnabled.value = true
  catalogSourceHost.value = null
  householdHasProtectedMinor.value = true
  currentUser.value = { is_admin: true }
  catalogFreshness.value = { checkedAt: null, stale: false, failed: false }
  loadUpdates.mockResolvedValue(undefined)
  nav.query = {}
  h.locale.value = 'en'
})

describe('One Apps page (D1)', () => {
  it('has no tabs: your apps, then More apps with Details', () => {
    const { queryByRole, getByRole, getByText } = render(<AppsPage />)
    expect(queryByRole('tablist')).toBeNull()
    expect(getByRole('heading', { level: 2, name: 'In your household' })).toBeTruthy()
    expect(getByRole('heading', { level: 3, name: 'Chess' })).toBeTruthy()
    expect(getByText(/^Added by Anna · .*12/)).toBeTruthy()
    expect(getByRole('heading', { level: 2, name: /More apps/ })).toBeTruthy()
    fireEvent.click(getByRole('button', { name: 'Details about Notes' }))
    expect(nav.route).toHaveBeenCalledWith('/apps/notes/about')
  })

  it('shows the rating chip only when it is not "Everyone"', () => {
    const { container } = render(<AppsPage />)
    const chips = Array.from(container.querySelectorAll('.sh-age-chip')).map(c => c.textContent)
    expect(chips).toEqual(['13+'])
  })

  it('puts the app Needs-you rows (invites, requests) on top', () => {
    const { getByTestId } = render(<AppsPage />)
    expect(getByTestId('needs-you').textContent).toBe('app_challenge,app_request')
  })

  it('names apps in the viewer\'s language from the catalog', () => {
    h.locale.value = 'de'
    const { getByRole } = render(<AppsPage />)
    expect(getByRole('heading', { level: 3, name: 'Schach' })).toBeTruthy()
  })

  it('no search box under 8 apps, a search box at 8, filters at 12', () => {
    catalog.value = manyEntries(SEARCH_AT - 1)
    const a = render(<AppsPage />)
    expect(a.queryByRole('searchbox')).toBeNull()
    a.unmount()
    catalog.value = manyEntries(SEARCH_AT)
    const b = render(<AppsPage />)
    expect(b.getByRole('searchbox', { name: 'Search apps' })).toBeTruthy()
    expect(b.queryByRole('group', { name: 'Show apps' })).toBeNull()
    b.unmount()
    catalog.value = manyEntries(FILTERS_AT)
    const c = render(<AppsPage />)
    expect(c.getByRole('group', { name: 'Show apps' })).toBeTruthy()
  })

  it('search narrows both lists and says when nothing matches', () => {
    catalog.value = [makeEntry(), ...manyEntries(9)]
    const { getByRole, queryByRole, getByText } = render(<AppsPage />)
    fireEvent.input(getByRole('searchbox'), { target: { value: 'Game 3' } })
    expect(getByRole('heading', { level: 3, name: 'Game 3' })).toBeTruthy()
    expect(queryByRole('heading', { level: 3, name: 'Chess' })).toBeNull()
    fireEvent.input(getByRole('searchbox'), { target: { value: 'zebra' } })
    expect(getByText('No apps match “zebra”.')).toBeTruthy()
  })

  it('filter chips pick apps to play together or on your own', () => {
    installedApps.value = []
    catalog.value = manyEntries(FILTERS_AT)
    const { getByRole, queryByRole } = render(<AppsPage />)
    fireEvent.click(getByRole('button', { name: 'On your own' }))
    expect(getByRole('button', { name: 'On your own' }).getAttribute('aria-pressed')).toBe('true')
    expect(getByRole('heading', { level: 3, name: 'Game 1' })).toBeTruthy()
    expect(queryByRole('heading', { level: 3, name: 'Game 2' })).toBeNull()
  })

  it('an app needing a newer Social Home says so in More apps', () => {
    catalog.value = [makeEntry({ app_id: 'go', name: 'Go', compatible: false })]
    const { getByText } = render(<AppsPage />)
    expect(getByText('Needs a newer Social Home')).toBeTruthy()
  })
})

describe('First run with one app (nothing added yet)', () => {
  beforeEach(() => {
    installedApps.value = []
    catalog.value = [makeEntry()]
  })

  it('an admin sees one featured card with the three permissions and Add to household', () => {
    const { getByRole, getByText, queryByRole, queryByText } = render(<AppsPage />)
    expect(getByRole('heading', { level: 2, name: 'Nothing added yet' })).toBeTruthy()
    expect(getByText('Saves its own data for each person')).toBeTruthy()
    expect(getByText('Lets you pick people to play with')).toBeTruthy()
    expect(getByText('Sends invites and moves to other households')).toBeTruthy()
    expect(getByText(/From Social Home/)).toBeTruthy()
    expect(getByText(/More apps are on the way/)).toBeTruthy()
    expect(queryByRole('searchbox')).toBeNull()
    // Never "verified", never "Store".
    expect(queryByText(/verified|store/i)).toBeNull()
    expect(getByRole('button', { name: 'Add to household' })).toBeTruthy()
  })

  it('Add to household asks first: "Add Chess for everyone?", then adds it', async () => {
    const { getByRole, getAllByRole } = render(<AppsPage />)
    fireEvent.click(getByRole('button', { name: 'Add to household' }))
    const dialog = getByRole('dialog', { name: 'Add Chess for everyone?' })
    expect(dialog.textContent).toContain('Chess will be able to:')
    expect(dialog.textContent).toContain("You choose someone in Social Home. Chess learns only who you picked and people you've played with.")
    expect(dialog.textContent).toContain('It can never see your password')
    expect(dialog.textContent).toContain('Everyone in your household can open it.')
    expect(dialog.textContent).toContain('Protected accounts play only with people they can already message.')
    expect(installApp).not.toHaveBeenCalled()
    const buttons = getAllByRole('button', { name: 'Add to household' })
    fireEvent.click(buttons[buttons.length - 1])
    await vi.waitFor(() => expect(installApp).toHaveBeenCalledWith('chess'))
  })

  it('a member sees Ask an admin instead, and the request is sent from the dialog', async () => {
    currentUser.value = { is_admin: false }
    const { getByRole, queryByRole } = render(<AppsPage />)
    expect(queryByRole('button', { name: 'Add to household' })).toBeNull()
    fireEvent.click(getByRole('button', { name: 'Ask an admin for Chess' }))
    const dialog = getByRole('dialog', { name: 'Ask an admin for Chess?' })
    expect(dialog.textContent).toContain("They'll see what the app can do before adding it.")
    fireEvent.click(getByRole('button', { name: 'Send request' }))
    await vi.waitFor(() => expect(requestApp).toHaveBeenCalledWith('chess'))
    await vi.waitFor(() => expect(showToast).toHaveBeenCalledWith('Request sent.', 'success'))
  })

  it('a protected account asks a grown-up', () => {
    currentUser.value = { is_admin: false, protected: true }
    const { getByRole } = render(<AppsPage />)
    fireEvent.click(getByRole('button', { name: 'Ask a grown-up for Chess' }))
    expect(getByRole('dialog', { name: 'Ask a grown-up for Chess?' })).toBeTruthy()
  })

  it('an app already asked for reads "Asked"', () => {
    currentUser.value = { is_admin: false }
    catalog.value = [makeEntry({ requested: true })]
    const { getByText, queryByRole } = render(<AppsPage />)
    expect(getByText('Asked')).toBeTruthy()
    expect(queryByRole('button', { name: 'Ask an admin for Chess' })).toBeNull()
  })

  it('an operator\'s catalog is named by its host (D6)', () => {
    catalogSourceHost.value = 'apps.example.org'
    const { getByText } = render(<AppsPage />)
    expect(getByText(/From apps\.example\.org, set by your server operator/)).toBeTruthy()
  })
})

describe('AppsPage ?app= (a "new app" notification)', () => {
  it.each([true, false])('picks the app out and puts focus on it (admin: %s)', (isAdmin) => {
    currentUser.value = { is_admin: isAdmin }
    installedApps.value = [makeApp(), makeApp({ app_id: 'go', name: 'Go' })]
    nav.query = { app: 'go' }
    const { getByRole } = render(<AppsPage />)
    const go = getByRole('heading', { name: 'Go' }).closest('.sh-apps-row')!
    const chess = getByRole('heading', { name: 'Chess' }).closest('.sh-apps-row')!
    expect(go.classList.contains('sh-apps-row--picked')).toBe(true)
    expect(chess.classList.contains('sh-apps-row--picked')).toBe(false)
    expect(document.activeElement).toBe(go)
  })

  it('an app that is gone picks nothing out', () => {
    nav.query = { app: 'gone' }
    const { container } = render(<AppsPage />)
    expect(container.querySelector('.sh-apps-row--picked')).toBeNull()
  })
})

describe('Icons never load from a remote host', () => {
  it('renders the placeholder for an https icon and the data: icon inline', () => {
    installedApps.value = [makeApp({ icon: 'https://tracker.example/i.png' })]
    catalog.value = [
      makeEntry({ app_id: 'notes', name: 'Notes', icon_url: 'https://tracker.example/c.png' }),
      makeEntry({ app_id: 'go', name: 'Go', icon_url: 'data:image/svg+xml,%3Csvg%3E%3C/svg%3E' }),
    ]
    const { container } = render(<AppsPage />)
    const srcs = Array.from(container.querySelectorAll('img')).map((i) => i.getAttribute('src'))
    expect(srcs).toEqual(['data:image/svg+xml,%3Csvg%3E%3C/svg%3E'])
  })
})

describe('Members', () => {
  it('see enabled apps only, no ⋯ menu, no Check for updates', () => {
    currentUser.value = { is_admin: false }
    installedApps.value = [makeApp(), makeApp({ app_id: 'x', name: 'Hidden', enabled: false })]
    const { getByRole, queryByText, queryByRole } = render(<AppsPage />)
    expect(getByRole('heading', { level: 3, name: 'Chess' })).toBeTruthy()
    expect(queryByText('Hidden')).toBeNull()
    expect(queryByRole('button', { name: 'Chess settings' })).toBeNull()
    expect(queryByText('Check for updates')).toBeNull()
  })
})

describe('Admin ⋯ menu', () => {
  it('holds about, turn off, ages and uninstall', () => {
    const { getByRole, queryByRole } = render(<AppsPage />)
    expect(queryByRole('menuitemradio')).toBeNull()
    fireEvent.click(getByRole('button', { name: 'Chess settings' }))
    expect(getByRole('menuitem', { name: 'About this app' })).toBeTruthy()
    expect(getByRole('menuitem', { name: 'Turn off' })).toBeTruthy()
    expect(getByRole('menuitemradio', { name: 'Minimum age: Everyone' }).getAttribute('aria-checked')).toBe('true')
    expect(getByRole('menuitem', { name: 'Uninstall…' })).toBeTruthy()
  })

  it('selecting a minimum-age radio calls setMinAge', () => {
    const { getByRole } = render(<AppsPage />)
    fireEvent.click(getByRole('button', { name: 'Chess settings' }))
    fireEvent.click(getByRole('menuitemradio', { name: /13\+/ }))
    expect(setMinAge).toHaveBeenCalledWith('chess', 13)
  })

  it('Turn off calls setEnabled with false; a failure says what failed', async () => {
    setEnabled.mockRejectedValueOnce(new Error('API 500'))
    const { getByRole } = render(<AppsPage />)
    fireEvent.click(getByRole('button', { name: 'Chess settings' }))
    fireEvent.click(getByRole('menuitem', { name: 'Turn off' }))
    expect(setEnabled).toHaveBeenCalledWith('chess', false)
    await vi.waitFor(() => expect(showToast).toHaveBeenCalledWith("Couldn't disable the app.", 'error'))
  })

  it('hides the minimum-age choices when the household has no protected account', () => {
    householdHasProtectedMinor.value = false
    const { getByRole, queryByRole } = render(<AppsPage />)
    fireEvent.click(getByRole('button', { name: 'Chess settings' }))
    expect(queryByRole('menuitemradio')).toBeNull()
  })

  it('a turned-off app shows Off and Turn on instead of Open', () => {
    installedApps.value = [makeApp({ enabled: false })]
    const { getByText, getByRole, queryByRole } = render(<AppsPage />)
    expect(getByText('Off')).toBeTruthy()
    expect(queryByRole('button', { name: 'Open Chess' })).toBeNull()
    fireEvent.click(getByRole('button', { name: 'Turn on Chess' }))
    expect(setEnabled).toHaveBeenCalledWith('chess', true)
  })

  it('Open goes to the app in the SPA', () => {
    const { getByRole } = render(<AppsPage />)
    fireEvent.click(getByRole('button', { name: 'Open Chess' }))
    expect(nav.route).toHaveBeenCalledWith('/apps/chess')
  })

  it('the ⋯ menu works from the keyboard', async () => {
    const { getByRole } = render(<AppsPage />)
    const trigger = getByRole('button', { name: 'Chess settings' })
    trigger.focus()
    fireEvent.click(trigger)
    await vi.waitFor(() => expect(document.activeElement?.textContent).toContain('Minimum age: Everyone'))
    fireEvent.keyDown(document.activeElement!, { key: 'End' })
    expect(document.activeElement?.textContent).toBe('Uninstall…')
    fireEvent.keyDown(document.activeElement!, { key: 'Escape' })
    expect(document.activeElement).toBe(trigger)
  })
})

describe('Updates (admins)', () => {
  it('an update row offers What\'s new and Update; versions read as dates', async () => {
    updates.value = [{ app_id: 'chess', name: 'Chess', current_version: '2026.10.28', latest_version: '2026.10.29', compatible: true }]
    const { getByRole, getByText } = render(<AppsPage />)
    expect(getByRole('heading', { level: 3, name: 'Chess has an update' })).toBeTruthy()
    expect(getByText(/28.*2026 → .*29.*2026/)).toBeTruthy()
    fireEvent.click(getByRole('button', { name: "What's new in Chess" }))
    expect(nav.route).toHaveBeenCalledWith('/apps/chess/about#whats-new')
    fireEvent.click(getByRole('button', { name: 'Update Chess' }))
    await vi.waitFor(() => expect(updateApp).toHaveBeenCalledWith('chess'))
  })

  it('an update needing a newer Social Home has no Update button', () => {
    updates.value = [{ app_id: 'chess', name: 'Chess', current_version: '2026.10.28', latest_version: '2026.11.2', compatible: false }]
    const { queryByRole, getAllByText } = render(<AppsPage />)
    expect(queryByRole('button', { name: 'Update Chess' })).toBeNull()
    expect(getAllByText('Needs a newer Social Home').length).toBeGreaterThan(0)
  })

  it('members never see the Updates section', () => {
    currentUser.value = { is_admin: false }
    updates.value = [{ app_id: 'chess', name: 'Chess', current_version: '1', latest_version: '2' }]
    const { queryByRole } = render(<AppsPage />)
    expect(queryByRole('heading', { name: 'Updates' })).toBeNull()
  })
})

describe('Catalog states', () => {
  it('a turned-off catalog says so', () => {
    catalogEnabled.value = false
    catalog.value = []
    const { getByText, queryByText } = render(<AppsPage />)
    expect(getByText(/The app catalog is turned off on this Social Home/)).toBeTruthy()
    expect(queryByText('No apps available in the catalog.')).toBeNull()
  })

  it('nothing installed and an empty catalog says so', () => {
    installedApps.value = []
    catalog.value = []
    const { getByText } = render(<AppsPage />)
    expect(getByText('No apps available in the catalog.')).toBeTruthy()
  })
})

describe('Check for updates is honest about reaching the app list', () => {
  const AT = '2026-10-28T20:15:00+00:00'

  it('says "All apps are up to date" only after the app list answered', async () => {
    loadUpdates.mockImplementationOnce(async () => {
      catalogFreshness.value = { checkedAt: AT, stale: false, failed: false }
    })
    const { getByText } = render(<AppsPage />)
    fireEvent.click(getByText('Check for updates'))
    await vi.waitFor(() =>
      expect(showToast).toHaveBeenCalledWith('All apps are up to date', 'info'))
  })

  it('a check that could not reach the app list says so', async () => {
    loadUpdates.mockImplementationOnce(async () => {
      catalogFreshness.value = { checkedAt: AT, stale: true, failed: false }
    })
    const { getByText } = render(<AppsPage />)
    fireEvent.click(getByText('Check for updates'))
    await vi.waitFor(() => expect(showToast).toHaveBeenCalled())
    const [message, kind] = showToast.mock.calls[0]
    expect(message).toMatch(/^Couldn't reach the app list — showing what we knew at .+\.$/)
    expect(kind).toBe('error')
  })

  it('a stale app list shows the strip with Retry for admins', () => {
    catalogFreshness.value = { checkedAt: AT, stale: true, failed: false }
    const { getByRole } = render(<AppsPage />)
    expect(getByRole('status').textContent).toMatch(/Couldn't reach the app list/)
    loadUpdates.mockClear()
    fireEvent.click(getByRole('button', { name: 'Retry' }))
    expect(loadUpdates).toHaveBeenCalledWith(true)
  })

  it('members see the stale strip without Retry', () => {
    currentUser.value = { is_admin: false }
    catalogFreshness.value = { checkedAt: AT, stale: true, failed: false }
    const { getByRole, queryByRole } = render(<AppsPage />)
    expect(getByRole('status')).toBeTruthy()
    expect(queryByRole('button', { name: 'Retry' })).toBeNull()
  })
})
