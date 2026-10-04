import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'
import { LocationProvider } from 'preact-iso'

vi.mock('@/api', () => ({ api: { get: vi.fn(), post: vi.fn() } }))
vi.mock('@/ws', () => ({ ws: { on: vi.fn(() => () => {}) } }))

import { currentUser } from '@/store/auth'
import { isGuardian } from '@/store/guardian'
import { instanceConfig } from '@/store/instance'
import { active as activeCalls } from '@/store/calls'
import { dmUnreadTotal } from '@/store/dms'
import { toggles } from '@/components/HouseholdToggles'
import { userPreferences } from '@/store/userPreferences'
import { shortcutsHelpOpen } from '@/lib/shortcuts'
import { SideNav } from './SideNav'
import { setLocale } from '@/i18n/i18n'

const ALL_FEATURES_ON = {
  feat_feed: true, feat_pages: true, feat_tasks: true,
  feat_stickies: true, feat_calendar: true,
  feat_presence: true, feat_gallery: true, feat_timetable: true,
  allow_text: true, allow_image: true, allow_video: true,
  allow_file: true, allow_poll: true, allow_schedule: true,
  allow_highlight_share: true,
  allow_link_preview: true,
  household_name: 'Hearth',
}

function setUser(partial: Partial<{ is_admin: boolean; display_name: string; picture_url: string | null }> = {}) {
  currentUser.value = {
    user_id: 'u-1',
    username: 'pascal',
    display_name: partial.display_name ?? 'Pascal',
    is_admin: partial.is_admin ?? false,
    picture_url: partial.picture_url ?? null,
    picture_hash: null,
    bio: null,
    is_new_member: false,
  }
}

function renderAt(path: string) {
  window.history.pushState(null, '', path)
  return render(
    <LocationProvider>
      <SideNav />
    </LocationProvider>,
  )
}

beforeEach(() => {
  currentUser.value = null
  isGuardian.value = false
  activeCalls.value = []
  dmUnreadTotal.value = 0
  toggles.value = { ...ALL_FEATURES_ON }
  userPreferences.value = {
    user_id: 'u-1',
    hide_highlights: false,
    hide_momentum: false,
    hide_bazaar: false,
  }
  // Default to standalone for tests that don't care about platform
  // mode. The identity-strip behaviour test explicitly flips this to
  // ``'haos'`` to assert the strip disappears under HA Supervisor.
  instanceConfig.value = {
    mode: 'standalone',
    instance_name: 'Hearth', instance_id: null,
    capabilities: [],
    setup_required: false,
  }
})

describe('SideNav', () => {
  it('renders the four groups in IA order: At home → Talk → Browse → Settings', () => {
    setUser({ is_admin: true })
    const { container } = renderAt('/')
    const headers = Array.from(container.querySelectorAll('.sh-sidenav-group-header'))
      .map((el) => el.textContent?.trim())
    expect(headers).toEqual(['At home', 'Talk', 'Browse', 'Settings'])
  })

  it('exposes each group as a labelled <nav> landmark', () => {
    setUser({ is_admin: true })
    const { container } = renderAt('/')
    const navs = container.querySelectorAll('nav[aria-labelledby^="sidenav-group-"]')
    expect(navs.length).toBe(4)
    for (const nav of Array.from(navs)) {
      const id = nav.getAttribute('aria-labelledby')!
      const heading = container.querySelector(`#${id}`)
      expect(heading).toBeTruthy()
      expect(heading?.tagName.toLowerCase()).toBe('h2')
    }
  })

  it('hides Pages and Organize when their feature toggles are off', () => {
    setUser({ is_admin: true })
    toggles.value = {
      ...ALL_FEATURES_ON,
      feat_pages: false,
      feat_tasks: false,
      feat_stickies: false,
    }
    const { queryByText } = renderAt('/')
    expect(queryByText('Pages')).toBeNull()
    // Organize covers Tasks + Shopping + Stickies; the row hides when
    // both ``feat_tasks`` and ``feat_stickies`` are off.
    expect(queryByText('Organize')).toBeNull()
    // Other items in the same group still render.
    expect(queryByText('Feed')).toBeTruthy()
    expect(queryByText('Gallery')).toBeTruthy()
  })

  it('keeps Calendar in the nav while either Calendar or Timetable is on', () => {
    setUser()
    toggles.value = { ...ALL_FEATURES_ON, feat_calendar: false, feat_timetable: true }
    const onlyTimetable = renderAt('/')
    expect(onlyTimetable.queryByText('Calendar')).toBeTruthy()
    onlyTimetable.unmount()

    toggles.value = { ...ALL_FEATURES_ON, feat_calendar: true, feat_timetable: false }
    const onlyCalendar = renderAt('/')
    expect(onlyCalendar.queryByText('Calendar')).toBeTruthy()
    onlyCalendar.unmount()

    toggles.value = { ...ALL_FEATURES_ON, feat_calendar: false, feat_timetable: false }
    const neither = renderAt('/')
    expect(neither.queryByText('Calendar')).toBeNull()
  })

  it('shows Bazaar when hide_bazaar is false — it renders unless the user opted out', () => {
    setUser({ is_admin: true })
    userPreferences.value = { ...userPreferences.value, hide_bazaar: false }
    const { queryByText, container } = renderAt('/')
    expect(queryByText('Bazaar')).toBeTruthy()
    expect(queryByText('Spaces')).toBeTruthy()
    const headers = Array.from(container.querySelectorAll('.sh-sidenav-group-header'))
      .map((el) => el.textContent?.trim())
    expect(headers).toContain('Browse')
  })

  it('hides the gated Settings sub-entries for a non-admin non-guardian, but keeps Personal + the header', () => {
    // Settings now always has at least the ungated Personal item, so
    // the *group header* never disappears for an authenticated user.
    // The gated sub-entries (Parent Control / Connections / Admin)
    // still hide individually based on role.
    setUser({ is_admin: false })
    isGuardian.value = false
    const { queryByText, container } = renderAt('/')
    expect(queryByText('Settings')).toBeTruthy()
    expect(queryByText('Personal')).toBeTruthy()
    expect(queryByText('Admin')).toBeNull()
    expect(queryByText('Connections')).toBeNull()
    expect(queryByText('Parental controls')).toBeNull()
    // AT HOME header still renders because Shopping/Presence/Gallery
    // are unconditionally visible.
    const headers = Array.from(container.querySelectorAll('.sh-sidenav-group-header'))
      .map((el) => el.textContent?.trim())
    expect(headers).toContain('At home')
    expect(headers).toContain('Settings')
  })

  it('hides Admin and Connections for non-admin users', () => {
    setUser({ is_admin: false })
    const { queryByText } = renderAt('/')
    expect(queryByText('Admin')).toBeNull()
    expect(queryByText('Connections')).toBeNull()
  })

  it('renders a Personal link to /settings inside the Settings group for every authenticated user', () => {
    setUser({ is_admin: false })
    isGuardian.value = false
    const { getByText } = renderAt('/')
    const link = getByText('Personal').closest('a')
    expect(link).toBeTruthy()
    expect(link?.getAttribute('href')).toBe('/settings')
  })

  it('offers a "Keyboard shortcuts" button in the Settings group that opens the help dialog', () => {
    setUser({ is_admin: false })
    shortcutsHelpOpen.value = false
    const { getByRole } = renderAt('/')
    const settings = getByRole('navigation', { name: 'Settings' })
    const btn = getByRole('button', { name: 'Keyboard shortcuts' })
    expect(settings.contains(btn)).toBe(true)
    expect(btn.getAttribute('aria-haspopup')).toBe('dialog')
    fireEvent.click(btn)
    expect(shortcutsHelpOpen.value).toBe(true)
    shortcutsHelpOpen.value = false
  })

  it('also renders Personal for admins (alongside Connections / Admin)', () => {
    setUser({ is_admin: true })
    isGuardian.value = true
    const { getByText, queryByText } = renderAt('/')
    expect(getByText('Personal').closest('a')?.getAttribute('href')).toBe('/settings')
    expect(queryByText('Admin')).toBeTruthy()
    expect(queryByText('Connections')).toBeTruthy()
    expect(queryByText('Parental controls')).toBeTruthy()
  })

  it('shows Admin and Connections for admin users', () => {
    setUser({ is_admin: true })
    const { queryByText } = renderAt('/')
    expect(queryByText('Admin')).toBeTruthy()
    expect(queryByText('Connections')).toBeTruthy()
  })

  it('hides Parental controls when the caller is not a guardian', () => {
    setUser({ is_admin: true })
    isGuardian.value = false
    const { queryByText } = renderAt('/')
    expect(queryByText('Parental controls')).toBeNull()
  })

  it('shows Parental controls when isGuardian is true and links to /parent', () => {
    setUser({ is_admin: false })
    isGuardian.value = true
    const { getByText } = renderAt('/')
    const link = getByText('Parental controls').closest('a')
    expect(link).toBeTruthy()
    expect(link?.getAttribute('href')).toBe('/parent')
  })

  it('renders the identity strip in standalone mode with avatar + display name linking to /settings', () => {
    instanceConfig.value = {
      mode: 'standalone',
      instance_name: 'Hearth', instance_id: null,
      capabilities: [],
      setup_required: false,
    }
    setUser({ display_name: 'Pascal Vizeli', picture_url: '/pic.jpg' })
    const { container } = renderAt('/')
    const strip = container.querySelector('.sh-sidenav-identity')
    expect(strip).toBeTruthy()
    expect(strip?.tagName.toLowerCase()).toBe('a')
    expect(strip?.getAttribute('href')).toBe('/settings')
    expect(strip?.textContent).toContain('Pascal Vizeli')
    // The strip itself is the settings entry point — no nested
    // action surfaces inside it.
    expect(strip?.querySelector('a, button')).toBeNull()
  })

  it('also renders the identity strip in ha mode (SH is the primary UI surface)', () => {
    instanceConfig.value = {
      mode: 'ha',
      instance_name: 'Hearth', instance_id: null,
      capabilities: ['ha_person_directory'],
      setup_required: false,
    }
    setUser({ display_name: 'Pascal Vizeli' })
    const { container } = renderAt('/')
    expect(container.querySelector('.sh-sidenav-identity')).toBeTruthy()
  })

  it('hides the identity strip in haos mode (HA Core sidebar already shows the signed-in user)', () => {
    instanceConfig.value = {
      mode: 'haos',
      instance_name: 'Hearth', instance_id: null,
      capabilities: ['ingress', 'ha_person_directory'],
      setup_required: false,
    }
    setUser({ display_name: 'Pascal Vizeli' })
    const { container } = renderAt('/')
    expect(container.querySelector('.sh-sidenav-identity')).toBeNull()
    // Personal entry in the Settings group remains so users still
    // have a sidebar path to /settings.
    const personal = container.querySelector('a[href="/settings"]')
    expect(personal).toBeTruthy()
  })

  it('marks the active group with sh-sidenav-group--active when on a child route', () => {
    setUser({ is_admin: true })
    const { container } = renderAt('/calendar')
    const homeNav = container.querySelector('nav[aria-labelledby="sidenav-group-home"]')
    expect(homeNav?.classList.contains('sh-sidenav-group--active')).toBe(true)
    const browseNav = container.querySelector('nav[aria-labelledby="sidenav-group-browse"]')
    expect(browseNav?.classList.contains('sh-sidenav-group--active')).toBe(false)
  })

  it('sets aria-current="page" on the active link', () => {
    setUser({ is_admin: true })
    const { getByText } = renderAt('/calendar')
    const calendarLink = getByText('Calendar').closest('a')
    expect(calendarLink?.getAttribute('aria-current')).toBe('page')
    const organizeLink = getByText('Organize').closest('a')
    expect(organizeLink?.getAttribute('aria-current')).toBeNull()
  })

  it('Corner sits in BROWSE pointing at /corner, not in LOCAL', () => {
    setUser({ is_admin: true })
    const { container, getByText } = renderAt('/')
    const browseNav = container.querySelector('nav[aria-labelledby="sidenav-group-browse"]')!
    expect(browseNav.textContent).toContain('Corner')
    const localNav = container.querySelector('nav[aria-labelledby="sidenav-group-local"]')!
    expect(localNav.textContent).not.toContain('Corner')
    expect(localNav.textContent).not.toContain('Dashboard')
    const cornerLink = getByText('Corner').closest('a')
    expect(cornerLink?.getAttribute('href')).toBe('/corner')
  })

  it('does not render Notifications or Search links — those live in the top bar only', () => {
    setUser({ is_admin: true })
    const { queryByText } = renderAt('/')
    expect(queryByText('Notifications')).toBeNull()
    expect(queryByText('Search')).toBeNull()
  })

  it('hides the Calls fast-lane entry when no call is active', () => {
    setUser({ is_admin: true })
    const { queryByText } = renderAt('/')
    expect(queryByText('Calls')).toBeNull()
    // Chats is always present under Talk.
    expect(queryByText('Chats')).toBeTruthy()
  })

  it('shows the Calls fast-lane entry pointing at /dms?tab=calls when a call is live', () => {
    setUser({ is_admin: true })
    activeCalls.value = [{
      call_id: 'c-1', status: 'in_progress', caller: 'u1',
      callee: 'u2', call_type: 'audio', created_at: 0,
    }]
    const { getByText } = renderAt('/')
    const link = getByText('Calls').closest('a')
    expect(link).toBeTruthy()
    expect(link?.getAttribute('href')).toBe('/dms?tab=calls')
  })

  it('renders an unread badge on Chats when dmUnreadTotal > 0', () => {
    setUser({ is_admin: true })
    dmUnreadTotal.value = 4
    const { getByText } = renderAt('/')
    const link = getByText('Chats').closest('a')!
    const badge = link.querySelector('.sh-sidenav-badge')
    expect(badge?.textContent).toBe('4')
  })

  it('caps the Chats unread badge at 99+', () => {
    setUser({ is_admin: true })
    dmUnreadTotal.value = 250
    const { getByText } = renderAt('/')
    const badge = getByText('Chats').closest('a')!.querySelector('.sh-sidenav-badge')
    expect(badge?.textContent).toBe('99+')
  })

  it('hides the Chats badge when there are no unread DMs', () => {
    setUser({ is_admin: true })
    dmUnreadTotal.value = 0
    const { getByText } = renderAt('/')
    const link = getByText('Chats').closest('a')!
    expect(link.querySelector('.sh-sidenav-badge')).toBeNull()
  })

  it('names the households link plainly as Connections, not Federation', () => {
    setUser({ is_admin: true })
    const { queryByText, getByText } = renderAt('/')
    expect(queryByText('Federation')).toBeNull()
    const link = getByText('Connections').closest('a')
    expect(link?.getAttribute('href')).toBe('/connections')
  })

  it('renders Presence and Gallery from the household toggles', () => {
    setUser({ is_admin: true })
    toggles.value = { ...ALL_FEATURES_ON, feat_presence: true, feat_gallery: true }
    const { getByText } = renderAt('/')
    expect(getByText('Presence')).toBeTruthy()
    expect(getByText('Gallery')).toBeTruthy()
  })

  it('drops the standalone Highlight archive / Moments archive entries — those live as tabs now', () => {
    setUser({ is_admin: true })
    const { queryByText } = renderAt('/')
    expect(queryByText('Highlight archive')).toBeNull()
    expect(queryByText('Moments archive')).toBeNull()
  })

  it('hides Presence when feat_presence is false', () => {
    setUser({ is_admin: true })
    toggles.value = { ...ALL_FEATURES_ON, feat_presence: false }
    const { queryByText } = renderAt('/')
    expect(queryByText('Presence')).toBeNull()
    // Gallery still visible
    expect(queryByText('Gallery')).toBeTruthy()
  })

  it('hides Gallery when feat_gallery is false', () => {
    setUser({ is_admin: true })
    toggles.value = { ...ALL_FEATURES_ON, feat_gallery: false }
    const { queryByText } = renderAt('/')
    expect(queryByText('Gallery')).toBeNull()
    // Presence still visible
    expect(queryByText('Presence')).toBeTruthy()
  })

  it('hides Highlights when userPreferences.hide_highlights is true', () => {
    setUser({ is_admin: true })
    userPreferences.value = { ...userPreferences.value, hide_highlights: true }
    const { queryByText } = renderAt('/')
    expect(queryByText('Highlights')).toBeNull()
    // Momentum and Chats still visible
    expect(queryByText('Momentum')).toBeTruthy()
    expect(queryByText('Chats')).toBeTruthy()
  })

  it('hides Momentum when userPreferences.hide_momentum is true', () => {
    setUser({ is_admin: true })
    userPreferences.value = { ...userPreferences.value, hide_momentum: true }
    const { queryByText } = renderAt('/')
    expect(queryByText('Momentum')).toBeNull()
    // Highlights still visible
    expect(queryByText('Highlights')).toBeTruthy()
  })

  it('hides Bazaar when userPreferences.hide_bazaar is true', () => {
    setUser({ is_admin: true })
    userPreferences.value = { ...userPreferences.value, hide_bazaar: true }
    const { queryByText } = renderAt('/')
    expect(queryByText('Bazaar')).toBeNull()
    // Spaces and Corner still visible
    expect(queryByText('Spaces')).toBeTruthy()
    expect(queryByText('Corner')).toBeTruthy()
  })

  it('shows all three user-pref sections when hide_* flags are false', () => {
    setUser({ is_admin: true })
    userPreferences.value = {
      user_id: 'u-1',
      hide_highlights: false,
      hide_momentum: false,
      hide_bazaar: false,
    }
    const { queryByText } = renderAt('/')
    expect(queryByText('Highlights')).toBeTruthy()
    expect(queryByText('Momentum')).toBeTruthy()
    expect(queryByText('Bazaar')).toBeTruthy()
  })
})

describe('SideNav — links under the HA ingress prefix', () => {
  // ``baseUrl.ts`` reads ``document.baseURI`` once at module load, so
  // set the ingress ``<base href>`` first and re-import the modules.
  let baseEl: HTMLBaseElement
  beforeEach(() => {
    baseEl = document.createElement('base')
    baseEl.href = '/api/hassio_ingress/tok/'
    document.head.prepend(baseEl)
    vi.resetModules()
  })
  afterEach(() => {
    baseEl.remove()
    vi.resetModules()
    window.history.pushState(null, '', '/')
  })

  it('every nav link carries the ingress prefix (/api/hassio_ingress/<token>/)', async () => {
    const toggleMod = await import('@/components/HouseholdToggles')
    toggleMod.toggles.value = { ...ALL_FEATURES_ON }
    const auth = await import('@/store/auth')
    auth.currentUser.value = {
      user_id: 'u-1', username: 'pascal', display_name: 'Pascal', is_admin: true,
      picture_url: null, picture_hash: null, bio: null, is_new_member: false,
    }
    const { SideNav: PrefixedSideNav } = await import('./SideNav')
    const iso = await import('preact-iso')
    window.history.pushState(null, '', '/api/hassio_ingress/tok/')
    const { container } = render(
      <iso.LocationProvider><PrefixedSideNav /></iso.LocationProvider>,
    )
    const hrefs = [...container.querySelectorAll('a[href]')].map(a => a.getAttribute('href')!)
    expect(hrefs).toContain('/api/hassio_ingress/tok/feed')
    expect(hrefs).toContain('/api/hassio_ingress/tok/dms')
    expect(hrefs).toContain('/api/hassio_ingress/tok/settings')
    expect(hrefs.filter(h => !h.startsWith('/api/hassio_ingress/tok/'))).toEqual([])
  })
})

describe('SideNav in German', () => {
  afterEach(async () => { await setLocale('en') })

  it('renders the section headers, links and unread badge in German', async () => {
    await setLocale('de')
    setUser({ is_admin: true })
    isGuardian.value = true
    dmUnreadTotal.value = 3
    const { container, getByText } = renderAt('/')
    const headers = Array.from(container.querySelectorAll('.sh-sidenav-group-header'))
      .map((el) => el.textContent?.trim())
    expect(headers).toEqual(['Zuhause', 'Reden', 'Entdecken', 'Einstellungen'])
    for (const label of ['Kalender', 'Organisieren', 'Chats', 'Räume', 'Freunde',
      'Persönlich', 'Jugendschutz', 'Verbindungen']) {
      expect(getByText(label).closest('a'), label).toBeTruthy()
    }
    expect(getByText('Verbindungen').closest('a')?.getAttribute('href')).toBe('/connections')
    expect(container.querySelector('.sh-sidenav-badge')?.getAttribute('aria-label'))
      .toBe('3 ungelesen')
    expect(container.querySelector('aside')?.getAttribute('aria-label')).toBe('Seitenleiste')
  })
})
