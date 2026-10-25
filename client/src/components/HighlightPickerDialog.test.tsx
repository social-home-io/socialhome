import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render } from '@testing-library/preact'

vi.mock('@/api', () => {
  const m = { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() }
  return { api: m }
})
vi.mock('./Toast', () => ({ showToast: vi.fn() }))
vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u1', display_name: 'Pascal' } },
}))

import { HighlightPickerDialog, openHighlightPicker } from './HighlightPickerDialog'
import { api } from '@/api'

const apiMock = api as unknown as { get: ReturnType<typeof vi.fn> }

describe('HighlightPickerDialog', () => {
  beforeEach(() => {
    apiMock.get.mockReset()
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
  })

  it('renders nothing when closed', () => {
    const { container } = render(<HighlightPickerDialog />)
    expect(container.querySelector('.sh-highlight-picker')).toBeNull()
  })

  it('opens with a loading state and lists my highlights', async () => {
    apiMock.get.mockResolvedValueOnce([
      {
        highlight: {
          id: 's1',
          author_user_id: 'u1',
          highlight_date: '2026-05-03',
          audience_kind: 'all_paired',
          audience: [],
          created_at: '2026-05-03T08:00:00Z',
          expires_at: '2026-06-02T08:00:00Z',
        },
        frames: [
          {
            id: 'f1',
            highlight_id: 's1',
            sequence: 1,
            frame_type: 'image',
            media_url: '/api/media/x.webp',
            caption_text: null,
            caption_emoji: null,
            duration_ms: null,
            created_at: '2026-05-03T08:00:00Z',
          },
        ],
        unseen_count: 0,
      },
    ])
    openHighlightPicker({ scope: 'household' })
    const { findByText } = render(<HighlightPickerDialog />)
    // The day in words, like the rest of Highlights — never the raw key.
    expect(await findByText('May 3, 2026')).toBeTruthy()
    expect(document.body.textContent).not.toContain('2026-05-03')
  })
})
