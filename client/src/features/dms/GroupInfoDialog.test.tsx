import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

const get = vi.fn()
const post = vi.fn()
const patch = vi.fn()
const del = vi.fn()

vi.mock('@/api', () => ({
  api: {
    get: (...a: unknown[]) => get(...a),
    post: (...a: unknown[]) => post(...a),
    patch: (...a: unknown[]) => patch(...a),
    delete: (...a: unknown[]) => del(...a),
  },
}))
vi.mock('@/store/auth', () => ({ currentUser: { value: { user_id: 'u-me' } } }))

const MEMBERS = [
  { user_id: 'u-me', username: 'me', display_name: 'Me', picture_url: null, is_self: true, instance_id: null },
  { user_id: 'u-ana', username: 'ana', display_name: 'Ana', picture_url: null, is_self: false, instance_id: null },
  {
    user_id: 'u-bro', username: 'bob', display_name: 'Bob', picture_url: null, is_self: false,
    instance_id: 'inst-b', household_name: "Brother's house",
  },
  {
    user_id: 'u-cid', username: 'cid', display_name: 'Cid', picture_url: null, is_self: false,
    instance_id: 'inst-c', household_name: null,
  },
]

const FRIENDS = {
  instance: {
    instance_id: 'me-inst',
    display_name: 'Home',
    members: [
      { user_id: 'u-me', username: 'me', display_name: 'Me', picture_url: null },
      { user_id: 'u-ana', username: 'ana', display_name: 'Ana', picture_url: null },
      { user_id: 'u-lu', username: 'lu', display_name: 'Lu', picture_url: null },
    ],
  },
  households: [
    {
      instance_id: 'inst-g', display_name: "Gran's house", supports_group_dm: false,
      members: [{ user_id: 'u-gran', remote_username: 'gran', display_name: 'Gran', picture_url: null }],
    },
    {
      instance_id: 'inst-b', display_name: "Brother's house", supports_group_dm: true,
      members: [
        { user_id: 'u-bro', remote_username: 'bob', display_name: 'Bob', picture_url: null },
        { user_id: 'u-kim', remote_username: 'kim', display_name: 'Kim', picture_url: null },
      ],
    },
  ],
}

async function mount(props: { managedHere: boolean; name?: string | null }) {
  const { GroupInfoDialog } = await import('./GroupInfoDialog')
  const onChanged = vi.fn()
  const onLeft = vi.fn()
  const view = render(
    <GroupInfoDialog
      open
      convId="g1"
      name={props.name ?? 'Crew'}
      managedHere={props.managedHere}
      members={MEMBERS}
      onClose={() => {}}
      onChanged={onChanged}
      onLeft={onLeft}
    />,
  )
  return { ...view, onChanged, onLeft }
}

beforeEach(() => {
  get.mockReset(); post.mockReset(); patch.mockReset(); del.mockReset()
  post.mockResolvedValue({ ok: true })
  patch.mockResolvedValue({ ok: true })
  del.mockResolvedValue({ ok: true })
  get.mockResolvedValue(FRIENDS)
})

describe('GroupInfoDialog', () => {
  it('lists every member with their household', async () => {
    const { container } = await mount({ managedHere: true })
    const text = container.ownerDocument.body.textContent ?? ''
    expect(text).toContain('4 people')
    expect(text).toContain('Me (you)')
    expect(text).toContain("at Brother's house")
    expect(text).toContain('at another household')
    expect(text).toContain('Home')
  })

  it('on another household’s group: no rename / add / remove, only leave', async () => {
    const { queryByText, queryByLabelText, getByText } = await mount({ managedHere: false })
    expect(getByText(/Only people in the household that started this group/)).toBeTruthy()
    expect(queryByText('Add people')).toBeNull()
    expect(queryByLabelText(/Remove Ana/)).toBeNull()
    expect(getByText('Leave group')).toBeTruthy()
  })

  it('adds a local and a remote person; a too-old household is greyed with the reason', async () => {
    const { getByText, findByText, onChanged } = await mount({ managedHere: true })
    fireEvent.click(getByText('Add people'))
    await findByText('Kim')
    // People already in the group are not offered again.
    const body = document.body.textContent ?? ''
    expect(body.match(/Ana/g)?.length).toBe(1)
    const gran = (await findByText('Gran')).closest('button') as HTMLButtonElement
    expect(gran.disabled).toBe(true)
    expect(gran.textContent).toContain("Gran's house needs a Social Home update")
    fireEvent.click((await findByText('Lu')).closest('button') as HTMLElement)
    fireEvent.click((await findByText('Kim')).closest('button') as HTMLElement)
    fireEvent.click(getByText('Add (2)'))
    await waitFor(() => expect(post).toHaveBeenCalled())
    expect(post).toHaveBeenCalledWith('/api/conversations/g1/members', {
      usernames: ['lu'],
      user_ids: ['u-kim'],
    })
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
  })

  it('shows a way forward when people can’t be loaded', async () => {
    get.mockRejectedValue(new Error('offline'))
    const { getByText, findByRole } = await mount({ managedHere: true })
    fireEvent.click(getByText('Add people'))
    expect((await findByRole('alert')).textContent).toContain('try again')
  })

  it('removes a member only after confirming', async () => {
    const { getByLabelText, findByText } = await mount({ managedHere: true })
    fireEvent.click(getByLabelText('Remove Bob from the group'))
    expect(del).not.toHaveBeenCalled()
    await findByText('Remove from group?')
    const confirm = [...document.querySelectorAll('button')].find(
      b => b.textContent === 'Remove' && !b.getAttribute('aria-label'),
    ) as HTMLButtonElement
    fireEvent.click(confirm)
    await waitFor(() => expect(del).toHaveBeenCalledWith('/api/conversations/g1/members/u-bro'))
  })

  it('renames the group', async () => {
    const { container, getByText } = await mount({ managedHere: true })
    const input = container.ownerDocument.querySelector('.sh-groupinfo-name input') as HTMLInputElement
    fireEvent.input(input, { target: { value: '  Lunch  ' } })
    fireEvent.click(getByText('Save'))
    await waitFor(() => expect(patch).toHaveBeenCalledWith('/api/conversations/g1', { name: 'Lunch' }))
  })

  it('leaving asks first, then leaves', async () => {
    const { getByText, findByText, onLeft } = await mount({ managedHere: false })
    fireEvent.click(getByText('Leave group'))
    await findByText('Leave this group?')
    fireEvent.click(getByText('Leave'))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/api/conversations/g1/leave'))
    await waitFor(() => expect(onLeft).toHaveBeenCalled())
  })
})
