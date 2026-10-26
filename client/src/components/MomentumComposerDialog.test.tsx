import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, waitFor, act } from '@testing-library/preact'
import {
  MomentumComposerDialog,
  openMomentumComposer,
} from './MomentumComposerDialog'
import { ApiError } from '@/api'
import { currentUser } from '@/store/auth'
import { registrations } from '@/store/momentPublic'
import { showToast } from './Toast'
import { confirmDialog } from './confirm'
import { uploadWithProgress } from './UploadProgress'
import type { Moment, MomentPublicRegistration, User } from '@/types'

const mockApi = {
  get: vi.fn(),
  post: vi.fn(),
}
vi.mock('@/api', async (orig) => {
  const real = await orig<typeof import('@/api')>()
  return {
    ...real,
    get api() {
      return mockApi
    },
  }
})

vi.mock('./Toast', () => ({
  showToast: vi.fn(),
}))

const routeSpy = vi.hoisted(() => vi.fn())
vi.mock('preact-iso', () => ({ useLocation: () => ({ route: routeSpy }) }))

vi.mock('./confirm', () => ({
  confirmDialog: vi.fn(),
}))

vi.mock('./UploadProgress', () => ({
  uploadWithProgress: vi.fn(),
  UploadProgressBar: () => null,
}))

vi.mock('@/utils/videoDuration', () => ({
  formatSeconds: (ms: number) => (ms / 1000).toFixed(1),
  readVideoDurationMs: vi.fn(async () => 3_000),
}))

/** A registration whose "share by default" is on. */
const REG: MomentPublicRegistration = { user_id: 'u1', gfs_id: 'g1', registered_at: '', default_share: true }
/** A registration made after decision 4: "share by default" is off. */
const REG_OFF: MomentPublicRegistration = { ...REG, default_share: false }

let regs: MomentPublicRegistration[] = [REG]
let nextAllowed: string | null = null

function routeGet(path: string) {
  if (path === '/api/moments/public/registrations') return Promise.resolve(regs)
  if (path === '/api/moments/limits') return Promise.resolve({ next_allowed_at: nextAllowed })
  if (path === '/api/gfs/connections') return Promise.resolve([{ id: 'g1', display_name: 'Demo GFS', status: 'active' }])
  return Promise.reject(new Error(`unexpected GET ${path}`))
}

async function openAndSettle(...args: Parameters<typeof openMomentumComposer>) {
  await act(async () => {
    openMomentumComposer(...args)
    // loadRegistrations / limits resolve after a couple of microtasks.
    await new Promise((r) => setTimeout(r, 0))
  })
}

function publicSwitch(container: Element): HTMLInputElement | null {
  return container.querySelector('.sh-momentum-composer-public input[role="switch"]')
}

function submitButton(container: Element): HTMLButtonElement {
  return container.querySelector('button[type="submit"]') as HTMLButtonElement
}

function inMinutes(n: number): string {
  return new Date(Date.now() + n * 60_000).toISOString()
}

const PARENT = {
  id: 'm-parent',
  author_user_id: 'u-anna',
  content: 'Erster Frost heute Morgen',
  is_public: false,
  author: { display_name: 'Anna Berger', personal_alias: null, picture_url: null, household: 'Berger' },
} as unknown as Moment

describe('MomentumComposerDialog', () => {
  beforeEach(() => {
    regs = [REG]
    nextAllowed = null
    registrations.value = []
    currentUser.value = { user_id: 'u1', username: 'me', display_name: 'Me' } as User
    mockApi.get.mockReset()
    mockApi.post.mockReset()
    mockApi.get.mockImplementation(routeGet)
    mockApi.post.mockResolvedValue({ id: 'm-new' })
    vi.mocked(showToast).mockReset()
    vi.mocked(confirmDialog).mockReset()
    vi.mocked(uploadWithProgress).mockReset()
  })
  afterEach(() => {
    // Close whatever is still open so the next test starts clean.
    vi.mocked(confirmDialog).mockResolvedValue(true)
  })

  it('a new moment follows the "share by default" setting', async () => {
    const { container } = render(<MomentumComposerDialog />)
    await openAndSettle()
    await waitFor(() => expect(publicSwitch(container)?.checked).toBe(true))
  })

  it('public is off for a registration that does not share by default, and the button says when it is on', async () => {
    regs = [REG_OFF]
    const { container } = render(<MomentumComposerDialog />)
    await openAndSettle()
    await waitFor(() => expect(publicSwitch(container)).not.toBeNull())
    expect(publicSwitch(container)!.checked).toBe(false)
    expect(submitButton(container).textContent).toContain('Post moment')
    await act(async () => { fireEvent.click(publicSwitch(container)!) })
    expect(publicSwitch(container)!.checked).toBe(true)
    expect(submitButton(container).textContent).toContain('Post publicly')
    // The row names the GFS.
    await waitFor(() => expect(container.textContent).toContain('Demo GFS'))
  })

  it('a public choice made before the registrations load is never overwritten (B25)', async () => {
    // Cached from an earlier visit: share by default.
    registrations.value = [REG]
    let release: (v: MomentPublicRegistration[]) => void = () => {}
    mockApi.get.mockImplementation((path: string) =>
      path === '/api/moments/public/registrations'
        ? new Promise((r) => { release = r })
        : routeGet(path))
    const { container } = render(<MomentumComposerDialog />)
    await openAndSettle()
    expect(publicSwitch(container)!.checked).toBe(true)
    await act(async () => { fireEvent.click(publicSwitch(container)!) })
    expect(publicSwitch(container)!.checked).toBe(false)
    await act(async () => {
      release([REG])
      await new Promise((r) => setTimeout(r, 0))
    })
    expect(publicSwitch(container)!.checked).toBe(false)
  })

  it('the public row links to Settings › Privacy › Public sharing; leaving asks about a draft first', async () => {
    regs = [REG_OFF]
    const { container } = render(<MomentumComposerDialog />)
    await openAndSettle()
    const link = await waitFor(() => {
      const a = container.querySelector('a.sh-momentum-composer-public-settings') as HTMLAnchorElement
      expect(a).not.toBeNull()
      return a
    })
    expect(link.textContent).toBe('Public sharing settings')
    expect(link.getAttribute('href')).toBe('/settings#public-sharing')
    // A draft: the person is asked, and "keep writing" stays here.
    const ta = container.querySelector('textarea')!
    await act(async () => { fireEvent.input(ta, { target: { value: 'Half a thought' } }) })
    vi.mocked(confirmDialog).mockResolvedValueOnce(false)
    routeSpy.mockReset()
    await act(async () => { fireEvent.click(link) })
    expect(confirmDialog).toHaveBeenCalled()
    expect(routeSpy).not.toHaveBeenCalled()
    expect(container.querySelector('textarea')).not.toBeNull()
    vi.mocked(confirmDialog).mockResolvedValueOnce(true)
    await act(async () => { fireEvent.click(link) })
    await waitFor(() => expect(routeSpy).toHaveBeenCalledWith('/settings#public-sharing'))
  })

  it('no public row without a registration', async () => {
    regs = []
    const { container } = render(<MomentumComposerDialog />)
    await openAndSettle()
    expect(publicSwitch(container)).toBeNull()
  })

  it('says where a moment goes in one line, with the details behind (i)', async () => {
    const { container } = render(<MomentumComposerDialog />)
    await openAndSettle()
    const reach = container.querySelector('.sh-momentum-composer-reach')!
    expect(reach.textContent).toContain('Paired households and theirs, up to 3 hops')
    // Never a "Change" link next to reach: max_hops only changes what you see.
    expect(reach.querySelector('a')).toBeNull()
    const info = reach.querySelector('button[aria-expanded]') as HTMLButtonElement
    expect(info.getAttribute('aria-expanded')).toBe('false')
    await act(async () => { fireEvent.click(info) })
    expect(info.getAttribute('aria-expanded')).toBe('true')
    const more = container.querySelector('.sh-momentum-composer-reach-more')!
    // How long followers keep it: the author's own setting (#1175).
    expect(more.textContent).toMatch(/keep it for \d+\sdays \(your setting\)/)
    expect(more.textContent).toContain('only changes what reaches you')
  })

  it('a protected account is told its moments reach its household and paired households only', async () => {
    currentUser.value = { user_id: 'u1', username: 'finn', display_name: 'Finn', protected: true } as User
    const { container } = render(<MomentumComposerDialog />)
    await openAndSettle()
    const reach = container.querySelector('.sh-momentum-composer-reach')!
    expect(reach.textContent).toContain('Your household and paired households')
    expect(reach.textContent).not.toContain('3 hops')
  })

  it('shows the next allowed time before writing; drafting stays possible', async () => {
    nextAllowed = inMinutes(9)
    const { container } = render(<MomentumComposerDialog />)
    await openAndSettle()
    await waitFor(() => expect(container.querySelector('.sh-momentum-composer-limit')).not.toBeNull())
    const banner = container.querySelector('.sh-momentum-composer-limit')!
    expect(banner.getAttribute('role')).toBe('status')
    expect(banner.textContent).toMatch(/next moment at \d{1,2}:\d{2}/)
    expect(banner.textContent).toContain('in 9 min')
    const textarea = container.querySelector('textarea')!
    expect(textarea.disabled).toBe(false)
    fireEvent.input(textarea, { target: { value: 'draft' } })
    const btn = submitButton(container)
    expect(btn.disabled).toBe(true)
    expect(btn.textContent).toMatch(/Post at \d{1,2}:\d{2}/)
  })

  it('a time already past does not hold the button', async () => {
    nextAllowed = inMinutes(-1)
    const { container } = render(<MomentumComposerDialog />)
    await openAndSettle()
    expect(container.querySelector('.sh-momentum-composer-limit')).toBeNull()
    expect(submitButton(container).disabled).toBe(false)
  })

  it('a reply is exempt from the limit', async () => {
    nextAllowed = inMinutes(9)
    const { container } = render(<MomentumComposerDialog />)
    await openAndSettle('m-parent', { parent: PARENT })
    expect(mockApi.get).not.toHaveBeenCalledWith('/api/moments/limits')
    expect(container.querySelector('.sh-momentum-composer-limit')).toBeNull()
    expect(submitButton(container).disabled).toBe(false)
  })

  it('a 429 keeps the draft and shows when the next moment can go, without a toast', async () => {
    const at = inMinutes(5)
    mockApi.post.mockRejectedValue(
      new ApiError(429, '/api/moments', { code: 'MOMENT_RATE_LIMIT', detail: 'x', next_allowed_at: at }),
    )
    const { container } = render(<MomentumComposerDialog />)
    await openAndSettle()
    const textarea = container.querySelector('textarea')!
    fireEvent.input(textarea, { target: { value: 'hello' } })
    await act(async () => { fireEvent.submit(container.querySelector('form')!) })
    await waitFor(() => expect(container.querySelector('.sh-momentum-composer-limit')).not.toBeNull())
    expect(textarea.value).toBe('hello')
    expect(showToast).not.toHaveBeenCalled()
    expect(submitButton(container).disabled).toBe(true)
  })

  it('Post is disabled while the media uploads (B9)', async () => {
    let finish: (v: { url: string; signed_url: string }) => void = () => {}
    vi.mocked(uploadWithProgress).mockImplementation(() => new Promise((r) => { finish = r as typeof finish }))
    const { container } = render(<MomentumComposerDialog />)
    await openAndSettle()
    fireEvent.input(container.querySelector('textarea')!, { target: { value: 'pic' } })
    const input = container.querySelector('input[type="file"]') as HTMLInputElement
    const file = new File(['x'], 'a.jpg', { type: 'image/jpeg' })
    Object.defineProperty(input, 'files', { value: [file] })
    await act(async () => { fireEvent.change(input) })
    expect(submitButton(container).disabled).toBe(true)
    await act(async () => {
      finish({ url: 'api/media/a.webp', signed_url: 'api/media/a.webp?sig=1' })
      await new Promise((r) => setTimeout(r, 0))
    })
    expect(submitButton(container).disabled).toBe(false)
  })

  it('closing with text asks before discarding it (C18)', async () => {
    vi.mocked(confirmDialog).mockResolvedValue(false)
    const { container, getByText } = render(<MomentumComposerDialog />)
    await openAndSettle()
    fireEvent.input(container.querySelector('textarea')!, { target: { value: 'half a thought' } })
    await act(async () => { fireEvent.click(getByText('Cancel')) })
    expect(confirmDialog).toHaveBeenCalledTimes(1)
    // "Keep writing": still open, text kept.
    expect(container.querySelector('textarea')?.value).toBe('half a thought')
    vi.mocked(confirmDialog).mockResolvedValue(true)
    await act(async () => { fireEvent.click(getByText('Cancel')) })
    await waitFor(() => expect(container.querySelector('textarea')).toBeNull())
    // Cancel never posts what was written (it was a submit button).
    expect(mockApi.post).not.toHaveBeenCalled()
  })

  it('closing an empty composer does not ask', async () => {
    const { container, getByText } = render(<MomentumComposerDialog />)
    await openAndSettle()
    await act(async () => { fireEvent.click(getByText('Cancel')) })
    expect(confirmDialog).not.toHaveBeenCalled()
    await waitFor(() => expect(container.querySelector('textarea')).toBeNull())
  })

  it('a reply quotes its parent and says honestly where it goes', async () => {
    const { container } = render(<MomentumComposerDialog />)
    await openAndSettle('m-parent', { parent: PARENT })
    const quote = container.querySelector('.sh-momentum-composer-quote')!
    expect(quote.textContent).toContain('Anna Berger')
    expect(quote.textContent).toContain('Erster Frost heute Morgen')
    expect(container.querySelector('.sh-momentum-composer-reach')!.textContent)
      .toContain('Your reply goes to your paired households and theirs, like a moment')
  })

  it('a reply to a moment that is not public offers no public toggle', async () => {
    const { container } = render(<MomentumComposerDialog />)
    await openAndSettle('m-parent', { parent: PARENT })
    expect(publicSwitch(container)).toBeNull()
    const text = container.querySelector('textarea')!
    fireEvent.input(text, { target: { value: 'hi' } })
    fireEvent.submit(container.querySelector('form')!)
    await waitFor(() => expect(mockApi.post).toHaveBeenCalled())
    const body = mockApi.post.mock.calls[0][1] as Record<string, unknown>
    expect(body.parent_moment_id).toBe('m-parent')
    expect(body.is_public).not.toBe(true)
  })

  it('a deep-linked reply reads its parent: quote, and still private when the parent is', async () => {
    mockApi.get.mockImplementation((path: string) =>
      path === '/api/moments/m-parent'
        ? Promise.resolve({ moment: PARENT, replies: [], reactions: [] })
        : routeGet(path))
    const { container } = render(<MomentumComposerDialog />)
    await openAndSettle('m-parent')
    await waitFor(() => expect(container.querySelector('.sh-momentum-composer-quote')).not.toBeNull())
    expect(publicSwitch(container)).toBeNull()
  })

  it('a reply whose parent cannot be read stays private', async () => {
    const { container } = render(<MomentumComposerDialog />)
    await openAndSettle('m-parent')
    expect(publicSwitch(container)).toBeNull()
  })

  it('a reply to a public moment offers the toggle but never pre-ticks it', async () => {
    const { container } = render(<MomentumComposerDialog />)
    await openAndSettle('m-parent', { parent: { ...PARENT, is_public: true } })
    await waitFor(() => expect(publicSwitch(container)).not.toBeNull())
    expect(publicSwitch(container)!.checked).toBe(false)
    await act(async () => { fireEvent.click(publicSwitch(container)!) })
    expect(submitButton(container).textContent).toContain('Reply publicly')
  })
})
