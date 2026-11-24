import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

const showToastSpy = vi.hoisted(() => vi.fn())
vi.mock('@/components/Toast', () => ({ showToast: showToastSpy }))
const me = vi.hoisted(() => ({ value: { user_id: 'u-me' } as { user_id: string } | null }))
vi.mock('@/store/auth', () => ({ currentUser: me }))
const blocked = vi.hoisted(() => new Set<string>())
vi.mock('@/store/blocks', () => ({ isBlocked: (id: string) => blocked.has(id) }))

import { FollowToggle, toggleFollow } from './FollowToggle'

beforeEach(() => {
  showToastSpy.mockReset()
  me.value = { user_id: 'u-me' }
  blocked.clear()
})

describe('FollowToggle', () => {
  it('keeps one name and carries the state in aria-pressed', () => {
    const { getByRole, rerender } = render(
      <FollowToggle name="Daan" following={false} busy={false} onClick={() => {}} />)
    const off = getByRole('button', { name: 'Follow Daan' })
    expect(off.getAttribute('aria-pressed')).toBe('false')
    expect(off.textContent).toBe('Follow')
    rerender(<FollowToggle name="Daan" following busy={false} onClick={() => {}} />)
    const on = getByRole('button', { name: 'Follow Daan' })
    expect(on.getAttribute('aria-pressed')).toBe('true')
    expect(on.textContent).toBe('Following')
  })

  it('while busy it stays focusable and ignores taps', () => {
    const onClick = vi.fn()
    const { getByRole } = render(<FollowToggle name="Daan" following={false} busy onClick={onClick} />)
    const btn = getByRole('button', { name: 'Follow Daan' })
    expect(btn.hasAttribute('disabled')).toBe(false)
    expect(btn.getAttribute('aria-disabled')).toBe('true')
    fireEvent.click(btn)
    expect(onClick).not.toHaveBeenCalled()
  })
})

describe('toggleFollow', () => {
  const ok = () => Promise.resolve()

  it('a failed unfollow says so', async () => {
    await toggleFollow('u-daan', 'Daan', true, ok, () => Promise.reject(new Error('offline')))
    expect(showToastSpy).toHaveBeenCalledWith("Couldn't unfollow: offline", 'error')
  })

  it('a failed Undo says the follow failed', async () => {
    const follow = vi.fn().mockRejectedValue(new Error('offline'))
    await toggleFollow('u-daan', 'Daan', true, follow, ok)
    showToastSpy.mock.calls[0][2].action.onClick()
    expect(follow).toHaveBeenCalled()
    await waitFor(() =>
      expect(showToastSpy).toHaveBeenLastCalledWith("Couldn't follow: offline", 'error'))
  })

  it('Undo does nothing once someone else signed in, or the person is blocked', async () => {
    const follow = vi.fn().mockResolvedValue(undefined)
    await toggleFollow('u-daan', 'Daan', true, follow, ok)
    const undo = showToastSpy.mock.calls[0][2].action.onClick
    me.value = { user_id: 'u-other' }
    await undo()
    me.value = { user_id: 'u-me' }
    blocked.add('u-daan')
    await undo()
    expect(follow).not.toHaveBeenCalled()
  })
})
