/**
 * SpaceSettingsPage — one page, a URL per section: the index (links with
 * aria-current, never a tab widget), what each kind of viewer gets, the
 * save model (a save bar only for Basics, a leave guard that lets section
 * jumps through), the live config refresh and the ingress prefix.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, render, waitFor } from '@testing-library/preact'

const apiGet = vi.fn()
const apiPatch = vi.fn()
vi.mock('@/api', () => ({
  api: {
    get: (...args: unknown[]) => apiGet(...args),
    patch: (...args: unknown[]) => apiPatch(...args),
    post: vi.fn(), put: vi.fn(), delete: vi.fn(), upload: vi.fn(),
  },
}))

type Handler = (e: { type: string; data: Record<string, unknown> }) => void
const handlers = new Map<string, Set<Handler>>()
vi.mock('@/ws', () => ({
  ws: {
    on: (type: string, h: Handler) => {
      if (!handlers.has(type)) handlers.set(type, new Set())
      handlers.get(type)!.add(h)
      return () => { handlers.get(type)?.delete(h) }
    },
  },
}))
function emit(type: string, data: Record<string, unknown>) {
  handlers.get(type)?.forEach(h => h({ type, data: { type, ...data } }))
}

const nav = vi.hoisted(() => ({ section: undefined as string | undefined, route: vi.fn() }))
vi.mock('preact-iso', async (orig) => ({
  ...(await orig<object>()),
  useRoute: () => ({ params: { id: 'sp-1', section: nav.section } }),
  useLocation: () => ({ route: nav.route, path: '/spaces/sp-1/settings', url: '/spaces/sp-1/settings' }),
}))
vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u1', username: 'anna', display_name: 'Anna', is_admin: false } },
}))
const toast = vi.hoisted(() => vi.fn())
vi.mock('@/components/Toast', () => ({ showToast: toast }))
const confirm = vi.hoisted(() => vi.fn())
vi.mock('@/components/confirm', () => ({ confirmDialog: confirm }))

import { confirmLeave, shouldConfirmLeave } from '@/router/navigationGuard'

let detail: Record<string, unknown>
let role: string
beforeEach(() => {
  handlers.clear()
  nav.section = undefined
  nav.route.mockReset()
  apiGet.mockReset()
  apiPatch.mockReset()
  toast.mockReset()
  role = 'owner'
  detail = {
    id: 'sp-1', name: 'Garden', description: 'Veg', emoji: '🥕', about_markdown: '',
    features: {}, owner_instance_id: null, space_type: 'private', join_mode: 'invite_only',
  }
  apiGet.mockImplementation(async (url: string) => {
    if (url === '/api/spaces/sp-1') return detail
    if (url === '/api/spaces/sp-1/members') {
      return [{ user_id: 'u1', role, display_name: 'Anna' }, { user_id: 'u2', role: 'admin' }]
    }
    if (url === '/api/spaces/sp-1/notif-prefs') return { level: 'all' }
    if (url.endsWith('/zones')) return { zones: [] }
    if (url.endsWith('/links')) return { links: [] }
    return []
  })
  apiPatch.mockResolvedValue({})
})

async function mount() {
  const { default: Page } = await import('./SpaceSettingsPage')
  const view = render(<Page />)
  await view.findByRole('heading', { level: 1, name: 'Settings' })
  return view
}

function indexLinks(view: Awaited<ReturnType<typeof mount>>) {
  const navEl = view.getByRole('navigation', { name: 'Settings sections' })
  return [...navEl.querySelectorAll('a')]
}

describe('SpaceSettingsPage — one page, a URL per section', () => {
  it('indexes every admin section with real links, the current one marked', async () => {
    const view = await mount()
    const links = indexLinks(view)
    expect(links.map(a => a.textContent)).toEqual([
      'Basics', 'Who can find it', 'Members & joining', 'Sections', 'Your notifications',
      'Location & zones', 'Moderation & age', 'Bots', 'Archive or delete',
    ])
    expect(links[0].getAttribute('href')).toBe('/spaces/sp-1/settings/basics')
    expect(links[0].getAttribute('aria-current')).toBe('page')
    expect(links.filter(a => a.hasAttribute('aria-current'))).toHaveLength(1)
    // Honest semantics: links in a nav, no tab widget anywhere.
    expect(view.queryAllByRole('tab')).toHaveLength(0)
    expect(view.queryAllByRole('tablist')).toHaveLength(0)
    // Every section is on the page, with its own anchor.
    for (const s of ['basics', 'find', 'members', 'sections', 'notifications', 'location', 'moderation', 'bots', 'danger']) {
      expect(view.container.querySelector(`#settings-${s}`)).not.toBeNull()
    }
  })

  it('a section URL marks that section and scrolls to it', async () => {
    nav.section = 'sections'
    const scrolled: string[] = []
    const had = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = function (this: Element) { scrolled.push(this.id) }
    const view = await mount()
    const current = indexLinks(view).find(a => a.getAttribute('aria-current') === 'page')
    expect(current?.textContent).toBe('Sections')
    await waitFor(() => expect(scrolled).toContain('settings-sections'))
    Element.prototype.scrollIntoView = had
  })

  it('the Danger zone is collapsed unless its own URL opens it', async () => {
    const view = await mount()
    expect((view.getByTestId('settings-danger') as HTMLDetailsElement).open).toBe(false)
    view.unmount()
    nav.section = 'danger'
    const again = await mount()
    expect((again.getByTestId('settings-danger') as HTMLDetailsElement).open).toBe(true)
  })

  it('a plain member gets their notifications, "You in this space", bots and Leave', async () => {
    role = 'member'
    const view = await mount()
    expect(indexLinks(view).map(a => a.textContent)).toEqual([
      'Your notifications', 'You in this space', 'Bots', 'Leave',
    ])
    expect(view.container.querySelector('#settings-basics')).toBeNull()
    expect(view.getByTestId('settings-you-profile')).toBeTruthy()
    expect((view.getByTestId('settings-leave') as HTMLDetailsElement).open).toBe(false)
    // One "Bots" heading on the page, not three.
    expect(view.queryAllByText('Bots & automations', { exact: false })).toHaveLength(0)
  })

  it('a follower gets notifications and Unfollow only', async () => {
    // A follower's roster lists the members, not the followers: the
    // detail's my_role says who is looking.
    role = 'member'
    apiGet.mockImplementation(async (url: string) => {
      if (url === '/api/spaces/sp-1') return { ...detail, my_role: 'subscriber' }
      if (url === '/api/spaces/sp-1/members') return [{ user_id: 'u9', role: 'owner' }]
      if (url === '/api/spaces/sp-1/notif-prefs') return { level: 'all' }
      return []
    })
    const view = await mount()
    expect(indexLinks(view).map(a => a.textContent)).toEqual(['Your notifications', 'Unfollow'])
    expect(view.getByTestId('settings-unfollow')).toBeTruthy()
    expect(view.queryByTestId('settings-you-profile')).toBeNull()
  })

  it('someone with no seat reads "members only"', async () => {
    apiGet.mockImplementation(async (url: string) => {
      if (url === '/api/spaces/sp-1') return detail
      return []
    })
    const { default: Page } = await import('./SpaceSettingsPage')
    const view = render(<Page />)
    expect(await view.findByText('Space members only')).toBeTruthy()
  })
})

describe('SpaceSettingsPage — save model', () => {
  it('shows the save bar only while Basics has unsaved text, and saves just that', async () => {
    const view = await mount()
    expect(view.queryByRole('region', { name: 'Unsaved changes' })).toBeNull()
    const name = view.getByLabelText('Name') as HTMLInputElement
    fireEvent.input(name, { target: { value: 'Allotment' } })
    const bar = await view.findByRole('region', { name: 'Unsaved changes' })
    expect(bar.textContent).toContain('1 unsaved change')
    fireEvent.click(view.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(apiPatch).toHaveBeenCalledWith('/api/spaces/sp-1', { name: 'Allotment' }))
  })

  it('Discard puts the draft back', async () => {
    const view = await mount()
    fireEvent.input(view.getByLabelText('Name'), { target: { value: 'X' } })
    fireEvent.input(view.getByLabelText('Short description'), { target: { value: 'Y' } })
    expect((await view.findByRole('region', { name: 'Unsaved changes' })).textContent)
      .toContain('2 unsaved changes')
    fireEvent.click(view.getByRole('button', { name: 'Discard' }))
    await waitFor(() => expect(view.queryByRole('region', { name: 'Unsaved changes' })).toBeNull())
    expect((view.getByLabelText('Name') as HTMLInputElement).value).toBe('Garden')
  })

  it('refuses to save an empty name', async () => {
    const view = await mount()
    fireEvent.input(view.getByLabelText('Name'), { target: { value: '  ' } })
    fireEvent.click(await view.findByRole('button', { name: 'Save' }))
    expect(apiPatch).not.toHaveBeenCalled()
    expect(toast).toHaveBeenCalledWith('Give the space a name.', 'error')
  })

  it('a switch elsewhere saves at once and raises no save bar', async () => {
    const view = await mount()
    fireEvent.click(view.getByRole('switch', { name: 'Gallery' }))
    await waitFor(() => expect(apiPatch).toHaveBeenCalledWith(
      '/api/spaces/sp-1', { features: { gallery: false } },
    ))
    expect(view.queryByRole('region', { name: 'Unsaved changes' })).toBeNull()
  })

  it('leaving with a draft asks; jumping between sections does not', async () => {
    const view = await mount()
    expect(shouldConfirmLeave('/spaces/sp-1')).toBe(false)
    fireEvent.input(view.getByLabelText('Name'), { target: { value: 'X' } })
    await view.findByRole('region', { name: 'Unsaved changes' })
    expect(shouldConfirmLeave('/spaces/sp-1')).toBe(true)
    expect(shouldConfirmLeave('/spaces/sp-1/settings/members')).toBe(false)
    view.unmount()
    await waitFor(() => expect(shouldConfirmLeave('/spaces/sp-1')).toBe(false))
  })

  it('agreeing to leave discards the draft; staying keeps it', async () => {
    const view = await mount()
    fireEvent.input(view.getByLabelText('Name'), { target: { value: 'X' } })
    await view.findByRole('region', { name: 'Unsaved changes' })
    confirm.mockResolvedValueOnce(false)
    expect(await confirmLeave()).toBe(false)
    expect((view.getByLabelText('Name') as HTMLInputElement).value).toBe('X')
    confirm.mockResolvedValueOnce(true)
    expect(await confirmLeave()).toBe(true)
    await waitFor(() => expect((view.getByLabelText('Name') as HTMLInputElement).value).toBe('Garden'))
  })

  it('a forwarded save (space hosted elsewhere) drops the draft', async () => {
    apiPatch.mockResolvedValue({ forwarded: true })
    const view = await mount()
    fireEvent.input(view.getByLabelText('Name'), { target: { value: 'X' } })
    fireEvent.click(await view.findByRole('button', { name: 'Save' }))
    await waitFor(() => expect(view.queryByRole('region', { name: 'Unsaved changes' })).toBeNull())
    expect(toast).toHaveBeenCalledWith(expect.stringContaining('Sent to the host household'), 'info')
  })
})

describe('SpaceSettingsPage — live config', () => {
  it('follows a rename and an admin grant made elsewhere', async () => {
    role = 'member'
    const view = await mount()
    expect(view.getByRole('link', { name: /Garden/ })).toBeTruthy()
    expect(view.container.querySelector('#settings-basics')).toBeNull()
    detail = { ...detail, name: 'Allotment' }
    role = 'admin'
    await act(async () => { emit('space.config.changed', { space_id: 'sp-1', event_type: 'admin_granted' }) })
    await view.findByRole('link', { name: /Allotment/ })
    await waitFor(() => expect(view.container.querySelector('#settings-basics')).not.toBeNull())
  })

  it('a failed live refresh keeps the page', async () => {
    const view = await mount()
    apiGet.mockRejectedValue(new Error('offline'))
    await act(async () => { emit('space.config.changed', { space_id: 'sp-1', event_type: 'rename' }) })
    await new Promise(r => setTimeout(r, 0))
    expect(view.getByRole('heading', { level: 1, name: 'Settings' })).toBeTruthy()
    expect(view.queryByText('Space not found')).toBeNull()
  })

  it('shows the GFS publish mode to the owner only', async () => {
    detail = { ...detail, space_type: 'public', join_mode: 'open', features: { allow_subscribers: true } }
    role = 'admin'
    const view = await mount()
    expect(view.queryByRole('radiogroup', { name: /Posting through the GFS/ })).toBeNull()
    role = 'owner'
    await act(async () => { emit('space.config.changed', { space_id: 'sp-1', event_type: 'owner_transferred' }) })
    const group = await view.findByRole('radiogroup', { name: /Posting through the GFS/ })
    expect(group.querySelector('[aria-checked="true"]')?.textContent).toBe('Trusted')
  })
})

describe('SpaceSettingsPage — a member sees where a global space is listed', () => {
  it('lists the GFSs read-only under "You in this space"', async () => {
    role = 'member'
    detail = {
      ...detail, space_type: 'global',
      gfs_servers: [
        { display_name: 'Town GFS', inbox_url: 'https://town.example', connected: true },
        { display_name: 'Club GFS', inbox_url: 'https://club.example', connected: false },
      ],
      gfs_unlisted: false,
    }
    const view = await mount()
    const section = await view.findByTestId('member-gfs-servers')
    expect(section.textContent).toContain('Town GFS')
    expect(section.textContent).toContain('Club GFS')
    expect(section.querySelectorAll('button')).toHaveLength(0)
  })
})

describe('SpaceSettingsPage — under the HA ingress prefix', () => {
  // ``baseUrl.ts`` reads ``document.baseURI`` once at module load: set the
  // ingress ``<base href>`` first, then import the page afresh.
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
  })

  it('every section link, the back link and Manage zones keep /api/hassio_ingress/<token>/', async () => {
    detail = { ...detail, features: { location: true } }
    const view = await mount()
    for (const a of indexLinks(view)) {
      expect(a.getAttribute('href')).toMatch(/^\/api\/hassio_ingress\/tok\/spaces\/sp-1\/settings\/[a-z]+$/)
    }
    expect(view.getByRole('link', { name: /Garden/ }).getAttribute('href'))
      .toBe('/api/hassio_ingress/tok/spaces/sp-1')
    expect(view.getByRole('link', { name: 'Manage zones' }).getAttribute('href'))
      .toBe('/api/hassio_ingress/tok/spaces/sp-1/zones')
  })
})
