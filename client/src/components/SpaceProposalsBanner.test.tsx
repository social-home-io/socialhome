import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor, fireEvent } from '@testing-library/preact'

const { apiGet, apiPost } = vi.hoisted(() => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
}))
let wsHandler: ((e: unknown) => void) | null = null
vi.mock('@/api', () => ({ api: { get: apiGet, post: apiPost } }))
vi.mock('@/ws', () => ({
  ws: {
    on: (_t: string, h: (e: unknown) => void) => {
      wsHandler = h
      return () => {
        wsHandler = null
      }
    },
  },
}))
vi.mock('./Toast', () => ({ showToast: vi.fn() }))

import { SpaceProposalsBanner } from './SpaceProposalsBanner'
import { currentUser } from '@/store/auth'
import type { User } from '@/types'

function proposal(over: Record<string, unknown> = {}) {
  return {
    id: 'p1',
    action: 'dissolve',
    status: 'pending',
    approvals: 1,
    needed: 2,
    total_admins: 2,
    proposed_by_user: 'alice',
    ...over,
  }
}

describe('SpaceProposalsBanner', () => {
  beforeEach(() => {
    apiGet.mockReset()
    apiPost.mockReset()
    wsHandler = null
  })

  it('renders a pending dissolve proposal with the tally', async () => {
    apiGet.mockResolvedValue({ proposals: [proposal()] })
    const { container } = render(
      <SpaceProposalsBanner spaceId="s1" canVote={true} isOwner={false} />,
    )
    await waitFor(() =>
      expect(container.textContent).toContain('Admin approval needed'),
    )
    expect(container.textContent).toContain('permanently delete this space')
    expect(container.textContent).toContain('1 of 2')
  })

  it('shows Approve/Reject only when the viewer can vote', async () => {
    apiGet.mockResolvedValue({ proposals: [proposal()] })
    const { container, rerender } = render(
      <SpaceProposalsBanner spaceId="s1" canVote={false} isOwner={false} />,
    )
    await waitFor(() =>
      expect(container.textContent).toContain('Waiting for the space admins'),
    )
    expect(container.querySelector('button')).toBeNull()

    apiGet.mockResolvedValue({ proposals: [proposal()] })
    rerender(<SpaceProposalsBanner spaceId="s1" canVote={true} isOwner={false} />)
    await waitFor(() => expect(container.querySelector('button')).not.toBeNull())
  })

  it('POSTs a vote on Approve', async () => {
    apiGet.mockResolvedValue({ proposals: [proposal()] })
    apiPost.mockResolvedValue({})
    const { getByText } = render(
      <SpaceProposalsBanner spaceId="s1" canVote={true} isOwner={false} />,
    )
    await waitFor(() => getByText('Approve'))
    fireEvent.click(getByText('Approve'))
    await waitFor(() =>
      expect(apiPost).toHaveBeenCalledWith(
        '/api/spaces/s1/proposals/p1/vote',
        { approve: true },
      ),
    )
  })

  it('drops the banner when a WS frame resolves the proposal', async () => {
    apiGet.mockResolvedValue({ proposals: [proposal()] })
    const { container } = render(
      <SpaceProposalsBanner spaceId="s1" canVote={true} isOwner={false} />,
    )
    await waitFor(() =>
      expect(container.textContent).toContain('Admin approval needed'),
    )
    wsHandler?.({ space_id: 's1', proposal: proposal({ status: 'executed' }) })
    await waitFor(() =>
      expect(container.textContent).not.toContain('Admin approval needed'),
    )
  })

  function ownerActionProposal(over: Record<string, unknown> = {}) {
    return proposal({
      id: 'pa1',
      action: 'remote_admin_action',
      owner_only: true,
      fwd_action: 'ban',
      fwd_params: { user_id: 'u9' },
      total_admins: 1,
      needed: 1,
      approvals: 0,
      proposed_by_user: 'bob',
      ...over,
    })
  }

  it('renders a remote_admin_action proposal with copy + requester', async () => {
    apiGet.mockResolvedValue({ proposals: [ownerActionProposal()] })
    const { container } = render(
      <SpaceProposalsBanner spaceId="s1" canVote={true} isOwner={true} />,
    )
    await waitFor(() =>
      expect(container.textContent).toContain('Owner approval needed'),
    )
    expect(container.textContent).toContain('remove a member')
    expect(container.textContent).toContain('Requested by bob')
    // No fwd_target_label → no trailing "(name)".
    expect(container.textContent).not.toContain('remove a member (')
  })

  it('appends the target name when fwd_target_label is present', async () => {
    apiGet.mockResolvedValue({
      proposals: [ownerActionProposal({ fwd_target_label: 'Alice' })],
    })
    const { container } = render(
      <SpaceProposalsBanner spaceId="s1" canVote={true} isOwner={true} />,
    )
    await waitFor(() =>
      expect(container.textContent).toContain('Owner approval needed'),
    )
    expect(container.textContent).toContain('remove a member (Alice)')
  })

  it('shows vote buttons on an owner_only proposal to the owner', async () => {
    apiGet.mockResolvedValue({ proposals: [ownerActionProposal()] })
    const { container, getByText } = render(
      <SpaceProposalsBanner spaceId="s1" canVote={true} isOwner={true} />,
    )
    await waitFor(() => getByText('Approve'))
    expect(getByText('Reject')).not.toBeNull()
    expect(container.textContent).toContain('remove a member')
  })

  it('hides vote buttons on an owner_only proposal from a non-owner admin', async () => {
    apiGet.mockResolvedValue({ proposals: [ownerActionProposal()] })
    const { container } = render(
      <SpaceProposalsBanner spaceId="s1" canVote={true} isOwner={false} />,
    )
    await waitFor(() =>
      expect(container.textContent).toContain('Owner approval needed'),
    )
    // Tally / description still visible…
    expect(container.textContent).toContain('remove a member')
    expect(container.textContent).toContain('Waiting for the space owner')
    // …but no Approve / Reject for a co-admin.
    expect(container.querySelector('button')).toBeNull()
  })

  it('still shows buttons for a non-owner-only proposal to a non-owner admin', async () => {
    apiGet.mockResolvedValue({ proposals: [proposal()] })
    const { container } = render(
      <SpaceProposalsBanner spaceId="s1" canVote={true} isOwner={false} />,
    )
    await waitFor(() => expect(container.querySelector('button')).not.toBeNull())
    expect(container.textContent).toContain('Admin approval needed')
  })

  it('POSTs a vote on Approve for a remote_admin_action', async () => {
    apiGet.mockResolvedValue({ proposals: [ownerActionProposal()] })
    apiPost.mockResolvedValue({})
    const { getByText } = render(
      <SpaceProposalsBanner spaceId="s1" canVote={true} isOwner={true} />,
    )
    await waitFor(() => getByText('Approve'))
    fireEvent.click(getByText('Approve'))
    await waitFor(() =>
      expect(apiPost).toHaveBeenCalledWith(
        '/api/spaces/s1/proposals/pa1/vote',
        { approve: true },
      ),
    )
  })
})

describe('SpaceProposalsBanner — protected admin', () => {
  beforeEach(() => {
    apiGet.mockReset()
    apiPost.mockReset()
    currentUser.value = {
      user_id: 'u-kid', username: 'kid', display_name: 'Kid', is_admin: false,
      picture_url: null, picture_hash: null, bio: null, is_new_member: false,
      protected: true, restrictions: ['public_spaces'],
    } as User
  })

  it('can reject but not approve making the space public', async () => {
    apiGet.mockResolvedValue({
      proposals: [proposal({ action: 'set_public_tier', params: { space_type: 'global' } })],
    })
    const { container, findByText } = render(
      <SpaceProposalsBanner spaceId="s1" canVote={true} isOwner={true} />,
    )
    await findByText('Reject')
    const labels = [...container.querySelectorAll('button')].map(b => b.textContent)
    expect(labels).not.toContain('Approve')
    expect(container.textContent).toContain("you can't approve making this space public")
    currentUser.value = null
  })

  it('still approves proposals that are not a publication', async () => {
    apiGet.mockResolvedValue({
      proposals: [proposal({ action: 'set_public_tier', params: { space_type: 'private' } })],
    })
    const { findByText } = render(
      <SpaceProposalsBanner spaceId="s1" canVote={true} isOwner={true} />,
    )
    await findByText('Approve')
    currentUser.value = null
  })
})
