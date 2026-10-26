import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('@/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))

import { api } from '@/api'
import { householdUsers } from '@/store/householdUsers'
import type { User } from '@/types'
import { replyPrivately } from './privateReply'

describe('replyPrivately — a highlight reply is a 1:1 chat (owner decision 11)', () => {
  beforeEach(() => {
    vi.mocked(api.post).mockReset()
    vi.mocked(api.post).mockImplementation(async (path: string) => (
      path === '/api/conversations/dm' ? { id: 'c-1', type: 'dm' } : { message_id: 'm-1' }
    ))
    householdUsers.value = new Map([
      ['uid-lena', { user_id: 'uid-lena', username: 'lena', display_name: 'Lena' } as User],
    ])
  })

  it('opens (or finds) the chat with a household member by username, then quotes the frame', async () => {
    const id = await replyPrivately({ frameId: 'f-1', authorId: 'uid-lena', content: ' So schön! ', via: '' })
    expect(id).toBe('c-1')
    expect(vi.mocked(api.post).mock.calls).toEqual([
      ['/api/conversations/dm', { username: 'lena' }],
      ['/api/highlights/frames/f-1/dm-reply', { conversation_id: 'c-1', content: 'So schön!' }],
    ])
  })

  it('a paired household member is addressed by user id; a share-card reply keeps via_post', async () => {
    await replyPrivately({ frameId: 'f-2', authorId: 'ru-amaka', content: 'hi', via: '?via_post=p-9' })
    expect(vi.mocked(api.post).mock.calls).toEqual([
      ['/api/conversations/dm', { user_id: 'ru-amaka' }],
      ['/api/highlights/frames/f-2/dm-reply?via_post=p-9', { conversation_id: 'c-1', content: 'hi' }],
    ])
  })

  it('no chat, no message: a failed chat lookup stops before sending', async () => {
    vi.mocked(api.post).mockRejectedValueOnce(new Error('not found'))
    await expect(replyPrivately({ frameId: 'f-1', authorId: 'ru-x', content: 'hi', via: '' }))
      .rejects.toThrow('not found')
    expect(api.post).toHaveBeenCalledTimes(1)
  })
})
