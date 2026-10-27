import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

vi.mock('@/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))
const route = vi.hoisted(() => vi.fn())
vi.mock('preact-iso', () => ({ useLocation: () => ({ route, url: '/momentum' }) }))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))
const openPerson = vi.hoisted(() => vi.fn())
vi.mock('@/components/PersonSheet', () => ({ openPerson }))
const openReport = vi.hoisted(() => vi.fn())
vi.mock('@/components/ReportDialog', () => ({ openReport }))

import { api } from '@/api'
import type { Moment } from '@/types'
import { MomentCard, clipLength, reactionMode } from './MomentCard'

const ME = 'uid-me'

function moment(extra: Partial<Moment> = {}): Moment {
  return {
    id: 'm-1', author_user_id: 'uid-bob', content: 'Kaffee ☕ #garten',
    media_url: null, media_type: null, duration_ms: null, parent_moment_id: null,
    origin_instance_id: 'peer-o', created_at: new Date(Date.now() - 3_600_000).toISOString(),
    expires_at: new Date(Date.now() + 86_400_000).toISOString(),
    reaction_count: 0, reply_count: 0, received_via: 'self', hop_count: 1,
    reactions_reach: 'author', reaction_counts: {}, reaction_totals_at: null, my_reaction: null,
    author: { display_name: 'Bob Berger', picture_url: null, household: 'Berger' },
    ...extra,
  }
}

function renderCard(m: Moment, onPatch = vi.fn()) {
  return { onPatch, ...render(<MomentCard m={m} me={ME} onPatch={onPatch} onDeleted={vi.fn()} />) }
}

describe('reactionMode', () => {
  it('own moments summarise, complete counts are chips, a remote count is only yours', () => {
    expect(reactionMode(moment({ author_user_id: ME }), ME)).toBe('own')
    // Written in this household: every reaction is here.
    expect(reactionMode(moment({ received_via: 'self' }), ME)).toBe('counted')
    // The author's household sent its totals (v_69).
    expect(reactionMode(moment({ received_via: 'household', reaction_totals_at: 1 }), ME)).toBe('counted')
    // Reactions stay here: the household's count is the whole story.
    expect(reactionMode(moment({ received_via: 'household', reactions_reach: 'household' }), ME)).toBe('counted')
    // A paired author without totals yet: only your own reaction is known.
    expect(reactionMode(moment({ received_via: 'household' }), ME)).toBe('mine-only')
  })
})

describe('MomentCard', () => {
  beforeEach(() => {
    route.mockReset(); openPerson.mockReset(); openReport.mockReset()
    vi.mocked(api.put).mockReset().mockResolvedValue({})
    vi.mocked(api.delete).mockReset().mockResolvedValue({})
  })

  it('is an article named for its author; avatar and name open the person sheet', () => {
    const { getByRole, container } = renderCard(moment())
    expect(getByRole('article', { name: 'Moment by Bob Berger' })).toBeTruthy()
    fireEvent.click(getByRole('button', { name: 'Bob Berger' }))
    expect(openPerson).toHaveBeenCalledWith('uid-bob', expect.anything())
    // The user's words pick their own direction (RTL UI, LTR text).
    expect(container.querySelector('.sh-moment-card__text')?.getAttribute('dir')).toBe('auto')
    expect(container.querySelector('.sh-moment-card__name')?.getAttribute('dir')).toBe('auto')
  })

  it('the author of a moment that came through a GFS opens as someone known through it (one Follow)', () => {
    const { getByRole } = renderCard(moment({ received_via: 'gfs', received_via_gfs_id: 'g1', hop_count: undefined }))
    fireEvent.click(getByRole('button', { name: 'Bob Berger' }))
    expect(openPerson).toHaveBeenCalledWith('uid-bob', {
      person: expect.objectContaining({ public_via: { gfs_id: 'g1' } }),
    })
    openPerson.mockReset()
    const paired = renderCard(moment({ received_via: 'household' }))
    fireEvent.click(paired.getAllByRole('button', { name: 'Bob Berger' }).at(-1)!)
    expect(openPerson.mock.calls[0][1].person.public_via).toBeUndefined()
  })

  it('⋯ holds actions on the moment, never the person sheet', async () => {
    const { getByRole, findByRole } = renderCard(moment({ can_save: true }))
    fireEvent.click(getByRole('button', { name: 'More actions for this moment' }))
    const menu = await findByRole('menu')
    const items = [...menu.querySelectorAll('[role="menuitem"]')].map(b => b.textContent)
    expect(items).toEqual(['Reply', 'Copy text', 'Save', 'Report'])
    expect(openPerson).not.toHaveBeenCalled()
    fireEvent.click(getByRole('menuitem', { name: 'Report' }))
    expect(openReport).toHaveBeenCalledWith('moment', 'm-1')
  })

  it('your own moment offers Delete and Who sees this', async () => {
    const { getByRole, findByRole, findByText } = renderCard(moment({ author_user_id: ME }))
    fireEvent.click(getByRole('button', { name: 'More actions for this moment' }))
    const menu = await findByRole('menu')
    const items = [...menu.querySelectorAll('[role="menuitem"]')].map(b => b.textContent)
    expect(items).toEqual(['Reply', 'Copy text', 'Who sees this', 'Delete'])
    fireEvent.click(getByRole('menuitem', { name: 'Who sees this' }))
    expect(await findByText(/up to 3 hops away/)).toBeTruthy()
  })

  it('provenance: paired household, hops away, via GFS — nothing for your own household', () => {
    const paired = renderCard(moment({ received_via: 'household' }))
    expect(paired.container.querySelector('.sh-moment-card__meta')?.textContent).toContain('Berger · paired')
    paired.unmount()
    const far = renderCard(moment({ received_via: 'household', hop_count: 2 }))
    expect(far.container.querySelector('.sh-moment-card__meta')?.textContent).toContain('2 hops away')
    far.unmount()
    const gfs = renderCard(moment({ received_via: 'gfs' }))
    expect(gfs.container.querySelector('.sh-moment-card__meta')?.textContent).toContain('via GFS')
    gfs.unmount()
    const home = renderCard(moment())
    expect(home.container.querySelector('.sh-moment-card__meta')?.textContent).not.toMatch(/paired|hops|GFS/)
  })

  it('counted mode: one chip per emoji, yours pressed; tapping yours clears it', async () => {
    const { getByRole, onPatch } = renderCard(moment({
      reaction_counts: { '😂': 2, '❤️': 1 }, reaction_count: 3, my_reaction: '❤️',
    }))
    const mine = getByRole('button', { name: '❤️ 1, your reaction' })
    expect(mine.getAttribute('aria-pressed')).toBe('true')
    expect(getByRole('button', { name: '😂 2' }).getAttribute('aria-pressed')).toBe('false')
    fireEvent.click(mine)
    await waitFor(() => expect(api.delete).toHaveBeenCalledWith('/api/moments/m-1/reaction'))
    expect(onPatch).toHaveBeenCalledWith('m-1', expect.objectContaining({
      my_reaction: null, reaction_counts: { '😂': 2 }, reaction_count: 2,
    }))
  })

  it('counted mode: tapping another emoji moves your reaction there', async () => {
    const { getByRole, onPatch } = renderCard(moment({
      reaction_counts: { '😂': 2, '❤️': 1 }, reaction_count: 3, my_reaction: '❤️',
    }))
    fireEvent.click(getByRole('button', { name: '😂 2' }))
    await waitFor(() => expect(api.put).toHaveBeenCalledWith('/api/moments/m-1/reaction', { emoji: '😂' }))
    expect(onPatch).toHaveBeenCalledWith('m-1', expect.objectContaining({
      my_reaction: '😂', reaction_counts: { '😂': 3 }, reaction_count: 3,
    }))
  })

  it('mine-only mode: no partial numbers, one toggle that says "You"', () => {
    const { container, getByRole } = renderCard(moment({
      received_via: 'household', reaction_counts: { '😂': 1, '😮': 1 }, reaction_count: 2, my_reaction: '😮',
    }))
    const toggle = getByRole('button', { name: 'Your reaction: 😮. Change or remove' })
    expect(toggle.textContent).toContain('You')
    expect(container.textContent).not.toMatch(/😂/)
    expect(container.querySelector('.sh-moment-card__rx')?.textContent).not.toMatch(/\d/)
  })

  it('mine-only mode without a reaction offers React with the emoji choices', async () => {
    const { getByRole, findByRole, onPatch } = renderCard(moment({ received_via: 'household' }))
    fireEvent.click(getByRole('button', { name: 'React' }))
    await findByRole('menu')
    fireEvent.click(getByRole('menuitemradio', { name: '🔥' }))
    await waitFor(() => expect(api.put).toHaveBeenCalledWith('/api/moments/m-1/reaction', { emoji: '🔥' }))
    expect(onPatch).toHaveBeenCalledWith('m-1', expect.objectContaining({ my_reaction: '🔥' }))
  })

  it('household-only reach keeps the note, and React is described by it', () => {
    const { getByRole, container } = renderCard(moment({
      received_via: 'household', reactions_reach: 'household',
    }))
    const note = container.querySelector('.sh-moment-card__reach')!
    expect(note.textContent).toBe('Reactions here are seen in your household only.')
    expect(getByRole('button', { name: 'React' }).getAttribute('aria-describedby')).toBe(note.id)
  })

  it('your own moment shows a summary of its reactions, not buttons', () => {
    const { container, queryByRole } = renderCard(moment({
      author_user_id: ME, reaction_counts: { '❤️': 2, '🔥': 1 }, reaction_count: 3,
    }))
    expect(container.querySelector('.sh-moment-card__rxsum')?.textContent).toContain('3 reactions')
    expect(queryByRole('button', { name: 'React' })).toBeNull()
  })

  it('Reply opens the moment with the reply field; the newest reply sits under it', () => {
    const { getByRole, container } = renderCard(moment({
      reply_count: 2,
      latest_reply: {
        ...moment({ id: 'r-1', author_user_id: ME, content: 'Im Schrank', parent_moment_id: 'm-1' }),
      },
    }))
    expect(getByRole('link', { name: 'Reply' }).getAttribute('href')).toBe('/momentum/m-1/reply')
    expect(getByRole('link', { name: '2 replies' }).getAttribute('href')).toBe('/momentum/m-1')
    const nested = container.querySelector('.sh-moment-card__reply')!
    expect(nested.textContent).toContain('You')
    expect(nested.textContent).toContain('Im Schrank')
    expect(nested.querySelector('[dir="auto"]')).toBeTruthy()
  })

  it('as the moment page head: no thread or Reply link, ⋯ Reply goes to the page field', async () => {
    const onReply = vi.fn()
    const { queryByRole, getByRole, findByRole, getByText, container } = render(
      <MomentCard
        m={moment({ reply_count: 2, latest_reply: moment({ id: 'r-1', parent_moment_id: 'm-1' }) })}
        me={ME} onPatch={vi.fn()} onDeleted={vi.fn()}
        page={{ onReply, reactedBy: 'Anna reacted', status: <p>status band</p> }}
      />,
    )
    expect(queryByRole('link', { name: 'Reply' })).toBeNull()
    expect(container.querySelector('.sh-moment-card__thread')).toBeNull()
    expect(getByText('status band')).toBeTruthy()
    expect(getByText('Anna reacted')).toBeTruthy()
    fireEvent.click(getByRole('button', { name: 'More actions for this moment' }))
    await findByRole('menu')
    fireEvent.click(getByRole('menuitem', { name: 'Reply' }))
    expect(onReply).toHaveBeenCalled()
    expect(route).not.toHaveBeenCalled()
  })

  it('as the page head, your own moment shows its reactions as chips', () => {
    const { getByRole, queryByRole } = render(
      <MomentCard
        m={moment({ author_user_id: ME, reaction_count: 2, reaction_counts: { '😂': 2 } })}
        me={ME} onPatch={vi.fn()} onDeleted={vi.fn()} page={{ onReply: vi.fn() }}
      />,
    )
    expect(getByRole('button', { name: /😂.*2/ })).toBeTruthy()
    // The status band says who sees it; ⋯ has no "Who sees this".
    fireEvent.click(getByRole('button', { name: 'More actions for this moment' }))
    expect(queryByRole('menuitem', { name: 'Who sees this' })).toBeNull()
  })

  it('a video is a poster with ▶ and its length; the player comes on tap', () => {
    const { getByRole, container } = renderCard(moment({
      media_type: 'video', media_url: 'api/media/v.webm?sig=s',
      media_thumbnail_url: 'api/media/v.webp?sig=s', duration_ms: 3_600, media_status: 'ready',
    }))
    expect(container.querySelector('video')).toBeNull()
    const play = getByRole('button', { name: 'Play video, 0:03' })
    expect(play.querySelector('img')?.getAttribute('src')).toBe('api/media/v.webp?sig=s')
    expect(play.textContent).toContain('0:03')
    fireEvent.click(play)
    const video = container.querySelector('video')!
    expect(video.hasAttribute('controls')).toBe(true)
  })
})

describe('clipLength', () => {
  it('counts whole seconds down like the native player, never 0:00', () => {
    expect(clipLength(3_600)).toBe('0:03')
    expect(clipLength(3_000)).toBe('0:03')
    expect(clipLength(59_999)).toBe('0:59')
    expect(clipLength(61_500)).toBe('1:01')
    expect(clipLength(400)).toBe('0:01')
  })
})
