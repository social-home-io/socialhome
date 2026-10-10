import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor, cleanup } from '@testing-library/preact'

vi.mock('@/i18n/i18n', () => ({
  t: (k: string, p?: Record<string, string>) => (p ? `${k}:${JSON.stringify(p)}` : k),
}))
vi.mock('@/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api')>()),
  api: { get: vi.fn(), put: vi.fn(), post: vi.fn(), delete: vi.fn() },
}))
vi.mock('./Toast', () => ({ showToast: vi.fn() }))

import { SpaceGfsListing, hasGfsServersToShow, publicSpaceUrl } from './SpaceGfsListing'
import { api, ApiError as RealApiError } from '@/api'
import { showToast } from './Toast'
import { currentUser } from '@/store/auth'
import type { Space, SpaceGfsServersView } from '@/types'

const apiMock = api as unknown as Record<'get' | 'put' | 'post' | 'delete', ReturnType<typeof vi.fn>>

function setViewer(isAdmin: boolean) {
  currentUser.value = {
    user_id: 'u1', username: 'u', display_name: 'U', is_admin: isAdmin,
  } as typeof currentUser.value
}

function space(over: Partial<Space> = {}): Space {
  return {
    id: 's-1', name: 'World', space_type: 'global', join_mode: 'open',
    ...over,
  } as Space
}

const TOWN = { gfs_id: 'g-town', display_name: 'Town GFS', inbox_url: 'https://town.example' }
const CLUB = { gfs_id: 'g-club', display_name: 'Club GFS', inbox_url: 'https://club.example/' }

/** The owner's view as the server builds it: unless a test spells out
 *  ``listings``, they are the listed ``available`` rows (active
 *  connections). */
function ownerView(over: Partial<SpaceGfsServersView> = {}): SpaceGfsServersView {
  const available = over.available ?? [
    { ...TOWN, listed: true, listing_status: 'active' },
    { ...CLUB, listed: false, listing_status: null },
  ]
  return {
    space_type: 'global',
    gfs_ids: ['g-town'],
    max: 8,
    unlisted: false,
    servers: [{ display_name: 'Town GFS', inbox_url: 'https://town.example', connected: true }],
    listings: available.filter(a => a.listed).map(a => ({
      gfs_id: a.gfs_id,
      display_name: a.display_name,
      inbox_url: a.inbox_url,
      connection_status: 'active' as const,
      listing_status: a.listing_status ?? 'active',
    })),
    ...over,
    available,
  }
}

const flush = () => new Promise(r => setTimeout(r, 0))

function renderOwner(s: Space = space()) {
  return render(<SpaceGfsListing space={s} isOwner isRemoteSpace={false} />)
}

beforeEach(() => {
  cleanup()
  for (const m of Object.values(apiMock)) m.mockReset()
  vi.mocked(showToast).mockReset()
  setViewer(true)
})

describe('SpaceGfsListing — by tier', () => {
  it('a public space gets the "connected households only" note and no Publish buttons', () => {
    const { queryByTestId, container } = render(
      <SpaceGfsListing space={space({ space_type: 'public' })} isOwner isRemoteSpace={false} />,
    )
    expect(queryByTestId('gfs-public-note')!.textContent).toBe('space.gfs_listing.public_note')
    expect(container.querySelectorAll('button')).toHaveLength(0)
    // Nothing is fetched: a public space never touches a GFS.
    expect(apiMock.get).not.toHaveBeenCalled()
  })

  it.each(['private', 'household'] as const)(
    'a %s space says only global spaces are listed', (tier) => {
      const { queryByTestId } = render(
        <SpaceGfsListing space={space({ space_type: tier })} isOwner isRemoteSpace={false} />,
      )
      expect(queryByTestId('gfs-publish-tier-note')).toBeTruthy()
      expect(apiMock.get).not.toHaveBeenCalled()
    })

  it('renders nothing on a remote stub of a private space', () => {
    const { container } = render(
      <SpaceGfsListing space={space({ space_type: 'private' })} isOwner={false} isRemoteSpace />,
    )
    expect(container.innerHTML).toBe('')
  })
})

describe('SpaceGfsListing — the owner edits the list', () => {
  it('lists the chosen GFSs with status, public link and Remove', async () => {
    apiMock.get.mockResolvedValue(ownerView())
    const { getByTestId, queryByTestId } = renderOwner()
    await flush()
    expect(apiMock.get).toHaveBeenCalledWith('/api/spaces/s-1/gfs-servers')
    const row = getByTestId('gfs-row-g-town')
    expect(row.textContent).toContain('Town GFS')
    expect(row.textContent).toContain('space.gfs_listing.listed')
    expect(row.querySelector('a')!.getAttribute('href')).toBe('https://town.example/spaces/s-1')
    expect(row.querySelector('button[aria-label]')!.getAttribute('aria-label'))
      .toBe('space.gfs_listing.remove_aria:{"name":"Town GFS"}')
    // The not-listed connection is offered in the Add picker, not as a row.
    expect(queryByTestId('gfs-row-g-club')).toBeNull()
    const select = getByTestId('gfs-add-select') as HTMLSelectElement
    expect([...select.options].map(o => o.value)).toEqual(['g-club'])
    expect(queryByTestId('gfs-unlisted-banner')).toBeNull()
  })

  it('a pending listing shows the review hint and no public link', async () => {
    apiMock.get.mockResolvedValue(ownerView({
      available: [{ ...TOWN, listed: true, listing_status: 'pending' }],
    }))
    const { getByTestId } = renderOwner()
    await flush()
    const row = getByTestId('gfs-row-g-town')
    expect(row.textContent).toContain('space.publish_pending')
    expect(row.textContent).toContain('space.publish_pending_hint')
    expect(row.querySelector('.sh-federation-public-url')).toBeNull()
  })

  it('Add POSTs the one picked GFS (an atomic add, never a full replace), then reloads', async () => {
    apiMock.get.mockResolvedValueOnce(ownerView()).mockResolvedValueOnce(ownerView({
      available: [
        { ...TOWN, listed: true, listing_status: 'active' },
        { ...CLUB, listed: true, listing_status: 'active' },
      ],
    }))
    apiMock.post.mockResolvedValue({
      space_id: 's-1', gfs_connection_id: 'g-club', published_at: '', status: 'active',
    })
    const { getByText, findByTestId } = renderOwner()
    await flush()
    fireEvent.click(getByText('space.gfs_listing.add'))
    await waitFor(() => expect(apiMock.post).toHaveBeenCalledWith('/api/spaces/s-1/publish/g-club'))
    // Never a PUT of [...listed, new]: that would drop listings this view
    // doesn't show (held connections) and undo another tab's change.
    expect(apiMock.put).not.toHaveBeenCalled()
    expect(showToast).toHaveBeenCalledWith('space.gfs_listing.added:{"name":"Club GFS"}', 'success')
    // Every connection lists it now: the picker gives way to a note.
    expect(await findByTestId('gfs-all-listed')).toBeTruthy()
  })

  it('a refused add shows the server sentence, never a success', async () => {
    apiMock.get.mockResolvedValue(ownerView())
    apiMock.post.mockRejectedValue(new Error('The GFS could not be reached.'))
    const { getByText } = renderOwner()
    await flush()
    fireEvent.click(getByText('space.gfs_listing.add'))
    await waitFor(() => expect(showToast).toHaveBeenCalledWith(
      'The GFS could not be reached.', 'error',
    ))
    expect(showToast).not.toHaveBeenCalledWith(expect.stringContaining('added'), 'success')
  })

  it('a refused add with no message names the GFS', async () => {
    apiMock.get.mockResolvedValue(ownerView())
    apiMock.post.mockRejectedValue(new Error(''))
    const { getByText } = renderOwner()
    await flush()
    fireEvent.click(getByText('space.gfs_listing.add'))
    await waitFor(() => expect(showToast).toHaveBeenCalledWith(
      'space.gfs_listing.add_failed:{"name":"Club GFS"}', 'error',
    ))
  })

  it('Remove withdraws one listing straight away, then focuses the Add picker', async () => {
    const two = ownerView({
      available: [
        { ...TOWN, listed: true, listing_status: 'active' },
        { ...CLUB, listed: true, listing_status: 'active' },
      ],
    })
    apiMock.get.mockResolvedValueOnce(two).mockResolvedValue(ownerView())
    apiMock.delete.mockResolvedValue(undefined)
    const { getByTestId } = renderOwner()
    await flush()
    fireEvent.click(getByTestId('gfs-row-g-club').querySelector('button[aria-label]')!)
    await waitFor(() => expect(apiMock.delete).toHaveBeenCalledWith('/api/spaces/s-1/publish/g-club'))
    expect(showToast).toHaveBeenCalledWith('space.gfs_listing.removed:{"name":"Club GFS"}', 'success')
    // The clicked button is gone with its row; focus lands on the picker.
    await waitFor(() => expect(document.activeElement).toBe(getByTestId('gfs-add-select')))
  })

  it('stays busy until the fresh list is in — a second click sends no second DELETE', async () => {
    const two = ownerView({
      available: [
        { ...TOWN, listed: true, listing_status: 'active' },
        { ...CLUB, listed: true, listing_status: 'active' },
      ],
    })
    let release: (v: SpaceGfsServersView) => void = () => {}
    apiMock.get.mockResolvedValueOnce(two)
      .mockImplementationOnce(() => new Promise(r => { release = r }))
    apiMock.delete.mockResolvedValue(undefined)
    const { getByTestId } = renderOwner()
    await flush()
    const btn = getByTestId('gfs-row-g-club').querySelector('button[aria-label]') as HTMLButtonElement
    fireEvent.click(btn)
    await waitFor(() => expect(apiMock.delete).toHaveBeenCalledTimes(1))
    // The reload is still in flight: the row's button is disabled/spinning.
    fireEvent.click(btn)
    fireEvent.click(getByTestId('gfs-row-g-town').querySelector('button[aria-label]')!)
    expect(apiMock.delete).toHaveBeenCalledTimes(1)
    release(ownerView())
    await flush()
  })

  it('lists a listing held over an inactive connection, and Remove still works', async () => {
    // A suspended connection is absent from ``available`` but its listing
    // is in ``listings``: the owner can see and withdraw it.
    const HELD = { gfs_id: 'g-held', display_name: 'Held GFS', inbox_url: 'https://held.example' }
    apiMock.get.mockResolvedValue(ownerView({
      gfs_ids: ['g-held', 'g-town'],
      listings: [
        { ...HELD, connection_status: 'suspended', listing_status: 'active' },
        { ...TOWN, connection_status: 'active', listing_status: 'active' },
      ],
    }))
    apiMock.delete.mockResolvedValue(undefined)
    const { getByTestId, queryByTestId } = renderOwner()
    await flush()
    const row = getByTestId('gfs-row-g-held')
    expect(row.textContent).toContain('space.gfs_listing.connection_inactive')
    expect(getByTestId('gfs-row-inactive-g-held').textContent)
      .toBe('space.gfs_listing.connection_inactive_hint')
    // No public link for a listing nobody keeps up to date.
    expect(row.querySelector('.sh-federation-public-url')).toBeNull()
    expect(queryByTestId('gfs-row-inactive-g-town')).toBeNull()
    fireEvent.click(row.querySelector('button[aria-label]')!)
    await waitFor(() => expect(apiMock.delete).toHaveBeenCalledWith('/api/spaces/s-1/publish/g-held'))
  })

  it('a listing on an inactive connection counts toward the last-one confirm', async () => {
    apiMock.get.mockResolvedValue(ownerView({
      gfs_ids: ['g-held'],
      listings: [{ gfs_id: 'g-held', display_name: 'Held GFS', inbox_url: 'https://held.example',
        connection_status: 'pending', listing_status: 'active' }],
      available: [{ ...TOWN, listed: false, listing_status: null }],
    }))
    const { getByTestId, container } = renderOwner()
    await flush()
    // The active connection is offered for adding; the held one is a row.
    expect([...(getByTestId('gfs-add-select') as HTMLSelectElement).options].map(o => o.value))
      .toEqual(['g-town'])
    fireEvent.click(getByTestId('gfs-row-g-held').querySelector('button[aria-label]')!)
    expect(container.querySelector('[role="dialog"]')).toBeTruthy()
  })

  it('a 404 on Remove means it is already gone — success, not an error', async () => {
    const two = ownerView({
      available: [
        { ...TOWN, listed: true, listing_status: 'active' },
        { ...CLUB, listed: true, listing_status: 'active' },
      ],
    })
    apiMock.get.mockResolvedValue(two)
    apiMock.delete.mockRejectedValue(new RealApiError(404, '/api/spaces/s-1/publish/g-club', null))
    const { getByTestId } = renderOwner()
    await flush()
    fireEvent.click(getByTestId('gfs-row-g-club').querySelector('button[aria-label]')!)
    await waitFor(() => expect(showToast).toHaveBeenCalledWith(
      'space.gfs_listing.removed:{"name":"Club GFS"}', 'success',
    ))
    expect(showToast).not.toHaveBeenCalledWith(expect.anything(), 'error')
  })

  it('removing the last GFS asks first, and Cancel keeps it', async () => {
    apiMock.get.mockResolvedValue(ownerView())
    const { getByTestId, container } = renderOwner()
    await flush()
    fireEvent.click(getByTestId('gfs-row-g-town').querySelector('button[aria-label]')!)
    const dialog = container.querySelector('[role="dialog"]') as HTMLElement
    expect(dialog.textContent).toContain('space.gfs_listing.remove_last_title')
    fireEvent.click([...dialog.querySelectorAll('button')].find(b => b.textContent === 'common.cancel')!)
    expect(apiMock.delete).not.toHaveBeenCalled()
    fireEvent.click(getByTestId('gfs-row-g-town').querySelector('button[aria-label]')!)
    const again = container.querySelector('[role="dialog"]') as HTMLElement
    fireEvent.click(again.querySelector('.sh-btn--danger')!)
    await waitFor(() => expect(apiMock.delete).toHaveBeenCalledWith('/api/spaces/s-1/publish/g-town'))
  })

  it('"global, not listed" shows the owner banner above the picker', async () => {
    apiMock.get.mockResolvedValue(ownerView({
      unlisted: true, gfs_ids: [], servers: [],
      available: [{ ...TOWN, listed: false, listing_status: null }],
    }))
    const { findByTestId, getByTestId } = renderOwner()
    const banner = await findByTestId('gfs-unlisted-banner')
    expect(banner.textContent).toContain('space.gfs_listing.unlisted_owner')
    expect(banner.getAttribute('role')).toBe('status')
    expect(getByTestId('gfs-add-select')).toBeTruthy()
  })

  it('at the cap the picker gives way to the limit', async () => {
    apiMock.get.mockResolvedValue(ownerView({ max: 1 }))
    const { findByTestId, queryByTestId } = renderOwner()
    expect((await findByTestId('gfs-at-max')).textContent)
      .toBe('error.gfs_selection.too_many:{"max":"1"}')
    expect(queryByTestId('gfs-add-select')).toBeNull()
  })

  it('with no GFS connected, a non-admin owner is told to ask an admin', async () => {
    setViewer(false)
    apiMock.get.mockResolvedValue(ownerView({ available: [], unlisted: true, gfs_ids: [] }))
    const { findByTestId } = renderOwner()
    const none = await findByTestId('gfs-no-connections')
    expect(none.textContent).toBe('space.gfs_listing.none_member')
    expect(none.querySelector('a')).toBeNull()
  })

  it('a failed load offers Retry', async () => {
    apiMock.get.mockRejectedValueOnce(new Error('boom')).mockResolvedValueOnce(ownerView())
    const { findByTestId, getByText } = renderOwner()
    await findByTestId('gfs-listing-load-failed')
    fireEvent.click(getByText('common.retry'))
    expect(await findByTestId('gfs-row-g-town')).toBeTruthy()
  })
})

describe('SpaceGfsListing — everyone else sees the list read-only', () => {
  const listed = space({
    gfs_servers: [
      { display_name: 'Town GFS', inbox_url: 'https://town.example', connected: true },
      { display_name: 'Club GFS', inbox_url: 'https://club.example', connected: false },
    ],
  })

  it('a non-owner admin sees the list, no buttons, and a hint per unconnected GFS', () => {
    const { getByTestId, getAllByTestId, container } = render(
      <SpaceGfsListing space={listed} isOwner={false} isRemoteSpace={false} />,
    )
    expect(getByTestId('gfs-listing-readonly').textContent).toContain('space.gfs_listing.intro_member')
    expect(container.querySelectorAll('button')).toHaveLength(0)
    expect(container.textContent).toContain('space.gfs_listing.connected')
    const hints = getAllByTestId('gfs-connect-hint')
    expect(hints).toHaveLength(1)
    expect(hints[0].textContent).toContain('space.gfs_listing.connect_hint:{"name":"Club GFS"}')
    expect(hints[0].querySelector('a')!.getAttribute('href')).toBe('/connections')
    // The owner's picker never loads for a non-owner.
    expect(apiMock.get).not.toHaveBeenCalled()
  })

  it('the owner on a remote stub (another household hosts it) is read-only too', () => {
    const { getByTestId } = render(
      <SpaceGfsListing space={listed} isOwner isRemoteSpace />,
    )
    expect(getByTestId('gfs-listing-readonly')).toBeTruthy()
  })

  it('a non-admin is told to ask an admin, without a link', () => {
    setViewer(false)
    const { getByTestId } = render(
      <SpaceGfsListing space={listed} isOwner={false} isRemoteSpace />,
    )
    const hint = getByTestId('gfs-connect-hint')
    expect(hint.textContent).toBe('space.gfs_listing.connect_hint_member:{"name":"Club GFS"}')
    expect(hint.querySelector('a')).toBeNull()
  })

  it('"global, not listed" shows the member banner', () => {
    const { getByTestId } = render(
      <SpaceGfsListing space={space({ gfs_servers: [], gfs_unlisted: true })}
        isOwner={false} isRemoteSpace />,
    )
    expect(getByTestId('gfs-unlisted-banner').textContent)
      .toContain('space.gfs_listing.unlisted_member')
  })

  it('shows nothing while no list is known yet', () => {
    const s = space({ gfs_servers: null, gfs_unlisted: false })
    expect(hasGfsServersToShow(s)).toBe(false)
    const { container } = render(<SpaceGfsListing space={s} isOwner={false} isRemoteSpace />)
    expect(container.innerHTML).toBe('')
  })
})

describe('publicSpaceUrl', () => {
  it('joins the GFS root and the space page without a double slash', () => {
    expect(publicSpaceUrl('https://g.example/', 's')).toBe('https://g.example/spaces/s')
    expect(publicSpaceUrl('https://g.example', 's')).toBe('https://g.example/spaces/s')
  })
})

describe('SpaceGfsListing — links under the HA ingress prefix', () => {
  // ``baseUrl.ts`` reads ``document.baseURI`` once at module load, so set
  // the ingress ``<base href>`` first and re-import the component.
  let baseEl: HTMLBaseElement
  beforeEach(() => {
    baseEl = document.createElement('base')
    baseEl.href = '/api/hassio_ingress/tok/'
    document.head.prepend(baseEl)
    vi.resetModules()
  })
  afterEach(() => {
    baseEl.remove()
    vi.resetModules()
  })

  it('the "connect to this GFS" link keeps the ingress prefix (/api/hassio_ingress/<token>/)', async () => {
    const mod = await import('./SpaceGfsListing')
    const auth = await import('@/store/auth')
    auth.currentUser.value = {
      user_id: 'u1', username: 'u', display_name: 'U', is_admin: true,
    } as typeof auth.currentUser.value
    const { getByTestId } = render(
      <mod.SpaceGfsListing
        space={space({
          gfs_servers: [{ display_name: 'Club GFS', inbox_url: 'https://club.example', connected: false }],
        })}
        isOwner={false}
        isRemoteSpace
      />,
    )
    expect(getByTestId('gfs-connect-hint').querySelector('a')!.getAttribute('href'))
      .toBe('/api/hassio_ingress/tok/connections')
  })
})
