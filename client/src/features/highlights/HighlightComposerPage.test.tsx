import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, waitFor, fireEvent } from '@testing-library/preact'

vi.mock('@/api', () => {
  const m = { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() }
  return { api: m }
})

vi.mock('preact-iso', () => ({
  useLocation: () => ({ route: vi.fn() }),
}))

vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))

vi.mock('@/components/UploadProgress', () => {
  let n = 0
  return {
    UploadProgressBar: () => null,
    uploadWithProgress: vi.fn(async () => {
      n += 1
      return { url: `/api/media/f${n}.webp`, signed_url: `/api/media/f${n}.webp?sig=x` }
    }),
  }
})

vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u-me', username: 'pascal', display_name: 'Pascal' } },
}))

import { api } from '@/api'
import { currentUser } from '@/store/auth'
import HighlightComposerPage, { audienceFromFriends } from './HighlightComposerPage'

const get = api.get as ReturnType<typeof vi.fn>

/** Shape of ``GET /api/friends`` (socialhome/routes/friends.py). */
const FRIENDS = {
  instance: {
    instance_id: 'inst-me',
    display_name: 'Home',
    members: [
      { user_id: 'u-me', username: 'pascal', display_name: 'Pascal' },
      { user_id: 'u-anna', username: 'anna', display_name: 'Anna' },
    ],
  },
  households: [
    {
      instance_id: 'inst-b',
      display_name: 'The Smiths',
      members: [
        { user_id: 'u-bob', remote_username: 'bob', display_name: 'Bob', personal_alias: 'Uncle Bob' },
      ],
    },
  ],
  totals: { households: 2, people: 3 },
}

function mockGet(friends: unknown) {
  get.mockReset()
  get.mockImplementation((path: string) => {
    if (path === '/api/friends') {
      return friends instanceof Error ? Promise.reject(friends) : Promise.resolve(friends)
    }
    if (path === '/api/highlights') return Promise.resolve([])
    return Promise.reject(new Error(`unexpected GET ${path}`))
  })
}

describe('audienceFromFriends', () => {
  it('maps households and people, dropping the author, preferring aliases', () => {
    const out = audienceFromFriends(FRIENDS, 'u-me')
    expect(out.households).toEqual([{ instance_id: 'inst-b', display_name: 'The Smiths' }])
    expect(out.people).toEqual([
      { user_id: 'u-anna', display_name: 'Anna', household_name: null },
      { user_id: 'u-bob', display_name: 'Uncle Bob', household_name: 'The Smiths' },
    ])
  })
})

describe('HighlightComposerPage audience picker', () => {
  beforeEach(() => mockGet(FRIENDS))

  it('loads households + people from GET /api/friends (not the dead routes)', async () => {
    const view = render(<HighlightComposerPage />)
    await waitFor(() => expect(get).toHaveBeenCalledWith('/api/friends'))
    const urls = get.mock.calls.map(c => c[0])
    expect(urls).not.toContain('/api/instances?status=confirmed')
    expect(urls).not.toContain('/api/connections/people')

    fireEvent.click(view.getByLabelText('Pick households'))
    expect(await view.findByLabelText('The Smiths')).toBeTruthy()

    fireEvent.click(view.getByText(/per-person picker/))
    fireEvent.click(view.getByLabelText('Pick people'))
    expect(await view.findByText('Uncle Bob')).toBeTruthy()
    expect(view.getByText('Anna')).toBeTruthy()
    expect(view.getByText(/The Smiths/, { selector: 'span' })).toBeTruthy()
  })

  it('surfaces a load failure with Retry instead of an empty picker', async () => {
    mockGet(new Error('API 500: /api/friends'))
    const view = render(<HighlightComposerPage />)
    fireEvent.click(view.getByLabelText('Pick households'))
    const alert = await view.findByRole('alert')
    expect(alert.textContent).toMatch(/Couldn't load your connections/)
    expect(view.queryByText('No paired households yet.')).toBeNull()

    mockGet(FRIENDS)
    fireEvent.click(view.getByRole('button', { name: 'Retry' }))
    expect(await view.findByLabelText('The Smiths')).toBeTruthy()
    expect(view.queryByRole('alert')).toBeNull()
  })

  it('shows the empty state when there are no connections', async () => {
    mockGet({ instance: { members: [] }, households: [] })
    const view = render(<HighlightComposerPage />)
    await waitFor(() => expect(get).toHaveBeenCalledWith('/api/friends'))
    fireEvent.click(view.getByLabelText('Pick households'))
    expect(await view.findByText('No paired households yet.')).toBeTruthy()
  })
})

describe('HighlightComposerPage today\'s audience', () => {
  const me = currentUser.value as unknown as Record<string, unknown>
  const today = new Date().toISOString().slice(0, 10)

  function mockToday(
    audience: { kind: string, ids: string[] } | null,
    { published = false }: { published?: boolean } = {},
  ) {
    get.mockReset()
    get.mockImplementation((path: string) => {
      if (path === '/api/friends') return Promise.resolve(FRIENDS)
      if (path === '/api/highlights') {
        return Promise.resolve(audience ? [{
          highlight: {
            id: 'h1', author_user_id: 'u-me', highlight_date: today,
            audience_kind: audience.kind, audience: audience.ids,
          },
          frames: [{ id: 'f1' }],
          unseen_count: 0,
        }] : [])
      }
      if (path === '/api/highlights/h1/publish') return Promise.resolve({ published })
      return Promise.reject(new Error(`unexpected GET ${path}`))
    })
  }

  /** Stage two photos and post them; the bodies the composer sent. */
  async function postTwoFrames(view: ReturnType<typeof render>) {
    const post = api.post as ReturnType<typeof vi.fn>
    post.mockReset()
    post.mockResolvedValue({ highlight: { id: 'h1' }, frame: { id: 'f' } })
    const input = view.container.querySelector('input[type=file]') as HTMLInputElement
    const files = [
      new File(['a'], 'a.webp', { type: 'image/webp' }),
      new File(['b'], 'b.webp', { type: 'image/webp' }),
    ]
    fireEvent.change(input, { target: { files } })
    const submit = await view.findByRole('button', { name: /Post 2 frames/ })
    fireEvent.click(submit)
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2))
    return post.mock.calls.map(c => c[1] as Record<string, unknown>)
  }

  beforeEach(() => { delete me.preferences_json })

  it('every frame names the audience it showed, so a frame after UTC midnight never falls back to the Settings default', async () => {
    mockToday({ kind: 'users', ids: ['u-anna'] })
    const view = render(<HighlightComposerPage />)
    await view.findByText(/Today's highlight is for Anna\./)
    const bodies = await postTwoFrames(view)
    for (const body of bodies) {
      expect(body.audience_kind).toBe('users')
      expect(body.audience).toEqual(['u-anna'])
    }
  })

  it('a new day sends the picked audience with every frame, not only the first', async () => {
    mockToday(null)
    const view = render(<HighlightComposerPage />)
    fireEvent.click(await view.findByLabelText(/Pick households/))
    fireEvent.click(await view.findByLabelText(/The Smiths/))
    const bodies = await postTwoFrames(view)
    for (const body of bodies) {
      expect(body.audience_kind).toBe('households')
      expect(body.audience).toEqual(['inst-b'])
    }
  })

  it('says new frames also reach anyone with the public link', async () => {
    mockToday({ kind: 'users', ids: ['u-anna'] }, { published: true })
    const view = render(<HighlightComposerPage />)
    expect(await view.findByText(
      "Today's highlight is for Anna and has a public link. New frames are added to it, so anyone with the link sees them too.",
    )).toBeTruthy()
  })

  it('appending shows today\'s audience as fixed, by name, with no picker', async () => {
    mockToday({ kind: 'users', ids: ['u-anna'] })
    const view = render(<HighlightComposerPage />)
    expect(await view.findByText(
      "Today's highlight is for Anna. New frames are added to it, so they also show wherever you shared it.",
    )).toBeTruthy()
    expect(view.queryByLabelText(/All paired households/)).toBeNull()
    expect(view.queryByLabelText('Pick households')).toBeNull()
  })

  it('a new day starts from the Settings default and marks it', async () => {
    me.preferences_json = '{"highlights": {"default_audience": {"kind": "households"}}}'
    mockToday(null)
    const view = render(<HighlightComposerPage />)
    const households = await view.findByLabelText(/Pick households/) as HTMLInputElement
    expect(households.checked).toBe(true)
    expect(households.parentElement!.textContent).toContain('(your default)')
    const all = view.getByLabelText(/All paired households/) as HTMLInputElement
    expect(all.checked).toBe(false)
    expect(all.parentElement!.textContent).not.toContain('default')
  })

  it('without a Settings default, all paired households is the marked default', async () => {
    mockToday(null)
    const view = render(<HighlightComposerPage />)
    const all = await view.findByLabelText(/All paired households/) as HTMLInputElement
    expect(all.checked).toBe(true)
    expect(all.parentElement!.textContent).toContain('(your default)')
  })
})
