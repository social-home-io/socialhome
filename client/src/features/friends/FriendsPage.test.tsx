import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, waitFor, fireEvent, within } from '@testing-library/preact'

vi.mock('@/api', () => {
  const m = { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() }
  return { api: m, _mock: m }
})

vi.mock('preact-iso', () => ({
  useLocation: () => ({ route: vi.fn() }),
}))

vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))

const authUser = vi.hoisted(() => ({
  value: { user_id: 'u-me', username: 'pascal', display_name: 'Pascal', is_admin: true } as
    { user_id: string; username: string; display_name: string; is_admin: boolean;
      status?: { emoji: string | null; text: string | null; expires_at: string | null } | null },
}))
vi.mock('@/store/auth', () => ({ currentUser: authUser }))

// LocationMap pulls in Leaflet which doesn't initialise cleanly in
// jsdom — stub it so the tests assert on the page, not map internals.
vi.mock('@/components/LocationMap', () => ({
  LocationMap: ({ markers }: { markers: { id: string; sub_label?: string }[] }) => (
    <div data-testid="map" data-markers={markers.length}>
      {markers.map((m) => <span key={m.id} class="map-marker" data-id={m.id}>{m.sub_label}</span>)}
    </div>
  ),
}))

vi.mock('@/components/Avatar', () => ({
  Avatar: ({ name, online }: { name: string; online?: string | null }) =>
    <span class="avatar" data-online={online ?? ''}>{name}</span>,
}))

const openPairingSpy = vi.fn()
vi.mock('@/components/PairingFlow', () => ({
  openPairing: (...a: unknown[]) => openPairingSpy(...a),
  PairingFlow: () => null,
}))

const openStatusEditorSpy = vi.fn()
vi.mock('@/components/StatusEditor', () => ({
  openStatusEditor: () => openStatusEditorSpy(),
}))

const openPersonSpy = vi.fn()
vi.mock('@/components/PersonSheet', () => ({
  openPerson: (...args: unknown[]) => openPersonSpy(...args),
}))

import FriendsPage from './FriendsPage'
import { api } from '@/api'
import { blockedUserIds, blockedUsers, _resetBlocksForTest } from '@/store/blocks'
import { householdAdmins } from '@/features/connections/connectionsData'

const apiGet = (api as unknown as { get: ReturnType<typeof vi.fn> }).get

type Row = Record<string, unknown>

function local(id: string, name: string, extra: Row = {}): Row {
  return { user_id: id, username: id, display_name: name, picture_url: null, ...extra }
}
function remote(id: string, inst: string, name: string, extra: Row = {}): Row {
  return { user_id: id, instance_id: inst, remote_username: id, display_name: name, picture_url: null, ...extra }
}
function hh(id: string, name: string, members: Row[], extra: Row = {}): Row {
  return {
    instance_id: id, display_name: name, home_lat: null, home_lon: null,
    paired_at: '2025-08-01T10:00:00+00:00', reachable: true, unreachable_since: null,
    members, member_count: members.length, ...extra,
  }
}

function payload(households: Row[] = [], members: Row[] = [local('u-me', 'Pascal')], instExtra: Row = {}) {
  return {
    instance: {
      instance_id: 'us', display_name: 'Vizeli Home', home_lat: null, home_lon: null,
      members, member_count: members.length, ...instExtra,
    },
    households,
    totals: { households: 1 + households.length, people: 0 },
  }
}

/** Route ``api.get`` by path; ``friends`` answers ``/api/friends``. */
function serve(friends: unknown, extra: Record<string, unknown> = {}) {
  apiGet.mockImplementation(async (path: string) => {
    if (path === '/api/friends') {
      if (friends instanceof Error) throw friends
      return friends
    }
    if (path in extra) return extra[path]
    if (path === '/api/blocks') return { blocks: [] }
    if (path === '/api/users') return []
    return {}
  })
}

/** Names of the person rows inside *root*, in order. */
function rowNames(root: ParentNode): string[] {
  return [...root.querySelectorAll('.sh-friends-row .sh-friends-row__text')].map((n) => n.textContent ?? '')
}

function cards(container: ParentNode): HTMLElement[] {
  return [...container.querySelectorAll<HTMLElement>('li.sh-friends-hh')]
}

const many = (inst: string, n: number) =>
  Array.from({ length: n }, (_, i) => remote(`${inst}-${i}`, inst, `Person ${String.fromCharCode(65 + i)}`))

describe('FriendsPage', () => {
  beforeEach(() => {
    apiGet.mockReset()
    openPersonSpy.mockReset()
    openPairingSpy.mockReset()
    openStatusEditorSpy.mockReset()
    _resetBlocksForTest()
    householdAdmins.value = null
    authUser.value = { user_id: 'u-me', username: 'pascal', display_name: 'Pascal', is_admin: true }
  })

  // ── Load states ─────────────────────────────────────────────────────

  it('a failed load shows an error card with Try again instead of an endless spinner', async () => {
    serve(new Error('network down'))
    const { container, findByRole } = render(<FriendsPage />)
    const alert = await findByRole('alert')
    expect(alert.textContent).toContain('Couldn’t load your people')
    expect(container.querySelector('.sh-spinner')).toBeNull()
    expect(container.querySelector('.sh-friends-hero')).toBeNull()
    expect(within(alert).getByRole('button', { name: 'Try again' })).toBeTruthy()
  })

  it('Try again re-fetches and renders the page on success', async () => {
    serve(new Error('network down'))
    const { container, findByRole, queryByRole } = render(<FriendsPage />)
    const alert = await findByRole('alert')
    serve(payload([hh('p-a', 'Alpha', [remote('r1', 'p-a', 'Ann')])]))
    fireEvent.click(within(alert).getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(container.querySelector('.sh-friends-hero')).not.toBeNull())
    expect(queryByRole('alert')).toBeNull()
  })

  it('a retry that fails again keeps the error card up', async () => {
    serve(new Error('down'))
    const { findByRole } = render(<FriendsPage />)
    const alert = await findByRole('alert')
    const btn = within(alert).getByRole('button', { name: 'Try again' }) as HTMLButtonElement
    fireEvent.click(btn)
    await waitFor(() => expect(btn.disabled).toBe(false))
    expect(alert.isConnected).toBe(true)
  })

  // ── Hero ────────────────────────────────────────────────────────────

  it('hero counts every visible person and every household, ours included', async () => {
    serve(payload(
      [
        hh('p-a', 'Alpha', [remote('r1', 'p-a', 'Ann'), remote('r2', 'p-a', 'Ben')]),
        hh('p-q', 'Quiet', []),
      ],
      [local('u-me', 'Pascal'), local('u-mel', 'Melanie')],
    ))
    const { findByRole } = render(<FriendsPage />)
    const h = await findByRole('heading', { level: 2, name: /people in/ })
    expect(h.textContent).toBe('4 people in 3 households')
    expect(h.querySelector('em')?.textContent).toBe('3 households')
  })

  it('admins get one "Pair a household" CTA in the hero', async () => {
    serve(payload([hh('p-a', 'Alpha', [remote('r1', 'p-a', 'Ann')])]))
    const { findAllByRole } = render(<FriendsPage />)
    const ctas = await findAllByRole('button', { name: 'Pair a household' })
    expect(ctas).toHaveLength(1)
    fireEvent.click(ctas[0])
    expect(openPairingSpy).toHaveBeenCalledWith('household')
  })

  it('members get "Ask <admin>" instead of a pairing button', async () => {
    authUser.value = { ...authUser.value, is_admin: false }
    serve(payload([hh('p-a', 'Alpha', [remote('r1', 'p-a', 'Ann')])]), {
      '/api/users': [{ user_id: 'u-me', display_name: 'Pascal', is_admin: true }],
    })
    const { container, queryByRole } = render(<FriendsPage />)
    await waitFor(() =>
      expect(container.querySelector('.sh-friends-hero')?.textContent).toContain('Ask Pascal'))
    expect(queryByRole('button', { name: 'Pair a household' })).toBeNull()
  })

  it('the hero says how long we have been connected and who is newest', async () => {
    serve(payload([
      hh('p-a', 'Alpha', [remote('r1', 'p-a', 'Ann')], { paired_at: '2025-08-03T10:00:00Z' }),
      hh('p-b', 'Beta', [remote('r2', 'p-b', 'Bo')], { paired_at: '2026-02-01T10:00:00Z' }),
    ]))
    const { container } = render(<FriendsPage />)
    await waitFor(() => expect(container.querySelector('.sh-conn-hero__sub')).not.toBeNull())
    const sub = container.querySelector('.sh-conn-hero__sub')!.textContent!
    expect(sub).toContain('Together since August 2025')
    expect(sub).toContain('newest: Beta')
  })

  // ── Empty ───────────────────────────────────────────────────────────

  it('no paired household: the empty state replaces the hero with exactly one CTA', async () => {
    serve(payload([]))
    const { container, findByRole, getAllByRole, queryByRole } = render(<FriendsPage />)
    await findByRole('heading', { name: 'Just your household, for now' })
    expect(container.querySelector('.sh-friends-hero')).toBeNull()
    expect(getAllByRole('button', { name: 'Pair a household' })).toHaveLength(1)
    // Nothing to switch between yet.
    expect(queryByRole('group', { name: 'View' })).toBeNull()
    // Our household still shows, with the self nudge.
    expect(container.textContent).toContain('At home')
    expect(container.textContent).toContain('Add a status so your household knows')
  })

  it('empty state for a member: no pairing button, an ask line instead', async () => {
    authUser.value = { ...authUser.value, is_admin: false }
    serve(payload([]))
    const { findByRole, queryByRole, container } = render(<FriendsPage />)
    await findByRole('heading', { name: 'Just your household, for now' })
    expect(queryByRole('button', { name: 'Pair a household' })).toBeNull()
    expect(container.querySelector('.sh-friends-empty')?.textContent).toContain('Ask an admin')
  })

  // ── Ordering ────────────────────────────────────────────────────────

  it('unreachable households come first, then A–Z (accent- and case-insensitive)', async () => {
    serve(payload([
      hh('p-z', 'zeta house', [remote('r1', 'p-z', 'Zed')]),
      hh('p-o', 'Öberg', [remote('r2', 'p-o', 'Olle')]),
      hh('p-b', "Brother's house", [remote('r3', 'p-b', 'Marco')],
        { reachable: false, unreachable_since: '2026-01-07 09:00:00' }),
      hh('p-a', 'Alpha', [remote('r4', 'p-a', 'Ann')]),
    ]))
    const { container } = render(<FriendsPage />)
    await waitFor(() => expect(cards(container)).toHaveLength(4))
    const names = cards(container).map((c) => c.querySelector('h3')?.textContent)
    expect(names).toEqual(["Brother's house", 'Alpha', 'Öberg', 'zeta house'])
    const down = cards(container)[0]
    expect(down.classList.contains('sh-friends-hh--down')).toBe(true)
    expect(down.textContent).toMatch(/Unreachable since/)
    expect(down.querySelector('.sh-conn-av[data-s="down"]')).not.toBeNull()
  })

  it('people inside a household are A–Z; our own row comes first at home', async () => {
    serve(payload(
      [hh('p-a', 'Alpha', [remote('r1', 'p-a', 'Zoe'), remote('r2', 'p-a', 'Ana'), remote('r3', 'p-a', 'Émile')])],
      [local('u-lina', 'Lina'), local('u-me', 'Pascal'), local('u-ada', 'Ada')],
    ))
    const { container } = render(<FriendsPage />)
    await waitFor(() => expect(cards(container)).toHaveLength(1))
    expect(rowNames(container.querySelector('.sh-friends-hh--home')!)).toEqual(['Pascal', 'Ada', 'Lina'])
    expect(rowNames(cards(container)[0])).toEqual(['Ana', 'Émile', 'Zoe'])
  })

  it('a household that shared nobody is a footer line, not a card', async () => {
    serve(payload([
      hh('p-a', 'Alpha', [remote('r1', 'p-a', 'Ann')]),
      hh('p-r', 'Casa Rossi', []),
    ]))
    const { container } = render(<FriendsPage />)
    await waitFor(() => expect(cards(container)).toHaveLength(1))
    expect(container.textContent).toContain('Casa Rossi hasn’t shared their people yet.')
  })

  // ── Collapse ────────────────────────────────────────────────────────

  it('more than 6 people collapse to 5 + "N more", which expands and folds back', async () => {
    serve(payload([hh('p-big', 'Big', many('p-big', 12)), hh('p-six', 'Six', many('p-six', 6))]))
    const { container } = render(<FriendsPage />)
    await waitFor(() => expect(cards(container)).toHaveLength(2))
    const [big, six] = cards(container)
    expect(big.querySelectorAll('.sh-friends-row')).toHaveLength(5)
    expect(six.querySelectorAll('.sh-friends-row')).toHaveLength(6)
    expect(within(six).queryByRole('button', { name: /more/ })).toBeNull()
    const more = within(big).getByRole('button', { name: '7 more' })
    expect(more.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(more)
    expect(big.querySelectorAll('.sh-friends-row')).toHaveLength(12)
    const fewer = within(big).getByRole('button', { name: 'Show fewer' })
    expect(fewer.getAttribute('aria-expanded')).toBe('true')
    fireEvent.click(fewer)
    expect(big.querySelectorAll('.sh-friends-row')).toHaveLength(5)
  })

  // ── Find ────────────────────────────────────────────────────────────

  it('"Find someone" appears only past 8 people', async () => {
    serve(payload([hh('p-a', 'Alpha', many('p-a', 7))]))
    const first = render(<FriendsPage />)
    await waitFor(() => expect(cards(first.container)).toHaveLength(1))
    // 1 (us) + 7 = 8 → no find box.
    expect(first.queryByRole('searchbox')).toBeNull()
    first.unmount()

    serve(payload([hh('p-a', 'Alpha', many('p-a', 8))]))
    const second = render(<FriendsPage />)
    await waitFor(() => expect(second.queryByRole('searchbox', { name: 'Find someone' })).not.toBeNull())
  })

  it('find filters by name, nickname or household, ignoring case and accents', async () => {
    serve(payload([
      hh('p-m', 'Familie Müller', [remote('r1', 'p-m', 'Klaus'), remote('r2', 'p-m', 'Jonas')]),
      hh('p-o', 'Okafor family', [
        remote('r3', 'p-o', 'Chidi', { personal_alias: 'Coach' }),
        remote('r4', 'p-o', 'Ngozi'), remote('r5', 'p-o', 'Tobi'),
      ]),
      hh('p-t', 'Tanaka', [remote('r6', 'p-t', '田中 健'), remote('r7', 'p-t', 'Sora'), remote('r8', 'p-t', 'Émile')]),
    ]))
    const { container, findByRole, getByRole } = render(<FriendsPage />)
    const box = await findByRole('searchbox', { name: 'Find someone' })

    fireEvent.input(box, { target: { value: 'MULLER' } })
    expect(rowNames(container)).toEqual(['Jonas', 'Klaus'])
    expect(getByRole('status').textContent).toBe('2 people found')

    fireEvent.input(box, { target: { value: 'coach' } })
    expect(rowNames(container)).toEqual(['Coach'])

    fireEvent.input(box, { target: { value: 'emile' } })
    expect(rowNames(container)).toEqual(['Émile'])

    fireEvent.input(box, { target: { value: '田中' } })
    expect(rowNames(container)).toEqual(['田中 健'])

    fireEvent.input(box, { target: { value: 'nobody-here' } })
    expect(container.textContent).toContain('No one matches “nobody-here”.')
    fireEvent.click(container.querySelector('.sh-friends-nomatch button') as HTMLElement)
    expect(rowNames(container).length).toBe(9)
  })

  it('Escape clears the find box', async () => {
    serve(payload([hh('p-a', 'Alpha', many('p-a', 9))]))
    const { container, findByRole } = render(<FriendsPage />)
    const box = await findByRole('searchbox') as HTMLInputElement
    fireEvent.input(box, { target: { value: 'Person A' } })
    expect(rowNames(container)).toEqual(['Person A'])
    fireEvent.keyDown(box, { key: 'Escape' })
    // Back to the browse list: us + Alpha collapsed to 5.
    expect(box.value).toBe('')
    expect(rowNames(container).length).toBe(6)
  })

  // ── Views ───────────────────────────────────────────────────────────

  it('People view lists everyone A–Z with letter dividers, non-Latin under #', async () => {
    serve(payload(
      [
        hh('p-o', 'Okafor family', [remote('r1', 'p-o', 'Amaka', {
          status: { emoji: '🎸', text: 'Band practice', expires_at: null },
        }), remote('r2', 'p-o', 'Chidi')]),
        hh('p-h', 'عائلة الحسن', [remote('r3', 'p-h', 'ليلى الحسن')]),
        hh('p-t', 'Tanaka', [remote('r4', 'p-t', '田中 健'), remote('r5', 'p-t', 'Ömer')]),
      ],
      [local('u-me', 'Pascal'), local('u-ada', 'Ada')],
    ))
    const { container, findByRole } = render(<FriendsPage />)
    fireEvent.click(await findByRole('button', { name: 'People' }))
    expect((await findByRole('button', { name: 'People' })).getAttribute('aria-pressed')).toBe('true')
    const letters = [...container.querySelectorAll('.sh-friends-letter')].map((g) => g.getAttribute('aria-label'))
    expect(letters).toEqual(['A', 'C', 'O', 'P', '#'])
    const names = rowNames(container)
    expect(names.slice(0, 5)).toEqual(['Ada', 'Amaka', 'Chidi', 'Ömer', 'Pascal'])
    expect(new Set(names.slice(5))).toEqual(new Set(['ليلى الحسن', '田中 健']))
    // Status and household on separate lines.
    const amaka = container.querySelector('[data-user-id="r1"]')!
    const metas = [...amaka.querySelectorAll('.sh-friends-row__meta')].map((m) => m.textContent)
    expect(metas).toEqual(['🎸 Band practice', 'Okafor family'])
    expect(container.querySelector('li.sh-friends-hh')).toBeNull()
  })

  it('Map view replaces the list; offered only when a household has a location', async () => {
    serve(payload(
      [
        hh('p-a', 'Alpha', [remote('r1', 'p-a', 'Ann')], { home_lat: 47.1, home_lon: 8.5 }),
        hh('p-b', 'Beta', [remote('r2', 'p-b', 'Bo')]),
      ],
      [local('u-me', 'Pascal')],
      { home_lat: 47.0, home_lon: 8.4 },
    ))
    const { container, findByRole, getByTestId } = render(<FriendsPage />)
    fireEvent.click(await findByRole('button', { name: 'Map' }))
    expect(getByTestId('map').getAttribute('data-markers')).toBe('2')
    expect(container.querySelector('li.sh-friends-hh')).toBeNull()
    expect(container.textContent).toContain('Not on the map (no location shared): Beta')
    fireEvent.click(await findByRole('button', { name: 'Households' }))
    expect(cards(container)).toHaveLength(2)
  })

  it('no coordinates anywhere: no Map option', async () => {
    serve(payload([hh('p-a', 'Alpha', [remote('r1', 'p-a', 'Ann')])]))
    const { findByRole, queryByRole } = render(<FriendsPage />)
    await findByRole('button', { name: 'People' })
    expect(queryByRole('button', { name: 'Map' })).toBeNull()
  })

  // ── Rows ────────────────────────────────────────────────────────────

  it('rows carry presence (remote too), status and an accessible name; whole row opens the sheet', async () => {
    serve(payload(
      [hh('p-o', 'Okafor family', [
        remote('r1', 'p-o', 'Amaka', {
          is_online: true, status: { emoji: '🎸', text: 'Band practice', expires_at: null },
        }),
        remote('r2', 'p-o', 'Jonas', { status: { emoji: '🍝', text: null, expires_at: null } }),
      ])],
      [local('u-me', 'Pascal'), local('u-noah', 'Noah', { last_seen_at: new Date(Date.now() - 3 * 3600e3).toISOString() })],
    ))
    const { container, findByRole } = render(<FriendsPage />)
    const amaka = await findByRole('button', { name: 'Amaka, online, 🎸 Band practice' })
    expect(amaka.querySelector('.avatar')?.getAttribute('data-online')).toBe('online')
    // Emoji-only status → avatar badge, no second line for a remote person.
    const jonas = container.querySelector('[data-user-id="r2"]')!
    expect(jonas.querySelector('.sh-friends-row__badge')?.textContent).toBe('🍝')
    expect(jonas.querySelector('.sh-friends-row__meta')).toBeNull()
    // Local and offline → "Seen …".
    const noah = container.querySelector('[data-user-id="u-noah"]')!
    expect(noah.querySelector('.sh-friends-row__meta')?.textContent).toMatch(/^Seen /)
    // No chevrons; the row itself is the button.
    expect(container.querySelector('.sh-conn-chev')).toBeNull()
    fireEvent.click(amaka)
    expect(openPersonSpy).toHaveBeenCalledTimes(1)
    const [uid, ctx] = openPersonSpy.mock.calls[0]
    expect(uid).toBe('r1')
    expect(ctx.person.household).toEqual({ instance_id: 'p-o', display_name: 'Okafor family', is_own: false })
    expect(ctx.person.is_local).toBe(false)
  })

  it('a nickname shows in place of the name and is patched after the sheet saves one', async () => {
    serve(payload([hh('p-b', "Brother's house", [remote('u-bro', 'p-b', 'Bob')])]))
    const { container, findByRole } = render(<FriendsPage />)
    fireEvent.click(await findByRole('button', { name: /^Bob/ }))
    const ctx = openPersonSpy.mock.calls[0][1]
    ctx.onAliasChanged('u-bro', 'Brother')
    await waitFor(() => expect(rowNames(container)).toContain('Brother'))
    expect(container.querySelector('[data-user-id="u-bro"]')?.getAttribute('aria-label'))
      .toBe('Brother, your nickname for Bob')
  })

  it('our own row opens the app-wide status editor, not the person sheet', async () => {
    serve(payload([hh('p-a', 'Alpha', [remote('r1', 'p-a', 'Ann')])]))
    const { findByRole } = render(<FriendsPage />)
    const self = await findByRole('button', { name: /^Pascal \(you\)/ })
    expect(self.getAttribute('aria-label')).toContain('edit your status')
    fireEvent.click(self)
    expect(openStatusEditorSpy).toHaveBeenCalledTimes(1)
    expect(openPersonSpy).not.toHaveBeenCalled()
  })

  it('our own row shows the status from currentUser, not the /api/friends snapshot', async () => {
    authUser.value = {
      ...authUser.value,
      status: { emoji: '🛠', text: 'Fixing the gate', expires_at: null },
    }
    serve(payload([hh('p-a', 'Alpha', [remote('r1', 'p-a', 'Ann')])]))
    const { container, findByRole } = render(<FriendsPage />)
    await findByRole('button', { name: /^Pascal \(you\)/ })
    await waitFor(() =>
      expect(container.querySelector('[data-user-id="u-me"] .sh-friends-row__meta')?.textContent)
        .toBe('🛠 Fixing the gate'))
  })

  // ── Blocked ─────────────────────────────────────────────────────────

  it('blocked people: a footer line with a Manage link to Settings › Privacy', async () => {
    serve(payload([hh('p-a', 'Alpha', [remote('r1', 'p-a', 'Ann')])]), {
      '/api/blocks': { blocks: [
        { user_id: 'x1', blocked_at: '2026-01-01' }, { user_id: 'x2', blocked_at: '2026-01-01' },
      ] },
    })
    const { container, findByRole } = render(<FriendsPage />)
    const manage = await findByRole('link', { name: 'Manage' })
    expect(manage.getAttribute('href')).toMatch(/\/settings#privacy$/)
    expect(container.querySelector('.sh-friends-foot--blocked')?.textContent)
      .toBe('2 people you blocked are hidden. Manage')
  })

  it('no blocks: no blocked footer', async () => {
    serve(payload([hh('p-a', 'Alpha', [remote('r1', 'p-a', 'Ann')])]))
    const { container, findByRole } = render(<FriendsPage />)
    await findByRole('button', { name: /^Ann/ })
    expect(container.querySelector('.sh-friends-foot--blocked')).toBeNull()
  })

  it('someone blocked from the sheet drops off the page (and the counts) at once', async () => {
    serve(payload([], [local('u-me', 'Pascal'), local('u-mom', 'Maria')]))
    const { container, findByRole } = render(<FriendsPage />)
    await findByRole('button', { name: /^Maria/ })
    blockedUsers.value = [{ user_id: 'u-mom', blocked_at: '2026-01-01' }]
    blockedUserIds.value = new Set(['u-mom'])
    await waitFor(() => expect(container.querySelector('[data-user-id="u-mom"]')).toBeNull())
    expect(container.textContent).toContain('1 person you blocked is hidden.')
  })
})
