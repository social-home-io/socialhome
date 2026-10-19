import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/preact'

const { patch, toast, confirm } = vi.hoisted(() => ({
  patch: vi.fn(), toast: vi.fn(), confirm: vi.fn(),
}))
vi.mock('@/api', () => ({ api: { patch } }))
vi.mock('@/components/Toast', () => ({ showToast: toast }))
vi.mock('@/components/confirm', () => ({ confirmDialog: confirm }))

import { patchSpace, privateGfsLinkMembers, useServerValue } from './spacePatch'

beforeEach(() => { patch.mockReset(); toast.mockReset(); confirm.mockReset() })

describe('patchSpace', () => {
  it('saves exactly the body and says so', async () => {
    patch.mockResolvedValue({})
    const onUpdate = vi.fn()
    expect(await patchSpace('s1', { join_mode: 'open' }, { onUpdate })).toBe('saved')
    expect(patch).toHaveBeenCalledWith('/api/spaces/s1', { join_mode: 'open' })
    expect(toast).toHaveBeenCalledWith('Saved', 'success')
    expect(onUpdate).toHaveBeenCalled()
  })

  it('a forwarded edit says the host applies it (access changes: the levels in force)', async () => {
    patch.mockResolvedValue({ forwarded: true })
    expect(await patchSpace('s1', { name: 'X' })).toBe('forwarded')
    expect(toast).toHaveBeenLastCalledWith(expect.stringContaining('Sent to the host household'), 'info')
    await patchSpace('s1', { features: { posts_access: 'admin_only' } })
    expect(toast).toHaveBeenLastCalledWith(expect.stringContaining('levels shown here'), 'info')
  })

  it('PEERS_TOO_OLD asks, then resends with force', async () => {
    patch
      .mockRejectedValueOnce({ code: 'PEERS_TOO_OLD', extra: { households: [{ instance_id: 'i', display_name: 'Granny' }] } })
      .mockResolvedValueOnce({})
    confirm.mockResolvedValue(true)
    const body = { features: { posts_access: 'moderated' } }
    expect(await patchSpace('s1', body)).toBe('saved')
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining('Granny'), expect.anything())
    expect(patch).toHaveBeenLastCalledWith('/api/spaces/s1', { ...body, force: true })
  })

  it('PEERS_TOO_OLD cancelled changes nothing', async () => {
    patch.mockRejectedValue({ code: 'PEERS_TOO_OLD', extra: { households: [] } })
    confirm.mockResolvedValue(false)
    expect(await patchSpace('s1', { features: {} })).toBe('failed')
    expect(patch).toHaveBeenCalledTimes(1)
  })

  it('PRIVATE_GFS_LINK_MEMBERS hands over the households', async () => {
    patch.mockRejectedValue({
      code: 'PRIVATE_GFS_LINK_MEMBERS',
      extra: { households: [{ instance_id: 'h1', display_name: '', members: [{ user_id: 'u' }] }] },
    })
    const onLinkMembers = vi.fn()
    expect(await patchSpace('s1', { features: { private_gfs: false } }, { onLinkMembers })).toBe('failed')
    expect(onLinkMembers).toHaveBeenCalledWith([
      { instance_id: 'h1', display_name: 'h1', members: [{ user_id: 'u', display_name: 'u' }] },
    ])
  })

  it('any other failure shows the server message', async () => {
    patch.mockRejectedValue(new Error('owner required'))
    expect(await patchSpace('s1', {})).toBe('failed')
    expect(toast).toHaveBeenCalledWith('owner required', 'error')
  })

  it('privateGfsLinkMembers ignores other errors', () => {
    expect(privateGfsLinkMembers(new Error('x'))).toBeNull()
    expect(privateGfsLinkMembers(null)).toBeNull()
  })
})

describe('useServerValue', () => {
  it('shows a local pick and snaps to a new server value', () => {
    const { result, rerender } = renderHook(
      (p: { v: boolean }) => useServerValue(p.v), { initialProps: { v: false } },
    )
    act(() => { result.current[1](true) })
    expect(result.current[0]).toBe(true)
    act(() => { rerender({ v: false }) })
    expect(result.current[0]).toBe(true)
    act(() => { rerender({ v: true }) })
    act(() => { result.current[1](false) })
    act(() => { rerender({ v: false }) })
    expect(result.current[0]).toBe(false)
  })
})
