import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, act, waitFor, cleanup } from '@testing-library/preact'

const post = vi.fn()
// The dialog loads GFS connections on open to decide whether the Global tier
// is selectable. Tests set ``gfsConns`` per-case before opening.
let gfsConns: { status: string }[] = []
const get = vi.fn(async (u: string) =>
  u === '/api/gfs/connections' ? gfsConns : [],
)
vi.mock('@/api', () => ({ api: { get: (...a: unknown[]) => get(...a as [string]), post: (...a: unknown[]) => post(...a) } }))
vi.mock('@/store/spaces', () => ({ loadSpaces: vi.fn() }))
vi.mock('./Toast', () => ({ showToast: vi.fn() }))
vi.mock('@/i18n/i18n', () => ({ t: (k: string) => k }))
// EmojiField pulls in the emoji-picker; not relevant here.
vi.mock('./EmojiField', () => ({ EmojiField: () => null }))

const { SpaceCreateDialog, openSpaceCreate } = await import('./SpaceCreateDialog')
const { currentUser } = await import('@/store/auth')

function setViewer(isAdmin: boolean) {
  currentUser.value = {
    user_id: 'u1', username: 'u', display_name: 'U', is_admin: isAdmin,
  } as typeof currentUser.value
}

const nameInput = (c: Element) =>
  c.querySelector('input[placeholder="e.g. Family, Makers Club"]') as HTMLInputElement
const createBtn = (c: Element) =>
  [...c.querySelectorAll('button')].find(b => b.textContent?.trim() === 'Create') as HTMLButtonElement
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

describe('SpaceCreateDialog — public location', () => {
  beforeEach(() => { post.mockReset(); get.mockClear(); gfsConns = []; cleanup() })

  it('module exports exist', () => {
    expect(typeof SpaceCreateDialog).toBe('function')
    expect(typeof openSpaceCreate).toBe('function')
  })

  it('selecting Public reveals a location field but does NOT gate Create on coords', async () => {
    const { container } = await open()
    await act(async () => { fireEvent.input(nameInput(container), { target: { value: 'Neighbourhood' } }) })
    expect(container.querySelector('.sh-space-create-location')).toBeNull()

    await act(async () => { fireEvent.click(pickVisibility(container, 'public')) })
    await waitFor(() => expect(container.querySelector('.sh-space-create-location')).not.toBeNull())
    // Name is set; location is optional now → Create is ENABLED.
    expect(createBtn(container).disabled).toBe(false)
  })

  it('submits lat/lon only for a public space', async () => {
    post.mockResolvedValue({ id: 's1' })
    const { container } = await open()
    await act(async () => { fireEvent.input(nameInput(container), { target: { value: 'Neighbourhood' } }) })
    await act(async () => { fireEvent.click(pickVisibility(container, 'public')) })
    const nums = container.querySelectorAll<HTMLInputElement>('.sh-space-create-location input[type="number"]')
    await act(async () => {
      fireEvent.input(nums[0], { target: { value: '52.52' } })
      fireEvent.input(nums[1], { target: { value: '13.405' } })
    })
    await act(async () => { fireEvent.click(createBtn(container)) })
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    const [url, body] = post.mock.calls[0]
    expect(url).toBe('/api/spaces')
    expect(body).toMatchObject({ space_type: 'public', lat: 52.52, lon: 13.405 })
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
      .find(a => a.textContent === 'Connect a global server')
    expect(link).toBeDefined()
    expect(link!.getAttribute('href')).toMatch(/\/connections$/)
    expect(container.textContent).not.toContain('Ask a household admin')
    expect(container.textContent).toContain('Connect a global server to publish worldwide.')
  })

  it('a non-admin gets an "ask a household admin" hint and no Connections link', async () => {
    setViewer(false)
    const { container } = await open()
    await waitFor(() => expect(get).toHaveBeenCalledWith('/api/gfs/connections'))
    expect(container.textContent)
      .toContain('Ask a household admin to connect a global server first.')
    expect(container.querySelector('a[href$="/connections"]')).toBeNull()
    expect(container.textContent).not.toContain('Connect a global server to publish worldwide.')
    expect(visibility(container, 'global').disabled).toBe(true)
  })

  it('neither sees the hint once a global server is connected', async () => {
    gfsConns = [{ status: 'active' }]
    for (const isAdmin of [true, false]) {
      cleanup()
      setViewer(isAdmin)
      const { container } = await open()
      await waitFor(() => expect(visibility(container, 'global').disabled).toBe(false))
      expect(container.textContent).not.toContain('Want a Global space?')
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
