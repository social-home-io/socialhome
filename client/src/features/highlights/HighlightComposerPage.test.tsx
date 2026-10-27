import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, waitFor, fireEvent, within } from '@testing-library/preact'

vi.mock('@/api', () => {
  const m = { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() }
  return { api: m }
})

vi.mock('preact-iso', () => ({
  useLocation: () => ({ route: vi.fn() }),
}))

vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))

const upload = vi.hoisted(() => ({ n: 0, status: undefined as 'processing' | undefined }))
vi.mock('@/components/UploadProgress', () => ({
  UploadProgressBar: () => null,
  uploadWithProgress: vi.fn(async (file: File) => {
    upload.n += 1
    const ext = file.type.startsWith('video/') ? 'webm' : 'webp'
    return {
      url: `api/media/f${upload.n}.${ext}`,
      signed_url: `api/media/f${upload.n}.${ext}?sig=x`,
      filename: `f${upload.n}.${ext}`,
      ...(upload.status ? { media_status: upload.status } : {}),
    }
  }),
}))

vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u-me', username: 'pascal', display_name: 'Pascal' } },
}))

import { api } from '@/api'
import { currentUser } from '@/store/auth'
import { markMediaReady } from '@/store/mediaReady'
import HighlightComposerPage, { audienceFromFriends } from './HighlightComposerPage'

const get = api.get as ReturnType<typeof vi.fn>
const post = api.post as ReturnType<typeof vi.fn>
const patch = api.patch as ReturnType<typeof vi.fn>
const del = api.delete as ReturnType<typeof vi.fn>
const me = currentUser.value as unknown as Record<string, unknown>
const TODAY = new Date().toISOString().slice(0, 10)

/** Shape of ``GET /api/friends`` (socialhome/routes/friends.py). */
const FRIENDS = {
  instance: {
    instance_id: 'inst-me',
    display_name: 'Berger',
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
    {
      instance_id: 'inst-d',
      display_name: 'Dora’s',
      members: [{ user_id: 'u-dora', remote_username: 'dora', display_name: 'Dora' }],
    },
  ],
  totals: { households: 3, people: 4 },
}

function mockServer(
  today: { kind: string, ids: string[], frames?: number } | null,
  {
    friends = FRIENDS as unknown,
    published = false,
    shared = { household: false, spaces: [] as Array<{ id: string, name: string }> },
  }: {
    friends?: unknown
    published?: boolean
    shared?: { household: boolean, spaces: Array<{ id: string, name: string }> }
  } = {},
) {
  get.mockReset()
  get.mockImplementation((path: string) => {
    if (path === '/api/friends') {
      return friends instanceof Error ? Promise.reject(friends) : Promise.resolve(friends)
    }
    if (path === '/api/highlights') {
      return Promise.resolve(today ? [{
        highlight: {
          id: 'h1', author_user_id: 'u-me', highlight_date: TODAY,
          audience_kind: today.kind, audience: today.ids,
          created_at: `${TODAY}T08:12:00+00:00`,
        },
        frames: Array.from({ length: today.frames ?? 1 }, (_, i) => ({ id: `f${i}` })),
        unseen_count: 0,
      }] : [])
    }
    if (path === '/api/highlights/h1/publish') return Promise.resolve({ published })
    if (path === '/api/highlights/h1/audience' && today) {
      return Promise.resolve({ audience_kind: today.kind, audience: today.ids, published, shared })
    }
    return Promise.reject(new Error(`unexpected GET ${path}`))
  })
}

function stage(view: ReturnType<typeof render>, files: File[]) {
  const input = view.container.querySelector('input[type=file]') as HTMLInputElement
  fireEvent.change(input, { target: { files } })
}

const photo = (name: string) => new File(['x'], name, { type: 'image/webp' })

/** Stage two photos and post them; the bodies the composer sent. */
async function postTwoFrames(view: ReturnType<typeof render>, button = /^(Share|Add)$/) {
  post.mockReset()
  post.mockResolvedValue({ highlight: { id: 'h1' }, frame: { id: 'f' } })
  stage(view, [photo('a.webp'), photo('b.webp')])
  await view.findByRole('button', { name: /: b\.webp$/ })
  fireEvent.click(view.getByRole('button', { name: button }))
  await waitFor(() => expect(post).toHaveBeenCalledTimes(2))
  return post.mock.calls.map(c => c[1] as Record<string, unknown>)
}

beforeEach(() => {
  delete me.preferences_json
  upload.status = undefined
  patch.mockReset()
})

describe('audienceFromFriends', () => {
  it('maps households and people, dropping the author, preferring aliases', () => {
    const out = audienceFromFriends(FRIENDS, 'u-me')
    expect(out.households.map(h => h.display_name)).toEqual(['The Smiths', 'Dora’s'])
    expect(out.people).toEqual([
      { user_id: 'u-anna', display_name: 'Anna', household_name: null },
      { user_id: 'u-bob', display_name: 'Uncle Bob', household_name: 'The Smiths' },
      { user_id: 'u-dora', display_name: 'Dora', household_name: 'Dora’s' },
    ])
  })
})

describe('first frame of the day: who can see it', () => {
  it('offers all paired / your household / choose, the Settings default preselected', async () => {
    mockServer(null)
    const view = render(<HighlightComposerPage />)
    expect(await view.findByRole('heading', { name: "Today's highlight" })).toBeTruthy()
    const all = view.getByLabelText(/All paired households/) as HTMLInputElement
    expect(all.checked).toBe(true)
    expect(all.closest('label')!.textContent).toContain('Your default (Settings)')
    const own = view.getByLabelText(/Your household only/)
    await waitFor(() => expect(own.closest('label')!.textContent).toContain('Berger · 2 people'))
  })

  it('loads households and people from GET /api/friends', async () => {
    mockServer(null)
    const view = render(<HighlightComposerPage />)
    fireEvent.click(await view.findByLabelText(/Choose households or people/))
    expect(await view.findByLabelText('The Smiths')).toBeTruthy()
    fireEvent.click(view.getByRole('button', { name: 'People' }))
    expect(await view.findByText('Uncle Bob')).toBeTruthy()
    expect(view.getByText('Anna')).toBeTruthy()
  })

  it('choosing needs at least one pick before Share (B21)', async () => {
    mockServer(null)
    const view = render(<HighlightComposerPage />)
    stage(view, [photo('a.webp')])
    const share = await view.findByRole('button', { name: 'Share' }) as HTMLButtonElement
    await waitFor(() => expect(share.disabled).toBe(false))
    fireEvent.click(view.getByLabelText(/Choose households or people/))
    expect(view.getByText('Pick at least one')).toBeTruthy()
    expect(share.disabled).toBe(true)
    fireEvent.click(await view.findByLabelText('The Smiths'))
    expect(share.disabled).toBe(false)
    expect(view.queryByText('Pick at least one')).toBeNull()
  })

  it('sends the picked audience with every frame, not only the first', async () => {
    mockServer(null)
    const view = render(<HighlightComposerPage />)
    fireEvent.click(await view.findByLabelText(/Choose households or people/))
    fireEvent.click(await view.findByLabelText('The Smiths'))
    for (const body of await postTwoFrames(view)) {
      expect(body.audience_kind).toBe('households')
      expect(body.audience).toEqual(['inst-b'])
    }
  })

  it('"Your household only" sends households with none listed', async () => {
    mockServer(null)
    const view = render(<HighlightComposerPage />)
    fireEvent.click(await view.findByLabelText(/Your household only/))
    for (const body of await postTwoFrames(view)) {
      expect(body).toMatchObject({ audience_kind: 'households', audience: [] })
    }
  })

  it('pre-ticks the households chosen in Settings and drops one no longer paired', async () => {
    me.preferences_json = '{"highlights": {"default_audience": {"kind": "households", "ids": ["inst-b", "inst-gone"]}}}'
    mockServer(null)
    const view = render(<HighlightComposerPage />)
    const smiths = await view.findByLabelText('The Smiths') as HTMLInputElement
    expect(smiths.checked).toBe(true)
    for (const body of await postTwoFrames(view)) {
      expect(body.audience_kind).toBe('households')
      expect(body.audience).toEqual(['inst-b'])
    }
  })

  it('Settings "chosen" with nobody picked asks: choose is open, nothing ticked', async () => {
    me.preferences_json = '{"highlights": {"default_audience": {"kind": "households"}}}'
    mockServer(null)
    const view = render(<HighlightComposerPage />)
    const choose = await view.findByLabelText(/Choose households or people/) as HTMLInputElement
    expect(choose.checked).toBe(true)
    expect(view.getByText('Pick at least one')).toBeTruthy()
  })

  it('says how long it stays from the author’s own setting, never a fixed 30 days', async () => {
    me.preferences_json = '{"highlights": {"retention_days": 7}}'
    mockServer(null)
    const view = render(<HighlightComposerPage />)
    expect(await view.findByText(/Stays 7 days \(your setting\)/)).toBeTruthy()
    expect(view.container.textContent).not.toContain('30 days')
  })

  it('surfaces a load failure with Retry instead of an empty list', async () => {
    mockServer(null, { friends: new Error('API 500: /api/friends') })
    const view = render(<HighlightComposerPage />)
    fireEvent.click(await view.findByLabelText(/Choose households or people/))
    const alert = await view.findByRole('alert')
    expect(alert.textContent).toMatch(/Couldn't load your connections/)
    mockServer(null)
    fireEvent.click(view.getByRole('button', { name: 'Retry' }))
    expect(await view.findByLabelText('The Smiths')).toBeTruthy()
    expect(view.queryByRole('alert')).toBeNull()
  })

  it('shows the empty state when there are no connections', async () => {
    mockServer(null, { friends: { instance: { members: [] }, households: [] } })
    const view = render(<HighlightComposerPage />)
    fireEvent.click(await view.findByLabelText(/Choose households or people/))
    expect(await view.findByText('No paired households yet.')).toBeTruthy()
  })
})

describe('the frame strip', () => {
  beforeEach(() => mockServer(null))

  it('numbers frames, gives each a named 36 px ✕, and removes one', async () => {
    const view = render(<HighlightComposerPage />)
    stage(view, [photo('a.webp'), photo('b.webp')])
    await view.findByRole('button', { name: 'Frame 2: b.webp' })
    fireEvent.click(view.getByRole('button', { name: 'Remove b.webp' }))
    expect(view.queryByRole('button', { name: 'Frame 2: b.webp' })).toBeNull()
    expect(view.getByText('1 frame')).toBeTruthy()
  })

  it('reorders with Earlier / Later, and the order is what gets posted', async () => {
    const view = render(<HighlightComposerPage />)
    post.mockReset()
    post.mockResolvedValue({ highlight: { id: 'h1' }, frame: { id: 'f' } })
    stage(view, [photo('a.webp'), photo('b.webp')])
    // The last staged frame is selected; move it first.
    fireEvent.click(await view.findByRole('button', { name: 'Move frame 2 earlier' }))
    expect((view.getByRole('button', { name: 'Move frame 1 earlier' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(view.getByRole('button', { name: 'Share' }))
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2))
    const urls = post.mock.calls.map(c => (c[1] as { media_url: string }).media_url)
    expect(urls[0]).toMatch(/f\d+\.webp$/)
    expect(Number(urls[0].match(/f(\d+)/)![1])).toBeGreaterThan(Number(urls[1].match(/f(\d+)/)![1]))
  })

  it('a caption belongs to the selected frame', async () => {
    const view = render(<HighlightComposerPage />)
    post.mockReset()
    post.mockResolvedValue({ highlight: { id: 'h1' }, frame: { id: 'f' } })
    stage(view, [photo('a.webp'), photo('b.webp')])
    fireEvent.click(await view.findByRole('button', { name: 'Frame 1: a.webp' }))
    fireEvent.input(view.getByLabelText('Caption for frame 1'), { target: { value: 'Kürbisfeld!' } })
    fireEvent.click(view.getByRole('button', { name: 'Share' }))
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2))
    expect(post.mock.calls.map(c => (c[1] as { caption_text: unknown }).caption_text))
      .toEqual(['Kürbisfeld!', null])
  })

  it('a video still transcoding says "Preparing video…" instead of a broken preview', async () => {
    upload.status = 'processing'
    const view = render(<HighlightComposerPage />)
    stage(view, [new File(['v'], 'clip.mp4', { type: 'video/mp4' })])
    await view.findByRole('button', { name: 'Frame 1: clip.mp4' })
    expect(view.getAllByText('Preparing video…').length).toBe(2) // tile + preview
    expect(view.container.querySelector('video')).toBeNull()
    expect(view.getByText('1 frame · 1 video preparing')).toBeTruthy()
    markMediaReady(`f${upload.n}.webm`)
    await waitFor(() => expect(view.queryByText('Preparing video…')).toBeNull())
    expect(view.container.querySelector('video')).toBeTruthy()
  })
})

describe('adding to today’s highlight: the lock card', () => {
  it('shows who it goes to, when it was chosen, and no picker', async () => {
    mockServer({ kind: 'users', ids: ['u-anna'], frames: 5 })
    const view = render(<HighlightComposerPage />)
    expect(await view.findByRole('heading', { name: "Add to today's highlight" })).toBeTruthy()
    expect(await view.findByText('Goes to: Anna')).toBeTruthy()
    expect(view.getByText(/You chose this with today's first frame \(.+\)\. New frames go to the same people\. They also show wherever you shared it\./)).toBeTruthy()
    expect(view.queryByLabelText(/All paired households/)).toBeNull()
    expect(view.getByText('5 shared')).toBeTruthy()
  })

  it('names the public link when there is one', async () => {
    mockServer({ kind: 'users', ids: ['u-anna'] }, { published: true })
    const view = render(<HighlightComposerPage />)
    expect(await view.findByText(/It has a public link, so anyone with the link sees them too\./)).toBeTruthy()
  })

  it('every frame names today’s audience, so one after UTC midnight never takes the Settings default', async () => {
    me.preferences_json = '{"highlights": {"default_audience": {"kind": "all_paired"}}}'
    mockServer({ kind: 'users', ids: ['u-anna'] })
    const view = render(<HighlightComposerPage />)
    await view.findByText('Goes to: Anna')
    for (const body of await postTwoFrames(view, /^Add$/)) {
      expect(body.audience_kind).toBe('users')
      expect(body.audience).toEqual(['u-anna'])
    }
  })
})

describe('Change for all of today…', () => {
  async function openSheet() {
    mockServer({ kind: 'all_paired', ids: [], frames: 5 })
    const view = render(<HighlightComposerPage />)
    await view.findByText('Goes to: all your paired households')
    fireEvent.click(view.getByRole('button', { name: 'Change for all of today…' }))
    const sheet = await view.findByRole('dialog', { name: "Who sees today's highlight" })
    return { view, sheet: within(sheet) }
  }

  it('says exactly what changes for everyone, and that watching can’t be undone', async () => {
    const { sheet } = await openSheet()
    expect(sheet.getByText("That's who sees it now.")).toBeTruthy()
    expect((sheet.getByRole('button', { name: 'Change for everyone' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(sheet.getByLabelText(/Your household only/))
    expect(sheet.getByText("Won't see today's highlight any more:")).toBeTruthy()
    expect(sheet.getByText('The Smiths, Dora’s')).toBeTruthy()
    expect(sheet.getByText(/People who already watched keep what they saw/)).toBeTruthy()
  })

  it('widening names who gets every frame of today', async () => {
    mockServer({ kind: 'households', ids: ['inst-b'], frames: 5 })
    const view = render(<HighlightComposerPage />)
    await view.findByText('Goes to: The Smiths')
    fireEvent.click(view.getByRole('button', { name: 'Change for all of today…' }))
    const sheet = within(await view.findByRole('dialog'))
    fireEvent.click(sheet.getByLabelText(/All paired households/))
    expect(sheet.getByText('Will see all 5 of today\'s frames:')).toBeTruthy()
    expect(sheet.getByText('Dora’s')).toBeTruthy()
  })

  it('PATCHes the new audience and the lock card follows', async () => {
    const { view, sheet } = await openSheet()
    patch.mockResolvedValue({
      highlight: { id: 'h1', author_user_id: 'u-me', highlight_date: TODAY, audience_kind: 'households', audience: [] },
    })
    fireEvent.click(sheet.getByLabelText(/Your household only/))
    fireEvent.click(sheet.getByRole('button', { name: 'Change for everyone' }))
    await waitFor(() => expect(patch).toHaveBeenCalledWith(
      '/api/highlights/h1/audience', { audience_kind: 'households', audience: [] },
    ))
    expect(await view.findByText('Goes to: your household only')).toBeTruthy()
    expect(view.queryByRole('dialog')).toBeNull()
    // Later frames carry the new audience.
    for (const body of await postTwoFrames(view, /^Add$/)) {
      expect(body).toMatchObject({ audience_kind: 'households', audience: [] })
    }
  })

  it('a failed change keeps the sheet and the choice, with the error', async () => {
    const { view, sheet } = await openSheet()
    patch.mockRejectedValue(new Error("Only today's highlight can change who sees it."))
    fireEvent.click(sheet.getByLabelText(/Your household only/))
    fireEvent.click(sheet.getByRole('button', { name: 'Change for everyone' }))
    expect((await sheet.findByRole('alert')).textContent).toContain("Couldn't change it")
    expect((sheet.getByLabelText(/Your household only/) as HTMLInputElement).checked).toBe(true)
    expect(view.getByText('Goes to: all your paired households')).toBeTruthy()
  })

  it('a narrowing with a public link offers to remove it, pre-ticked, and does on confirm', async () => {
    mockServer({ kind: 'all_paired', ids: [], frames: 2 }, { published: true })
    const view = render(<HighlightComposerPage />)
    await view.findByText('Goes to: all your paired households')
    fireEvent.click(view.getByRole('button', { name: 'Change for all of today…' }))
    const sheet = within(await view.findByRole('dialog'))
    // Widening or no change: nothing about the link.
    expect(sheet.queryByLabelText('Also remove the public link')).toBeNull()
    fireEvent.click(sheet.getByLabelText(/Your household only/))
    const box = await sheet.findByLabelText('Also remove the public link') as HTMLInputElement
    expect(box.checked).toBe(true)
    expect(sheet.queryByText('Anyone with the public link can still watch it.')).toBeNull()
    patch.mockResolvedValue({
      highlight: { id: 'h1', author_user_id: 'u-me', highlight_date: TODAY, audience_kind: 'households', audience: [] },
    })
    del.mockReset()
    del.mockResolvedValue(undefined)
    fireEvent.click(sheet.getByRole('button', { name: 'Change for everyone' }))
    await waitFor(() => expect(del).toHaveBeenCalledWith('/api/highlights/h1/publish'))
    expect(patch.mock.invocationCallOrder[0]).toBeLessThan(del.mock.invocationCallOrder[0])
    // The lock card no longer mentions a public link.
    expect(await view.findByText(/They also show wherever you shared it\./)).toBeTruthy()
  })

  it('unticked, the link stays — and the sheet says so', async () => {
    mockServer({ kind: 'all_paired', ids: [], frames: 2 }, { published: true })
    const view = render(<HighlightComposerPage />)
    await view.findByText('Goes to: all your paired households')
    fireEvent.click(view.getByRole('button', { name: 'Change for all of today…' }))
    const sheet = within(await view.findByRole('dialog'))
    fireEvent.click(sheet.getByLabelText(/Your household only/))
    fireEvent.click(await sheet.findByLabelText('Also remove the public link'))
    expect(sheet.getByText('Anyone with the public link can still watch it.')).toBeTruthy()
    expect(sheet.getByText("Won't see it in their Highlights any more:")).toBeTruthy()
    patch.mockResolvedValue({
      highlight: { id: 'h1', author_user_id: 'u-me', highlight_date: TODAY, audience_kind: 'households', audience: [] },
    })
    del.mockReset()
    fireEvent.click(sheet.getByRole('button', { name: 'Change for everyone' }))
    await waitFor(() => expect(view.queryByRole('dialog')).toBeNull())
    expect(del).not.toHaveBeenCalled()
  })

  it('a narrowing of a shared highlight says who can still open it from the post', async () => {
    mockServer(
      { kind: 'all_paired', ids: [], frames: 2 },
      { shared: { household: true, spaces: [{ id: 's1', name: 'Trip Group' }, { id: 's2', name: 'Book club' }] } },
    )
    const view = render(<HighlightComposerPage />)
    await view.findByText('Goes to: all your paired households')
    fireEvent.click(view.getByRole('button', { name: 'Change for all of today…' }))
    const sheet = within(await view.findByRole('dialog'))
    fireEvent.click(sheet.getByLabelText(/Your household only/))
    expect(await sheet.findByText('People in Trip Group, Book club can still open it from your post.')).toBeTruthy()
    expect(sheet.getByText("Won't see it in their Highlights any more:")).toBeTruthy()
    expect(sheet.queryByLabelText('Also remove the public link')).toBeNull()
    // Household-wide: every housemate sees it anyway — no feed note.
    const feedNote = 'Everyone in your household can still open it from your post in the household feed.'
    expect(sheet.queryByText(feedNote)).toBeNull()
    // Picked people leave housemates out: the feed post still opens it.
    fireEvent.click(sheet.getByLabelText(/Choose households or people/))
    fireEvent.click(sheet.getByRole('button', { name: 'People' }))
    fireEvent.click(await sheet.findByLabelText(/Uncle Bob/))
    expect(await sheet.findByText(feedNote)).toBeTruthy()
  })

  it('Keep closes it and changes nothing', async () => {
    const { view, sheet } = await openSheet()
    fireEvent.click(sheet.getByLabelText(/Your household only/))
    fireEvent.click(sheet.getByRole('button', { name: 'Keep' }))
    await waitFor(() => expect(view.queryByRole('dialog')).toBeNull())
    expect(patch).not.toHaveBeenCalled()
  })
})
