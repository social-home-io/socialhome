import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor, cleanup } from '@testing-library/preact'

const apiPatch = vi.fn()
vi.mock('@/api', async (orig) => ({
  ...(await orig<object>()),
  api: {
    get: vi.fn().mockResolvedValue([]),
    post: vi.fn().mockResolvedValue({}),
    patch: (...a: unknown[]) => apiPatch(...a),
    delete: vi.fn().mockResolvedValue(undefined),
  },
}))

import { ApiError } from '@/api'
import { MemberActionSheet, openMemberActions } from './MemberActionSheet'

beforeEach(() => {
  cleanup()
  apiPatch.mockReset()
  apiPatch.mockResolvedValue({})
})

function roleButtons(container: Element): string[] {
  return Array.from(container.querySelectorAll('[data-role-option]'))
    .map(b => (b as HTMLElement).dataset.roleOption as string)
}

describe('MemberActionSheet role picker', () => {
  it('the owner sees admin + moderator for a member', () => {
    openMemberActions('sp-1', 'u-bob', 'member', null, 'owner')
    const r = render(<MemberActionSheet onUpdate={() => {}} />)
    expect(roleButtons(r.baseElement)).toEqual(['admin', 'moderator'])
  })

  it('the owner sees moderator + member for an admin', () => {
    openMemberActions('sp-1', 'u-bob', 'admin', null, 'owner')
    const r = render(<MemberActionSheet onUpdate={() => {}} />)
    expect(roleButtons(r.baseElement)).toEqual(['moderator', 'member'])
  })

  it('an admin sees only moderator for a member, member for a moderator', () => {
    openMemberActions('sp-1', 'u-bob', 'member', null, 'admin')
    const r = render(<MemberActionSheet onUpdate={() => {}} />)
    expect(roleButtons(r.baseElement)).toEqual(['moderator'])
    cleanup()
    openMemberActions('sp-1', 'u-bob', 'moderator', null, 'admin')
    const r2 = render(<MemberActionSheet onUpdate={() => {}} />)
    expect(roleButtons(r2.baseElement)).toEqual(['member'])
  })

  it('an admin sees no role options on another admin', () => {
    openMemberActions('sp-1', 'u-bob', 'admin', null, 'admin')
    const r = render(<MemberActionSheet onUpdate={() => {}} />)
    expect(roleButtons(r.baseElement)).toEqual([])
  })

  it('a remote member is changed over the remote-member route', async () => {
    openMemberActions('sp-1', 'u-bob', 'member', 'peer-x', 'owner')
    const onUpdate = vi.fn()
    const r = render(<MemberActionSheet onUpdate={onUpdate} />)
    fireEvent.click(r.baseElement.querySelector('[data-role-option="moderator"]')!)
    await waitFor(() => expect(onUpdate).toHaveBeenCalled())
    expect(apiPatch).toHaveBeenCalledWith(
      '/api/spaces/sp-1/remote-members/peer-x/u-bob', { role: 'moderator' },
    )
  })

  it('a behind household is explained in translated copy, keyed on the code', async () => {
    apiPatch.mockRejectedValue(new ApiError(403, '/x', {
      code: 'HOUSEHOLD_UPGRADE_REQUIRED',
      detail: "this member's household must upgrade before they can be a moderator",
    }))
    openMemberActions('sp-1', 'u-bob', 'member', 'peer-x', 'owner')
    const onUpdate = vi.fn()
    const r = render(<MemberActionSheet onUpdate={onUpdate} />)
    fireEvent.click(r.baseElement.querySelector('[data-role-option="moderator"]')!)
    const alert = await r.findByRole('alert')
    expect(alert.textContent).toBe(
      "Their household needs to update Social Home before they can be a moderator.",
    )
    expect(onUpdate).not.toHaveBeenCalled()
    expect(roleButtons(r.baseElement)).toEqual(['admin', 'moderator'])
  })

  it('any other refusal shows translated copy, never the raw server string', async () => {
    apiPatch.mockRejectedValue(new ApiError(403, '/x', {
      code: 'FORBIDDEN', detail: 'a space admin cannot change a member to admin',
    }))
    openMemberActions('sp-1', 'u-bob', 'member', null, 'owner')
    const r = render(<MemberActionSheet onUpdate={() => {}} />)
    fireEvent.click(r.baseElement.querySelector('[data-role-option="admin"]')!)
    const alert = await r.findByRole('alert')
    expect(alert.textContent).toBe("You can't give this role.")
  })
})
