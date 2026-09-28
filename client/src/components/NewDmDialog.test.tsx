import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render } from '@testing-library/preact'

beforeEach(() => {
  vi.resetModules()
})

/** Minimal ``/api/friends`` payload helper. The dialog flattens local
 *  + remote members into one picker list, so the test doubles need to
 *  match the wire shape. */
function _friendsPayload(opts: {
  meId?: string
  meName?: string
  localMembers?: Array<{ user_id: string; username: string; display_name: string }>
  remoteHouseholds?: Array<{
    instance_id: string
    display_name: string
    supports_group_dm?: boolean
    members: Array<{ user_id: string; remote_username: string; display_name: string }>
  }>
}) {
  const localMembers = [
    ...(opts.meId
      ? [
          {
            user_id: opts.meId,
            username: opts.meId,
            display_name: opts.meName ?? opts.meId,
            picture_url: null,
          },
        ]
      : []),
    ...(opts.localMembers ?? []).map(m => ({ ...m, picture_url: null })),
  ]
  const households = (opts.remoteHouseholds ?? []).map(h => ({
    instance_id: h.instance_id,
    display_name: h.display_name,
    supports_group_dm: h.supports_group_dm,
    members: h.members.map(m => ({ ...m, picture_url: null })),
  }))
  return {
    instance: {
      instance_id: 'my-instance',
      display_name: 'My Home',
      members: localMembers,
    },
    households,
  }
}

describe('NewDmDialog — module surface', () => {
  it('module exports exist', async () => {
    vi.doMock('@/api', () => ({ api: { get: vi.fn(), post: vi.fn() } }))
    vi.doMock('@/store/auth', () => ({ currentUser: { value: null } }))
    const mod = await import('./NewDmDialog')
    expect(mod).toBeTruthy()
    expect(Object.keys(mod).length).toBeGreaterThan(0)
  })
})

describe('NewDmDialog — local picker', () => {
  it('omits the current user from the recipient list', async () => {
    const get = vi.fn(async () =>
      _friendsPayload({
        meId: 'u-pascal',
        meName: 'Pascal',
        localMembers: [
          { user_id: 'u-maria', username: 'maria', display_name: 'Maria' },
          { user_id: 'u-lina', username: 'lina', display_name: 'Lina' },
        ],
      }),
    )
    vi.doMock('@/api', () => ({ api: { get, post: vi.fn() } }))
    vi.doMock('@/store/auth', () => ({
      currentUser: { value: { user_id: 'u-pascal', username: 'pascal', display_name: 'Pascal' } },
    }))
    const { NewDmDialog, openNewDm } = await import('./NewDmDialog')
    openNewDm()
    await new Promise((r) => setTimeout(r, 0))
    const { findByRole, queryByText } = render(<NewDmDialog />)
    await findByRole('dialog')
    expect(queryByText(/Maria/)).toBeTruthy()
    expect(queryByText(/Lina/)).toBeTruthy()
    expect(queryByText(/Pascal/)).toBeNull()
  })

  it('fetches /api/friends and renders remote-household members tagged with the household name', async () => {
    const get = vi.fn(async () =>
      _friendsPayload({
        meId: 'u-me',
        meName: 'Me',
        remoteHouseholds: [
          {
            instance_id: 'z7k63zfi',
            display_name: "Brother's house",
            members: [
              { user_id: 'u-bro', remote_username: 'bob', display_name: 'Bob' },
            ],
          },
        ],
      }),
    )
    vi.doMock('@/api', () => ({ api: { get, post: vi.fn() } }))
    vi.doMock('@/store/auth', () => ({
      currentUser: { value: { user_id: 'u-me', username: 'me' } },
    }))
    const { NewDmDialog, openNewDm } = await import('./NewDmDialog')
    openNewDm()
    await new Promise((r) => setTimeout(r, 0))
    expect(get).toHaveBeenCalledWith('/api/friends')
    const { findByRole, container } = render(<NewDmDialog />)
    await findByRole('dialog')
    const text = container.textContent ?? ''
    expect(text).toContain('Bob')
    expect(text).toContain("Brother's house")
  })

  it('renders the group-name field only after a 2nd member is picked', async () => {
    const get = vi.fn(async () =>
      _friendsPayload({
        meId: 'u-pascal',
        meName: 'Pascal',
        localMembers: [
          { user_id: 'u-maria', username: 'maria', display_name: 'Maria' },
          { user_id: 'u-lina', username: 'lina', display_name: 'Lina' },
        ],
      }),
    )
    vi.doMock('@/api', () => ({ api: { get, post: vi.fn() } }))
    vi.doMock('@/store/auth', () => ({
      currentUser: { value: { user_id: 'u-pascal', username: 'pascal' } },
    }))
    const { NewDmDialog, openNewDm } = await import('./NewDmDialog')
    openNewDm()
    await new Promise((r) => setTimeout(r, 0))
    const { container, findByRole, queryByText } = render(<NewDmDialog />)
    await findByRole('dialog')
    expect(queryByText(/Group name/)).toBeNull()

    const rows = container.querySelectorAll('.sh-newdm-row')
    ;(rows[0] as HTMLElement).click()
    await new Promise((r) => setTimeout(r, 0))
    expect(queryByText(/Group name/)).toBeNull()

    ;(rows[1] as HTMLElement).click()
    await new Promise((r) => setTimeout(r, 0))
    expect(queryByText(/Group name/)).toBeTruthy()
  })
})

describe('NewDmDialog — submit routing', () => {
  it('1-pick local → POST /api/conversations/dm with {username}', async () => {
    const post = vi.fn(async () => ({ id: 'cdm', type: 'dm' }))
    const get = vi.fn(async () =>
      _friendsPayload({
        meId: 'u-pascal',
        localMembers: [
          { user_id: 'u-maria', username: 'maria', display_name: 'Maria' },
        ],
      }),
    )
    vi.doMock('@/api', () => ({ api: { get, post } }))
    vi.doMock('@/store/auth', () => ({
      currentUser: { value: { user_id: 'u-pascal', username: 'pascal' } },
    }))
    const { NewDmDialog, openNewDm } = await import('./NewDmDialog')
    openNewDm()
    await new Promise((r) => setTimeout(r, 0))
    const { container, findByRole, getByText } = render(<NewDmDialog />)
    await findByRole('dialog')
    ;(container.querySelector('.sh-newdm-row') as HTMLElement).click()
    await new Promise((r) => setTimeout(r, 0))
    ;(getByText(/^Start$/) as HTMLElement).click()
    await new Promise((r) => setTimeout(r, 0))
    expect(post).toHaveBeenCalledWith(
      '/api/conversations/dm',
      { username: 'maria' },
    )
  })

  it('1-pick remote (paired-household member) → POST /api/conversations/dm with {user_id}', async () => {
    const post = vi.fn(async () => ({ id: 'cdm-cross', type: 'dm' }))
    const get = vi.fn(async () =>
      _friendsPayload({
        meId: 'u-pascal',
        remoteHouseholds: [
          {
            instance_id: 'z7k63zfi',
            display_name: "Brother's house",
            members: [
              { user_id: 'u-bro', remote_username: 'bob', display_name: 'Bob' },
            ],
          },
        ],
      }),
    )
    vi.doMock('@/api', () => ({ api: { get, post } }))
    vi.doMock('@/store/auth', () => ({
      currentUser: { value: { user_id: 'u-pascal', username: 'pascal' } },
    }))
    const { NewDmDialog, openNewDm } = await import('./NewDmDialog')
    openNewDm()
    await new Promise((r) => setTimeout(r, 0))
    const { container, findByRole, getByText } = render(<NewDmDialog />)
    await findByRole('dialog')
    ;(container.querySelector('.sh-newdm-row') as HTMLElement).click()
    await new Promise((r) => setTimeout(r, 0))
    ;(getByText(/^Start$/) as HTMLElement).click()
    await new Promise((r) => setTimeout(r, 0))
    expect(post).toHaveBeenCalledWith(
      '/api/conversations/dm',
      { user_id: 'u-bro' },
    )
  })

  it('2-pick local → POST /api/conversations/group with members[] usernames', async () => {
    const post = vi.fn(async () => ({ id: 'cgroup', type: 'group_dm' }))
    const get = vi.fn(async () =>
      _friendsPayload({
        meId: 'u-pascal',
        localMembers: [
          { user_id: 'u-maria', username: 'maria', display_name: 'Maria' },
          { user_id: 'u-lina', username: 'lina', display_name: 'Lina' },
        ],
      }),
    )
    vi.doMock('@/api', () => ({ api: { get, post } }))
    vi.doMock('@/store/auth', () => ({
      currentUser: { value: { user_id: 'u-pascal', username: 'pascal' } },
    }))
    const { NewDmDialog, openNewDm } = await import('./NewDmDialog')
    openNewDm()
    await new Promise((r) => setTimeout(r, 0))
    const { container, findByRole, getByText } = render(<NewDmDialog />)
    await findByRole('dialog')
    const rows = container.querySelectorAll('.sh-newdm-row')
    ;(rows[0] as HTMLElement).click()
    ;(rows[1] as HTMLElement).click()
    await new Promise((r) => setTimeout(r, 0))
    ;(getByText(/^Start group/) as HTMLElement).click()
    await new Promise((r) => setTimeout(r, 0))
    expect(post).toHaveBeenCalledWith(
      '/api/conversations/group',
      { members: ['maria', 'lina'] },
    )
  })

  it('a group can mix local people and people from other households', async () => {
    const post = vi.fn(async () => ({ id: 'cgroup', type: 'group_dm' }))
    const get = vi.fn(async () =>
      _friendsPayload({
        meId: 'u-pascal',
        localMembers: [
          { user_id: 'u-maria', username: 'maria', display_name: 'Maria' },
        ],
        remoteHouseholds: [
          {
            instance_id: 'z7k63zfi',
            display_name: "Brother's house",
            supports_group_dm: true,
            members: [
              { user_id: 'u-bro', remote_username: 'bob', display_name: 'Bob' },
            ],
          },
        ],
      }),
    )
    vi.doMock('@/api', () => ({ api: { get, post } }))
    vi.doMock('@/store/auth', () => ({
      currentUser: { value: { user_id: 'u-pascal', username: 'pascal' } },
    }))
    const { NewDmDialog, openNewDm } = await import('./NewDmDialog')
    openNewDm()
    await new Promise((r) => setTimeout(r, 0))
    const { container, findByRole, getByText } = render(<NewDmDialog />)
    await findByRole('dialog')
    const rows = container.querySelectorAll('.sh-newdm-row')
    ;(rows[1] as HTMLElement).click()
    ;(rows[0] as HTMLElement).click()
    await new Promise((r) => setTimeout(r, 0))
    const maria = container.querySelectorAll('.sh-newdm-row')[0] as HTMLButtonElement
    expect(maria.disabled).toBe(false)
    ;(getByText(/^Start group/) as HTMLElement).click()
    await new Promise((r) => setTimeout(r, 0))
    expect(post).toHaveBeenCalledWith('/api/conversations/group', {
      members: ['maria'],
      member_user_ids: ['u-bro'],
    })
  })

  it('a household too old for group chats is 1:1 only, and the row says why', async () => {
    const post = vi.fn()
    const get = vi.fn(async () =>
      _friendsPayload({
        meId: 'u-pascal',
        localMembers: [
          { user_id: 'u-maria', username: 'maria', display_name: 'Maria' },
        ],
        remoteHouseholds: [
          {
            instance_id: 'old-inst',
            display_name: "Gran's house",
            members: [
              { user_id: 'u-gran', remote_username: 'gran', display_name: 'Gran' },
            ],
          },
        ],
      }),
    )
    vi.doMock('@/api', () => ({ api: { get, post } }))
    vi.doMock('@/store/auth', () => ({
      currentUser: { value: { user_id: 'u-pascal', username: 'pascal' } },
    }))
    const { NewDmDialog, openNewDm } = await import('./NewDmDialog')
    openNewDm()
    await new Promise((r) => setTimeout(r, 0))
    const { container, findByRole } = render(<NewDmDialog />)
    await findByRole('dialog')
    // Nothing picked yet: Gran is fine for a 1:1.
    let gran = container.querySelectorAll('.sh-newdm-row')[1] as HTMLButtonElement
    expect(gran.disabled).toBe(false)
    ;(container.querySelectorAll('.sh-newdm-row')[0] as HTMLElement).click()
    await new Promise((r) => setTimeout(r, 0))
    gran = container.querySelectorAll('.sh-newdm-row')[1] as HTMLButtonElement
    expect(gran.disabled).toBe(true)
    expect(gran.classList.contains('sh-newdm-row--disabled')).toBe(true)
    expect(gran.textContent).toContain("Gran's house needs a Social Home update")
  })
})
