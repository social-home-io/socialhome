import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, waitFor, cleanup, fireEvent } from '@testing-library/preact'

// useRoute supplies the space id; useLocation supplies route().
const routeMock = vi.fn()
vi.mock('preact-iso', () => ({
  useRoute: () => ({ params: { id: 'sp-1' } }),
  useLocation: () => ({ route: routeMock }),
}))
vi.mock('@/api', () => ({ api: { get: vi.fn(), post: vi.fn() } }))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))
vi.mock('@/baseUrl', () => ({
  basePath: '/',
  addBase: (p: string) => p,
  stripBase: (p: string) => p,
}))

const { api } = (await import('@/api')) as unknown as {
  api: { get: ReturnType<typeof vi.fn>; post: ReturnType<typeof vi.fn> }
}
const { cacheDirectoryEntries, directoryCache } = await import(
  '@/store/spaceDirectory'
)
import type { DirectoryEntry } from '@/types'

function entry(over: Partial<DirectoryEntry>): DirectoryEntry {
  return {
    space_id:           'sp-1',
    host_instance_id:   'local',
    host_display_name:  'Your household',
    host_is_paired:     true,
    name:               'Space One',
    description:        '',
    emoji:              '',
    member_count:       1,
    scope:              'public',
    join_mode:          'open',
    min_age:            0,
    already_member:     false,
    already_subscribed: false,
    ...over,
  }
}

beforeEach(() => {
  api.get.mockReset()
  api.post.mockReset()
  api.post.mockResolvedValue({})
  routeMock.mockReset()
  directoryCache.value = new Map()
})

afterEach(() => cleanup())

async function renderPage() {
  const { default: SpacePublicDetailPage } = await import(
    './SpacePublicDetailPage'
  )
  return render(<SpacePublicDetailPage />)
}

describe('SpacePublicDetailPage onPrimary', () => {
  it('local OPEN space joins immediately without a modal', async () => {
    cacheDirectoryEntries([
      entry({ host_instance_id: 'local', scope: 'public', join_mode: 'open' }),
    ])
    const { container, getByText } = await renderPage()
    await waitFor(() => getByText('Join space'))
    fireEvent.click(getByText('Join space'))
    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith(
        '/api/spaces/sp-1/join-requests',
        {},
      )
    })
    // No JoinRequestModal popped.
    expect(container.querySelector('.sh-modal, [role="dialog"]')).toBeNull()
    expect(routeMock).toHaveBeenCalledWith('/spaces/sp-1')
  })

  it('remote OPEN space sends the public_spaces join-request, no modal', async () => {
    cacheDirectoryEntries([
      entry({
        host_instance_id: 'remote-1',
        host_display_name: 'Friends',
        host_is_paired:    true,
        scope:             'global',
        join_mode:         'open',
      }),
    ])
    const { container, getByText } = await renderPage()
    await waitFor(() => getByText('Join space'))
    fireEvent.click(getByText('Join space'))
    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith(
        '/api/public_spaces/sp-1/join-request',
        { host_instance_id: 'remote-1' },
      )
    })
    expect(container.querySelector('.sh-modal, [role="dialog"]')).toBeNull()
  })

  it('INVITE-ONLY space says the content is private and offers no way in', async () => {
    cacheDirectoryEntries([
      entry({
        host_instance_id: 'remote-1',
        host_display_name: 'Friends',
        scope:            'global',
        join_mode:        'invite_only',
      }),
    ])
    const { getByText, getByRole } = await renderPage()
    await waitFor(() => getByText(/content is private/i))
    // The chip is honest about readability, not just about joining…
    expect(getByText(/Posts in this space stay private/i)).toBeTruthy()
    // …and the only CTA is disabled, so nothing can be sent.
    expect((getByRole('button') as HTMLButtonElement).disabled).toBe(true)
    expect(api.post).not.toHaveBeenCalled()
  })

  it('REQUEST space still pops the JoinRequestModal (no immediate send)', async () => {
    cacheDirectoryEntries([
      entry({ host_instance_id: 'local', scope: 'public', join_mode: 'request' }),
    ])
    const { getByText } = await renderPage()
    await waitFor(() => getByText('Request to join'))
    fireEvent.click(getByText('Request to join'))
    await waitFor(() => getByText('Send request'))
    // Modal is open; nothing sent yet.
    expect(api.post).not.toHaveBeenCalled()
  })
})
