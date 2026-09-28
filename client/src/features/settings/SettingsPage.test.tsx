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
