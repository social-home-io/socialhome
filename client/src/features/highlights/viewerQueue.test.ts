import { describe, it, expect } from 'vitest'
import type { HighlightInboxItem } from '@/types'
import { neighbours } from './viewerQueue'

const NOW = Date.parse('2026-10-26T12:00:00Z')

/** One author's day; ``hoursAgo`` orders the Today list (newest first). */
const item = (author: string, unseen: number, hoursAgo: number): HighlightInboxItem => {
  const at = new Date(NOW - hoursAgo * 3_600_000).toISOString()
  return {
    highlight: {
      id: `h-${author}`, author_user_id: author, highlight_date: '2026-10-26',
      audience_kind: 'all_paired', audience_label: 'paired',
      created_at: at, expires_at: '2026-11-25T08:00:00Z',
    },
    frames: [{
      id: `f-${author}`, highlight_id: `h-${author}`, sequence: 1, frame_type: 'image',
      media_url: 'api/media/x.webp', caption_text: null, caption_emoji: null,
      duration_ms: null, created_at: at,
    }],
    unseen_count: unseen,
  }
}

// Today: New = thomas (1 h), maxi (2 h), finn (3 h); Seen = hilde (4 h).
const inbox = [item('thomas', 2, 1), item('maxi', 1, 2), item('finn', 3, 3), item('hilde', 0, 4), item('me', 0, 0.5)]

describe('neighbours — who comes before and after in the viewer (C8)', () => {
  it('up next is the next person with unseen frames, in Today order', () => {
    const n = neighbours(inbox, 'me', 'thomas', new Set(), NOW)
    expect(n.prev).toBeNull()
    expect(n.upNext?.authorId).toBe('maxi')
    expect(n.next?.authorId).toBe('maxi')
  })

  it('wraps to an earlier unseen person, skips people watched this session', () => {
    const n = neighbours(inbox, 'me', 'finn', new Set(['thomas']), NOW)
    expect(n.prev?.authorId).toBe('maxi')
    expect(n.upNext?.authorId).toBe('maxi')
  })

  it('with nobody unseen left there is no "up next"; Next still steps through Today', () => {
    const n = neighbours(inbox, 'me', 'finn', new Set(['thomas', 'maxi']), NOW)
    expect(n.upNext).toBeNull()
    expect(n.next?.authorId).toBe('hilde')
    const last = neighbours(inbox, 'me', 'hilde', new Set(['thomas', 'maxi', 'finn']), NOW)
    expect(last.upNext).toBeNull()
    expect(last.next).toBeNull()
    expect(last.prev?.authorId).toBe('finn')
  })

  it('your own highlight leads to the first unseen person', () => {
    const n = neighbours(inbox, 'me', 'me', new Set(), NOW)
    expect(n.prev).toBeNull()
    expect(n.upNext?.authorId).toBe('thomas')
  })

  it('someone not on Today (an older day, a share card) still gets an up next', () => {
    const n = neighbours(inbox, 'me', 'stranger', new Set(), NOW)
    expect(n.prev).toBeNull()
    expect(n.upNext?.authorId).toBe('thomas')
  })
})
