import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor, fireEvent } from '@testing-library/preact'

vi.mock('@/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))
const route = vi.hoisted(() => vi.fn())
const where = vi.hoisted(() => ({
  momentId: 'm-amaka',
  path: '/momentum/m-amaka',
  query: {} as Record<string, string>,
}))
vi.mock('preact-iso', () => ({
  useRoute: () => ({ params: { momentId: where.momentId } }),
  useLocation: () => ({
    route,
    url: where.path + (where.query.reply ? `?reply=${where.query.reply}` : ''),
    path: where.path,
    query: where.query,
  }),
}))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))
vi.mock('@/ws', () => ({ ws: { on: () => () => undefined } }))
const openReport = vi.hoisted(() => vi.fn())
vi.mock('@/components/ReportDialog', () => ({ openReport }))
const confirmDialog = vi.hoisted(() => vi.fn(async () => true))
vi.mock('@/components/confirm', () => ({ confirmDialog }))

import { api } from '@/api'
import { showToast } from '@/components/Toast'
import { currentUser } from '@/store/auth'
import { householdUsers } from '@/store/householdUsers'
import type { Moment, MomentDetail, User } from '@/types'
import { noteMomentumList, _resetMomentumListForTests } from './backLink'
import { _resetGfsNamesForTests } from './gfsNames'
import MomentumDetailPage, { reactedSummary } from './MomentumDetailPage'

function moment(id: string, author: string, extra: Partial<Moment> = {}): Moment {
  return {
    id, author_user_id: author, content: `hello from ${id}`,
    media_url: null, media_type: null, duration_ms: null, parent_moment_id: null,
    origin_instance_id: 'peer-o', created_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + 86_400_000).toISOString(),
    reaction_count: 0, reply_count: 0, ...extra,
  }
}

function user(id: string, name: string): User {
  return { user_id: id, username: id, display_name: name } as User
}

/** Answer the detail read(s) with ``by`` (path → detail or a thrown error). */
function serve(by: Record<string, MomentDetail | Error>, extra: Record<string, unknown> = {}) {
  vi.mocked(api.get).mockImplementation(async (path: string) => {
    const hit = by[path]
    if (hit instanceof Error) throw hit
    if (hit) return hit
    if (path in extra) return extra[path]
    return []
  })
}

const notFound = () => Object.assign(new Error('moment not found'), { status: 404 })

beforeEach(() => {
  where.momentId = 'm-amaka'
  where.path = '/momentum/m-amaka'
  where.query = {}
  route.mockReset()
  openReport.mockReset()
  confirmDialog.mockReset().mockResolvedValue(true)
  vi.mocked(showToast).mockClear()
  vi.mocked(api.post).mockReset()
  vi.mocked(api.put).mockReset().mockResolvedValue({})
  vi.mocked(api.delete).mockReset().mockResolvedValue({})
  currentUser.value = { user_id: 'u-me', username: 'me', display_name: 'Me', is_admin: false } as never
  householdUsers.value = new Map()
  _resetMomentumListForTests()
  _resetGfsNamesForTests()
})

describe('MomentumDetailPage authors', () => {
  it('names the remote author and a remote replier by the server\'s author', async () => {
    serve({
      '/api/moments/m-amaka': {
        moment: moment('m-amaka', 'ru-amaka', {
          author: { display_name: 'Amaka Okafor', picture_url: 'api/users/ru-amaka/picture?v=1&sig=s' },
        }),
        replies: [moment('m-reply', 'ru-chidi', {
          parent_moment_id: 'm-amaka',
          author: { display_name: 'Chidi Okafor', picture_url: null },
        })],
        reactions: [],
      },
    })
    const { container } = render(<MomentumDetailPage />)
    await waitFor(() => expect(container.textContent).toContain('Amaka Okafor'))
    expect(container.textContent).toContain('Chidi Okafor')
    expect(container.textContent).not.toContain('Someone')
    expect(container.textContent).not.toContain('ru-chidi')
    expect(
      [...container.querySelectorAll('img')].map(i => i.getAttribute('src')),
    ).toContain('api/users/ru-amaka/picture?v=1&sig=s')
  })

  it('shows a GFS-followed author\'s name, picture and public bio', async () => {
    serve({
      '/api/moments/m-amaka': {
        moment: moment('m-amaka', 'u-gfs-noor', {
          received_via: 'gfs',
          received_via_gfs_id: 'g1',
          author: {
            display_name: 'Noor Haddad',
            picture_url: 'api/gfs/g1/moments/users/u-gfs-noor/picture?v=0123&sig=s',
            household: null,
            bio: 'Beekeeper. Posts the bees.',
          },
        }),
        replies: [], reactions: [],
      },
    })
    const { container } = render(<MomentumDetailPage />)
    await waitFor(() => expect(container.textContent).toContain('Noor Haddad'))
    expect(
      container.querySelector('[data-testid="moment-author-bio"]')?.textContent,
    ).toBe('Beekeeper. Posts the bees.')
  })

  it('shows no bio line for a household author', async () => {
    serve({
      '/api/moments/m-amaka': {
        moment: moment('m-amaka', 'ru-amaka', { author: { display_name: 'Amaka Okafor', picture_url: null } }),
        replies: [], reactions: [],
      },
    })
    const { container } = render(<MomentumDetailPage />)
    await waitFor(() => expect(container.textContent).toContain('Amaka Okafor'))
    expect(container.querySelector('[data-testid="moment-author-bio"]')).toBeNull()
  })
})

describe('MomentumDetailPage back link', () => {
  it('goes back to the tab and tag the person came from', async () => {
    noteMomentumList('/momentum?tag=garten')
    serve({ '/api/moments/m-amaka': { moment: moment('m-amaka', 'ru-amaka'), replies: [], reactions: [] } })
    const { findByRole } = render(<MomentumDetailPage />)
    const back = await findByRole('link', { name: 'Moments' })
    expect(back.getAttribute('href')).toMatch(/\/momentum\?tag=garten$/)
    fireEvent.click(back)
    expect(route).toHaveBeenCalledWith('/momentum?tag=garten')
  })

  it('names Yours when that is where the person came from', async () => {
    noteMomentumList('/momentum/yours')
    serve({ '/api/moments/m-amaka': { moment: moment('m-amaka', 'u-me'), replies: [], reactions: [] } })
    const { findByRole } = render(<MomentumDetailPage />)
    expect((await findByRole('link', { name: 'Yours' })).getAttribute('href')).toMatch(/\/momentum\/yours$/)
  })

  it('a direct visit goes back to Moments', async () => {
    serve({ '/api/moments/m-amaka': { moment: moment('m-amaka', 'ru-amaka'), replies: [], reactions: [] } })
    const { findByRole } = render(<MomentumDetailPage />)
    expect((await findByRole('link', { name: 'Moments' })).getAttribute('href')).toMatch(/\/momentum$/)
  })
})

describe('MomentumDetailPage status of your own moment', () => {
  const asAuthor = (extra: Partial<Moment>, registered = false) => serve(
    { '/api/moments/m-amaka': { moment: moment('m-amaka', 'u-me', extra), replies: [], reactions: [] } },
    {
      '/api/gfs/connections': [{ id: 'g1', display_name: 'Demo GFS' }],
      '/api/moments/public/registrations': registered ? [{ gfs_id: 'g1', default_share: false }] : [],
    },
  )

  it('says who sees a household moment, and offers no way to make it public', async () => {
    asAuthor({ is_public: false })
    const { findByRole, container } = render(<MomentumDetailPage />)
    const band = await findByRole('group', { name: 'Who sees this' })
    expect(band.textContent).toContain('Your household and paired households, up to 3 hops')
    expect(band.textContent).toMatch(/followers keep it 7\sdays/)
    expect(container.textContent).not.toMatch(/Stop sharing|Make public|Share publicly|Copy link/)
  })

  it('a protected account’s moment stays with its household and paired households', async () => {
    currentUser.value = { user_id: 'u-me', username: 'me', protected: true } as never
    asAuthor({ is_public: false })
    const { findByRole } = render(<MomentumDetailPage />)
    const band = await findByRole('group', { name: 'Who sees this' })
    expect(band.textContent).toContain('Your household and your paired households')
    expect(band.textContent).not.toContain('3 hops')
    expect(band.textContent).not.toContain('followers')
  })

  it('a public moment names its GFS and copies the share link — never "Stop sharing"', async () => {
    asAuthor({ is_public: true }, true)
    vi.mocked(api.post).mockResolvedValue({ url: 'https://viewer.example/v/x/#m=m-amaka' })
    const writeText = vi.fn(async () => undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    const { findByRole, container } = render(<MomentumDetailPage />)
    const band = await findByRole('group', { name: 'Who sees this' })
    await waitFor(() => expect(band.textContent).toContain('Public on Demo GFS'))
    expect(container.textContent).not.toMatch(/Stop sharing/)
    fireEvent.click(await findByRole('button', { name: 'Copy link' }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('https://viewer.example/v/x/#m=m-amaka'))
    expect(api.post).toHaveBeenCalledWith('/api/moments/m-amaka/share-link', {})
  })

  it('a failed share link says so and copies nothing', async () => {
    asAuthor({ is_public: true })
    vi.mocked(api.post).mockRejectedValue(new Error('nope'))
    const writeText = vi.fn(async () => undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    const { findByRole } = render(<MomentumDetailPage />)
    fireEvent.click(await findByRole('button', { name: 'Copy link' }))
    await waitFor(() => expect(showToast).toHaveBeenCalled())
    expect(writeText).not.toHaveBeenCalled()
  })

  it('an ended moment says only you see it now, with no reply field or link', async () => {
    const past = Date.now() - 20 * 86_400_000
    asAuthor({
      created_at: new Date(past).toISOString(),
      expires_at: new Date(past + 7 * 86_400_000).toISOString(),
      is_public: true,
    })
    const { findByRole, queryByRole } = render(<MomentumDetailPage />)
    const band = await findByRole('group', { name: 'Who sees this' })
    expect(band.textContent).toContain('Only you see this now. It stays under Yours.')
    expect(queryByRole('textbox', { name: /Write a reply/ })).toBeNull()
    expect(queryByRole('button', { name: 'Copy link' })).toBeNull()
  })

  it('no status band on someone else’s moment', async () => {
    serve({ '/api/moments/m-amaka': { moment: moment('m-amaka', 'ru-amaka'), replies: [], reactions: [] } })
    const { findByText, queryByRole } = render(<MomentumDetailPage />)
    await findByText('hello from m-amaka')
    expect(queryByRole('group', { name: 'Who sees this' })).toBeNull()
  })
})

describe('MomentumDetailPage gone vs failed', () => {
  it('a moment that is gone says so and leads back, without a toast', async () => {
    serve({ '/api/moments/m-amaka': notFound() })
    const { findByRole, container } = render(<MomentumDetailPage />)
    const state = await findByRole('status')
    expect(state.textContent).toContain("This moment isn't here any more")
    expect(container.querySelector('.sh-empty-state a[href$="/momentum"]')?.textContent).toBe('Back to Momentum')
    expect(container.querySelector('[role="alert"]')).toBeNull()
    expect(showToast).not.toHaveBeenCalled()
  })

  it('a failed load is not "gone": it says so and offers Try again', async () => {
    let fail = true
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path !== '/api/moments/m-amaka') return []
      if (fail) throw new Error('offline')
      return {
        moment: moment('m-amaka', 'ru-amaka', { author: { display_name: 'Amaka Okafor', picture_url: null } }),
        replies: [], reactions: [],
      } satisfies MomentDetail
    })
    const { findByRole, container } = render(<MomentumDetailPage />)
    expect((await findByRole('alert')).textContent).toContain("Couldn't load this moment")
    expect(container.textContent).not.toContain("isn't here any more")
    fail = false
    fireEvent.click(await findByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(container.textContent).toContain('Amaka Okafor'))
  })
})

describe('MomentumDetailPage save', () => {
  it('says a saved moment stays after it ended, and ⋯ shows it saved', async () => {
    serve({
      '/api/moments/m-amaka': {
        moment: moment('m-amaka', 'ru-amaka', {
          expires_at: new Date(Date.now() - 3_600_000).toISOString(),
          saved_by_me: true, can_save: true,
          author: { display_name: 'Amaka Okafor', picture_url: null },
        }),
        replies: [], reactions: [],
      },
    })
    const { findByRole, container, getByRole } = render(<MomentumDetailPage />)
    fireEvent.click(await findByRole('button', { name: 'More actions for this moment' }))
    expect(getByRole('menuitem', { name: 'Saved' })).toBeTruthy()
    expect(container.textContent).toContain('You saved this. It stays with you until the author deletes it.')
  })
})

describe('MomentumDetailPage reactions', () => {
  const NOTE = 'Reactions here are seen in your household only.'

  it('says a reaction stays in the household where it cannot reach the author', async () => {
    serve({
      '/api/moments/m-amaka': {
        moment: moment('m-amaka', 'ru-amaka', { reactions_reach: 'household', reaction_counts: {} }),
        replies: [], reactions: [],
      },
    })
    const { findByText, getAllByRole } = render(<MomentumDetailPage />)
    await findByText(NOTE)
    expect(getAllByRole('button', { name: /React/ }).length).toBeGreaterThan(0)
  })

  it('keeps the author household totals line (v_69) with its chips', async () => {
    serve({
      '/api/moments/m-amaka': {
        moment: moment('m-amaka', 'ru-amaka', {
          received_via: 'household',
          reactions_reach: 'author',
          reaction_count: 12,
          reaction_counts: { '🔥': 9, '👏': 3 },
          reaction_totals_at: Date.now() - 60_000,
          my_reaction: '👏',
        }),
        replies: [],
        reactions: [{ moment_id: 'm-amaka', reactor_user_id: 'u-me', emoji: '👏', reacted_at: '' }],
      },
    })
    const { findByTestId, getByRole, container } = render(<MomentumDetailPage />)
    expect((await findByTestId('moment-reaction-totals')).textContent).toContain('Reactions from everyone')
    expect(getByRole('button', { name: /🔥.*9/ })).toBeTruthy()
    expect(getByRole('button', { name: /👏.*3/ }).getAttribute('aria-pressed')).toBe('true')
    expect(container.textContent).not.toContain(NOTE)
  })

  it('names who reacted where they are known here', async () => {
    householdUsers.value = new Map([
      ['u-anna', user('u-anna', 'Anna')],
      ['u-lena', user('u-lena', 'Lena')],
      ['u-hilde', user('u-hilde', 'Oma Hilde')],
    ])
    serve({
      '/api/moments/m-amaka': {
        moment: moment('m-amaka', 'u-me', {
          received_via: 'self',
          reaction_count: 4, reaction_counts: { '😂': 2, '❤️': 2 },
        }),
        replies: [],
        reactions: [
          { moment_id: 'm-amaka', reactor_user_id: 'u-anna', emoji: '😂', reacted_at: '' },
          { moment_id: 'm-amaka', reactor_user_id: 'u-lena', emoji: '😂', reacted_at: '' },
          { moment_id: 'm-amaka', reactor_user_id: 'u-hilde', emoji: '❤️', reacted_at: '' },
          { moment_id: 'm-amaka', reactor_user_id: 'ru-far', emoji: '❤️', reacted_at: '' },
        ],
      },
    }, { '/api/users': [...householdUsers.value.values()] })
    const { findByTestId } = render(<MomentumDetailPage />)
    expect((await findByTestId('moment-reacted-by')).textContent)
      .toBe('Anna, Lena, Oma Hilde, and 1 other reacted')
  })

  it('a reaction shows at once, before the server answers (B6)', async () => {
    let resolve!: () => void
    vi.mocked(api.put).mockReturnValue(new Promise<void>((r) => { resolve = r }) as never)
    serve({
      '/api/moments/m-amaka': {
        moment: moment('m-amaka', 'ru-amaka', {
          received_via: 'self', reaction_count: 1, reaction_counts: { '🔥': 1 }, my_reaction: null,
        }),
        replies: [], reactions: [],
      },
    })
    const { findByRole } = render(<MomentumDetailPage />)
    const chip = await findByRole('button', { name: /🔥.*1/ })
    fireEvent.click(chip)
    await waitFor(async () => expect(
      (await findByRole('button', { name: /🔥.*2/ })).getAttribute('aria-pressed'),
    ).toBe('true'))
    expect(api.put).toHaveBeenCalledWith('/api/moments/m-amaka/reaction', { emoji: '🔥' })
    resolve()
  })
})

describe('reactedSummary', () => {
  it('lists names, then the rest as a count', () => {
    expect(reactedSummary([], 0)).toBeNull()
    expect(reactedSummary(['Anna'], 0)).toBe('Anna reacted')
    expect(reactedSummary(['Anna', 'Lena'], 0)).toBe('Anna and Lena reacted')
    expect(reactedSummary(['Anna'], 3)).toBe('Anna and 3 others reacted')
    expect(reactedSummary([], 2)).toBeNull()
  })
})

describe('MomentumDetailPage replies', () => {
  const thread = (replies: Moment[], extra: Partial<Moment> = {}): MomentDetail => ({
    moment: moment('m-amaka', 'ru-amaka', { author: { display_name: 'Amaka Okafor', picture_url: null }, ...extra }),
    replies,
    reactions: [],
  })
  const reply = (id: string, author: string, name: string) => moment(id, author, {
    parent_moment_id: 'm-amaka', author: { display_name: name, picture_url: null },
  })

  it('your reply offers Delete reply, which removes it', async () => {
    serve({ '/api/moments/m-amaka': thread([reply('r-1', 'u-me', 'Me')]) })
    const { findByRole, getByRole, container } = render(<MomentumDetailPage />)
    const more = await findByRole('button', { name: 'Actions for this reply' })
    expect(more.className).toContain('sh-moment-reply__more')
    fireEvent.click(more)
    fireEvent.click(getByRole('menuitem', { name: 'Delete reply' }))
    await waitFor(() => expect(api.delete).toHaveBeenCalledWith('/api/moments/r-1'))
    await waitFor(() => expect(container.querySelector('#moment-reply-r-1')).toBeNull())
  })

  it('someone else’s reply offers Report; a household admin can also remove it', async () => {
    serve({ '/api/moments/m-amaka': thread([reply('r-2', 'ru-chidi', 'Chidi')]) })
    const { findByRole, getByRole, queryByRole, unmount } = render(<MomentumDetailPage />)
    fireEvent.click(await findByRole('button', { name: 'Actions for this reply' }))
    expect(queryByRole('menuitem', { name: 'Remove reply' })).toBeNull()
    fireEvent.click(getByRole('menuitem', { name: 'Report' }))
    expect(openReport).toHaveBeenCalledWith('moment', 'r-2')
    unmount()

    currentUser.value = { user_id: 'u-me', username: 'me', is_admin: true } as never
    const admin = render(<MomentumDetailPage />)
    fireEvent.click(await admin.findByRole('button', { name: 'Actions for this reply' }))
    fireEvent.click(admin.getByRole('menuitem', { name: 'Remove reply' }))
    await waitFor(() => expect(api.delete).toHaveBeenCalledWith('/api/moments/r-2'))
  })

  it('writes a reply in place, with the honest reach and no public switch', async () => {
    serve({ '/api/moments/m-amaka': thread([]) })
    vi.mocked(api.post).mockResolvedValue(reply('r-new', 'u-me', 'Me'))
    const { findByRole, getByRole, container } = render(<MomentumDetailPage />)
    const field = await findByRole('textbox', { name: 'Write a reply' })
    expect(container.textContent).toContain('Your reply goes to your paired households and theirs, like a moment.')
    expect(container.querySelector('[role="switch"]')).toBeNull()
    fireEvent.input(field, { target: { value: 'Gefunden!' } })
    fireEvent.click(getByRole('button', { name: 'Reply' }))
    await waitFor(() => expect(api.post).toHaveBeenCalledWith('/api/moments', {
      content: 'Gefunden!', parent_moment_id: 'm-amaka',
    }))
    await waitFor(() => expect(container.querySelector('#moment-reply-r-new')).not.toBeNull())
    expect((field as HTMLTextAreaElement).value).toBe('')
  })

  it('offers the public switch only under a public moment, never pre-ticked', async () => {
    serve({ '/api/moments/m-amaka': thread([], { is_public: true }) }, { '/api/moments/public/registrations': [{ gfs_id: 'g1', default_share: true }] })
    vi.mocked(api.post).mockResolvedValue(reply('r-new', 'u-me', 'Me'))
    const { findByRole, getByRole } = render(<MomentumDetailPage />)
    const sw = await findByRole('switch')
    expect((sw as HTMLInputElement).checked).toBe(false)
    fireEvent.input(getByRole('textbox', { name: 'Write a reply' }), { target: { value: 'Hi' } })
    fireEvent.click(getByRole('button', { name: 'Reply' }))
    await waitFor(() => expect(api.post).toHaveBeenCalledWith('/api/moments', {
      content: 'Hi', parent_moment_id: 'm-amaka', is_public: false,
    }))
  })

  it('a household moment has no public switch even when you are registered', async () => {
    serve({ '/api/moments/m-amaka': thread([], { is_public: false }) }, { '/api/moments/public/registrations': [{ gfs_id: 'g1', default_share: true }] })
    const { findByRole, container } = render(<MomentumDetailPage />)
    await findByRole('textbox', { name: 'Write a reply' })
    expect(container.querySelector('[role="switch"]')).toBeNull()
  })

  it('/momentum/{id}/reply focuses the reply field', async () => {
    where.path = '/momentum/m-amaka/reply'
    serve({ '/api/moments/m-amaka': thread([]) })
    const { findByRole } = render(<MomentumDetailPage />)
    const field = await findByRole('textbox', { name: 'Write a reply' })
    await waitFor(() => expect(document.activeElement).toBe(field))
  })

  it('a link to a reply picks it out and scrolls it into view', async () => {
    where.query = { reply: 'r-2' }
    const scroll = vi.fn()
    Element.prototype.scrollIntoView = scroll
    serve({ '/api/moments/m-amaka': thread([reply('r-1', 'ru-a', 'A'), reply('r-2', 'ru-b', 'B')]) })
    const { container } = render(<MomentumDetailPage />)
    await waitFor(() => expect(container.querySelector('#moment-reply-r-2')?.className)
      .toContain('sh-moment-reply--picked'))
    expect(container.querySelector('#moment-reply-r-1')?.className).not.toContain('--picked')
    await waitFor(() => expect(scroll).toHaveBeenCalled())
  })

  it('opening a reply shows its moment with the reply picked out', async () => {
    where.momentId = 'r-2'
    where.path = '/momentum/r-2'
    serve({
      '/api/moments/r-2': { moment: reply('r-2', 'ru-b', 'B'), replies: [], reactions: [] },
      '/api/moments/m-amaka': thread([reply('r-2', 'ru-b', 'B')]),
    })
    render(<MomentumDetailPage />)
    await waitFor(() => expect(route).toHaveBeenCalledWith('/momentum/m-amaka?reply=r-2', true))
  })

  it('a reply whose moment is gone shows alone and says so', async () => {
    where.query = { reply: 'r-2' }
    serve({
      '/api/moments/m-amaka': notFound(),
      '/api/moments/r-2': { moment: reply('r-2', 'ru-b', 'Bea'), replies: [], reactions: [] },
    })
    const { findByText, queryByRole, container } = render(<MomentumDetailPage />)
    await findByText('The moment this answers has ended')
    expect(container.textContent).toContain('hello from r-2')
    expect(container.textContent).not.toContain("isn't here any more")
    expect(queryByRole('textbox', { name: 'Write a reply' })).toBeNull()
  })
})
