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
vi.mock('@/components/LocalAreaMap', () => ({ LocalAreaMap: () => <div data-testid="area-map" /> }))
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
const { currentUser } = await import('@/store/auth')
const { resetHouseholdHome } = await import('@/store/householdHome')
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
    const { getByText, queryByRole } = await renderPage()
    await waitFor(() => getByText(/content is private/i))
    expect(getByText(/Only members can read this space/i)).toBeTruthy()
    expect(getByText(/A member has to invite you/i)).toBeTruthy()
    // …no action at all — a neutral line says how people get in.
    expect(queryByRole('button')).toBeNull()
    expect(getByText('Members can invite you with a link.')).toBeTruthy()
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

describe('SpacePublicDetailPage — a GFS-listed space whose household we never paired with', () => {
  // Households met through a GFS never talk directly: never suggest
  // pairing. Follow when the space takes followers, otherwise a neutral
  // line and no action — for admins and members alike.
  const far = (allow: boolean) => entry({
    host_instance_id:  'remote-9',
    host_display_name: 'Far Household',
    host_is_paired:    false,
    scope:             'global',
    join_mode:         'request',
    allow_subscribers: allow,
  })

  it('offers Follow when the space takes followers', async () => {
    cacheDirectoryEntries([far(true)])
    const { container, getByText } = await renderPage()
    await waitFor(() => getByText('Follow'))
    fireEvent.click(getByText('Follow'))
    await waitFor(() => expect(api.post).toHaveBeenCalledWith('/api/spaces/sp-1/subscribe', {}))
    expect(container.textContent).not.toMatch(/Connect|Connections|pair/i)
  })

  it('otherwise says members can invite you, with no action', async () => {
    cacheDirectoryEntries([far(false)])
    const { container, getByText, queryByRole } = await renderPage()
    await waitFor(() => getByText('Members can invite you with a link.'))
    expect(queryByRole('button')).toBeNull()
    expect(container.textContent).not.toMatch(/Connect|Connections|pair/i)
  })
})

describe('SpacePublicDetailPage cold link (no directory entry)', () => {
  it("a caller with no seat reads the member count off the space's landing fields", async () => {
    // ``/members`` is for seats only — the landing fields carry the count.
    api.get.mockImplementation(async (url: string) => {
      if (url === '/api/spaces/sp-1') {
        return {
          id: 'sp-1', name: 'Chess club', description: '', emoji: '',
          space_type: 'public', join_mode: 'request', category: 'general',
          features: { allow_subscribers: true }, member_count: 7,
        }
      }
      throw Object.assign(new Error('Not a space member.'), { status: 403 })
    })
    const { container, getByText } = await renderPage()
    await waitFor(() => getByText('Chess club'))
    expect(container.textContent).toContain('7 members')
    expect(api.get).not.toHaveBeenCalledWith('/api/spaces/sp-1/members')
  })

  it('a household space you are not in: landing, then Join seats you', async () => {
    // Household scope is discoverable to every local user: the server
    // answers a neighbour with the landing fields, and Join on an open
    // space seats them at once.
    api.get.mockImplementation(async (url: string) => {
      if (url === '/api/spaces/sp-1') {
        return {
          id: 'sp-1', name: 'Hottingen Community Choir',
          description: 'We sing on Tuesdays.', emoji: '🎶',
          space_type: 'household', join_mode: 'open', category: 'general',
          features: { allow_subscribers: false }, member_count: 1,
        }
      }
      throw Object.assign(new Error('Not a space member.'), { status: 403 })
    })
    api.post.mockResolvedValue({ request_id: null, joined: true })
    const { container, getByText } = await renderPage()
    await waitFor(() => getByText('Hottingen Community Choir'))
    expect(container.textContent).toContain('Your household')
    expect(container.textContent).toContain('1 member')
    expect(container.textContent).toContain('We sing on Tuesdays.')
    // Everyone at home is a member of a household space: no join-mode chip.
    expect(container.textContent).not.toContain('Open to join')
    fireEvent.click(getByText('Join space'))
    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith('/api/spaces/sp-1/join-requests', {})
    })
    expect(routeMock).toHaveBeenCalledWith('/spaces/sp-1')
  })
})

describe('SpacePublicDetailPage — a member opening their own space (cold link)', () => {
  // ``GET /api/spaces/{id}`` answers a seated caller with the full space,
  // ``my_role`` and ``member_count`` included — no roster read needed.
  function seated(spaceType: string, myRole: string, joinMode = 'invite_only') {
    api.get.mockImplementation(async (url: string) => {
      if (url === '/api/spaces/sp-1') {
        return {
          id: 'sp-1', name: 'Vizeli Family', description: 'Our family space', emoji: '🏡',
          space_type: spaceType, join_mode: joinMode, category: 'general',
          features: { allow_subscribers: false }, my_role: myRole, member_count: 2,
        }
      }
      throw new Error(`unexpected ${url}`)
    })
  }

  beforeEach(() => {
    currentUser.value = {
      user_id: 'u1', username: 'u', display_name: 'U', is_admin: false,
      picture_url: null, bio: null, is_new_member: false,
    } as unknown as typeof currentUser.value
  })
  afterEach(() => { currentUser.value = null })

  it('a member of a private space sees "You\'re a member" and an Open space action, not "Invite required"', async () => {
    seated('private', 'owner')
    const { container, getByText } = await renderPage()
    await waitFor(() => getByText('Vizeli Family'))
    expect(container.textContent).toContain("You're a member")
    expect(container.textContent).not.toContain('Invite required')
    // Nothing about how to get in, or that only members can read it.
    expect(container.textContent).not.toContain('Only members can read')
    expect(container.textContent).not.toContain('has to invite you')
    const open = getByText('Open space').closest('button')!
    expect(open.disabled).toBe(false)
    fireEvent.click(open)
    expect(routeMock).toHaveBeenCalledWith('/spaces/sp-1')
    expect(container.textContent).toContain('2 members')
  })

  it.each([
    ['private', 'Private'],
    ['household', 'Your household'],
    ['public', 'Public'],
    ['global', 'Global'],
  ])('a %s space is labelled "%s"', async (spaceType, label) => {
    seated(spaceType, 'member')
    const { container, getByText } = await renderPage()
    await waitFor(() => getByText('Vizeli Family'))
    const chip = container.querySelector('.sh-scope-chip')!
    expect(chip.textContent).toContain(label)
    for (const other of ['Private', 'Your household', 'Public', 'Global']) {
      if (other !== label) expect(chip.textContent).not.toContain(other)
    }
  })

  it('a follower (subscriber seat) is not called a member', async () => {
    seated('global', 'subscriber', 'request')
    const { container, getByText } = await renderPage()
    await waitFor(() => getByText('Vizeli Family'))
    expect(container.textContent).not.toContain("You're a member")
    expect(container.querySelector('.sh-subscribed-pill')).not.toBeNull()
    expect(getByText('Request to join')).toBeTruthy()
  })
})

describe('SpacePublicDetailPage — a global space\'s place pin', () => {
  beforeEach(() => { resetHouseholdHome() })

  it('says roughly how far away it is, from our own home, and that its rules apply', async () => {
    api.get.mockImplementation((url: string) => Promise.resolve(
      url === '/api/friends' ? { instance: { home_lat: 47.37, home_lon: 8.563 } } : {},
    ))
    cacheDirectoryEntries([entry({
      scope: 'global', host_instance_id: 'iid-far', host_is_paired: true, join_mode: 'request',
      lat: 46.9481, lon: 7.4474,
    })])
    const { getByTestId, getByText } = await renderPage()
    await waitFor(() => expect(getByTestId('place-distance').textContent).toContain('about 95 km away'))
    expect(getByText('You’re expected to follow this space’s rules.')).toBeTruthy()
  })

  it('no pin (or no home): no distance', async () => {
    api.get.mockImplementation(() => Promise.resolve({}))
    cacheDirectoryEntries([entry({ scope: 'global', host_instance_id: 'iid-far' })])
    const { queryByTestId, container } = await renderPage()
    await waitFor(() => expect(container.querySelector('h1')).not.toBeNull())
    expect(queryByTestId('place-distance')).toBeNull()
  })
})

describe('SpacePublicDetailPage — local community', () => {
  const AREA = { lat: 47.3769, lon: 8.5417, radius_km: 5 }
  const home = (h: { lat: number; lon: number } | null) => {
    api.get.mockImplementation((url: string) => Promise.resolve(
      url === '/api/friends' ? { instance: { home_lat: h?.lat ?? null, home_lon: h?.lon ?? null } } : {},
    ))
  }
  beforeEach(() => {
    resetHouseholdHome()
    currentUser.value = null
  })

  const LC = (in_area: boolean | null) => ({ radius_km: 5, in_area })

  it('a space this household hosts: the exact area, a map and "In your area"', async () => {
    home({ lat: 47.392, lon: 8.56 })
    cacheDirectoryEntries([entry({ host_instance_id: 'local', local_area: AREA })])
    const { getByText, getByTestId, container } = await renderPage()
    await waitFor(() => getByText('In your area'))
    expect(container.textContent).toContain('Within 5 km of 47.3769° N, 8.5417° E')
    expect(getByTestId('area-map')).toBeTruthy()
    expect(container.querySelector('.sh-local-area-chip')?.textContent).toContain('Within 5 km')
    expect(getByText('Join space')).toBeTruthy()
  })

  it('a paired household\'s community: no map, no centre — the radius and the host\'s verdict', async () => {
    home({ lat: 47.392, lon: 8.56 })
    cacheDirectoryEntries([entry({
      host_instance_id: 'iid-b', host_display_name: 'Keller household', local_community: LC(true),
      home_shared_with_host: true,
    })])
    const { getByText, queryByTestId, getByTestId, container } = await renderPage()
    await waitFor(() => getByText('In your area'))
    expect(getByTestId('local-area-where').textContent)
      .toBe('Local community · within 5 km of the host’s chosen centre')
    expect(queryByTestId('area-map')).toBeNull()
    expect(container.textContent).not.toMatch(/°\s*[NSEW]/)
    expect(getByText('Join space')).toBeTruthy()
  })

  it('near the edge: unsure, offers the join and says the host will check', async () => {
    home({ lat: 47.392, lon: 8.56 })
    cacheDirectoryEntries([entry({
      host_instance_id: 'iid-b',
      local_community: { radius_km: 5, in_area: null, area_edge: true },
      home_shared_with_host: true,
    })])
    const { getByText, container } = await renderPage()
    await waitFor(() => getByText('Near the edge — ask to join and the host will check'))
    expect(getByText('Join space')).toBeTruthy()
    expect(container.textContent).toContain('near the edge of this area')
    expect(container.querySelector('.sh-local-area-badge--edge')).toBeTruthy()
  })

  it('outside: says so and offers no join the host would refuse', async () => {
    home({ lat: 46.9481, lon: 7.4474 })
    cacheDirectoryEntries([entry({ host_instance_id: 'iid-b', local_community: LC(false), home_shared_with_host: true })])
    const { getByText, queryByText, container } = await renderPage()
    await waitFor(() => getByText('Outside your area'))
    expect(queryByText('Join space')).toBeNull()
    expect(container.textContent).toContain("Your household's home is outside this area")
    // Not the "ask for an invite link" line — the area is the reason.
    expect(container.querySelector('.sh-space-public__invite-note')).toBeNull()
  })

  it('home not shared with the host: names the host and points an admin at Connections', async () => {
    home({ lat: 47.392, lon: 8.56 })
    currentUser.value = { user_id: 'u1', username: 'u', display_name: 'U', is_admin: true } as typeof currentUser.value
    cacheDirectoryEntries([entry({
      host_instance_id: 'iid-b', host_display_name: 'Keller household', local_community: LC(null),
      home_shared_with_host: false,
    })])
    const { getByText, queryByText } = await renderPage()
    await waitFor(() => getByText('Share your home with Keller household to check'))
    expect(queryByText('Join space')).toBeNull()
    const link = getByText('Open Connections') as HTMLAnchorElement
    // Ingress: the href goes through addBase (mocked to identity here).
    expect(link.getAttribute('href')).toBe('/connections')
    fireEvent.click(link)
    expect(routeMock).toHaveBeenCalledWith('/connections')
  })

  it('no home set for this household: says so', async () => {
    home(null)
    cacheDirectoryEntries([entry({ host_instance_id: 'local', local_area: AREA })])
    const { getByText, queryByText } = await renderPage()
    await waitFor(() => getByText("Your home location isn't set"))
    expect(queryByText('Join space')).toBeNull()
  })

  it('a seated member sees the space opens, without a verdict', async () => {
    home(null)
    cacheDirectoryEntries([entry({ host_instance_id: 'local', local_area: AREA, already_member: true })])
    const { getByText, queryByTestId } = await renderPage()
    await waitFor(() => getByText('Open space'))
    expect(queryByTestId('local-area-badge')).toBeNull()
  })
})
