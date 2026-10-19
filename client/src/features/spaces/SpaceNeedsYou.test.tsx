import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

const { apiPost } = vi.hoisted(() => ({ apiPost: vi.fn() }))
vi.mock('@/api', () => ({ api: { post: apiPost } }))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))

import { SpaceNeedsYou, proposalMeta } from './SpaceNeedsYou'
import type { AttentionProposal, SpaceAttention } from '@/store/spaceAttention'
import type { SpaceCompat } from '@/components/SpaceVersionNotice'

function proposal(over: Partial<AttentionProposal> = {}): AttentionProposal {
  return {
    id: 'p1', action: 'set_public_tier', params: { space_type: 'public' }, status: 'pending',
    approvals: 1, needed: 2, required: 2, total_admins: 2, proposed_by_user: 'u-me',
    voted_by_me: false, ...over,
  }
}

function attention(over: Partial<SpaceAttention> = {}): SpaceAttention {
  return { spaceId: 's1', review: 0, reports: 0, joinRequests: 0, proposals: [], ...over }
}

const compat: SpaceCompat = {
  ours: 60, min_member_proto_version: 50,
  lagging_features: ['Faster photo and video transfer'],
  behind_members: [{ instance_id: 'h', display_name: "Brother's house", proto_version: 50,
                     lacking_features: ['Faster photo and video transfer'] }],
}

function show(a: SpaceAttention | null, { canVote = true, isOwner = true, c = null as SpaceCompat | null } = {}) {
  const onChanged = vi.fn()
  const r = render(
    <SpaceNeedsYou spaceId="s1" attention={a} compat={c} canVote={canVote} isOwner={isOwner}
                   onChanged={onChanged} />,
  )
  return { ...r, onChanged }
}

const rows = (c: Element) => [...c.querySelectorAll('.sh-needs-you__row')]

beforeEach(() => { apiPost.mockReset() })

describe('SpaceNeedsYou', () => {
  it('renders nothing when nothing waits', () => {
    expect(show(null).container.innerHTML).toBe('')
    expect(show(attention()).container.innerHTML).toBe('')
  })

  it('one card: a heading with the filled count and one row per kind, in order', () => {
    const { container } = show(attention({
      review: 2, reports: 1, joinRequests: 3, proposals: [proposal()],
    }), { c: compat })
    const heading = container.querySelector('h2.sh-needs-you__heading') as HTMLElement
    expect(heading.textContent).toContain('Needs you')
    // Same total as More: 1 proposal awaiting my vote + 2 + 1 + 3 (the
    // version row waits for nobody's decision).
    expect(heading.querySelector('.sh-space-badge--needs')?.textContent).toBe('7')
    expect(rows(container).map((r) => r.getAttribute('data-row')))
      .toEqual(['proposal', 'review', 'reports', 'join', 'version'])
    expect(container.textContent).toContain('2 waiting for review')
    expect(container.textContent).toContain('3 join requests')
    expect(container.textContent).toContain('1 report')
  })

  it('review, reports and join requests lead to Moderation', () => {
    const { container } = show(attention({ review: 1, joinRequests: 1, reports: 1 }))
    for (const row of rows(container)) {
      expect(row.getAttribute('href')).toBe('/spaces/s1/moderation')
    }
  })

  it('a proposal row says the proposal and opens the vote dialog', async () => {
    apiPost.mockResolvedValue({})
    const { container, getByText, onChanged } = show(attention({ proposals: [proposal()] }), { isOwner: false })
    const row = rows(container)[0] as HTMLButtonElement
    expect(row.textContent).toContain('Proposal to change the publication tier to “public”')
    expect(row.textContent).toContain('1 of 2 approvals · Needs your vote')
    fireEvent.click(row)
    fireEvent.click(getByText('Approve'))
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith('/api/spaces/s1/proposals/p1/vote', { approve: true }))
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
  })

  it('never asks the proposer to vote again (bug 9)', () => {
    const { container } = show(attention({ proposals: [proposal({ voted_by_me: true })] }))
    expect(container.textContent).toContain('1 of 2 approvals · You voted')
    fireEvent.click(rows(container)[0])
    const dialog = document.querySelector('[role="dialog"]') as HTMLElement
    expect(dialog.querySelectorAll('.sh-proposal-banner__actions button')).toHaveLength(0)
    expect(dialog.textContent).toContain('You already voted')
  })

  it('the version row opens which features lag and whom the space waits on', () => {
    const { container } = show(attention(), { c: compat })
    expect(container.textContent).toContain('1 household needs an update')
    fireEvent.click(rows(container)[0])
    const dialog = document.querySelector('[role="dialog"]') as HTMLElement
    expect(dialog.textContent).toContain("Brother's house (v50)")
  })
})

describe('proposalMeta', () => {
  it('a co-admin waits on an owner-only proposal', () => {
    expect(proposalMeta(proposal({ owner_only: true }), true, false)).toBe('Waiting for the space owner to decide.')
  })

  it('a viewer who may not vote sees the tally and that admins decide', () => {
    expect(proposalMeta(proposal(), false, false)).toBe('1 of 2 approvals · Waiting for the admins')
  })
})

describe('the heading count', () => {
  it('a proposal I already voted on is listed but not counted', () => {
    const { container } = show(attention({ review: 1, proposals: [proposal({ voted_by_me: true })] }))
    expect(rows(container).map((r) => r.getAttribute('data-row'))).toEqual(['proposal', 'review'])
    expect(container.querySelector('.sh-needs-you__heading .sh-space-badge--needs')?.textContent).toBe('1')
  })

  it('only my own voted proposal: the card shows it, without a count', () => {
    const { container } = show(attention({ proposals: [proposal({ voted_by_me: true })] }))
    expect(rows(container)).toHaveLength(1)
    expect(container.querySelector('.sh-needs-you__heading .sh-space-badge--needs')).toBeNull()
  })
})
