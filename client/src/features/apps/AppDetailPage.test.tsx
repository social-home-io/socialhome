import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'

const h = vi.hoisted(() => ({
  installedApps: { value: [] as any[] },
  catalog: { value: [] as any[] },
  updates: { value: [] as any[] },
  appsLoading: { value: false },
  catalogLoading: { value: false },
  catalogLoaded: { value: true },
  catalogSourceHost: { value: null as string | null },
  householdHasProtectedMinor: { value: false },
  currentUser: { value: { is_admin: true } as { is_admin: boolean, protected?: boolean } | null },
  needsYou: { value: [] as any[] },
  installApp: vi.fn().mockResolvedValue(undefined),
  requestApp: vi.fn().mockResolvedValue(undefined),
  dismissRequests: vi.fn().mockResolvedValue(undefined),
  setEnabled: vi.fn().mockResolvedValue(undefined),
  updateApp: vi.fn().mockResolvedValue(undefined),
  dropHandled: vi.fn(),
  showToast: vi.fn(),
  params: { appId: 'chess' } as Record<string, string>,
  route: vi.fn(),
}))

vi.mock('@/store/apps', async (orig) => {
  const real = await orig<typeof import('@/store/apps')>()
  return {
    installedApps: h.installedApps, catalog: h.catalog, updates: h.updates,
    appsLoading: h.appsLoading, catalogLoading: h.catalogLoading, catalogLoaded: h.catalogLoaded,
    catalogSourceHost: h.catalogSourceHost, householdHasProtectedMinor: h.householdHasProtectedMinor,
    loadInstalled: vi.fn(), loadCatalog: vi.fn(), loadUpdates: vi.fn(), loadProtectionStatus: vi.fn(),
    localized: real.localized,
    installApp: h.installApp, requestApp: h.requestApp, dismissRequests: h.dismissRequests,
    setEnabled: h.setEnabled, updateApp: h.updateApp,
    setMinAge: vi.fn(), uninstallApp: vi.fn(),
  }
})
vi.mock('@/features/notifications/needsYou', () => ({
  needsYou: h.needsYou, loadNeedsYou: vi.fn(), dropHandled: h.dropHandled,
}))
vi.mock('@/features/notifications/NeedsYouSection', () => ({ NeedsYouSection: () => null }))
vi.mock('@/store/auth', () => ({ currentUser: h.currentUser }))
vi.mock('@/store/pageTitle', () => ({ useTitle: () => {} }))
vi.mock('@/components/Toast', () => ({ showToast: h.showToast }))
vi.mock('@/baseUrl', () => ({ addBase: (p: string) => p, basePath: '/' }))
vi.mock('preact-iso', async (orig) => ({
  ...(await orig<object>()),
  useLocation: () => ({ route: h.route, path: '/apps/chess/about', url: '/apps/chess/about', query: {} }),
  useRoute: () => ({ params: h.params }),
}))

import AppDetailPage from './AppDetailPage'

function entry(over: Partial<any> = {}) {
  return {
    app_id: 'chess', name: 'Chess', latest_version: '2026.10.29',
    description: 'Play chess with another household.', icon_url: null,
    capabilities: ['storage', 'federation'], permissions: ['storage', 'people', 'federation'],
    i18n: {}, bundle_size: 16198, min_age: 0, languages: ['en', 'de'],
    publisher: 'Social Home', homepage: 'https://github.com/social-home-io/socialhome-apps',
    min_core_version: '2026.10.29',
    changelog: [{ version: '2026.10.29', date: '2026-10-29', notes: { en: 'Keyboard play.', de: 'Tastatur.' } }],
    compatible: true, requested: false,
    ...over,
  }
}

function installed(over: Partial<any> = {}) {
  return {
    app_id: 'chess', name: 'Chess', version: '2026.10.28', enabled: true,
    capabilities: ['storage', 'federation'], permissions: ['storage', 'people', 'federation'],
    icon: null, min_age: 0, installed_at: '2026-10-12T09:00:00+00:00', installed_by_name: 'Anna',
    ...over,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  h.installedApps.value = []
  h.catalog.value = [entry()]
  h.updates.value = []
  h.catalogLoaded.value = true
  h.catalogSourceHost.value = null
  h.currentUser.value = { is_admin: true }
  h.needsYou.value = []
  h.params.appId = 'chess'
})

describe('AppDetailPage — what an app can do', () => {
  it('shows the permissions in full sentences and the never-list', () => {
    const { getByRole, getByText } = render(<AppDetailPage />)
    expect(getByRole('heading', { level: 1, name: 'Chess' })).toBeTruthy()
    expect(getByRole('heading', { level: 2, name: 'What Chess can do' })).toBeTruthy()
    expect(getByText('Like saved games and settings. Only Chess can read it, and only for you.')).toBeTruthy()
    expect(getByText('Only to the person you picked, encrypted home to home.')).toBeTruthy()
    expect(getByRole('heading', { level: 3, name: 'Chess can never' })).toBeTruthy()
    expect(getByText('reach the internet or other websites')).toBeTruthy()
    expect(getByText('send anything without you choosing who')).toBeTruthy()
  })

  it('lists the facts: source, publisher, rating, languages by their own names', () => {
    const { getByText, container } = render(<AppDetailPage />)
    expect(getByText("Social Home's app list")).toBeTruthy()
    expect(getByText(/From Social Home/)).toBeTruthy()
    expect(container.textContent).toContain('English, Deutsch')
    // The Languages fact names them too, not a count.
    const langs = [...container.querySelectorAll('.sh-app-facts__row')].find(r => r.querySelector('dt')?.textContent === 'Languages')
    expect(langs?.querySelector('dd')?.textContent).toBe('English, Deutsch')
    expect(getByText('Social Home 2026.10.29')).toBeTruthy()
    expect(container.textContent).toContain('Source: github.com/social-home-io/socialhome-apps')
    expect(container.textContent).not.toMatch(/verified/i)
  })

  it("shows What's new in the viewer's language", () => {
    const { getByRole, getByText } = render(<AppDetailPage />)
    expect(getByRole('heading', { level: 2, name: "What's new" })).toBeTruthy()
    expect(getByText('Keyboard play.')).toBeTruthy()
  })

  it('an admin adds it through the consent dialog', async () => {
    const { getByRole, getAllByRole } = render(<AppDetailPage />)
    fireEvent.click(getByRole('button', { name: 'Add to household' }))
    expect(getByRole('dialog', { name: 'Add Chess for everyone?' })).toBeTruthy()
    const buttons = getAllByRole('button', { name: 'Add to household' })
    fireEvent.click(buttons[buttons.length - 1])
    await vi.waitFor(() => expect(h.installApp).toHaveBeenCalledWith('chess'))
  })

  it('the consent dialog states the age gate when the catalog rates it', () => {
    h.catalog.value = [entry({ min_age: 13 })]
    const { getByRole } = render(<AppDetailPage />)
    fireEvent.click(getByRole('button', { name: 'Add to household' }))
    expect(getByRole('dialog').textContent).toContain('Everyone in your household aged 13 and over can open it.')
  })

  it('an app needing a newer Social Home offers no Add button', () => {
    h.catalog.value = [entry({ compatible: false, min_core_version: '2026.11.2' })]
    const { queryByRole, getByText } = render(<AppDetailPage />)
    expect(queryByRole('button', { name: 'Add to household' })).toBeNull()
    expect(getByText(/Needs Social Home 2026.11.2 or newer/)).toBeTruthy()
  })

  it('a member asks an admin, and nothing installs', async () => {
    h.currentUser.value = { is_admin: false }
    const { getByRole, queryByRole } = render(<AppDetailPage />)
    expect(queryByRole('button', { name: 'Add to household' })).toBeNull()
    fireEvent.click(getByRole('button', { name: 'Ask an admin for Chess' }))
    fireEvent.click(getByRole('button', { name: 'Send request' }))
    await vi.waitFor(() => expect(h.requestApp).toHaveBeenCalledWith('chess'))
    expect(h.installApp).not.toHaveBeenCalled()
  })

  it('once added: Open, who added it, and admins manage it here', async () => {
    h.installedApps.value = [installed()]
    h.updates.value = [{ app_id: 'chess', name: 'Chess', current_version: '2026.10.28', latest_version: '2026.10.29', compatible: true }]
    const { getByRole, getByText } = render(<AppDetailPage />)
    fireEvent.click(getByRole('button', { name: 'Open Chess' }))
    expect(h.route).toHaveBeenCalledWith('/apps/chess')
    expect(getByText(/Added by Anna/)).toBeTruthy()
    fireEvent.click(getByRole('button', { name: /^Update to / }))
    await vi.waitFor(() => expect(h.updateApp).toHaveBeenCalledWith('chess'))
    fireEvent.click(getByRole('button', { name: 'Turn off' }))
    expect(h.setEnabled).toHaveBeenCalledWith('chess', false)
  })

  it('Not now shows only when someone asked, and answers the requests', async () => {
    const a = render(<AppDetailPage />)
    expect(a.queryByRole('button', { name: 'Not now' })).toBeNull()
    a.unmount()
    h.needsYou.value = [{ id: 'n1', type: 'app_request', object_key: 'app:chess' }]
    const { getByRole } = render(<AppDetailPage />)
    fireEvent.click(getByRole('button', { name: 'Not now' }))
    await vi.waitFor(() => expect(h.dismissRequests).toHaveBeenCalledWith('chess'))
    expect(h.dropHandled).toHaveBeenCalledWith(['app:chess'])
  })

  it('an app nobody has says it is not available', () => {
    h.params.appId = 'nope'
    const { getByRole } = render(<AppDetailPage />)
    expect(getByRole('heading', { name: "This app isn't available" })).toBeTruthy()
  })

  it('the back link goes to /apps (inside the ingress prefix via addBase)', () => {
    const { getByRole } = render(<AppDetailPage />)
    expect(getByRole('link', { name: /Apps/ }).getAttribute('href')).toBe('/apps')
  })
})
