import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, cleanup } from '@testing-library/preact'

vi.mock('@/i18n/i18n', () => ({
  t: (k: string, p?: Record<string, string>) => (p ? `${k}:${JSON.stringify(p)}` : k),
}))
const base = vi.hoisted(() => ({ prefix: '' }))
vi.mock('@/baseUrl', async (orig) => ({
  ...(await orig<object>()),
  addBase: (p: string) => `${base.prefix}${p}`,
}))

import {
  SpaceGfsNotice, connectGfsPath, isGfsNoticeDismissed, missingGfsServers,
  type GfsNoticeSpace,
} from './SpaceGfsNotice'
import { currentUser } from '@/store/auth'

const TOWN = { display_name: 'Town GFS', inbox_url: 'https://town.example', connected: true }
const CLUB = { display_name: 'Club GFS', inbox_url: 'https://club.example', connected: false }

function setViewer(isAdmin: boolean, userId = 'u1') {
  currentUser.value = {
    user_id: userId, username: 'u', display_name: 'U', is_admin: isAdmin,
  } as typeof currentUser.value
}

function space(over: Partial<GfsNoticeSpace> = {}): GfsNoticeSpace {
  return {
    id: 's-1', space_type: 'global', gfs_servers: [TOWN, CLUB], ...over,
  } as GfsNoticeSpace
}

beforeEach(() => {
  window.localStorage.clear()
  base.prefix = ''
  setViewer(true)
})
afterEach(() => cleanup())

describe('missingGfsServers', () => {
  it('lists the GFSs of a global space this household is not on', () => {
    expect(missingGfsServers(space()).map(g => g.inbox_url)).toEqual(['https://club.example'])
  })

  it('covers a private space only with the private GFS channel on', () => {
    expect(missingGfsServers(space({ space_type: 'private' }))).toEqual([])
    expect(missingGfsServers(space({
      space_type: 'private', features: { private_gfs: true } as GfsNoticeSpace['features'],
    })).map(g => g.inbox_url)).toEqual(['https://club.example'])
  })

  it('is empty for other tiers and when every GFS is connected', () => {
    expect(missingGfsServers(space({ space_type: 'public' }))).toEqual([])
    expect(missingGfsServers(space({ gfs_servers: [TOWN] }))).toEqual([])
    expect(missingGfsServers(space({ gfs_servers: null }))).toEqual([])
  })
})

describe('SpaceGfsNotice — a household standing in for the host', () => {
  it('says the GFS is needed and cannot be dismissed', () => {
    const { getByTestId, queryByTestId } = render(
      <SpaceGfsNotice space={space({ stands_in_for_host: true })} />,
    )
    expect(getByTestId('space-gfs-notice-needed').textContent)
      .toContain('space.gfs_notice.needed:{"name":"Club GFS"}')
    expect(queryByTestId('space-gfs-notice-dismiss')).toBeNull()
    expect(queryByTestId('space-gfs-notice-optional')).toBeNull()
  })

  it('stays even after an earlier dismissal of the optional notice', () => {
    render(<SpaceGfsNotice space={space()} />).getByTestId('space-gfs-notice-dismiss').click()
    cleanup()
    const { getByTestId } = render(<SpaceGfsNotice space={space({ stands_in_for_host: true })} />)
    expect(getByTestId('space-gfs-notice-needed')).toBeTruthy()
  })
})

describe('SpaceGfsNotice — any other household', () => {
  it('is optional and dismissible', () => {
    const { getByTestId, queryByTestId } = render(<SpaceGfsNotice space={space()} />)
    expect(getByTestId('space-gfs-notice-optional').textContent)
      .toContain('space.gfs_notice.optional:{"name":"Club GFS"}')
    fireEvent.click(getByTestId('space-gfs-notice-dismiss'))
    expect(queryByTestId('space-gfs-notice')).toBeNull()
  })

  it('remembers the dismissal per viewer, per space and per GFS', () => {
    const { getByTestId } = render(<SpaceGfsNotice space={space()} />)
    fireEvent.click(getByTestId('space-gfs-notice-dismiss'))
    cleanup()
    expect(isGfsNoticeDismissed('u1', 's-1', 'https://club.example')).toBe(true)
    // A fresh mount (another visit) stays quiet.
    expect(render(<SpaceGfsNotice space={space()} />).queryByTestId('space-gfs-notice')).toBeNull()
    cleanup()
    // Another space, another GFS of the same space, another person: still shown.
    expect(render(<SpaceGfsNotice space={space({ id: 's-2' })} />)
      .getByTestId('space-gfs-notice-optional')).toBeTruthy()
    cleanup()
    const other = { display_name: 'Other GFS', inbox_url: 'https://other.example', connected: false }
    const r = render(<SpaceGfsNotice space={space({ gfs_servers: [TOWN, CLUB, other] })} />)
    expect(r.getAllByTestId('space-gfs-notice-optional')).toHaveLength(1)
    expect(r.getByTestId('space-gfs-notice-optional').textContent).toContain('Other GFS')
    cleanup()
    setViewer(true, 'u2')
    expect(render(<SpaceGfsNotice space={space()} />)
      .getByTestId('space-gfs-notice-optional')).toBeTruthy()
  })

  it('a dismissal in one space does not hide the row in the next (same mount)', () => {
    const set = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    try {
      const r = render(<SpaceGfsNotice space={space()} />)
      fireEvent.click(r.getByTestId('space-gfs-notice-dismiss'))
      expect(r.queryByTestId('space-gfs-notice')).toBeNull()
      r.rerender(<SpaceGfsNotice space={space({ id: 's-2' })} />)
      expect(r.getByTestId('space-gfs-notice-optional')).toBeTruthy()
    } finally {
      set.mockRestore()
    }
  })

  it('moves focus to the next row, else to the main region, on dismiss', async () => {
    const main = document.createElement('main')
    main.id = 'main'
    main.tabIndex = -1
    document.body.appendChild(main)
    try {
      const other = { display_name: 'Other GFS', inbox_url: 'https://other.example', connected: false }
      const r = render(<SpaceGfsNotice space={space({ gfs_servers: [CLUB, other] })} />)
      fireEvent.click(r.getAllByTestId('space-gfs-notice-dismiss')[0])
      await new Promise(res => setTimeout(res, 0))
      expect(document.activeElement?.getAttribute('data-gfs')).toBe('https://other.example')
      fireEvent.click(r.getByTestId('space-gfs-notice-dismiss'))
      await new Promise(res => setTimeout(res, 0))
      expect(document.activeElement).toBe(main)
    } finally {
      main.remove()
    }
  })

  it('still works when storage is blocked', () => {
    const spy = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    const set = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    try {
      const { getByTestId, queryByTestId } = render(<SpaceGfsNotice space={space()} />)
      fireEvent.click(getByTestId('space-gfs-notice-dismiss'))
      expect(queryByTestId('space-gfs-notice')).toBeNull()
    } finally {
      spy.mockRestore()
      set.mockRestore()
    }
  })

  it('renders nothing when every GFS is connected', () => {
    expect(render(<SpaceGfsNotice space={space({ gfs_servers: [TOWN] })} />)
      .queryByTestId('space-gfs-notice')).toBeNull()
  })
})

describe('SpaceGfsNotice — connect / ask an admin', () => {
  it('a household admin gets a one-click connect to that GFS', () => {
    const { getByTestId, queryByTestId } = render(<SpaceGfsNotice space={space()} />)
    const link = getByTestId('space-gfs-notice-connect') as HTMLAnchorElement
    expect(link.getAttribute('href'))
      .toBe('/connections?connect_gfs=https%3A%2F%2Fclub.example')
    expect(queryByTestId('space-gfs-notice-ask-admin')).toBeNull()
  })

  it('the connect link keeps the ingress prefix (/api/hassio_ingress/<token>/)', () => {
    base.prefix = '/api/hassio_ingress/tok'
    const { getByTestId } = render(<SpaceGfsNotice space={space({ stands_in_for_host: true })} />)
    expect(getByTestId('space-gfs-notice-connect').getAttribute('href'))
      .toBe('/api/hassio_ingress/tok' + connectGfsPath('https://club.example'))
  })

  it('a person who is not a household admin gets the ask-an-admin hint, no button', () => {
    setViewer(false)
    const hints = {
      optional: 'space.gfs_listing.connect_hint_member:{"name":"Club GFS"}',
      needed: 'space.gfs_notice.needed_member:{"name":"Club GFS"}',
    }
    for (const [kind, hint] of Object.entries(hints)) {
      const { getByTestId, queryByTestId } = render(
        <SpaceGfsNotice space={space({ stands_in_for_host: kind === 'needed' })} />,
      )
      expect(getByTestId(`space-gfs-notice-${kind}`)).toBeTruthy()
      expect(getByTestId('space-gfs-notice-ask-admin').textContent).toBe(hint)
      expect(queryByTestId('space-gfs-notice-connect')).toBeNull()
      cleanup()
    }
  })

  it('the connect button names its GFS for a screen reader', () => {
    const link = render(<SpaceGfsNotice space={space()} />).getByTestId('space-gfs-notice-connect')
    expect(link.textContent).toBe('space.gfs_notice.connect')
    expect(link.getAttribute('aria-label')).toBe('space.gfs_notice.connect_aria:{"name":"Club GFS"}')
  })
})
