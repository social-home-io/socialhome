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

const showToast = vi.fn()
vi.mock('./Toast', () => ({ showToast: (...a: unknown[]) => showToast(...a) }))

import { ApiError } from '@/api'
import { MemberActionSheet, openMemberActions } from './MemberActionSheet'

beforeEach(() => {
  cleanup()
  apiPatch.mockReset()
  apiPatch.mockResolvedValue({})
  showToast.mockReset()
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

  it('a stub admin can pick a role; a forwarded change says it went to the host', async () => {
    // v_47: on a member household the PATCH answers 202 {forwarded: true}.
    apiPatch.mockResolvedValue({ user_id: 'u-bob', role: 'moderator', forwarded: true })
    openMemberActions('sp-1', 'u-bob', 'member', 'peer-c', 'admin')
    const onUpdate = vi.fn()
    const r = render(<MemberActionSheet onUpdate={onUpdate} />)
    expect(roleButtons(r.baseElement)).toEqual(['moderator'])
    fireEvent.click(r.baseElement.querySelector('[data-role-option="moderator"]')!)
    await waitFor(() => expect(onUpdate).toHaveBeenCalled())
    expect(showToast).toHaveBeenCalledWith(
      "Sent to the space's host. The new role shows once the host applies it.", 'info',
    )
    expect(showToast).not.toHaveBeenCalledWith(expect.stringContaining('Role changed'), 'success')
  })

  it('a change applied here still says "Role changed"', async () => {
    apiPatch.mockResolvedValue({ user_id: 'u-bob', role: 'moderator' })
    openMemberActions('sp-1', 'u-bob', 'member', null, 'owner')
    const r = render(<MemberActionSheet onUpdate={() => {}} />)
    fireEvent.click(r.baseElement.querySelector('[data-role-option="moderator"]')!)
    await waitFor(() => expect(showToast).toHaveBeenCalled())
    expect(showToast.mock.calls[0][1]).toBe('success')
  })

  it('a host too old for forwarded role changes is explained in the sheet', async () => {
    apiPatch.mockRejectedValue(new ApiError(409, '/x', {
      code: 'HOST_TOO_OLD', detail: 'raw server text',
    }))
    openMemberActions('sp-1', 'u-bob', 'member', null, 'admin')
    const r = render(<MemberActionSheet onUpdate={() => {}} />)
    fireEvent.click(r.baseElement.querySelector('[data-role-option="moderator"]')!)
    const alert = await r.findByRole('alert')
    expect(alert.textContent).toBe(
      "This space's host household needs an update before roles can be changed from here.",
    )
  })

  it('an unreachable host is explained — the change was not sent', async () => {
    apiPatch.mockRejectedValue(new ApiError(503, '/x', {
      code: 'HOST_UNREACHABLE', detail: 'raw', reason: 'unreachable',
    }))
    openMemberActions('sp-1', 'u-bob', 'member', null, 'admin')
    const r = render(<MemberActionSheet onUpdate={() => {}} />)
    fireEvent.click(r.baseElement.querySelector('[data-role-option="moderator"]')!)
    const alert = await r.findByRole('alert')
    expect(alert.textContent).toBe(
      "Couldn't reach the space's host household. Nothing was sent — try again later.",
    )
    expect(showToast).not.toHaveBeenCalled()
  })

  it('the owner gets no role picker', () => {
    openMemberActions('sp-1', 'u-h', 'owner', 'host-x', 'admin')
    const r = render(<MemberActionSheet onUpdate={() => {}} />)
    expect(roleButtons(r.baseElement)).toEqual([])
  })
})
