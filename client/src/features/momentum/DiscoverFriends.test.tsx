import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor, fireEvent } from '@testing-library/preact'

vi.mock('@/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))
const showToastSpy = vi.hoisted(() => vi.fn())
vi.mock('@/components/Toast', () => ({ showToast: showToastSpy }))
const openPersonSpy = vi.hoisted(() => vi.fn())
vi.mock('@/components/PersonSheet', () => ({ openPerson: openPersonSpy }))

import { api } from '@/api'
import { _resetFollowsForTest, followedUserIds } from '@/store/follows'
import type { FriendsPayload } from '@/features/friends/friendsModel'
import { DiscoverFriends, discoverFriends } from './DiscoverFriends'

const local = (id: string, name: string) => ({
  user_id: id, username: name.toLowerCase(), display_name: name, picture_url: null,
})
const remote = (id: string, name: string, instance: string) => ({
  user_id: id, instance_id: instance, remote_username: name.toLowerCase(),
  display_name: name, picture_url: null,
})
const household = (id: string, name: string, members: ReturnType<typeof remote>[]) => ({
  instance_id: id, display_name: name, home_lat: null, home_lon: null, paired_at: null,
  reachable: true, members, member_count: members.length,
})

function payload(): FriendsPayload {
  return {
    instance: {
      instance_id: 'i-own', display_name: 'Alpha House', home_lat: null, home_lon: null,
      members: [local('u-me', 'Alice'), local('u-zoe', 'Zoe'), local('u-ben', 'Ben')],
      member_count: 3,
    },
    households: [
      household('i-okafor', 'The Okafors', [remote('u-grace', 'Grace', 'i-okafor'), remote('u-femi', 'Femi', 'i-okafor')]),
      household('i-bakker', 'Bakker family', [remote('u-daan', 'Daan', 'i-bakker')]),
    ],
    totals: { households: 3, people: 6 },
  }
}

describe('discoverFriends', () => {
  it('lists your own household first (without you), then paired households by name, people by name', () => {
    const people = discoverFriends(payload(), { selfId: 'u-me', locale: 'en' })
    expect(people.map((p) => p.shown)).toEqual(['Ben', 'Zoe', 'Daan', 'Femi', 'Grace'])
    expect(people[0].home.is_own).toBe(true)
    expect(people[2].home.display_name).toBe('Bakker family')
  })

  it('orders paired households by name only — an unreachable one does not jump ahead', () => {
    const data = payload()
    data.households[0].reachable = false
    const people = discoverFriends(data, { selfId: 'u-me', locale: 'en' })
    expect(people.map((p) => p.home.display_name)).toEqual(
      ['Alpha House', 'Alpha House', 'Bakker family', 'The Okafors', 'The Okafors'])
  })

  it('leaves out people the viewer blocked', () => {
    const people = discoverFriends(payload(), {
      selfId: 'u-me', locale: 'en', hidden: (id) => id === 'u-femi',
    })
    expect(people.map((p) => p.shown)).not.toContain('Femi')
  })
})

describe('DiscoverFriends', () => {
  const people = () => discoverFriends(payload(), { selfId: 'u-me', locale: 'en' })

  beforeEach(() => {
    vi.mocked(api.post).mockReset()
    vi.mocked(api.delete).mockReset()
    showToastSpy.mockReset()
    openPersonSpy.mockReset()
    _resetFollowsForTest()
  })

  it('names the section, counts who you follow and says a follow stays between households', async () => {
    followedUserIds.value = new Set(['u-daan'])
    const { getByRole, container } = render(<DiscoverFriends people={people()} query="" />)
    const heading = getByRole('heading', { name: 'Friends' })
    expect(heading.tagName).toBe('H2')
    expect(container.textContent).toContain('You follow 1 of 5')
    expect(container.textContent).toContain('stays between your households')
    const list = getByRole('list', { name: 'Friends' })
    expect(list.querySelectorAll('li').length).toBe(5)
    expect(getByRole('button', { name: 'Following Daan' }).getAttribute('aria-pressed')).toBe('true')
    expect(getByRole('button', { name: 'Follow Ben' }).getAttribute('aria-pressed')).toBe('false')
    // Each row says which household the person lives in.
    expect(list.querySelector('li')!.textContent).toContain('Alpha House')
  })

  it('Follow is the household follow and the row stays where it is', async () => {
    vi.mocked(api.post).mockResolvedValue({})
    const { getByRole } = render(<DiscoverFriends people={people()} query="" />)
    const before = [...getByRole('list', { name: 'Friends' }).querySelectorAll('li')].map((li) => li.textContent)
    fireEvent.click(getByRole('button', { name: 'Follow Femi' }))
    await waitFor(() => expect(api.post).toHaveBeenCalledWith('/api/moments/follows', { user_id: 'u-femi' }))
    const on = await waitFor(() => getByRole('button', { name: 'Following Femi' }))
    expect(on.getAttribute('aria-pressed')).toBe('true')
    const after = [...getByRole('list', { name: 'Friends' }).querySelectorAll('li')]
      .map((li) => li.textContent!.replace('Following', 'Follow'))
    expect(after).toEqual(before)
    expect(showToastSpy).toHaveBeenCalledWith('Following Femi', 'success')
  })

  it('Unfollow is one tap, says saved moments go, and Undo follows again', async () => {
    followedUserIds.value = new Set(['u-daan'])
    vi.mocked(api.delete).mockResolvedValue({})
    vi.mocked(api.post).mockResolvedValue({})
    const { getByRole } = render(<DiscoverFriends people={people()} query="" />)
    fireEvent.click(getByRole('button', { name: 'Following Daan' }))
    await waitFor(() => expect(api.delete).toHaveBeenCalledWith('/api/moments/follows/u-daan'))
    await waitFor(() => expect(showToastSpy).toHaveBeenCalled())
    const [message, kind, opts] = showToastSpy.mock.calls[0]
    expect(message).toBe('Unfollowed Daan. Any moments you saved from them are removed.')
    expect(kind).toBe('success')
    expect(opts.action.label).toBe('Undo')
    opts.action.onClick()
    await waitFor(() => expect(api.post).toHaveBeenCalledWith('/api/moments/follows', { user_id: 'u-daan' }))
    await waitFor(() => expect(getByRole('button', { name: 'Following Daan' })).toBeTruthy())
  })

  it('a failed follow says so and keeps the button as it was', async () => {
    vi.mocked(api.post).mockRejectedValue(new Error('offline'))
    const { getByRole } = render(<DiscoverFriends people={people()} query="" />)
    fireEvent.click(getByRole('button', { name: 'Follow Ben' }))
    await waitFor(() => expect(showToastSpy).toHaveBeenCalledWith("Couldn't follow: offline", 'error'))
    expect(getByRole('button', { name: 'Follow Ben' }).getAttribute('aria-pressed')).toBe('false')
  })

  it('a name opens the person sheet with their household', () => {
    const { getByRole } = render(<DiscoverFriends people={people()} query="" />)
    fireEvent.click(getByRole('button', { name: 'Grace' }))
    expect(openPersonSpy).toHaveBeenCalledWith('u-grace', {
      person: expect.objectContaining({
        display_name: 'Grace', is_local: false,
        household: { instance_id: 'i-okafor', display_name: 'The Okafors', is_own: false },
      }),
    })
  })

  it('more than six friends show five and an expander; a search shows every match', () => {
    const many = payload()
    many.households[0].members = 'ABCDEFG'.split('').map((c) => remote(`u-${c}`, `${c}name`, 'i-okafor'))
    const list = discoverFriends(many, { selfId: 'u-me', locale: 'en' })
    const { getByRole, rerender } = render(<DiscoverFriends people={list} query="" />)
    const ul = getByRole('list', { name: 'Friends' })
    expect(ul.querySelectorAll('li').length).toBe(5)
    const more = getByRole('button', { name: `${list.length - 5} more` })
    expect(more.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(more)
    expect(ul.querySelectorAll('li').length).toBe(list.length)
    expect(getByRole('button', { name: 'Show fewer' }).getAttribute('aria-expanded')).toBe('true')
    rerender(<DiscoverFriends people={list} query="name" />)
    expect(getByRole('list', { name: 'Friends' }).querySelectorAll('li').length).toBe(7)
  })

  it('a search with no friend matching says so', () => {
    const { container } = render(<DiscoverFriends people={people()} query="xyz" />)
    expect(container.textContent).toContain('No users match your search.')
  })

  it('no friends: one card pointing at Connections', () => {
    const { getByRole, container } = render(<DiscoverFriends people={[]} query="" />)
    expect(container.textContent).toContain('Pair with a household to follow its people here.')
    expect(getByRole('link', { name: 'Open Connections' }).getAttribute('href')).toBe('/connections')
  })

  it('a failed load is an inline error with Try again', () => {
    const onRetry = vi.fn()
    const { getByRole } = render(<DiscoverFriends people={null} failed query="" onRetry={onRetry} />)
    expect(getByRole('alert').textContent).toContain("Couldn't load your friends")
    fireEvent.click(getByRole('button', { name: 'Try again' }))
    expect(onRetry).toHaveBeenCalled()
  })

  it('while loading it shows a spinner, not an empty state', () => {
    const { container } = render(<DiscoverFriends people={null} query="" />)
    expect(container.textContent).not.toContain('Pair with a household')
    expect(container.querySelector('.sh-spinner, [role="status"]')).toBeTruthy()
  })
})
