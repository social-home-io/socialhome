import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor, fireEvent } from '@testing-library/preact'

const wsHandlers = vi.hoisted(() => new Map<string, () => void>())
vi.mock('@/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))
vi.mock('preact-iso', () => ({ useLocation: () => ({ route: vi.fn(), url: '/' }) }))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))
vi.mock('@/ws', () => ({
  ws: { on: (type: string, fn: () => void) => { wsHandlers.set(type, fn); return () => undefined } },
}))

import { api } from '@/api'
import type { HighlightInboxItem } from '@/types'
import HighlightsInboxTab from './HighlightsInboxTab'

const item: HighlightInboxItem = {
  highlight: {
    id: 'h1', author_user_id: 'ru-amaka', highlight_date: '2026-10-25',
    audience_kind: 'all_paired', audience_label: 'paired',
    created_at: '2026-10-25T08:00:00Z', expires_at: '2026-10-26T08:00:00Z',
    author: { display_name: 'Amaka Okafor', picture_url: null },
  },
  frames: [],
  unseen_count: 1,
} as unknown as HighlightInboxItem

describe('HighlightsInboxTab when a load fails', () => {
  beforeEach(() => { wsHandlers.clear(); vi.mocked(api.get).mockReset() })

  it('a failed first load is an alert with Try again, never "no highlights"', async () => {
    let fail = true
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path !== '/api/highlights') return []
      if (fail) throw new Error('offline')
      return [item]
    })
    const { findByRole, container } = render(<HighlightsInboxTab />)
    expect((await findByRole('alert')).textContent).toContain("Couldn't load highlights")
    expect(container.querySelector('.sh-empty-state:not(.sh-load-error)')).toBeNull()
    fail = false
    fireEvent.click(await findByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(container.textContent).toContain('Amaka Okafor'))
    expect(container.querySelector('[role="alert"]')).toBeNull()
  })

  it('a failed refresh keeps the list and says from when it is', async () => {
    let fail = false
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path !== '/api/highlights') return []
      if (fail) throw new Error('offline')
      return [item]
    })
    const { findByRole, container } = render(<HighlightsInboxTab />)
    await waitFor(() => expect(container.textContent).toContain('Amaka Okafor'))
    fail = true
    wsHandlers.get('highlight.frame_added')!()
    expect((await findByRole('status')).textContent).toMatch(/Couldn't refresh\. Showing highlights from/)
    expect(container.textContent).toContain('Amaka Okafor')
  })
})
