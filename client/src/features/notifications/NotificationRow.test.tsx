import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render } from '@testing-library/preact'

const apiPost = vi.fn()
vi.mock('@/api', () => ({
  api: { post: (...a: unknown[]) => apiPost(...a), get: vi.fn() },
}))

import type { Notification, Space } from '@/types'
import { spaces } from '@/store/spaces'
import {
  NewPill, NotificationRow, glyphFor, markRowRead, rowPlainText, withRead,
} from './NotificationRow'

function row(extra: Partial<Notification> = {}): Notification {
  return {
    id: 'n1', type: 'space_mention', title: 'title', body: null,
    link_url: '/spaces/s1?post=p1', read_at: null,
    created_at: new Date().toISOString(),
    object_key: 'space_post:p1', space_id: 's1',
    params: { actors: [{ id: 'u2', name: 'Ben' }], actor_count: 1, new: 1, total: 1, space_name: 'Salon' },
    ...extra,
  }
}

beforeEach(() => {
  apiPost.mockReset().mockResolvedValue({})
  spaces.value = []
})

describe('NotificationRow', () => {
  it('a menu sits beside the one link, never inside it', () => {
    const { container, getByRole } = render(
      <NotificationRow n={row()} menu={<button type="button">More</button>} />,
    )
    const link = container.querySelector('a.sh-notif-row')!
    const more = getByRole('button', { name: 'More' })
    expect(link.contains(more)).toBe(false)
    expect(container.querySelector('.sh-notif-item')!.contains(more)).toBe(true)
  })

  it('without a menu the row is just the link', () => {
    const { container } = render(<NotificationRow n={row()} />)
    expect(container.querySelector('.sh-notif-item')).toBeNull()
  })

  it('rowPlainText reads the sentence, else the title', () => {
    expect(rowPlainText(row())).toContain('Ben')
    expect(rowPlainText(row({ params: null, type: 'zzz', title: 'Old row' }))).toBe('Old row')
  })

  it('shows the actor\'s avatar with the type glyph', () => {
    const { container } = render(<NotificationRow n={row()} />)
    expect(container.querySelector('.sh-notif-av .sh-avatar')!.textContent).toBe('BE')
    expect(container.querySelector('.sh-notif-av__glyph')!.getAttribute('data-glyph')).toBe('at')
  })

  it('without an actor, the space\'s picture as a rounded square', () => {
    spaces.value = [{ id: 's1', name: 'Salon', emoji: '🎻' } as Space]
    const { container } = render(<NotificationRow n={row({
      type: 'moderation_pending',
      params: { actors: [], actor_count: 0, new: 1, total: 1, space_name: 'Salon' },
    })} />)
    expect(container.querySelector('.sh-notif-av__space')!.textContent).toBe('🎻')
    expect(container.querySelector('.sh-notif-title')!.textContent).toBe('Something is waiting for review in Salon')
  })

  it('without an actor or a space, a plain tile', () => {
    const { container } = render(<NotificationRow n={row({ type: 'task_deadline', space_id: null, params: null })} />)
    expect(container.querySelector('.sh-notif-av__sys')).not.toBeNull()
    expect(container.querySelector('.sh-notif-title')!.textContent).toBe('title')
  })

  it('the live space name wins in the sentence', () => {
    spaces.value = [{ id: 's1', name: 'Salon (renamed)', emoji: null } as Space]
    const { container } = render(<NotificationRow n={row()} />)
    expect(container.querySelector('.sh-notif-title')!.textContent).toBe('Ben mentioned you in Salon (renamed)')
  })

  it('a row with nowhere to go is a button; read rows have no "Unread"', () => {
    const { container } = render(<NotificationRow n={row({ link_url: null, read_at: '2026-10-27T10:00:00Z' })} />)
    const el = container.querySelector('.sh-notif-row')!
    expect(el.tagName).toBe('BUTTON')
    expect(el.querySelector('.sr-only')).toBeNull()
  })

  it('calls onOpen with the row', () => {
    const onOpen = vi.fn()
    const n = row()
    const { container } = render(<NotificationRow n={n} onOpen={onOpen} />)
    const a = container.querySelector<HTMLAnchorElement>('a.sh-notif-row')!
    a.addEventListener('click', e => e.preventDefault())
    a.click()
    expect(onOpen).toHaveBeenCalledWith(n)
  })
})

describe('glyphFor', () => {
  it.each([
    ['moment_reacted', 'heart'], ['space_mention', 'at'], ['space_here', 'at'],
    ['space_comment_added', 'comment'], ['moment_replied', 'comment'],
    ['space_post_created', 'post'], ['user_followed', 'person'], ['space_join_requested', 'person'],
    ['calendar_reminder', 'calendar'], ['task_assigned', 'task'], ['space_report', 'shield'],
    ['bazaar_bid_placed', 'tag'], ['dm_contact_requested', 'person'], ['app_challenge', 'game'],
    ['call_missed', 'phone'], ['chat_added', 'comment'], ['poll_result', 'poll'], ['poll_scheduled', 'poll'],
    ['report_answer', 'shield'],
  ])('%s → %s', (type, glyph) => {
    expect(glyphFor(type)).toBe(glyph)
  })
})

describe('reading a row', () => {
  it('by object when the row names its thing', async () => {
    await markRowRead(row())
    expect(apiPost).toHaveBeenCalledWith('/api/notifications/read-by-object', { object_keys: ['space_post:p1'] })
  })

  it('by id for a row from before 0129', async () => {
    await markRowRead(row({ object_key: null }))
    expect(apiPost).toHaveBeenCalledWith('/api/notifications/n1/read')
  })

  it('withRead flips the row and every unread row about the same thing', () => {
    const list = [
      row(),
      row({ id: 'n2', type: 'space_comment_added' }),
      row({ id: 'n3', object_key: 'space_post:other' }),
      row({ id: 'n4', object_key: null }),
    ]
    const out = withRead(list, list[0])
    expect(out.map(n => n.read_at !== null)).toEqual([true, true, false, false])
  })
})

describe('NewPill', () => {
  it('shows nothing at zero, "↑ N new" otherwise', () => {
    expect(render(<NewPill count={0} onShow={() => {}} />).container.innerHTML).toBe('')
    const { container } = render(<NewPill count={2} onShow={() => {}} />)
    expect(container.querySelector('button')!.textContent).toBe('↑ 2 new')
  })
})
