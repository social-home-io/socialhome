import { describe, it, expect, vi, beforeEach } from 'vitest'

const { wsSendMock, wsHandlers } = vi.hoisted(() => ({
  wsSendMock: vi.fn(),
  wsHandlers: {} as Record<string, (e: { data: unknown }) => void>,
}))

vi.mock('@/ws', () => ({
  ws: {
    send: wsSendMock,
    on: vi.fn((type: string, h: (e: { data: unknown }) => void) => {
      wsHandlers[type] = h
      return () => {}
    }),
  },
}))

import { render } from '@testing-library/preact'
import { act } from 'preact/test-utils'
import { householdUsers } from '@/store/householdUsers'
import type { User } from '@/types'
import { sendTyping, TypingIndicator } from './TypingIndicator'

describe('TypingIndicator — who is typing, never an id', () => {
  const typing = (cid: string, uid: string, username?: string) => act(() => {
    wsHandlers['conversation.user_typing']({
      data: { conversation_id: cid, sender_user_id: uid, sender_username: username },
    })
  })

  it('a cached person by name; else the frame username; else "Someone"', () => {
    householdUsers.value = new Map([['u-mia', { user_id: 'u-mia', username: 'mia', display_name: 'Mia' } as User]])
    typing('c1', 'u-mia')
    expect(render(<TypingIndicator scope="c1" />).container.textContent).toContain('Mia')
    typing('c2', 'u-remote-7xk2', 'erin')
    expect(render(<TypingIndicator scope="c2" />).container.textContent).toContain('erin')
    typing('c3', 'u-remote-9qz4')
    const text = render(<TypingIndicator scope="c3" />).container.textContent ?? ''
    expect(text).toContain('Someone')
    expect(text).not.toContain('u-remote-9qz4')
  })

  it('a typist from another household by the name the server sent', () => {
    act(() => {
      wsHandlers['conversation.user_typing']({
        data: {
          conversation_id: 'c4', sender_user_id: 'u-remote-3ab1',
          sender_username: 'erin', sender_display_name: 'Erin Okafor',
        },
      })
    })
    expect(render(<TypingIndicator scope="c4" />).container.textContent).toContain('Erin Okafor')
  })
})

describe('TypingIndicator', () => {
  beforeEach(() => {
    wsSendMock.mockReset()
  })

  it('module exports exist', async () => {
    const mod = await import('./TypingIndicator')
    expect(mod).toBeTruthy()
    expect(Object.keys(mod).length).toBeGreaterThan(0)
  })

  it('sendTyping(string) routes to a DM conversation_id frame', () => {
    sendTyping('conv-1')
    expect(wsSendMock).toHaveBeenCalledOnce()
    const [type, payload] = wsSendMock.mock.calls[0]
    expect(type).toBe('typing')
    expect(payload).toEqual({ conversation_id: 'conv-1' })
  })

  it('sendTyping({postId}) routes to a comment-thread frame (no space)', () => {
    sendTyping({ postId: 'post-7' })
    const [, payload] = wsSendMock.mock.calls[0]
    expect(payload).toEqual({ post_id: 'post-7' })
    expect('space_id' in payload).toBe(false)
  })

  it('sendTyping({postId, spaceId}) carries the space scope', () => {
    sendTyping({ postId: 'post-7', spaceId: 'space-x' })
    const [, payload] = wsSendMock.mock.calls[0]
    expect(payload).toEqual({ post_id: 'post-7', space_id: 'space-x' })
  })

  it('sendTyping({postId, spaceId: null}) omits space_id', () => {
    sendTyping({ postId: 'post-7', spaceId: null })
    const [, payload] = wsSendMock.mock.calls[0]
    expect(payload).toEqual({ post_id: 'post-7' })
  })
})
