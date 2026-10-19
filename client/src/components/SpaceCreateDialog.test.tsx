import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, act, waitFor, cleanup } from '@testing-library/preact'

const post = vi.fn()
// The dialog loads GFS connections on open to decide whether the Global tier
// is selectable. Tests set ``gfsConns`` per-case before opening.
let gfsConns: { status: string; id?: string; display_name?: string; inbox_url?: string }[] = []
const get = vi.fn(async (u: string) =>
  u === '/api/gfs/connections' ? gfsConns : [],
)
vi.mock('@/api', () => ({ api: { get: (...a: unknown[]) => get(...a as [string]), post: (...a: unknown[]) => post(...a) } }))
vi.mock('@/store/spaces', () => ({ loadSpaces: vi.fn() }))
vi.mock('./Toast', () => ({ showToast: vi.fn() }))
vi.mock('@/i18n/i18n', () => ({
  t: (k: string, p?: Record<string, string>) => (p ? `${k}:${JSON.stringify(p)}` : k),
  formatLocale: () => 'en',
}))
// EmojiField pulls in the emoji-picker; not relevant here.
vi.mock('./EmojiField', () => ({ EmojiField: () => null }))
const route = vi.fn()
vi.mock('preact-iso', () => ({ useLocation: () => ({ route, url: '/spaces', path: '/spaces' }) }))
const markSetupPending = vi.fn(async (_id: string) => {})
vi.mock('@/features/spaces/spaceSetup', () => ({
  markSetupPending: (id: string) => markSetupPending(id),
}))

const { SpaceCreateDialog, openSpaceCreate } = await import('./SpaceCreateDialog')
const { currentUser } = await import('@/store/auth')

function setViewer(isAdmin: boolean) {
  currentUser.value = {
    user_id: 'u1', username: 'u', display_name: 'U', is_admin: isAdmin,
  } as typeof currentUser.value
}

const nameInput = (c: Element) =>
  c.querySelector('input[placeholder="space.create.name_placeholder"]') as HTMLInputElement
const createBtn = (c: Element) =>
  [...c.querySelectorAll('button')].find(b => b.textContent?.trim() === 'space.create.submit') as HTMLButtonElement
const visibility = (c: Element, v: string) =>
  c.querySelector(`input[name="space-create-visibility"][value="${v}"]`) as HTMLInputElement
const pickVisibility = (c: Element, v: string) => visibility(c, v)
const minAgeSelect = (c: Element) =>
  c.querySelector('select[name="space-create-min-age"]') as HTMLSelectElement | null
const categorySelect = (c: Element) =>
  c.querySelector('select[name="space-create-category"]') as HTMLSelectElement | null

async function open() {
  const r = render(<SpaceCreateDialog />)
  await act(async () => { openSpaceCreate() })
  return r
}

describe('SpaceCreateDialog — no map location', () => {
  beforeEach(() => { post.mockReset(); get.mockClear(); gfsConns = []; cleanup() })

  it('module exports exist', () => {
    expect(typeof SpaceCreateDialog).toBe('function')
    expect(typeof openSpaceCreate).toBe('function')
  })

  it('a public space asks for no location and never reads the device position', async () => {
    post.mockResolvedValue({ id: 's1' })
    const getCurrentPosition = vi.fn()
    Object.defineProperty(navigator, 'geolocation', { value: { getCurrentPosition }, configurable: true })
    const { container } = await open()
    await act(async () => { fireEvent.input(nameInput(container), { target: { value: 'Neighbourhood' } }) })
    await act(async () => { fireEvent.click(pickVisibility(container, 'public')) })
    expect(container.querySelector('.sh-space-create-location')).toBeNull()
    expect(container.textContent).not.toContain('space.create.show_on_map')
    await act(async () => { fireEvent.click(createBtn(container)) })
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    const body = post.mock.calls[0][1] as Record<string, unknown>
    expect('lat' in body).toBe(false)
    expect('lon' in body).toBe(false)
    expect(getCurrentPosition).not.toHaveBeenCalled()
  })

  it('does not send coords for a non-public space', async () => {
    post.mockResolvedValue({ id: 's2' })
    const { container } = await open()
    await act(async () => { fireEvent.input(nameInput(container), { target: { value: 'Family' } }) })
    // default visibility is private
    await act(async () => { fireEvent.click(createBtn(container)) })
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    const body = post.mock.calls[0][1] as Record<string, unknown>
    expect('lat' in body).toBe(false)
    expect('lon' in body).toBe(false)
  })
})

describe('SpaceCreateDialog — after create', () => {
  beforeEach(() => { post.mockReset(); route.mockReset(); markSetupPending.mockClear(); cleanup() })

  it('lands in the new space (no list + toast) and asks "What is it for?" there', async () => {
    post.mockResolvedValue({ id: 'new-1' })
    const { showToast } = await import('./Toast')
    ;(showToast as ReturnType<typeof vi.fn>).mockClear()
    const { container } = await open()
    await act(async () => { fireEvent.input(nameInput(container), { target: { value: 'Book club' } }) })
    await act(async () => { fireEvent.click(createBtn(container)) })
    await waitFor(() => expect(route).toHaveBeenCalledWith('/spaces/new-1'))
    expect(markSetupPending).toHaveBeenCalledWith('new-1')
    expect(showToast).not.toHaveBeenCalled()
    expect(nameInput(container)).toBeNull()
  })

  it('still lands in the space when the setup flag cannot be stored', async () => {
    post.mockResolvedValue({ id: 'new-2' })
    markSetupPending.mockRejectedValueOnce(new Error('offline'))
    const { container } = await open()
    await act(async () => { fireEvent.input(nameInput(container), { target: { value: 'X' } }) })
    await act(async () => { fireEvent.click(createBtn(container)) })
    await waitFor(() => expect(route).toHaveBeenCalledWith('/spaces/new-2'))
  })
})

describe('SpaceCreateDialog — Global tier gating', () => {
  beforeEach(() => { post.mockReset(); get.mockClear(); gfsConns = []; cleanup() })

  it('disables the Global option with no active GFS connection', async () => {
    gfsConns = []
    const { container } = await open()
    // Wait for the connections fetch to settle.
    await waitFor(() => expect(get).toHaveBeenCalledWith('/api/gfs/connections'))
    await waitFor(() => expect(visibility(container, 'global').disabled).toBe(true))
  })

  it('disables Global when connections exist but none are active', async () => {
    gfsConns = [{ status: 'pending' }, { status: 'suspended' }]
    const { container } = await open()
    await waitFor(() => expect(visibility(container, 'global').disabled).toBe(true))
  })

  it('enables the Global option with an active GFS connection', async () => {
    gfsConns = [{ status: 'active' }]
    const { container } = await open()
    await waitFor(() => expect(visibility(container, 'global').disabled).toBe(false))
  })
})

// Connecting a global server is admin-only (connection management is
// admin-only on the backend), so only an admin is linked to Connections;
// a member gets a short "ask a household admin" hint instead.
describe('SpaceCreateDialog — connect-a-global-server hint is admin-only', () => {
  beforeEach(() => { post.mockReset(); get.mockClear(); gfsConns = []; cleanup() })

  it('an admin without a global server gets the Connections link (under the ingress base)', async () => {
    setViewer(true)
    const { container } = await open()
    await waitFor(() => expect(get).toHaveBeenCalledWith('/api/gfs/connections'))
    const link = [...container.querySelectorAll('a')]
      .find(a => a.textContent === 'space.create.gfs_hint_link')
    expect(link).toBeDefined()
    expect(link!.getAttribute('href')).toMatch(/\/connections$/)
    expect(container.textContent).not.toContain('space.create.gfs_hint_member')
    expect(container.textContent).toContain('space.create.gfs_hint_before')
    expect(container.textContent).toContain('space.create.global_needs_gfs_admin')
  })

  it('a non-admin gets an "ask a household admin" hint and no Connections link', async () => {
    setViewer(false)
    const { container } = await open()
    await waitFor(() => expect(get).toHaveBeenCalledWith('/api/gfs/connections'))
    expect(container.textContent).toContain('space.create.gfs_hint_member')
    expect(container.textContent).toContain('space.create.global_needs_gfs_member')
    expect(container.querySelector('a[href$="/connections"]')).toBeNull()
    expect(container.textContent).not.toContain('space.create.global_needs_gfs_admin')
    expect(visibility(container, 'global').disabled).toBe(true)
  })

  it('neither sees the hint once a global server is connected', async () => {
    gfsConns = [{ status: 'active' }]
    for (const isAdmin of [true, false]) {
      cleanup()
      setViewer(isAdmin)
      const { container } = await open()
      await waitFor(() => expect(visibility(container, 'global').disabled).toBe(false))
      expect(container.textContent).not.toContain('space.create.gfs_hint_before')
      expect(container.textContent).not.toContain('space.create.gfs_hint_member')
    }
  })
})

describe('SpaceCreateDialog — min-age + category', () => {
  beforeEach(() => { post.mockReset(); get.mockClear(); gfsConns = []; cleanup() })

  it('shows min-age + category for public', async () => {
    const { container } = await open()
    await act(async () => { fireEvent.input(nameInput(container), { target: { value: 'Neighbourhood' } }) })
    await act(async () => { fireEvent.click(pickVisibility(container, 'public')) })
    await waitFor(() => expect(minAgeSelect(container)).not.toBeNull())
    expect(categorySelect(container)).not.toBeNull()
  })

  it('shows min-age + category for global', async () => {
    gfsConns = [{ status: 'active' }]
    const { container } = await open()
    await waitFor(() => expect(visibility(container, 'global').disabled).toBe(false))
    await act(async () => { fireEvent.input(nameInput(container), { target: { value: 'World' } }) })
    await act(async () => { fireEvent.click(pickVisibility(container, 'global')) })
    await waitFor(() => expect(minAgeSelect(container)).not.toBeNull())
    expect(categorySelect(container)).not.toBeNull()
  })

  it('hides min-age + category for private and household', async () => {
    const { container } = await open()
    // default is private
    expect(minAgeSelect(container)).toBeNull()
    expect(categorySelect(container)).toBeNull()
    await act(async () => { fireEvent.click(pickVisibility(container, 'household')) })
    await waitFor(() => expect(minAgeSelect(container)).toBeNull())
    expect(categorySelect(container)).toBeNull()
  })

  it('hides the join-mode picker for household and sends open', async () => {
    post.mockResolvedValue({ id: 's9' })
    const { container } = await open()
    const joinPicker = () =>
      container.querySelector('input[name="space-create-join-mode"]')
    expect(joinPicker()).not.toBeNull()
    await act(async () => { fireEvent.input(nameInput(container), { target: { value: 'Home' } }) })
    await act(async () => { fireEvent.click(pickVisibility(container, 'household')) })
    await waitFor(() => expect(joinPicker()).toBeNull())
    await act(async () => { fireEvent.click(createBtn(container)) })
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    expect(post.mock.calls[0][1]).toMatchObject({
      space_type: 'household', join_mode: 'open',
    })
  })

  it('leaving household for public starts again from invite-only', async () => {
    const { container } = await open()
    await act(async () => { fireEvent.click(pickVisibility(container, 'household')) })
    await act(async () => { fireEvent.click(pickVisibility(container, 'public')) })
    await waitFor(() => {
      const checked = container.querySelector(
        'input[name="space-create-join-mode"]:checked',
      ) as HTMLInputElement | null
      expect(checked?.value).toBe('invite_only')
    })
  })

  it('sends category + min_age for public', async () => {
    post.mockResolvedValue({ id: 's3' })
    const { container } = await open()
    await act(async () => { fireEvent.input(nameInput(container), { target: { value: 'Neighbourhood' } }) })
    await act(async () => { fireEvent.click(pickVisibility(container, 'public')) })
    await waitFor(() => expect(categorySelect(container)).not.toBeNull())
    await act(async () => { fireEvent.change(categorySelect(container)!, { target: { value: 'gaming' } }) })
    await act(async () => { fireEvent.change(minAgeSelect(container)!, { target: { value: '18' } }) })
    await act(async () => { fireEvent.click(createBtn(container)) })
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    const body = post.mock.calls[0][1] as Record<string, unknown>
    expect(body).toMatchObject({ space_type: 'public', category: 'gaming', min_age: 18 })
  })

  it('labels categories and ages from the catalog but sends the stored values', async () => {
    post.mockResolvedValue({ id: 's5' })
    const { container } = await open()
    await act(async () => { fireEvent.input(nameInput(container), { target: { value: 'Neighbourhood' } }) })
    await act(async () => { fireEvent.click(pickVisibility(container, 'public')) })
    await waitFor(() => expect(categorySelect(container)).not.toBeNull())
    const local = categorySelect(container)!.querySelector('option[value="local"]')!
    expect(local.textContent).toBe('space.category.local')
    const ages = [...minAgeSelect(container)!.querySelectorAll('option')].map(o => o.textContent)
    expect(ages).toEqual([
      'space.create.min_age_none',
      'space.create.min_age_value:{"age":"13"}',
      'space.create.min_age_value:{"age":"16"}',
      'space.create.min_age_value:{"age":"18"}',
    ])
    await act(async () => { fireEvent.change(categorySelect(container)!, { target: { value: 'local' } }) })
    await act(async () => { fireEvent.click(createBtn(container)) })
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    expect(post.mock.calls[0][1]).toMatchObject({ category: 'local' })
  })

  it('takes the title, field labels and button from the catalog', async () => {
    const { container } = await open()
    const text = document.body.textContent ?? ''
    for (const key of [
      'space.create.title', 'space.create.name', 'space.create.description',
      'space.create.visibility', 'space.create.submit',
    ]) expect(text, key).toContain(key)
    expect(container.querySelector('textarea')?.getAttribute('placeholder'))
      .toBe('space.create.description_placeholder')
  })

  it('does not send category/min_age for a private space', async () => {
    post.mockResolvedValue({ id: 's4' })
    const { container } = await open()
    await act(async () => { fireEvent.input(nameInput(container), { target: { value: 'Family' } }) })
    await act(async () => { fireEvent.click(createBtn(container)) })
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    const body = post.mock.calls[0][1] as Record<string, unknown>
    expect('category' in body).toBe(false)
    expect('min_age' in body).toBe(false)
  })
})

describe('SpaceCreateDialog — protected account', () => {
  beforeEach(() => { post.mockReset(); get.mockClear(); gfsConns = [{ status: 'active' }]; cleanup() })

  it('offers public / global disabled with the reason, private stays selectable', async () => {
    currentUser.value = {
      user_id: 'u-kid', username: 'kid', display_name: 'Kid', is_admin: false,
      protected: true, restrictions: ['public_spaces'],
    } as typeof currentUser.value
    const { container } = await open()
    await waitFor(() => expect(get).toHaveBeenCalledWith('/api/gfs/connections'))
    expect(visibility(container, 'public').disabled).toBe(true)
    expect(visibility(container, 'global').disabled).toBe(true)
    expect(visibility(container, 'private').disabled).toBe(false)
    expect(container.textContent).toContain('protected.tier_unavailable')
    expect(container.querySelector('[role="note"][data-capability="public_spaces"]')).toBeTruthy()
  })

  it('leaves the tiers alone for an adult', async () => {
    setViewer(false)
    const { container } = await open()
    await waitFor(() => expect(visibility(container, 'global').disabled).toBe(false))
    expect(visibility(container, 'public').disabled).toBe(false)
    expect(container.querySelector('[data-capability="public_spaces"]')).toBeNull()
  })
})

// A global space is listed only on the GFSs the creator ticks — never on
// every connected one — and at least one is required.
describe('SpaceCreateDialog — choosing the GFSs of a global space', () => {
  const gfsA = { id: 'g-a', display_name: 'Town GFS', inbox_url: 'https://town.example', status: 'active' }
  const gfsB = { id: 'g-b', display_name: 'Club GFS', inbox_url: 'https://club.example/', status: 'active' }
  const gfsHeld = { id: 'g-h', display_name: 'Held GFS', inbox_url: 'https://held.example', status: 'pending' }
  const checklist = (c: Element) =>
    c.querySelector('[data-testid="space-create-checklist"]') as HTMLElement | null
  const boxes = (c: Element) =>
    [...c.querySelectorAll<HTMLInputElement>('input[name="space-create-gfs"]')]

  beforeEach(() => { post.mockReset(); get.mockClear(); gfsConns = []; cleanup() })

  async function openGlobal() {
    const r = await open()
    await waitFor(() => expect(visibility(r.container, 'global').disabled).toBe(false))
    await act(async () => { fireEvent.input(nameInput(r.container), { target: { value: 'World' } }) })
    await act(async () => { fireEvent.click(pickVisibility(r.container, 'global')) })
    return r
  }

  it('shows no checklist until Global is chosen', async () => {
    gfsConns = [gfsA]
    const { container } = await open()
    await waitFor(() => expect(visibility(container, 'global').disabled).toBe(false))
    expect(checklist(container)).toBeNull()
    await act(async () => { fireEvent.click(pickVisibility(container, 'public')) })
    expect(checklist(container)).toBeNull()
  })

  it('pre-ticks the only active GFS and sends it as gfs_ids', async () => {
    gfsConns = [gfsA, gfsHeld]
    post.mockResolvedValue({ id: 's1' })
    const { container } = await openGlobal()
    // The held (pending) connection is not offered.
    expect(boxes(container).map(b => b.value)).toEqual(['g-a'])
    expect(boxes(container)[0].checked).toBe(true)
    // Each row names the server and its host.
    expect(checklist(container)!.textContent).toContain('Town GFS')
    expect(checklist(container)!.textContent).toContain('town.example')
    await act(async () => { fireEvent.click(createBtn(container)) })
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    expect(post.mock.calls[0][1]).toMatchObject({ space_type: 'global', gfs_ids: ['g-a'] })
  })

  it('ticks nothing with several GFSs and requires at least one', async () => {
    gfsConns = [gfsA, gfsB]
    post.mockResolvedValue({ id: 's1' })
    const { container } = await openGlobal()
    expect(boxes(container).map(b => b.checked)).toEqual([false, false])
    await act(async () => { fireEvent.click(createBtn(container)) })
    // Nothing is sent; the reason sits under the list, announced.
    expect(post).not.toHaveBeenCalled()
    const err = container.querySelector('#space-create-error') as HTMLElement
    expect(err.textContent).toBe('error.gfs_selection_required')
    expect(err.getAttribute('role')).toBe('alert')
    expect(checklist(container)!.getAttribute('aria-describedby')).toBe('space-create-error')
    // The error may be below the fold on a phone: the list is brought into
    // view and its first box takes focus.
    await waitFor(() => expect(document.activeElement).toBe(boxes(container)[0]))
    // Ticking one clears the error and the create goes through with it.
    await act(async () => { fireEvent.click(boxes(container)[1]) })
    expect(container.querySelector('#space-create-error')).toBeNull()
    await act(async () => { fireEvent.click(createBtn(container)) })
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    expect(post.mock.calls[0][1]).toMatchObject({ gfs_ids: ['g-b'] })
  })

  it('sends every ticked GFS, and none for a non-global space', async () => {
    gfsConns = [gfsA, gfsB]
    post.mockResolvedValue({ id: 's1' })
    const { container } = await openGlobal()
    await act(async () => { fireEvent.click(boxes(container)[0]) })
    await act(async () => { fireEvent.click(boxes(container)[1]) })
    // Switching tiers keeps the choice for a switch back, but a public
    // space never carries one.
    await act(async () => { fireEvent.click(pickVisibility(container, 'public')) })
    await act(async () => { fireEvent.click(createBtn(container)) })
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    expect('gfs_ids' in (post.mock.calls[0][1] as object)).toBe(false)
  })

  it('shows the server refusal when the create fails', async () => {
    const { showToast } = await import('./Toast')
    gfsConns = [gfsA]
    post.mockRejectedValue(new Error('Choose at least one GFS to list this global space on.'))
    const { container } = await openGlobal()
    await act(async () => { fireEvent.click(createBtn(container)) })
    await waitFor(() => expect(showToast).toHaveBeenCalledWith(
      'Choose at least one GFS to list this global space on.', 'error',
    ))
  })
})
