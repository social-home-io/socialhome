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
    // Readability opt-in — independent of join_mode. Concrete, because a
    // directory row always carries one.
    allow_subscribers:  true,
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

  it('a space that takes no followers says the content is private', async () => {
    // The readability claim is keyed on allow_subscribers, NOT on the join
    // mode: this space is invite-only AND unreadable, and the page says both.
    cacheDirectoryEntries([
      entry({
        host_instance_id:  'remote-1',
        host_display_name: 'Friends',
        scope:             'global',
        join_mode:         'invite_only',
        allow_subscribers: false,
      }),
    ])
    const { getByText, getByRole } = await renderPage()
    await waitFor(() => getByText(/content is private/i))
    expect(getByText(/Only members can read this space/i)).toBeTruthy()
    expect(getByText(/A member has to invite you/i)).toBeTruthy()
    // …and the only CTA is disabled, so nothing can be sent.
    expect((getByRole('button') as HTMLButtonElement).disabled).toBe(true)
    expect(api.post).not.toHaveBeenCalled()
  })

  it('an INVITE-ONLY space that allows followers is NOT called private', async () => {
    // The broadcast shape: invited people post, anyone may read along. The
    // old model would have wrongly called this private.
    cacheDirectoryEntries([
      entry({
        host_instance_id:  'remote-1',
        host_display_name: 'Friends',
        scope:             'global',
        join_mode:         'invite_only',
        allow_subscribers: true,
      }),
    ])
    const { getByText, queryByText } = await renderPage()
    await waitFor(() => getByText(/Invite-only/))
    expect(queryByText(/content is private/i)).toBeNull()
    expect(queryByText(/Only members can read this space/i)).toBeNull()
  })

  it('an OPEN-to-join space with no followers IS called private', async () => {
    // …and the mirror image: anyone may join, nobody may merely read.
    cacheDirectoryEntries([
      entry({
        host_instance_id:  'remote-1',
        host_display_name: 'Friends',
        scope:             'global',
        join_mode:         'open',
        allow_subscribers: false,
      }),
    ])
    const { getByText } = await renderPage()
    await waitFor(() => getByText(/content is private/i))
    expect(getByText(/Only members can read this space/i)).toBeTruthy()
    expect(getByText(/Anyone can join this space/i)).toBeTruthy()
    // Joining is still offered — readability never gated the CTA.
    expect(getByText('Join space')).toBeTruthy()
  })

  it('APPROVAL-REQUIRED + private keeps the ask-to-join CTA live', async () => {
    cacheDirectoryEntries([
      entry({
        host_instance_id:  'remote-1',
        host_display_name: 'Friends',
        scope:             'global',
        join_mode:         'request',
        allow_subscribers: false,
      }),
    ])
    const { getByText, getByRole } = await renderPage()
    await waitFor(() => getByText(/content is private/i))
    expect(getByText(/Only members can read this space/i)).toBeTruthy()
    // How you get in is the part that differs from invite-only…
    expect(getByText(/You can ask to join/i)).toBeTruthy()
    // …and the CTA stays live, unlike the disabled invite-only one.
    const cta = getByRole('button') as HTMLButtonElement
    expect(cta.textContent).toBe('Request to join')
    expect(cta.disabled).toBe(false)
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
