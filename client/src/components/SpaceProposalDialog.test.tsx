import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor, fireEvent } from '@testing-library/preact'

const { apiPost } = vi.hoisted(() => ({ apiPost: vi.fn() }))
vi.mock('@/api', () => ({ api: { post: apiPost } }))
vi.mock('./Toast', () => ({ showToast: vi.fn() }))

import { SpaceProposalDialog, canVoteOn, describeProposal, type SpaceProposal } from './SpaceProposalDialog'
import { currentUser } from '@/store/auth'
import type { User } from '@/types'

function proposal(over: Record<string, unknown> = {}): SpaceProposal & { voted_by_me?: boolean } {
  return {
    id: 'p1',
    action: 'dissolve',
    status: 'pending',
    approvals: 1,
    needed: 2,
    total_admins: 2,
    proposed_by_user: 'alice',
    ...over,
  } as SpaceProposal & { voted_by_me?: boolean }
}

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

function open(p: ReturnType<typeof proposal>, { canVote = true, isOwner = false } = {}) {
  const onClose = vi.fn()
  const onVoted = vi.fn()
  const r = render(
    <SpaceProposalDialog spaceId="s1" proposal={p} canVote={canVote} isOwner={isOwner}
                         onClose={onClose} onVoted={onVoted} />,
  )
  return { ...r, onClose, onVoted, text: () => document.body.textContent ?? '' }
}

const buttons = () => [...document.querySelectorAll('.sh-proposal-banner__actions button')]

describe('SpaceProposalDialog', () => {
  beforeEach(() => {
    apiPost.mockReset()
  })

  it('renders a pending dissolve proposal with the tally', () => {
    const { text } = open(proposal())
    expect(text()).toContain('Admin approval needed')
    expect(text()).toContain('permanently delete this space')
    expect(text()).toContain('1 of 2')
  })

  it('shows Approve/Reject only when the viewer can vote', () => {
    const { text, unmount } = open(proposal(), { canVote: false })
    expect(text()).toContain('Waiting for the space admins')
    expect(buttons()).toHaveLength(0)
    unmount()
    open(proposal())
    expect(buttons().map((b) => b.textContent)).toEqual(['Reject', 'Approve'])
  })

  it('never asks the proposer or a voter to vote again (bug 9)', () => {
    const { text } = open(proposal({ voted_by_me: true }))
    expect(buttons()).toHaveLength(0)
    expect(text()).toContain('You already voted')
  })

  it('POSTs a vote on Approve, then reports it and closes', async () => {
    apiPost.mockResolvedValue({})
    const { getByText, onVoted, onClose } = open(proposal())
    fireEvent.click(getByText('Approve'))
    await waitFor(() =>
      expect(apiPost).toHaveBeenCalledWith('/api/spaces/s1/proposals/p1/vote', { approve: true }))
    await waitFor(() => expect(onVoted).toHaveBeenCalled())
    expect(onClose).toHaveBeenCalled()
  })

  it('a failed vote keeps the dialog open', async () => {
    apiPost.mockRejectedValue(new Error('nope'))
    const { getByText, onVoted, onClose } = open(proposal())
    fireEvent.click(getByText('Reject'))
    await waitFor(() => expect(apiPost).toHaveBeenCalled())
    expect(onVoted).not.toHaveBeenCalled()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('renders a remote_admin_action proposal with copy + requester', () => {
    const { text } = open(ownerActionProposal(), { isOwner: true })
    expect(text()).toContain('Owner approval needed')
    expect(text()).toContain('remove a member')
    // No fwd_target_label → no trailing "(name)".
    expect(text()).not.toContain('remove a member (')
  })

  it('names the requester by display name, never the raw user id', () => {
    const { text } = open(
      ownerActionProposal({ proposed_by_user: 'u-bob', proposed_by_label: 'Bob' }), { isOwner: true })
    expect(text()).toContain('Requested by Bob')
    expect(text()).not.toContain('u-bob')
  })

  it('an unresolved requester reads as a member of another household', () => {
    const { text } = open(
      ownerActionProposal({ proposed_by_user: 'u-bob', proposed_by_label: null }), { isOwner: true })
    expect(text()).toContain('Requested by a member of another household')
    expect(text()).not.toContain('u-bob')
  })

  it('the sentence is translated', async () => {
    const i18n = await import('@/i18n/i18n')
    const de = (await import('@/i18n/locales/de.json')).default as Record<string, string>
    await i18n.setLocale('de')
    try {
      const { text } = open(ownerActionProposal({ proposed_by_label: null }), { isOwner: true })
      const action = de['space.proposal.fwd.ban']
      expect(text()).toContain(de['space.proposal.owner_body'].replace('{action}', action))
      expect(text()).toContain(de['space.proposal.owner_title'])
      expect(text()).toContain(
        de['space.proposal.requested_by'].replace('{name}', de['space.proposal.requester_unknown']))
      expect(text()).not.toContain('needs the space owner')
    } finally {
      await i18n.setLocale('en')
    }
  })

  it('appends the target name when fwd_target_label is present', () => {
    const { text } = open(ownerActionProposal({ fwd_target_label: 'Alice' }), { isOwner: true })
    expect(text()).toContain('remove a member (Alice)')
  })

  it('shows vote buttons on an owner_only proposal to the owner only', () => {
    const { unmount, text } = open(ownerActionProposal(), { isOwner: true })
    expect(buttons().map((b) => b.textContent)).toEqual(['Reject', 'Approve'])
    unmount()
    open(ownerActionProposal(), { isOwner: false })
    expect(buttons()).toHaveLength(0)
    expect(text()).toContain('Waiting for the space owner')
  })

  it('POSTs a vote on Approve for a remote_admin_action', async () => {
    apiPost.mockResolvedValue({})
    const { getByText } = open(ownerActionProposal(), { isOwner: true })
    fireEvent.click(getByText('Approve'))
    await waitFor(() =>
      expect(apiPost).toHaveBeenCalledWith('/api/spaces/s1/proposals/pa1/vote', { approve: true }))
  })
})

describe('canVoteOn', () => {
  it('combines the role, owner-only and already-voted', () => {
    expect(canVoteOn(proposal(), true, false)).toBe(true)
    expect(canVoteOn(proposal(), false, true)).toBe(false)
    expect(canVoteOn(proposal({ voted_by_me: true }), true, true)).toBe(false)
    expect(canVoteOn(ownerActionProposal(), true, false)).toBe(false)
    expect(canVoteOn(ownerActionProposal(), true, true)).toBe(true)
  })
})

describe('describeProposal', () => {
  it('a forwarded role change says who and which role', () => {
    expect(describeProposal(proposal({
      action: 'remote_admin_action', owner_only: true,
      fwd_action: 'set_member_role',
      fwd_params: { instance_id: 'house-c', user_id: 'u-carol', from_role: 'member', role: 'moderator' },
      fwd_target_label: 'Carol',
    }))).toBe('make Carol a moderator')
  })

  it('a role change with no resolvable name still names the role', () => {
    expect(describeProposal(proposal({
      action: 'remote_admin_action', owner_only: true,
      fwd_action: 'set_member_role',
      fwd_params: { role: 'member' },
    }))).toBe('make someone a member')
  })
})

describe('SpaceProposalDialog — protected admin', () => {
  beforeEach(() => {
    currentUser.value = {
      user_id: 'u-kid', username: 'kid', display_name: 'Kid', is_admin: false,
      picture_url: null, picture_hash: null, bio: null, is_new_member: false,
      protected: true, restrictions: ['public_spaces'],
    } as User
  })

  it('can reject but not approve making the space public', () => {
    const { text } = open(proposal({ action: 'set_public_tier', params: { space_type: 'global' } }),
                          { isOwner: true })
    expect(buttons().map((b) => b.textContent)).toEqual(['Reject'])
    expect(text()).toContain("you can't approve making this space public")
    currentUser.value = null
  })

  it('still approves proposals that are not a publication', () => {
    open(proposal({ action: 'set_public_tier', params: { space_type: 'private' } }), { isOwner: true })
    expect(buttons().map((b) => b.textContent)).toContain('Approve')
    currentUser.value = null
  })
})
