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
  loadUninstallPreview: vi.fn(),
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
    catalogEnabled: h.catalogEnabled, catalogSourceHost: h.catalogSourceHost, catalogLoaded: { value: true },
    householdHasProtectedMinor: h.householdHasProtectedMinor,
    catalogFreshness: h.catalogFreshness,
    catalogReached: (f = h.catalogFreshness.value) => !f.stale && !f.failed,
    loadInstalled: vi.fn(), loadCatalog: vi.fn(), loadUpdates: h.loadUpdates,
    loadProtectionStatus: vi.fn(),
    updateApp: h.updateApp, installApp: h.installApp, requestApp: h.requestApp,
    newPermissionsAsked: real.newPermissionsAsked,
    uninstallApp: h.uninstallApp, setEnabled: h.setEnabled, setMinAge: h.setMinAge,
    loadUninstallPreview: h.loadUninstallPreview,
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

import AppsPage, { FILTERS_AT, SEARCH_AT, TEASER_MAX } from './AppsPage'

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
  localStorage.clear()
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

/** Render the page on the Store tab. */
const renderStore = () => {
  nav.query = { tab: 'store' }
  return render(<AppsPage />)
}
const card = (r: ReturnType<typeof render>, name: string) =>
  r.getByRole('article', { name })

describe('Your apps | Store', () => {
  it('"Apps" has the switch beside it; Your apps first when the household has apps', () => {
    const { getByRole } = render(<AppsPage />)
    const tabs = getByRole('tablist', { name: 'Apps' })
    expect(tabs.closest('.sh-apps-head__title')?.querySelector('h1')?.textContent).toBe('Apps')
    expect(getByRole('tab', { name: 'Your apps' }).getAttribute('aria-selected')).toBe('true')
    expect(getByRole('tabpanel', { name: 'Your apps' })).toBeTruthy()
  })

  it('a click switches, deep-links the view and remembers it', () => {
    const { getByRole } = render(<AppsPage />)
    fireEvent.click(getByRole('tab', { name: 'Store' }))
    expect(nav.route).toHaveBeenCalledWith('/apps?tab=store', true)
    expect(getByRole('tabpanel', { name: 'Store' })).toBeTruthy()
    expect(localStorage.getItem('sh_apps_tab')).toBe('store')
  })

  it('?tab=store opens the Store; the remembered view is the default', () => {
    const a = renderStore()
    expect(a.getByRole('tab', { name: 'Store' }).getAttribute('aria-selected')).toBe('true')
    a.unmount()
    nav.query = {}
    localStorage.setItem('sh_apps_tab', 'store')
    const b = render(<AppsPage />)
    expect(b.getByRole('tabpanel', { name: 'Store' })).toBeTruthy()
  })

  it('puts the app Needs-you rows (invites, requests) on top of both views', () => {
    const a = render(<AppsPage />)
    expect(a.getByTestId('needs-you').textContent).toBe('app_challenge,app_challenge_continue,app_challenge_waiting,app_request')
    a.unmount()
    expect(renderStore().getByTestId('needs-you')).toBeTruthy()
  })
})

describe('Your apps', () => {
  it('lists the household\'s apps with who added them, the date never split', () => {
    const { getByRole, getByText } = render(<AppsPage />)
    expect(getByRole('heading', { level: 2, name: 'In your household' })).toBeTruthy()
    expect(getByRole('heading', { level: 3, name: 'Chess' })).toBeTruthy()
    const added = getByText(/^Added by Anna · /)
    expect(added.textContent).toMatch(/12/)
    // "12 Oct" / "Oct 12" glued with a no-break space: never "Oct" alone.
    expect(added.textContent!.replace(/^Added by Anna · /, '')).not.toContain(' ')
  })

  it('says how many of the viewer\'s games are going, and nothing when none', () => {
    installedApps.value = [
      makeApp({ games_going: 2 }),
      makeApp({ app_id: 'go', name: 'Go', games_going: 1 }),
      makeApp({ app_id: 'notes', name: 'Notes', games_going: 0 }),
    ]
    const { getByText, queryByText } = render(<AppsPage />)
    expect(getByText('2 games going')).toBeTruthy()
    expect(getByText('1 game going')).toBeTruthy()
    expect(queryByText(/0 games? going/)).toBeNull()
    // Next to who added it, in the same facts line.
    expect(getByText('2 games going').parentElement!.textContent).toMatch(/^Added by Anna · .*2 games going$/)
  })

  it('shows the rating chip only when it is not "Everyone"', () => {
    installedApps.value = [makeApp(), makeApp({ app_id: 'notes', name: 'Notes', min_age: 13 })]
    const { container } = render(<AppsPage />)
    const chips = Array.from(container.querySelectorAll('.sh-age-chip')).map(c => c.textContent)
    expect(chips).toEqual(['13+'])
  })

  it('a More apps teaser shows apps to add and leads to the Store', async () => {
    const { getByRole } = render(<AppsPage />)
    expect(getByRole('heading', { level: 2, name: 'More apps' })).toBeTruthy()
    fireEvent.click(getByRole('button', { name: 'Details about Notes' }))
    expect(nav.route).toHaveBeenCalledWith('/apps/notes/about')
    fireEvent.click(getByRole('button', { name: 'See all in the Store' }))
    expect(getByRole('tab', { name: 'Store' }).getAttribute('aria-selected')).toBe('true')
    await vi.waitFor(() => expect(document.activeElement).toBe(getByRole('tab', { name: 'Store' })))
  })

  it('the teaser shows at most three apps and none needing a newer Social Home', () => {
    catalog.value = [makeEntry(), makeEntry({ app_id: 'old', name: 'Old', compatible: false }), ...manyEntries(5)]
    const { container, queryByText } = render(<AppsPage />)
    expect(container.querySelectorAll('section[aria-labelledby="sh-apps-h-more"] li').length).toBe(TEASER_MAX)
    expect(queryByText('Old')).toBeNull()
  })

  it('a search box only from 8 apps in the household', () => {
    installedApps.value = Array.from({ length: SEARCH_AT - 1 }, (_, i) => makeApp({ app_id: `a${i}`, name: `Game ${i}` }))
    const a = render(<AppsPage />)
    expect(a.queryByRole('searchbox')).toBeNull()
    a.unmount()
    installedApps.value = Array.from({ length: SEARCH_AT }, (_, i) => makeApp({ app_id: `a${i}`, name: `Game ${i}` }))
    const b = render(<AppsPage />)
    fireEvent.input(b.getByRole('searchbox', { name: 'Search apps' }), { target: { value: 'Game 3' } })
    expect(b.getByRole('heading', { level: 3, name: 'Game 3' })).toBeTruthy()
    expect(b.queryByRole('heading', { level: 3, name: 'Game 2' })).toBeNull()
    fireEvent.input(b.getByRole('searchbox'), { target: { value: 'zebra' } })
    expect(b.getByText('No apps match “zebra”.')).toBeTruthy()
  })

  it('a turned-off app says what is kept', () => {
    installedApps.value = [makeApp({ enabled: false })]
    const { getByText } = render(<AppsPage />)
    expect(getByText('Everything saved in it is kept')).toBeTruthy()
  })

  it('nothing added yet: says so and leads to the Store', () => {
    installedApps.value = []
    nav.query = { tab: 'yours' }
    const { getByRole } = render(<AppsPage />)
    expect(getByRole('heading', { level: 2, name: 'Nothing installed yet' })).toBeTruthy()
    fireEvent.click(getByRole('button', { name: 'Browse the Store' }))
    expect(getByRole('tabpanel', { name: 'Store' })).toBeTruthy()
  })

  it('a member whose household\'s apps are all off is not told "Nothing installed yet"', () => {
    currentUser.value = { is_admin: false }
    installedApps.value = [makeApp({ enabled: false })]
    const { queryByRole, getByRole } = render(<AppsPage />)
    expect(queryByRole('heading', { name: 'Nothing installed yet' })).toBeNull()
    expect(getByRole('heading', { level: 2, name: 'More apps' })).toBeTruthy()
  })

  it('says when the app list is turned off, so the missing apps make sense', () => {
    catalogEnabled.value = false
    const { getByText } = render(<AppsPage />)
    expect(getByText(/The app catalog is turned off on this Social Home/)).toBeTruthy()
  })

  it('a word searched in one view never hides the other view\'s list', () => {
    installedApps.value = Array.from({ length: SEARCH_AT }, (_, i) => makeApp({ app_id: `a${i}`, name: `Game ${i}` }))
    const { getByRole, getAllByRole } = render(<AppsPage />)
    fireEvent.input(getByRole('searchbox'), { target: { value: 'zebra' } })
    fireEvent.click(getByRole('tab', { name: 'Store' }))
    expect((getByRole('searchbox') as HTMLInputElement).value).toBe('')
    expect(getAllByRole('article').length).toBe(2)
  })
})

describe('Store', () => {
  it('a card per app: what it can do, who it is for, size and date', () => {
    const r = renderStore()
    const notes = card(r, 'Notes')
    expect(notes.textContent).toContain('From Social Home')
    expect(notes.textContent).toContain('Take notes')
    expect(notes.textContent).toContain('Saves its own data for each person')
    expect(notes.textContent).not.toContain('Sends invites')
    const meta = notes.querySelector('.sh-store-card__meta')!
    expect(meta.textContent).toMatch(/^13\+/)
    expect(meta.textContent).toMatch(/16\s?kB/i)
    const day = meta.lastElementChild!.textContent!
    expect(day).toMatch(/29/)
    expect(day).not.toContain(' ')
    expect(card(r, 'Chess').querySelector('.sh-store-card__meta')!.textContent).toMatch(/^Everyone/)
  })

  it('an app the household has reads "Installed" with Open', () => {
    const r = renderStore()
    const chess = card(r, 'Chess')
    expect(chess.textContent).toContain('Installed')
    fireEvent.click(r.getByRole('button', { name: 'Open Chess' }))
    expect(nav.route).toHaveBeenCalledWith('/apps/chess')
    expect(r.queryByRole('button', { name: 'Install Chess' })).toBeNull()
  })

  it('an app that is turned off reads "Off" with Details', () => {
    installedApps.value = [makeApp({ enabled: false })]
    const r = renderStore()
    const chess = card(r, 'Chess')
    expect(chess.textContent).toContain('Off')
    expect(r.queryByRole('button', { name: 'Open Chess' })).toBeNull()
    expect(r.getByRole('button', { name: 'Details about Chess' })).toBeTruthy()
  })

  it('Install asks first: "Install Notes for everyone?", then adds it', async () => {
    const r = renderStore()
    fireEvent.click(r.getByRole('button', { name: 'Install Notes' }))
    const dialog = r.getByRole('dialog', { name: 'Install Notes for everyone?' })
    expect(dialog.textContent).toContain('It can never see your password')
    expect(dialog.textContent).toContain('Everyone in your household aged 13 and over can open it.')
    expect(installApp).not.toHaveBeenCalled()
    fireEvent.click(r.getByRole('button', { name: 'Install' }))
    await vi.waitFor(() => expect(installApp).toHaveBeenCalledWith('notes'))
  })

  it('always has a search box; filters from 12 apps', () => {
    catalog.value = manyEntries(2)
    const a = renderStore()
    expect(a.getByRole('searchbox', { name: 'Search apps' })).toBeTruthy()
    expect(a.queryByRole('group', { name: 'Show apps' })).toBeNull()
    a.unmount()
    installedApps.value = []
    catalog.value = manyEntries(FILTERS_AT)
    const b = renderStore()
    fireEvent.click(b.getByRole('button', { name: 'On your own' }))
    expect(b.getByRole('button', { name: 'On your own' }).getAttribute('aria-pressed')).toBe('true')
    expect(b.getByRole('heading', { level: 3, name: 'Game 1' })).toBeTruthy()
    expect(b.queryByRole('heading', { level: 3, name: 'Game 2' })).toBeNull()
  })

  it('search narrows the cards and says when nothing matches', () => {
    catalog.value = [makeEntry(), ...manyEntries(3)]
    const r = renderStore()
    fireEvent.input(r.getByRole('searchbox'), { target: { value: 'Game 2' } })
    expect(r.getByRole('heading', { level: 3, name: 'Game 2' })).toBeTruthy()
    expect(r.queryByRole('heading', { level: 3, name: 'Chess' })).toBeNull()
    fireEvent.input(r.getByRole('searchbox'), { target: { value: 'zebra' } })
    expect(r.getByText('No apps match “zebra”.')).toBeTruthy()
  })

  it('names apps in the viewer\'s language', () => {
    h.locale.value = 'de'
    const r = renderStore()
    expect(r.getByRole('heading', { level: 3, name: 'Schach' })).toBeTruthy()
  })

  it('an app needing a newer Social Home says so and offers no Install', () => {
    catalog.value = [makeEntry({ app_id: 'go', name: 'Go', compatible: false })]
    const r = renderStore()
    expect(r.getByText('Needs a newer Social Home')).toBeTruthy()
    expect(r.queryByRole('button', { name: 'Install Go' })).toBeNull()
  })

  it('a group game shows how many play', () => {
    catalog.value = [makeEntry({ app_id: 'gift', name: 'Gift Draw', players: { min: 3, max: 12 } })]
    const r = renderStore()
    expect(card(r, 'Gift Draw').textContent).toContain('3–12 players')
  })

  it('ends with the sealed-box line, only what is true of every app', () => {
    const r = renderStore()
    expect(r.getByText("Apps run in a sealed box: they never see your password, posts, chats, photos or location, and can't reach the internet.")).toBeTruthy()
  })
})

describe('First run (nothing added yet)', () => {
  beforeEach(() => {
    installedApps.value = []
    catalog.value = [makeEntry()]
  })

  it('stays on the Store after the first app is added, with focus on its Open', async () => {
    installApp.mockImplementationOnce(async () => { installedApps.value = [makeApp()] })
    const { getByRole } = render(<AppsPage />)
    fireEvent.click(getByRole('button', { name: 'Install Chess' }))
    fireEvent.click(getByRole('button', { name: 'Install' }))
    // Re-render the page (the store's signal does that in the app).
    // (The store's signal re-renders the page in the app.)
    fireEvent.input(getByRole('searchbox'), { target: { value: 'c' } })
    await vi.waitFor(() => expect(getByRole('button', { name: 'Open Chess' })).toBeTruthy())
    expect(getByRole('tabpanel', { name: 'Store' })).toBeTruthy()
    await vi.waitFor(() => expect(document.activeElement).toBe(getByRole('button', { name: 'Open Chess' })))
  })

  it('opens the Store, with the intro and the three permissions', () => {
    const { getByRole, getByText, queryByText } = render(<AppsPage />)
    expect(getByRole('tabpanel', { name: 'Store' })).toBeTruthy()
    expect(getByText(/Small apps for your household/)).toBeTruthy()
    expect(getByText('Saves its own data for each person')).toBeTruthy()
    expect(getByText('Lets you pick people to play with')).toBeTruthy()
    expect(getByText('Sends invites and moves to other households')).toBeTruthy()
    expect(getByText(/More apps are on the way/)).toBeTruthy()
    // Never "verified".
    expect(queryByText(/verified/i)).toBeNull()
    expect(getByRole('button', { name: 'Install Chess' })).toBeTruthy()
  })

  it('the consent says what the app can do, for everyone', async () => {
    const { getByRole } = render(<AppsPage />)
    fireEvent.click(getByRole('button', { name: 'Install Chess' }))
    const dialog = getByRole('dialog', { name: 'Install Chess for everyone?' })
    expect(dialog.textContent).toContain('Chess will be able to:')
    expect(dialog.textContent).toContain("You choose someone in Social Home. Chess learns only who you picked and people you've played with.")
    expect(dialog.textContent).toContain('Everyone in your household can open it.')
    expect(dialog.textContent).toContain('Protected accounts play only with people they can already message.')
    fireEvent.click(getByRole('button', { name: 'Install' }))
    await vi.waitFor(() => expect(installApp).toHaveBeenCalledWith('chess'))
  })

  it('a member sees Ask an admin instead, and the request is sent from the dialog', async () => {
    currentUser.value = { is_admin: false }
    const { getByRole, queryByRole } = render(<AppsPage />)
    expect(queryByRole('button', { name: 'Install Chess' })).toBeNull()
    fireEvent.click(getByRole('button', { name: 'Ask an admin for Chess' }))
    const dialog = getByRole('dialog', { name: 'Ask an admin for Chess?' })
    expect(dialog.textContent).toContain("They'll see what the app can do before installing it.")
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
    const { getAllByText } = render(<AppsPage />)
    expect(getAllByText(/From apps\.example\.org, set by your server operator/).length).toBeGreaterThan(0)
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

  it('an app the household doesn\'t have opens the Store on its card', () => {
    localStorage.setItem('sh_apps_tab', 'yours')
    nav.query = { app: 'notes' }
    const r = render(<AppsPage />)
    const notes = card(r, 'Notes')
    expect(notes.classList.contains('sh-store-card--picked')).toBe(true)
    expect(document.activeElement).toBe(notes)
  })

  it('an app that is gone picks nothing out', () => {
    nav.query = { app: 'gone' }
    const { container } = render(<AppsPage />)
    expect(container.querySelector('.sh-apps-row--picked, .sh-store-card--picked')).toBeNull()
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

  it('floats above the page so the clipped app list never cuts it off', () => {
    const { getByRole } = render(<AppsPage />)
    fireEvent.click(getByRole('button', { name: 'Chess settings' }))
    expect(getByRole('menu').classList.contains('sh-timetable-menu--floating')).toBe(true)
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
    expect(getByText(/28.* → .*29/)).toBeTruthy()
    fireEvent.click(getByRole('button', { name: "What's new in Chess" }))
    expect(nav.route).toHaveBeenCalledWith('/apps/chess/about#whats-new')
    expect(getByText(/Same permissions/)).toBeTruthy()
    fireEvent.click(getByRole('button', { name: 'Update Chess' }))
    // Same permissions: one tap, no question.
    await vi.waitFor(() => expect(updateApp).toHaveBeenCalledWith('chess', []))
  })

  it('an update asking for more says so and asks before updating', async () => {
    updates.value = [{
      app_id: 'chess', name: 'Chess', current_version: '2026.10.21', latest_version: '2026.10.28',
      compatible: true, new_permissions: ['federation'],
      changelog: [{ version: '2026.10.28', date: null, notes: { en: 'Rematch with one tap.' } }],
    }]
    const { getByRole, getByText, findByRole } = render(<AppsPage />)
    expect(getByText('Asks for a new permission')).toBeTruthy()
    fireEvent.click(getByRole('button', { name: 'Review the update for Chess' }))
    const dialog = await findByRole('dialog')
    expect(dialog.textContent).toContain('Rematch with one tap.')
    expect(dialog.textContent).toContain('This version asks for one new permission.')
    expect(updateApp).not.toHaveBeenCalled()
    fireEvent.click(getByRole('button', { name: 'Accept and update' }))
    await vi.waitFor(() => expect(updateApp).toHaveBeenCalledWith('chess', ['federation']))
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
    const { getByText, queryByText } = renderStore()
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

describe('Uninstall says what will be lost (D9)', () => {
  const preview = (over: Partial<any> = {}) => ({
    people: 4, games: 3, invites: 1,
    told: ['Ben · Familie Okafor', 'Clara', 'Ömer'], told_more: 0, untold: 0,
    ...over,
  })
  const open = () => {
    const r = render(<AppsPage />)
    fireEvent.click(r.getByRole('button', { name: 'Chess settings' }))
    fireEvent.click(r.getByRole('menuitem', { name: 'Uninstall…' }))
    return r
  }
  const text = (el: Element) => (el.textContent ?? '').replace(/\s+/g, ' ').trim()

  it('counts people, games and invites, and names who will be told', async () => {
    h.loadUninstallPreview.mockResolvedValue(preview())
    const { getByRole } = open()
    const dialog = getByRole('alertdialog', { name: 'Uninstall Chess?' })
    expect(h.loadUninstallPreview).toHaveBeenCalledWith('chess')
    await vi.waitFor(() => expect(dialog.querySelectorAll('li').length).toBe(3))
    const lines = Array.from(dialog.querySelectorAll('li')).map(text)
    expect(lines).toEqual([
      '▣4 people lose their saved games and settings.',
      '⇄3 games with other households end. Ben · Familie Okafor, Clara, and Ömer will be told.',
      '✉1 open invite is withdrawn.',
    ])
    expect(dialog.querySelector('strong')?.textContent).toBe('4 people')
    // Never what the games hold: counts and names only.
    expect(text(dialog)).toContain('Want to keep everything? Turn it off instead.')
  })

  it('"Turn off instead" is the default and keeps everything', async () => {
    h.loadUninstallPreview.mockResolvedValue(preview())
    const { getByRole } = open()
    const keep = getByRole('button', { name: 'Turn off instead' })
    await vi.waitFor(() => expect(document.activeElement).toBe(keep))
    fireEvent.click(keep)
    await vi.waitFor(() => expect(setEnabled).toHaveBeenCalledWith('chess', false))
    expect(h.uninstallApp).not.toHaveBeenCalled()
    await vi.waitFor(() => expect(showToast).toHaveBeenCalledWith('Chess is off. Everything is kept.', 'success'))
  })

  it('"Uninstall and delete" uninstalls', async () => {
    h.loadUninstallPreview.mockResolvedValue(preview())
    const { getByRole } = open()
    fireEvent.click(getByRole('button', { name: 'Uninstall and delete' }))
    await vi.waitFor(() => expect(h.uninstallApp).toHaveBeenCalledWith('chess'))
    expect(setEnabled).not.toHaveBeenCalled()
    await vi.waitFor(() => expect(showToast).toHaveBeenCalledWith('Chess uninstalled.', 'info'))
  })

  it('says who can\'t be told on an older Social Home, and counts names past three', async () => {
    h.loadUninstallPreview.mockResolvedValue(preview({
      people: 1, games: 5, invites: 0, told_more: 2, untold: 1,
    }))
    const { findByText, getByRole } = open()
    expect(await findByText(/1 person/)).toBeTruthy()
    const dialog = getByRole('alertdialog')
    expect(text(dialog)).toContain('1 person loses their saved games and settings.')
    expect(text(dialog)).toContain(
      '5 games with other households end. Ben · Familie Okafor, Clara, Ömer, and 2 more will be told. '
      + "1 of them won't be told: their household has an older Social Home.",
    )
  })

  it('a game only with an older household: nobody is told', async () => {
    h.loadUninstallPreview.mockResolvedValue(preview({ people: 0, games: 1, invites: 0, told: [], untold: 1 }))
    const { findByText } = open()
    expect(await findByText(/Their household has an older Social Home, so they won't be told\./)).toBeTruthy()
  })

  it('counts group games with other households (v_76) — on their own, they still count', async () => {
    h.loadUninstallPreview.mockResolvedValue(preview({ people: 0, games: 0, invites: 0, told: [], tables: 2 }))
    const { getByRole } = open()
    const dialog = getByRole('alertdialog')
    await vi.waitFor(() => expect(dialog.querySelectorAll('li').length).toBe(1))
    expect(text(dialog.querySelector('li')!)).toBe(
      "⇶2 group games with other households end, or your household's players leave them.",
    )
  })

  it('nothing to lose says so', async () => {
    h.loadUninstallPreview.mockResolvedValue(preview({ people: 0, games: 0, invites: 0, told: [] }))
    const { findByText } = open()
    expect(await findByText('Nobody has saved anything in Chess yet, and nobody plays it with another household.')).toBeTruthy()
  })

  it('a failed count still warns and still lets the admin decide', async () => {
    h.loadUninstallPreview.mockRejectedValue(new Error('API 500'))
    const { findByText, getByRole } = open()
    expect(await findByText(/Everyone's saved games and settings are deleted/)).toBeTruthy()
    fireEvent.click(getByRole('button', { name: 'Uninstall and delete' }))
    await vi.waitFor(() => expect(h.uninstallApp).toHaveBeenCalledWith('chess'))
  })

  it('an app already off offers to keep it instead of turning it off', async () => {
    installedApps.value = [makeApp({ enabled: false })]
    h.loadUninstallPreview.mockResolvedValue(preview())
    const { getByRole, queryByRole, queryByText } = open()
    expect(queryByRole('button', { name: 'Turn off instead' })).toBeNull()
    expect(queryByText(/Chess is turned off: everything is kept/)).toBeTruthy()
    fireEvent.click(getByRole('button', { name: 'Keep Chess' }))
    await vi.waitFor(() => expect(queryByRole('alertdialog')).toBeNull())
    expect(h.uninstallApp).not.toHaveBeenCalled()
  })
})
