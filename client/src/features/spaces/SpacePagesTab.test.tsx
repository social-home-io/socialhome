import { describe, it, expect, vi } from 'vitest'
import { render, waitFor } from '@testing-library/preact'

const apiGet = vi.fn().mockResolvedValue([])
vi.mock('@/api', () => ({
  api: { get: (...a: unknown[]) => apiGet(...a), post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))
vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u1', username: 'me', display_name: 'Me', is_admin: false, picture_url: null, bio: null, is_new_member: false } },
  token: { value: 'tok' },
  isAuthed: { value: true },
}))
vi.mock('@/components/PendingReviewStrip', () => ({
  PendingReviewStrip: (p: { feature: string }) => <div data-testid="pending">{p.feature}</div>,
}))

import { SpacePagesTab } from './SpacePagesTab'

describe('SpacePagesTab', () => {
  it('loads the space pages, with the pending strip; a writer can add', async () => {
    const r = render(
      <SpacePagesTab spaceId="s1" role="member" level="open" writable adminOnly={false} archived={false} />,
    )
    await waitFor(() => expect(apiGet).toHaveBeenCalledWith('/api/spaces/s1/pages'))
    await waitFor(() => r.getByRole('button', { name: '+ New page' }))
    expect(r.getByTestId('pending').textContent).toBe('pages')
    expect(r.queryByTestId('access-note-pages')).toBeNull()
  })

  it('admin-only for a member: the note, no New page', async () => {
    const r = render(
      <SpacePagesTab spaceId="s1" role="member" level="admin_only" writable={false} adminOnly archived={false} />,
    )
    await waitFor(() => r.getByText('Only admins can add or edit pages here.'))
    expect(r.queryByRole('button', { name: '+ New page' })).toBeNull()
  })
})
