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
  householdHasProtectedMinor: { value: true },
  currentUser: { value: { is_admin: true } as { is_admin: boolean } | null },
  setEnabled: vi.fn().mockResolvedValue(undefined),
  setMinAge: vi.fn().mockResolvedValue(undefined),
  uninstallApp: vi.fn().mockResolvedValue(undefined),
  showToast: vi.fn(),
  catalogFreshness: {
    value: { checkedAt: null, stale: false, failed: false } as {
      checkedAt: string | null, stale: boolean, failed: boolean
    },
  },
  loadUpdates: vi.fn().mockResolvedValue(undefined),
}))
const {
  installedApps, catalog, updates, updatesChecking,
  appsLoading, catalogLoading, appsError, catalogError,
  householdHasProtectedMinor, currentUser, setEnabled, setMinAge,
  catalogEnabled, showToast, catalogFreshness, loadUpdates,
} = h

vi.mock('@/store/apps', () => ({
  installedApps: h.installedApps, catalog: h.catalog, updates: h.updates,
  updatesChecking: h.updatesChecking, appsLoading: h.appsLoading,
  catalogLoading: h.catalogLoading, appsError: h.appsError, catalogError: h.catalogError,
  catalogEnabled: h.catalogEnabled,
  householdHasProtectedMinor: h.householdHasProtectedMinor,
  catalogFreshness: h.catalogFreshness,
  catalogReached: (f = h.catalogFreshness.value) => !f.stale && !f.failed,
  loadInstalled: vi.fn(), loadCatalog: vi.fn(), loadUpdates: h.loadUpdates,
  loadProtectionStatus: vi.fn(),
  updateApp: vi.fn(), installApp: vi.fn(),
  uninstallApp: h.uninstallApp, setEnabled: h.setEnabled, setMinAge: h.setMinAge,
}))
vi.mock('@/store/auth', () => ({ currentUser: h.currentUser }))
vi.mock('@/store/pageTitle', () => ({ useTitle: () => {} }))
vi.mock('@/components/Toast', () => ({ showToast: h.showToast }))
vi.mock('@/baseUrl', () => ({ addBase: (p: string) => p, basePath: '/' }))
const nav = vi.hoisted(() => ({ route: vi.fn() }))
vi.mock('preact-iso', async (orig) => ({
  ...(await orig<object>()),
  useLocation: () => ({ route: nav.route, path: '/apps', url: '/apps', query: {} }),
}))

// Stub TabHeader (its ResizeObserver overflow logic is covered by its own
// test) — render plain tab buttons + the actions slot so AppsPage's
// tab-switching is what's under test here.
vi.mock('@/components/TabHeader', () => ({
  TabHeader: ({ activeTab, visibleTabs, labels, onSelectTab, actions }: any) => (
    <div>
      <div role="tablist">
        {visibleTabs.map((tab: string) => (
          <button
            key={tab}
            role="tab"
            aria-selected={activeTab === tab}
            onClick={() => onSelectTab(tab)}
          >
            {labels[tab]}
          </button>
        ))}
      </div>
      {actions}
    </div>
  ),
}))

import AppsPage from './AppsPage'

function makeApp(over: Partial<any> = {}) {
  return {
    app_id: 'chess', name: 'Chess', version: '1.0.0',
    enabled: true, capabilities: ['storage'], icon: null, min_age: 0,
    ...over,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  installedApps.value = [makeApp()]
  catalog.value = [{ app_id: 'notes', name: 'Notes', latest_version: '2.0.0', description: 'Take notes', icon_url: null, capabilities: [] }]
  updates.value = []
  updatesChecking.value = false
  appsLoading.value = false; catalogLoading.value = false
  appsError.value = null; catalogError.value = null
  catalogEnabled.value = true
  householdHasProtectedMinor.value = true
  currentUser.value = { is_admin: true }
  catalogFreshness.value = { checkedAt: null, stale: false, failed: false }
  loadUpdates.mockResolvedValue(undefined)
})

describe('AppsPage tabs (admin)', () => {
  it('renders Installed + Catalog tabs, Installed active by default', () => {
    const { getByRole } = render(<AppsPage />)
    expect(getByRole('tab', { name: 'Installed' }).getAttribute('aria-selected')).toBe('true')
    expect(getByRole('tab', { name: 'Catalog' }).getAttribute('aria-selected')).toBe('false')
  })

  it('Installed tab shows installed apps; Catalog tab shows catalog entries', () => {
    const { getByRole, getByText, queryByText } = render(<AppsPage />)
    // Installed view: the installed app name is shown.
    expect(getByText('Chess')).toBeTruthy()
    expect(queryByText('Notes')).toBeNull()
    // Switch to Catalog.
    fireEvent.click(getByRole('tab', { name: 'Catalog' }))
    expect(getByText('Notes')).toBeTruthy()
  })

  it('a turned-off catalog says so instead of "No apps available"', () => {
    catalogEnabled.value = false
    catalog.value = []
    const { getByRole, getByText, queryByText } = render(<AppsPage />)
    fireEvent.click(getByRole('tab', { name: 'Catalog' }))
    expect(getByText(/The app catalog is turned off on this Social Home/)).toBeTruthy()
    expect(queryByText('No apps available in the catalog.')).toBeNull()
  })

  it('shows "Check for updates" only on the Installed tab', () => {
    const { getByRole, queryByText } = render(<AppsPage />)
    expect(queryByText('Check for updates')).toBeTruthy()
    fireEvent.click(getByRole('tab', { name: 'Catalog' }))
    expect(queryByText('Check for updates')).toBeNull()
  })
})

describe('AppsPage icons never load from a remote host', () => {
  it('renders the placeholder for an https icon and the data: icon inline', () => {
    installedApps.value = [makeApp({ icon: 'https://tracker.example/i.png' })]
    catalog.value = [
      { app_id: 'notes', name: 'Notes', latest_version: '2.0.0', description: 'd',
        icon_url: 'https://tracker.example/c.png', capabilities: [] },
      { app_id: 'go', name: 'Go', latest_version: '1.0.0', description: 'd',
        icon_url: 'data:image/svg+xml,%3Csvg%3E%3C/svg%3E', capabilities: [] },
    ]
    const { container, getByRole } = render(<AppsPage />)
    expect(container.querySelector('img')).toBeNull()
    fireEvent.click(getByRole('tab', { name: 'Catalog' }))
    const srcs = Array.from(container.querySelectorAll('img')).map((i) => i.getAttribute('src'))
    expect(srcs).toEqual(['data:image/svg+xml,%3Csvg%3E%3C/svg%3E'])
  })
})

describe('AppsPage non-admin', () => {
  it('renders no tablist and hides disabled apps', () => {
    currentUser.value = { is_admin: false }
    installedApps.value = [makeApp(), makeApp({ app_id: 'x', name: 'Hidden', enabled: false })]
    const { queryByRole, getByText, queryByText } = render(<AppsPage />)
    expect(queryByRole('tablist')).toBeNull()
    expect(getByText('Chess')).toBeTruthy()
    expect(queryByText('Hidden')).toBeNull()        // disabled app not shown to non-admin
  })
})

describe('AppCard admin overflow menu', () => {
  it('hides admin settings until the ⋯ menu is opened', () => {
    const { getByRole, queryByRole } = render(<AppsPage />)
    // No menu items before opening.
    expect(queryByRole('menuitemradio')).toBeNull()
    fireEvent.click(getByRole('button', { name: 'Chess settings' }))
    // Enable/disable + 4 age radios + uninstall now present.
    expect(getByRole('menuitem', { name: 'Disable app' })).toBeTruthy()
    expect(getByRole('menuitemradio', { name: /Everyone/ }).getAttribute('aria-checked')).toBe('true')
    expect(getByRole('menuitem', { name: 'Uninstall…' })).toBeTruthy()
  })

  it('selecting a minimum-age radio calls setMinAge', () => {
    const { getByRole } = render(<AppsPage />)
    fireEvent.click(getByRole('button', { name: 'Chess settings' }))
    fireEvent.click(getByRole('menuitemradio', { name: /13\+/ }))
    expect(setMinAge).toHaveBeenCalledWith('chess', 13)
  })

  it('toggling enable calls setEnabled with the negated value', () => {
    const { getByRole } = render(<AppsPage />)
    fireEvent.click(getByRole('button', { name: 'Chess settings' }))
    fireEvent.click(getByRole('menuitem', { name: 'Disable app' }))
    expect(setEnabled).toHaveBeenCalledWith('chess', false)
  })

  it.each([
    [true, 'Disable app', "Couldn't disable the app."],
    [false, 'Enable app', "Couldn't enable the app."],
  ])('a failed toggle (enabled=%s) says what failed, not "Could not update app."',
    async (enabled, item, message) => {
      installedApps.value = [makeApp({ enabled })]
      setEnabled.mockRejectedValueOnce(new Error('API 500'))
      const { getByRole } = render(<AppsPage />)
      fireEvent.click(getByRole('button', { name: 'Chess settings' }))
      fireEvent.click(getByRole('menuitem', { name: item }))
      await vi.waitFor(() => expect(showToast).toHaveBeenCalledWith(message, 'error'))
      expect(showToast).not.toHaveBeenCalledWith('Could not update app.', 'error')
    })

  it('hides the minimum-age group when the household has no protected minor', () => {
    householdHasProtectedMinor.value = false
    const { getByRole, queryByRole } = render(<AppsPage />)
    fireEvent.click(getByRole('button', { name: 'Chess settings' }))
    // Enable/disable + Uninstall still present; age radios are gone.
    expect(getByRole('menuitem', { name: 'Disable app' })).toBeTruthy()
    expect(getByRole('menuitem', { name: 'Uninstall…' })).toBeTruthy()
    expect(queryByRole('menuitemradio')).toBeNull()
  })

  it('Open goes to the app in the SPA, never with a page load', () => {
    const { getByRole } = render(<AppsPage />)
    fireEvent.click(getByRole('button', { name: 'Open' }))
    expect(nav.route).toHaveBeenCalledWith('/apps/chess')
  })

  it('a disabled app shows a Disabled chip and no Open button', () => {
    installedApps.value = [makeApp({ enabled: false })]
    const { getByText, queryByRole } = render(<AppsPage />)
    expect(getByText('Disabled')).toBeTruthy()
    expect(queryByRole('button', { name: 'Open' })).toBeNull()
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

  it('a check that could not reach the app list says so, with when it last answered', async () => {
    loadUpdates.mockImplementationOnce(async () => {
      catalogFreshness.value = { checkedAt: AT, stale: true, failed: false }
    })
    const { getByText } = render(<AppsPage />)
    fireEvent.click(getByText('Check for updates'))
    await vi.waitFor(() => expect(showToast).toHaveBeenCalled())
    const [message, kind] = showToast.mock.calls[0]
    expect(message).toMatch(/^Couldn't reach the app list — showing what we knew at .+\.$/)
    expect(kind).toBe('error')
    expect(showToast).not.toHaveBeenCalledWith('All apps are up to date', 'info')
  })

  it('a check that failed outright never says "up to date"', async () => {
    loadUpdates.mockImplementationOnce(async () => {
      catalogFreshness.value = { checkedAt: null, stale: false, failed: true }
    })
    const { getByText } = render(<AppsPage />)
    fireEvent.click(getByText('Check for updates'))
    await vi.waitFor(() => expect(showToast).toHaveBeenCalledWith(
      "Couldn't reach the app list. Check the connection and try again.", 'error'))
  })

  it('a stale app list shows the strip with Retry for admins (which checks again)', () => {
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

  it('the Catalog tab error box is not doubled by the strip', () => {
    catalogFreshness.value = { checkedAt: null, stale: false, failed: true }
    catalogError.value = "Couldn't reach the app list. Check the connection and try again."
    const { getByRole, getAllByText } = render(<AppsPage />)
    fireEvent.click(getByRole('tab', { name: 'Catalog' }))
    expect(getAllByText(/Couldn't reach the app list/)).toHaveLength(1)
  })
})
