/**
 * SpaceSettingsPage × ``space.config.changed`` — the open settings page
 * follows a rename / role change made elsewhere, and a failed live
 * refresh never flips it to "Space not found".
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/preact'

const apiGet = vi.fn()
vi.mock('@/api', () => ({
  api: {
    get: (...args: unknown[]) => apiGet(...args),
    post: vi.fn(), patch: vi.fn(), delete: vi.fn(),
  },
}))

type Handler = (e: { type: string; data: Record<string, unknown> }) => void
const handlers = new Map<string, Set<Handler>>()
vi.mock('@/ws', () => ({
  ws: {
    on: (type: string, h: Handler) => {
      if (!handlers.has(type)) handlers.set(type, new Set())
      handlers.get(type)!.add(h)
      return () => { handlers.get(type)?.delete(h) }
    },
  },
}))
function emit(type: string, data: Record<string, unknown>) {
  handlers.get(type)?.forEach(h => h({ type, data: { type, ...data } }))
}

const route = vi.fn()
vi.mock('preact-iso', () => ({
  useRoute: () => ({ params: { id: 'sp-1' } }),
  useLocation: () => ({ route }),
}))
vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u1', username: 'anna' } },
}))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))

let detail: Record<string, unknown>
let role: string
beforeEach(() => {
  handlers.clear()
  route.mockReset()
  apiGet.mockReset()
  detail = { id: 'sp-1', name: 'Garden', features: {}, owner_instance_id: null }
  role = 'member'
  apiGet.mockImplementation(async (url: string) => {
    if (url === '/api/spaces/sp-1') return detail
    if (url === '/api/spaces/sp-1/members') return [{ user_id: 'u1', role }]
    return []
  })
})

describe('SpaceSettingsPage live config', () => {
  it('follows a rename and an admin grant made elsewhere', async () => {
    const { default: Page } = await import('./SpaceSettingsPage')
    const view = render(<Page />)
    await view.findByRole('heading', { name: /Garden — settings/ })
    // Plain member → only the Bots tab.
    expect(view.queryByRole('tab', { name: 'General' })).toBeNull()

    detail = { ...detail, name: 'Allotment' }
    role = 'admin'
    emit('space.config.changed', { space_id: 'sp-1', event_type: 'admin_granted' })
    await view.findByRole('heading', { name: /Allotment — settings/ })
    expect(view.getByRole('tab', { name: 'General' })).toBeTruthy()
  })

  it('a failed live refresh keeps the page', async () => {
    const { default: Page } = await import('./SpaceSettingsPage')
    const view = render(<Page />)
    await view.findByRole('heading', { name: /Garden — settings/ })
    apiGet.mockRejectedValue(new Error('offline'))
    emit('space.config.changed', { space_id: 'sp-1', event_type: 'rename' })
    await waitFor(() => expect(apiGet).toHaveBeenCalledWith('/api/spaces/sp-1'))
    await new Promise(r => setTimeout(r, 0))
    expect(view.getByRole('heading', { name: /Garden — settings/ })).toBeTruthy()
    expect(view.queryByText('Space not found')).toBeNull()
  })

  it('shows the GFS publish mode to the owner only', async () => {
    // Public space whose posts followers may read — where members publish
    // over the GFS at all.
    detail = {
      ...detail, space_type: 'public', join_mode: 'open',
      features: { allow_subscribers: true },
    }
    role = 'admin'
    const { default: Page } = await import('./SpaceSettingsPage')
    const view = render(<Page />)
    fireEvent.click(await view.findByRole('tab', { name: 'General' }))
    await view.findByRole('heading', { name: 'Space settings' })
    expect(view.queryByRole('radiogroup', { name: /Posting through the GFS/ })).toBeNull()

    role = 'owner'
    emit('space.config.changed', { space_id: 'sp-1', event_type: 'owner_transferred' })
    const group = await view.findByRole('radiogroup', { name: /Posting through the GFS/ })
    expect(group.querySelector('[aria-checked="true"]')?.textContent).toBe('Trusted')
  })
})
