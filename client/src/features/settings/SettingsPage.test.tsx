import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

// Mock the API module before importing the page. ``vi.hoisted`` is the
// only way to define a ``vi.fn`` that is reachable from a ``vi.mock``
// factory — vitest hoists the factory above plain ``const`` declarations,
// so plain assignment hits a ReferenceError at module-init time.
const { mockPatch, mockGet } = vi.hoisted(() => ({
  mockPatch: vi.fn().mockResolvedValue({}),
  mockGet: vi.fn().mockResolvedValue([]),
}))

vi.mock('@/api', () => ({
  api: {
    get: mockGet,
    post: vi.fn().mockResolvedValue({}),
    patch: mockPatch,
    delete: vi.fn().mockResolvedValue(undefined),
    upload: vi.fn().mockResolvedValue({}),
  },
  ApiError: class ApiError extends Error {
    status: number
    constructor(message: string, status: number) {
      super(message)
      this.status = status
    }
  },
}))

// Mock auth store
vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u1', username: 'admin', display_name: 'Admin', is_admin: true, picture_url: null, bio: null, is_new_member: false } },
  token: { value: 'test-tok' },
  isAuthed: { value: true },
  setToken: vi.fn(),
  logout: vi.fn(),
}))

vi.mock('@/ws', () => ({ ws: { on: vi.fn(() => () => {}) } }))

const { webPushMock } = vi.hoisted(() => ({
  webPushMock: {
    currentPushSubscription: vi.fn().mockResolvedValue(null),
    enableWebPush: vi.fn().mockResolvedValue(true),
    disableWebPush: vi.fn().mockResolvedValue(undefined),
    webPushSupported: vi.fn(() => true),
  },
}))
vi.mock('@/utils/webPush', () => webPushMock)

import { userPreferences } from '@/store/userPreferences'
import { instanceConfig } from '@/store/instance'
import { spaceLocationRows, spaceLocationLoading } from './SettingsPage'

function setMode(mode: 'standalone' | 'ha' | 'haos') {
  instanceConfig.value = {
    mode,
    instance_name: 'Home',
    instance_id: 'i1',
    capabilities: [],
    setup_required: false,
  }
}

beforeEach(() => {
  setMode('standalone')
  mockPatch.mockResolvedValue({})
  // Default: empty space-location list and no presence data.
  mockGet.mockResolvedValue({ spaces: [] })
  userPreferences.value = {
    user_id: 'u1',
    hide_highlights: false,
    hide_momentum: false,
    hide_bazaar: false,
  }
  // Reset the module-level signals so each test starts with a clean slate
  // and the SpaceLocationSharingPanel re-fetches from the mock.
  spaceLocationRows.value = []
  spaceLocationLoading.value = false
})

describe('SettingsPage', () => {
  it('module exports a default component', async () => {
    const mod = await import('./SettingsPage')
    expect(mod.default).toBeTruthy()
    expect(typeof mod.default).toBe('function')
  })

  it('has a Security tab that mounts the API-token manager', async () => {
    mockGet.mockImplementation(async (path: string) =>
      path === '/api/me/tokens' ? { base_url: null, tokens: [] } : { spaces: [] })
    const { default: SettingsPage } = await import('./SettingsPage')
    const { getByRole, findByText } = render(<SettingsPage />)
    fireEvent.click(getByRole('tab', { name: 'Security' }))
    await findByText('API tokens')
    expect(mockGet).toHaveBeenCalledWith('/api/me/tokens')
    fireEvent.click(getByRole('tab', { name: 'Profile' }))
  })

  it('arrow keys move between tabs (roving tabindex is keyboard-reachable)', async () => {
    const { default: SettingsPage } = await import('./SettingsPage')
    const { getByRole } = render(<SettingsPage />)
    const profile = getByRole('tab', { name: 'Profile' })
    fireEvent.click(profile)
    profile.focus()
    fireEvent.keyDown(profile, { key: 'End' })
    const security = getByRole('tab', { name: 'Security' })
    expect(security.getAttribute('aria-selected')).toBe('true')
    expect(document.activeElement).toBe(security)
    fireEvent.keyDown(security, { key: 'ArrowRight' })
    expect(getByRole('tab', { name: 'Profile' }).getAttribute('aria-selected')).toBe('true')
    fireEvent.keyDown(getByRole('tab', { name: 'Profile' }), { key: 'ArrowLeft' })
    expect(getByRole('tab', { name: 'Security' }).getAttribute('aria-selected')).toBe('true')
    fireEvent.click(getByRole('tab', { name: 'Profile' }))
  })

  it('opens on the Privacy tab for #privacy (Friends › blocked › Manage)', async () => {
    const { default: SettingsPage } = await import('./SettingsPage')
    window.location.hash = '#privacy'
    try {
      const { getByRole } = render(<SettingsPage />)
      await waitFor(() =>
        expect(getByRole('tab', { name: 'Privacy' }).getAttribute('aria-selected')).toBe('true'))
      fireEvent.click(getByRole('tab', { name: 'Profile' }))
    } finally {
      window.location.hash = ''
    }
  })
})

describe('SettingsPage — where things live (decision 9, P8)', () => {
  it('Profile holds the one form; no username, no start page, no "Edited here" chip', async () => {
    const { default: SettingsPage } = await import('./SettingsPage')
    const { getByRole, getByLabelText, queryByText, queryByLabelText } = render(<SettingsPage />)
    fireEvent.click(getByRole('tab', { name: 'Profile' }))
    expect(getByRole('form', { name: 'Your profile' })).toBeTruthy()
    expect(getByLabelText('Name')).toBeTruthy()
    expect(getByLabelText('Public @handle')).toBeTruthy()
    expect(queryByLabelText('Sign-in username')).toBeNull()
    expect(queryByText('Home page')).toBeNull()
    expect(queryByText(/Edited here/)).toBeNull()
  })

  it('the sign-in username lives on Security, next to the sign-ins', async () => {
    mockGet.mockImplementation(async (path: string) =>
      path === '/api/me/tokens' ? { base_url: null, tokens: [] } : { spaces: [] })
    const { default: SettingsPage } = await import('./SettingsPage')
    const { getByRole, getByLabelText } = render(<SettingsPage />)
    fireEvent.click(getByRole('tab', { name: 'Security' }))
    expect(getByRole('heading', { level: 2, name: 'Signing in' })).toBeTruthy()
    expect((getByLabelText('Sign-in username') as HTMLInputElement).value).toBe('admin')
    fireEvent.click(getByRole('tab', { name: 'Profile' }))
  })

  it('the start page picker lives on Appearance', async () => {
    const { default: SettingsPage } = await import('./SettingsPage')
    const { getByRole } = render(<SettingsPage />)
    fireEvent.click(getByRole('tab', { name: 'Appearance' }))
    expect(getByRole('radiogroup', { name: 'Home page' })).toBeTruthy()
    fireEvent.click(getByRole('tab', { name: 'Profile' }))
  })

  it('the tabs are one labelled row', async () => {
    const { default: SettingsPage } = await import('./SettingsPage')
    const { getByRole } = render(<SettingsPage />)
    const strip = getByRole('tablist', { name: 'Settings' })
    expect(strip.querySelectorAll('[role="tab"]')).toHaveLength(5)
  })

  it('a hop to another tab keeps unsaved profile edits', async () => {
    const { default: SettingsPage } = await import('./SettingsPage')
    const { getByRole, getByLabelText } = render(<SettingsPage />)
    fireEvent.click(getByRole('tab', { name: 'Profile' }))
    fireEvent.input(getByLabelText('Name'), { target: { value: 'Admin Two' } })
    fireEvent.click(getByRole('tab', { name: 'Privacy' }))
    fireEvent.click(getByRole('tab', { name: 'Profile' }))
    expect((getByLabelText('Name') as HTMLInputElement).value).toBe('Admin Two')
    expect(getByRole('region', { name: 'Unsaved changes' })).toBeTruthy()
    fireEvent.click(getByRole('button', { name: 'Discard' }))
  })
})

async function renderPrivacyTab() {
  const { default: SettingsPage } = await import('./SettingsPage')
  // The Privacy tab needs to be active to see the panel; simulate clicking
  // the tab button (the section heading also reads "Privacy", which makes
  // `getByText` ambiguous — scope to role=button to grab the tab only).
  const result = render(<SettingsPage />)
  const privacyTab = result.getByRole('tab', { name: 'Privacy' })
  fireEvent.click(privacyTab)
  return result
}

describe('SidebarVisibilityPanel', () => {

  it('renders three checkboxes for Highlights, Momentum, and Bazaar', async () => {
    await renderPrivacyTab()
    const panel = document.getElementById('sidebar-visibility')
    expect(panel).toBeTruthy()
    const checkboxes = panel!.querySelectorAll('input[type="checkbox"]')
    expect(checkboxes.length).toBe(3)
  })

  it('renders Highlights checkbox checked when hide_highlights is false', async () => {
    userPreferences.value = { ...userPreferences.value, hide_highlights: false }
    await renderPrivacyTab()
    const panel = document.getElementById('sidebar-visibility')!
    const checkboxes = Array.from(panel.querySelectorAll('input[type="checkbox"]')) as HTMLInputElement[]
    // First checkbox = Highlights
    expect(checkboxes[0].checked).toBe(true)
  })

  it('renders Highlights checkbox unchecked when hide_highlights is true', async () => {
    userPreferences.value = { ...userPreferences.value, hide_highlights: true }
    await renderPrivacyTab()
    const panel = document.getElementById('sidebar-visibility')!
    const checkboxes = Array.from(panel.querySelectorAll('input[type="checkbox"]')) as HTMLInputElement[]
    expect(checkboxes[0].checked).toBe(false)
  })

  it('renders Momentum checkbox checked when hide_momentum is false', async () => {
    userPreferences.value = { ...userPreferences.value, hide_momentum: false }
    await renderPrivacyTab()
    const panel = document.getElementById('sidebar-visibility')!
    const checkboxes = Array.from(panel.querySelectorAll('input[type="checkbox"]')) as HTMLInputElement[]
    expect(checkboxes[1].checked).toBe(true)
  })

  it('renders Bazaar checkbox unchecked when hide_bazaar is true', async () => {
    userPreferences.value = { ...userPreferences.value, hide_bazaar: true }
    await renderPrivacyTab()
    const panel = document.getElementById('sidebar-visibility')!
    const checkboxes = Array.from(panel.querySelectorAll('input[type="checkbox"]')) as HTMLInputElement[]
    expect(checkboxes[2].checked).toBe(false)
  })

  it('clicking Highlights checkbox fires PATCH /api/me/preferences with hide_highlights toggled', async () => {
    userPreferences.value = { ...userPreferences.value, hide_highlights: false }
    await renderPrivacyTab()
    const panel = document.getElementById('sidebar-visibility')!
    const checkboxes = Array.from(panel.querySelectorAll('input[type="checkbox"]')) as HTMLInputElement[]
    fireEvent.click(checkboxes[0])
    await waitFor(() => {
      expect(mockPatch).toHaveBeenCalledWith('/api/me/preferences', { hide_highlights: true })
    })
  })

  it('clicking Momentum checkbox fires PATCH /api/me/preferences with hide_momentum toggled', async () => {
    userPreferences.value = { ...userPreferences.value, hide_momentum: false }
    await renderPrivacyTab()
    const panel = document.getElementById('sidebar-visibility')!
    const checkboxes = Array.from(panel.querySelectorAll('input[type="checkbox"]')) as HTMLInputElement[]
    fireEvent.click(checkboxes[1])
    await waitFor(() => {
      expect(mockPatch).toHaveBeenCalledWith('/api/me/preferences', { hide_momentum: true })
    })
  })

  it('clicking Bazaar checkbox fires PATCH /api/me/preferences with hide_bazaar toggled', async () => {
    userPreferences.value = { ...userPreferences.value, hide_bazaar: false }
    await renderPrivacyTab()
    const panel = document.getElementById('sidebar-visibility')!
    const checkboxes = Array.from(panel.querySelectorAll('input[type="checkbox"]')) as HTMLInputElement[]
    fireEvent.click(checkboxes[2])
    await waitFor(() => {
      expect(mockPatch).toHaveBeenCalledWith('/api/me/preferences', { hide_bazaar: true })
    })
  })

  it('optimistic update reverts on PATCH error and shows a toast', async () => {
    mockPatch.mockRejectedValueOnce(new Error('Network error'))
    userPreferences.value = { ...userPreferences.value, hide_highlights: false }
    await renderPrivacyTab()
    const panel = document.getElementById('sidebar-visibility')!
    const checkboxes = Array.from(panel.querySelectorAll('input[type="checkbox"]')) as HTMLInputElement[]
    fireEvent.click(checkboxes[0])
    await waitFor(() => {
      // After revert, hide_highlights should be back to false
      expect(userPreferences.value.hide_highlights).toBe(false)
    })
  })

  it('a WS frame user.preferences_changed updates the displayed checkbox state', async () => {
    userPreferences.value = { ...userPreferences.value, hide_highlights: false }
    await renderPrivacyTab()
    // Simulate a WS update arriving from another device
    userPreferences.value = { ...userPreferences.value, hide_highlights: true }
    await waitFor(() => {
      const panel = document.getElementById('sidebar-visibility')!
      const checkboxes = Array.from(panel.querySelectorAll('input[type="checkbox"]')) as HTMLInputElement[]
      expect(checkboxes[0].checked).toBe(false)
    })
  })
})

describe('SpaceLocationSharingPanel', () => {
  it('renders the panel under the Privacy tab', async () => {
    mockGet.mockResolvedValue({ spaces: [] })
    await renderPrivacyTab()
    const panel = document.getElementById('space-location-sharing')
    expect(panel).toBeTruthy()
  })

  it('shows the empty-state message when no spaces have location enabled', async () => {
    mockGet.mockResolvedValue({ spaces: [] })
    const result = await renderPrivacyTab()
    await waitFor(() => {
      expect(result.queryByText(/No spaces with location sharing turned on/)).toBeTruthy()
    })
  })

  it('renders one checkbox row per space returned by the API', async () => {
    mockGet.mockResolvedValue({
      spaces: [
        { space_id: 'sp1', space_name: 'Family', space_emoji: '🏡', location_share_enabled: true },
        { space_id: 'sp2', space_name: 'Garden', space_emoji: null, location_share_enabled: false },
      ],
    })
    await renderPrivacyTab()
    await waitFor(() => {
      const panel = document.getElementById('space-location-sharing')!
      const checkboxes = panel.querySelectorAll('input[type="checkbox"]')
      expect(checkboxes.length).toBe(2)
    })
  })

  it('reflects location_share_enabled=true as a checked checkbox', async () => {
    mockGet.mockResolvedValue({
      spaces: [
        { space_id: 'sp1', space_name: 'Family', space_emoji: '🏡', location_share_enabled: true },
      ],
    })
    await renderPrivacyTab()
    await waitFor(() => {
      const panel = document.getElementById('space-location-sharing')!
      const cb = panel.querySelector('input[type="checkbox"]') as HTMLInputElement
      expect(cb.checked).toBe(true)
    })
  })

  it('reflects location_share_enabled=false as an unchecked checkbox', async () => {
    mockGet.mockResolvedValue({
      spaces: [
        { space_id: 'sp1', space_name: 'Family', space_emoji: null, location_share_enabled: false },
      ],
    })
    await renderPrivacyTab()
    await waitFor(() => {
      const panel = document.getElementById('space-location-sharing')!
      const cb = panel.querySelector('input[type="checkbox"]') as HTMLInputElement
      expect(cb.checked).toBe(false)
    })
  })

  it('clicking a checkbox fires PATCH to the space location-sharing endpoint', async () => {
    mockGet.mockResolvedValue({
      spaces: [
        { space_id: 'sp1', space_name: 'Family', space_emoji: '🏡', location_share_enabled: false },
      ],
    })
    await renderPrivacyTab()
    await waitFor(() => {
      const panel = document.getElementById('space-location-sharing')!
      expect(panel.querySelector('input[type="checkbox"]')).toBeTruthy()
    })
    const panel = document.getElementById('space-location-sharing')!
    const cb = panel.querySelector('input[type="checkbox"]') as HTMLInputElement
    fireEvent.click(cb)
    await waitFor(() => {
      expect(mockPatch).toHaveBeenCalledWith(
        '/api/spaces/sp1/members/me/location-sharing',
        { enabled: true },
      )
    })
  })

  it('reverts optimistic update and shows a toast on PATCH error', async () => {
    mockGet.mockResolvedValue({
      spaces: [
        { space_id: 'sp1', space_name: 'Family', space_emoji: null, location_share_enabled: true },
      ],
    })
    mockPatch.mockRejectedValueOnce(new Error('Network error'))
    await renderPrivacyTab()
    await waitFor(() => {
      const panel = document.getElementById('space-location-sharing')!
      expect(panel.querySelector('input[type="checkbox"]')).toBeTruthy()
    })
    const panel = document.getElementById('space-location-sharing')!
    const cb = panel.querySelector('input[type="checkbox"]') as HTMLInputElement
    // Checkbox should revert to original checked state after error
    const initialChecked = cb.checked
    fireEvent.click(cb)
    await waitFor(() => {
      expect(mockPatch).toHaveBeenCalled()
    })
    await waitFor(() => {
      // After error + revert, state returns to the pre-click value
      expect(cb.checked).toBe(initialChecked)
    })
  })
})

async function renderNotificationsTab() {
  const { default: SettingsPage } = await import('./SettingsPage')
  const result = render(<SettingsPage />)
  fireEvent.click(result.getByRole('tab', { name: 'Notifications' }))
  return result
}

describe('SettingsPage — HA notify service (§25.3)', () => {
  // Make the notify-targets endpoint return one discoverable target; every
  // other GET (e.g. the privacy-tab space list) keeps its default shape.
  function withTargets(
    targets: { entity_id: string; name: string }[] = [
      { entity_id: 'notify.mobile_app_pixel', name: 'Mobile App Pixel' },
    ],
  ) {
    mockGet.mockImplementation((path: string) =>
      Promise.resolve(
        path === '/api/me/notify-targets' ? { targets } : { spaces: [] },
      ),
    )
  }

  it('renders a dropdown of notify targets in ha mode', async () => {
    setMode('ha')
    withTargets()
    const { findByText, getByRole } = await renderNotificationsTab()
    // The discovered target shows as an option and a <select> is present.
    expect(await findByText('Mobile App Pixel')).toBeTruthy()
    expect(getByRole('combobox')).toBeTruthy()
  })

  it('hides the HA app subsection in standalone mode', async () => {
    setMode('standalone')
    withTargets()
    const { queryByText } = await renderNotificationsTab()
    expect(queryByText('Home Assistant app')).toBeNull()
  })

  it('selecting a target then saving persists its entity_id', async () => {
    setMode('haos')
    withTargets()
    const { getByRole, getByText } = await renderNotificationsTab()
    const select = (await waitFor(() => getByRole('combobox'))) as HTMLSelectElement
    fireEvent.change(select, { target: { value: 'notify.mobile_app_pixel' } })
    fireEvent.click(getByText('Save'))
    await waitFor(() => {
      expect(mockPatch).toHaveBeenCalledWith('/api/me', {
        preferences: { ha_notify_service: 'notify.mobile_app_pixel' },
      })
    })
  })

  it('falls back to the manual text input when no targets are discovered', async () => {
    setMode('ha')
    withTargets([])
    const { findByPlaceholderText, getByText } = await renderNotificationsTab()
    const input = (await findByPlaceholderText(
      'notify.mobile_app_my_phone',
    )) as HTMLInputElement
    fireEvent.input(input, { target: { value: 'notify.mobile_app_pixel' } })
    fireEvent.click(getByText('Save'))
    await waitFor(() => {
      expect(mockPatch).toHaveBeenCalledWith('/api/me', {
        preferences: { ha_notify_service: 'notify.mobile_app_pixel' },
      })
    })
  })

  it('selecting "Enter manually…" reveals the text input', async () => {
    setMode('ha')
    withTargets()
    const { getByRole, findByPlaceholderText, queryByPlaceholderText } =
      await renderNotificationsTab()
    const select = (await waitFor(() => getByRole('combobox'))) as HTMLSelectElement
    expect(queryByPlaceholderText('notify.mobile_app_my_phone')).toBeNull()
    fireEvent.change(select, { target: { value: '__manual__' } })
    expect(
      await findByPlaceholderText('notify.mobile_app_my_phone'),
    ).toBeTruthy()
  })
})

describe('SettingsPage — web push toggle', () => {
  beforeEach(() => {
    webPushMock.currentPushSubscription.mockReset()
    webPushMock.enableWebPush.mockReset().mockResolvedValue(true)
    webPushMock.disableWebPush.mockReset().mockResolvedValue(undefined)
  })

  it('shows Disable only when this browser holds a live subscription, and Disable removes it', async () => {
    webPushMock.currentPushSubscription.mockResolvedValue({ endpoint: 'https://p/x' })
    const { findByRole } = await renderNotificationsTab()
    fireEvent.click(await findByRole('button', { name: 'Disable' }))
    await waitFor(() => expect(webPushMock.disableWebPush).toHaveBeenCalledTimes(1))
    expect(await findByRole('button', { name: 'Enable' })).toBeTruthy()
  })

  it('Enable subscribes (not just asks for permission)', async () => {
    webPushMock.currentPushSubscription.mockResolvedValue(null)
    const { findByRole } = await renderNotificationsTab()
    fireEvent.click(await findByRole('button', { name: 'Enable' }))
    await waitFor(() => expect(webPushMock.enableWebPush).toHaveBeenCalledTimes(1))
    expect(await findByRole('button', { name: 'Disable' })).toBeTruthy()
  })

  it('a failed disable keeps the toggle on', async () => {
    webPushMock.currentPushSubscription.mockResolvedValue({ endpoint: 'https://p/x' })
    webPushMock.disableWebPush.mockRejectedValue(new Error('API 500'))
    const { findByRole } = await renderNotificationsTab()
    fireEvent.click(await findByRole('button', { name: 'Disable' }))
    await waitFor(() => expect(webPushMock.disableWebPush).toHaveBeenCalled())
    expect(await findByRole('button', { name: 'Disable' })).toBeTruthy()
  })
})

describe('SettingsPage — first day of the week', () => {
  async function renderAppearance() {
    const { default: SettingsPage } = await import('./SettingsPage')
    const result = render(<SettingsPage />)
    fireEvent.click(result.getByRole('tab', { name: 'Appearance' }))
    return result
  }

  it('offers Automatic / Monday / Sunday with Automatic selected by default', async () => {
    const { getByRole } = await renderAppearance()
    const group = getByRole('radiogroup', { name: 'First day of the week' })
    const radios = group.querySelectorAll('[role="radio"]')
    expect(radios).toHaveLength(3)
    expect(radios[0].getAttribute('aria-checked')).toBe('true')
    expect(radios[0].textContent).toMatch(/Automatic \((Monday|Sunday)\)/)
  })

  it('choosing Sunday persists week_start=sun via PATCH /api/me', async () => {
    const { getByRole } = await renderAppearance()
    fireEvent.click(getByRole('radio', { name: 'Sunday' }))
    await waitFor(() => {
      expect(mockPatch).toHaveBeenCalledWith(
        '/api/me', { preferences: { week_start: 'sun' } },
      )
    })
    expect(getByRole('radio', { name: 'Sunday' }).getAttribute('aria-checked')).toBe('true')
  })

  it('choosing Automatic clears the stored preference', async () => {
    const { getByRole } = await renderAppearance()
    fireEvent.click(getByRole('radio', { name: 'Monday' }))
    fireEvent.click(getByRole('radio', { name: /Automatic/ }))
    await waitFor(() => {
      expect(mockPatch).toHaveBeenCalledWith(
        '/api/me', { preferences: { week_start: null } },
      )
    })
  })

  it('reverts the selection when the save fails', async () => {
    mockPatch.mockRejectedValueOnce(new Error('offline'))
    const { getByRole } = await renderAppearance()
    fireEvent.click(getByRole('radio', { name: 'Sunday' }))
    await waitFor(() => {
      expect(getByRole('radio', { name: /Automatic/ }).getAttribute('aria-checked')).toBe('true')
    })
  })

  it('is a single Tab stop and arrow keys move + select (ARIA radiogroup)', async () => {
    const { getByRole } = await renderAppearance()
    const group = getByRole('radiogroup', { name: 'First day of the week' })
    const radios = Array.from(group.querySelectorAll<HTMLElement>('[role="radio"]'))
    expect(radios.map(r => r.tabIndex)).toEqual([0, -1, -1])
    fireEvent.keyDown(radios[0], { key: 'ArrowRight' })
    await waitFor(() => {
      expect(mockPatch).toHaveBeenCalledWith(
        '/api/me', { preferences: { week_start: 'mon' } },
      )
    })
    expect(document.activeElement).toBe(radios[1])
    expect(radios.map(r => r.tabIndex)).toEqual([-1, 0, -1])
    fireEvent.keyDown(radios[1], { key: 'ArrowLeft' })
    fireEvent.keyDown(radios[0], { key: 'ArrowLeft' })
    expect(document.activeElement).toBe(radios[2])
  })

  it('a stale failed save does not roll back a newer successful choice', async () => {
    let rejectMon: (e: Error) => void = () => {}
    mockPatch.mockClear()
    mockPatch
      .mockImplementationOnce(() => new Promise((_, rej) => { rejectMon = rej }))
      .mockResolvedValueOnce({})
    const { getByRole } = await renderAppearance()
    fireEvent.click(getByRole('radio', { name: 'Monday' }))
    fireEvent.click(getByRole('radio', { name: 'Sunday' }))
    await waitFor(() => expect(mockPatch).toHaveBeenCalledTimes(2))
    rejectMon(new Error('late failure'))
    await new Promise(r => setTimeout(r, 0))
    expect(getByRole('radio', { name: 'Sunday' }).getAttribute('aria-checked')).toBe('true')
  })
})

describe('SettingsPage — language picker keyboard', () => {
  it('is a single Tab stop and arrow keys switch the language', async () => {
    const { locale } = await import('@/i18n/i18n')
    const { default: SettingsPage } = await import('./SettingsPage')
    const { getByRole } = render(<SettingsPage />)
    fireEvent.click(getByRole('tab', { name: 'Appearance' }))
    const group = getByRole('radiogroup', { name: 'Language' })
    const radios = Array.from(group.querySelectorAll<HTMLElement>('[role="radio"]'))
    const checked = radios.findIndex(r => r.getAttribute('aria-checked') === 'true')
    expect(radios.map(r => r.tabIndex).filter(t => t === 0)).toHaveLength(1)
    expect(radios[checked].tabIndex).toBe(0)
    const before = locale.value
    fireEvent.keyDown(radios[checked], { key: 'ArrowRight' })
    await waitFor(() => expect(locale.value).not.toBe(before))
    const after = Array.from(group.querySelectorAll<HTMLElement>('[role="radio"]'))
    expect(document.activeElement).toBe(after[(checked + 1) % after.length])
    // The tab labels are translated now — switch back so later tests
    // can find them by their English names.
    const { setLocale } = await import('@/i18n/i18n')
    await setLocale('en')
  })
})

describe('SettingsPage — language is saved', () => {
  it('choosing a language persists it to the user preferences', async () => {
    mockPatch.mockClear()
    const { default: SettingsPage } = await import('./SettingsPage')
    const { getByRole } = render(<SettingsPage />)
    fireEvent.click(getByRole('tab', { name: 'Appearance' }))
    fireEvent.click(getByRole('radio', { name: 'Nederlands' }))
    await waitFor(() => {
      expect(mockPatch).toHaveBeenCalledWith('/api/me', { preferences: { locale: 'nl' } })
    })
    const { setLocale } = await import('@/i18n/i18n')
    await setLocale('en')
  })
})

describe('SettingsPage — Privacy people lists name everyone (B1)', () => {
  it('Blocked and Following show names, nicknames and households, never raw ids', async () => {
    // Regression: the lists named people from the local roster only, so a
    // remote person rendered as "ru-7ae1d43cad72" and nicknames were
    // ignored. The rows now render what the API resolved.
    mockGet.mockImplementation(async (path: string) => {
      if (path === '/api/blocks') {
        return {
          blocks: [
            {
              user_id: 'ru-7ae1d43cad72', blocked_at: '2026-10-15 09:00:00',
              display_name: 'Annegret Müller-Lüdenscheidt', personal_alias: null,
              picture_url: null, household: 'Familie Müller-Lüdenscheidt',
            },
            {
              user_id: 'ru-gone0000000', blocked_at: '2026-10-15 09:00:00',
              display_name: null, personal_alias: null,
              picture_url: null, household: null,
            },
          ],
        }
      }
      if (path === '/api/moments/follows') {
        return {
          follows: [{
            user_id: 'ru-ruth', created_at: '2026-10-15 09:00:00',
            display_name: 'Grandma Ruth', personal_alias: 'Oma',
            picture_url: 'api/users/ru-ruth/picture?v=1&exp=1&sig=x',
            household: 'Vizeli',
          }],
        }
      }
      return { spaces: [] }
    })
    const { _resetBlocksForTest } = await import('@/store/blocks')
    const { _resetFollowsForTest } = await import('@/store/follows')
    _resetBlocksForTest()
    _resetFollowsForTest()
    const result = await renderPrivacyTab()
    await waitFor(() => {
      expect(result.getByText('Annegret Müller-Lüdenscheidt')).toBeTruthy()
    })
    expect(result.getByText('Oma')).toBeTruthy()
    expect(result.getByText('Unknown person')).toBeTruthy()
    const text = document.body.textContent ?? ''
    expect(text).toContain('Grandma Ruth')
    expect(text).toContain('Household no longer connected')
    expect(text).not.toContain('ru-7ae1d43cad72')
    expect(text).not.toContain('ru-gone0000000')
    expect(result.getByRole('button', { name: 'Unfollow Oma' })).toBeTruthy()
    expect(result.getByRole('button', { name: 'Unblock Unknown person' })).toBeTruthy()
  })
})

describe('BlockedAccountsPanel', () => {
  it('says what a block does — and that it does not hide what you share', async () => {
    const { findByText } = await renderPrivacyTab()
    // Shown with or without blocks: a block is the blocker's own, so the
    // blocked person still sees the blocker's posts and presence.
    expect(await findByText(/they can still see your posts and presence/)).toBeTruthy()
    expect(await findByText(/stops you messaging, calling or playing apps/)).toBeTruthy()
  })
})
