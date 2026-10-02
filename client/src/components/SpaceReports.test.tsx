import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

const ROW = {
  id: 'r1', target_type: 'post', target_id: 'p1',
  reporter_user_id: 'uid-bob', reporter_instance_id: null, reporter_name: 'Bob',
  target_preview: 'buy cheap pills', target_gone: false,
  category: 'spam', notes: 'again', status: 'pending',
  created_at: '2026-04-18T00:00:00Z', space_id: 'sp-1',
}

function mockApi(rows: unknown[], extra: Record<string, unknown> = {}) {
  const api = {
    get: vi.fn(async () => rows),
    post: vi.fn(async () => ({})),
    delete: vi.fn(async () => undefined),
    ...extra,
  }
  vi.doMock('@/api', () => ({ api, ApiError: class extends Error {} }))
  return api
}

describe('SpaceReports', () => {
  beforeEach(() => {
    vi.resetModules()
  })

  it('lists the space reports with what, why, who and the preview', async () => {
    const api = mockApi([ROW])
    const { SpaceReports } = await import('./SpaceReports')
    const { findByText, getByText } = render(<SpaceReports spaceId="sp-1" />)
    expect(await findByText('buy cheap pills')).toBeTruthy()
    expect(getByText('Post')).toBeTruthy()
    expect(getByText(/Spam · reported by Bob/)).toBeTruthy()
    expect(getByText(/again/)).toBeTruthy()
    expect(api.get).toHaveBeenCalledWith('/api/spaces/sp-1/reports')
  })

  it('shows the empty state', async () => {
    mockApi([])
    const { SpaceReports } = await import('./SpaceReports')
    const { findByText } = render(<SpaceReports spaceId="sp-1" />)
    expect(await findByText('No open reports.')).toBeTruthy()
  })

  it('shows a retryable alert when loading fails', async () => {
    mockApi([], { get: vi.fn(async () => { throw new Error('boom') }) })
    const { SpaceReports } = await import('./SpaceReports')
    const { findByRole } = render(<SpaceReports spaceId="sp-1" />)
    const alert = await findByRole('alert')
    expect(alert.textContent).toContain('boom')
    expect(alert.textContent).toContain('Retry')
  })

  it('resolves and dismisses through the space endpoint', async () => {
    const api = mockApi([ROW, { ...ROW, id: 'r2', target_preview: 'second' }])
    const { SpaceReports } = await import('./SpaceReports')
    const { findByText, getAllByText, queryByText } = render(<SpaceReports spaceId="sp-1" />)
    await findByText('second')
    fireEvent.click(getAllByText('Resolve')[0])
    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith('/api/spaces/sp-1/reports/r1/resolve', { dismissed: false }))
    await waitFor(() => expect(queryByText('buy cheap pills')).toBeNull())
    fireEvent.click(getAllByText('Dismiss')[0])
    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith('/api/spaces/sp-1/reports/r2/resolve', { dismissed: true }))
  })

  it('puts the row back when resolving fails', async () => {
    mockApi([ROW], { post: vi.fn(async () => { throw new Error('nope') }) })
    const { SpaceReports } = await import('./SpaceReports')
    const { findByText, getByText } = render(<SpaceReports spaceId="sp-1" />)
    await findByText('buy cheap pills')
    fireEvent.click(getByText('Resolve'))
    expect(await findByText('buy cheap pills')).toBeTruthy()
  })

  it('opens the tab holding the item', async () => {
    mockApi([{ ...ROW, target_type: 'page', target_preview: 'House rules' }])
    const onOpenTab = vi.fn()
    const { SpaceReports } = await import('./SpaceReports')
    const { findByText } = render(<SpaceReports spaceId="sp-1" onOpenTab={onOpenTab} />)
    fireEvent.click(await findByText('Show in Pages'))
    expect(onOpenTab).toHaveBeenCalledWith('pages')
  })

  it('a removed item shows a note and no show / remove actions', async () => {
    mockApi([{ ...ROW, target_gone: true, target_preview: null }])
    const { SpaceReports } = await import('./SpaceReports')
    const { findByText, queryByText } = render(<SpaceReports spaceId="sp-1" onOpenTab={() => {}} />)
    expect(await findByText('This content has since been removed.')).toBeTruthy()
    expect(queryByText('Remove post')).toBeNull()
    expect(queryByText(/Show in/)).toBeNull()
  })

  it('a member report names the member', async () => {
    mockApi([{ ...ROW, target_type: 'user', target_id: 'uid-x', target_name: 'Xena', target_preview: null }])
    const { SpaceReports } = await import('./SpaceReports')
    const { findByText, getByText, queryByText } = render(<SpaceReports spaceId="sp-1" />)
    expect(await findByText('Xena')).toBeTruthy()
    expect(getByText('Member')).toBeTruthy()
    expect(queryByText('Remove post')).toBeNull()
  })

  it('remove post deletes it after confirming, then resolves', async () => {
    vi.doMock('./confirm', () => ({ confirmDialog: vi.fn(async () => true) }))
    const api = mockApi([ROW])
    const { SpaceReports } = await import('./SpaceReports')
    const { findByText } = render(<SpaceReports spaceId="sp-1" />)
    fireEvent.click(await findByText('Remove post'))
    await waitFor(() => expect(api.delete).toHaveBeenCalledWith('/api/spaces/sp-1/posts/p1'))
    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith('/api/spaces/sp-1/reports/r1/resolve', { dismissed: false }))
  })

  it('remove post does nothing when the confirm is cancelled', async () => {
    vi.doMock('./confirm', () => ({ confirmDialog: vi.fn(async () => false) }))
    const api = mockApi([ROW])
    const { SpaceReports } = await import('./SpaceReports')
    const { findByText } = render(<SpaceReports spaceId="sp-1" />)
    fireEvent.click(await findByText('Remove post'))
    await new Promise((r) => setTimeout(r, 0))
    expect(api.delete).not.toHaveBeenCalled()
  })

  it('a report about the viewer (sole authority) is anonymous and dismiss-only', async () => {
    mockApi([{
      ...ROW, reporter_name: null, reporter_user_id: null,
      anonymous: true, dismiss_only: true,
    }])
    const { SpaceReports } = await import('./SpaceReports')
    const { findByText, queryByText, getByText } = render(<SpaceReports spaceId="sp-1" />)
    expect(await findByText(/reported by someone in the space/)).toBeTruthy()
    expect(getByText(/This report is about you/)).toBeTruthy()
    expect(queryByText('Resolve')).toBeNull()
    expect(queryByText('Remove post')).toBeNull()
    expect(getByText('Dismiss')).toBeTruthy()
  })

  it('never renders notes on an anonymous row, even if some arrive', async () => {
    mockApi([{
      ...ROW, reporter_name: null, reporter_user_id: null,
      anonymous: true, dismiss_only: true, notes: 'it was me, Bob',
    }])
    const { SpaceReports } = await import('./SpaceReports')
    const { findByText, queryByText } = render(<SpaceReports spaceId="sp-1" />)
    await findByText(/This report is about you/)
    expect(queryByText(/it was me, Bob/)).toBeNull()
  })
})
