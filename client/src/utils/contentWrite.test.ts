/**
 * ``contentWrite``: a 202 ``{queued: true}`` (a write held for review in a
 * "Reviewed" space, §4.3) is NOT the created object — it toasts
 * "Submitted for review" and refreshes the author's pending items; any
 * other answer passes through untouched, without a toast.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

const showToast = vi.fn()
vi.mock('@/components/Toast', () => ({ showToast: (...a: unknown[]) => showToast(...a) }))
const refresh = vi.fn()
vi.mock('@/store/moderationMine', () => ({
  refreshModerationMine: (...a: unknown[]) => refresh(...a),
}))

import { classifyWrite, contentWrite, isQueuedWrite } from './contentWrite'

const QUEUED = {
  queued: true, item_id: 'abc', feature: 'posts', action: 'create', entity: 'post', target_id: 'p1',
}

beforeEach(() => {
  showToast.mockReset()
  refresh.mockReset()
})

describe('contentWrite', () => {
  it('202 {queued: true} → queued, the review toast, the pending strip refreshed', async () => {
    const res = await contentWrite(Promise.resolve(QUEUED), { spaceId: 'sp-1' })
    expect(res).toEqual({ queued: true, item: QUEUED })
    expect(showToast).toHaveBeenCalledWith(
      'Submitted for review — a moderator will look at it', 'info',
    )
    expect(refresh).toHaveBeenCalledWith('sp-1')
  })

  it('201 with the created object → not queued, data passed through, no toast', async () => {
    const post = { id: 'p1', type: 'text', content: 'hi' }
    const res = await contentWrite<typeof post>(Promise.resolve(post), { spaceId: 'sp-1' })
    expect(res).toEqual({ queued: false, data: post })
    expect(showToast).not.toHaveBeenCalled()
    expect(refresh).not.toHaveBeenCalled()
  })

  it('a 204 (null body) is a normal write', async () => {
    expect(await contentWrite(Promise.resolve(null))).toEqual({ queued: false, data: null })
  })

  it('toast: false leaves the toast to the caller', () => {
    expect(classifyWrite(QUEUED, { toast: false }).queued).toBe(true)
    expect(showToast).not.toHaveBeenCalled()
  })

  it('errors propagate unchanged', async () => {
    const err = new Error('boom')
    await expect(contentWrite(Promise.reject(err))).rejects.toBe(err)
    expect(showToast).not.toHaveBeenCalled()
  })

  it('isQueuedWrite only matches queued === true', () => {
    expect(isQueuedWrite(QUEUED)).toBe(true)
    expect(isQueuedWrite({ queued: 'yes' })).toBe(false)
    expect(isQueuedWrite({ id: 'x' })).toBe(false)
    expect(isQueuedWrite(null)).toBe(false)
  })
})
