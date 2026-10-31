/**
 * GroupSheet — Social Home's own sheet to pick people for one group game.
 *
 * Pinned here: only households that can take part are listed (one quiet
 * line when some were left out), search over person and household, the
 * cap at the app's players.max − 1 (hard ceiling 30), "Everyone in <space>"
 * adding / removing / partly fitting, a suggestion starting ticked, the
 * min gate, the 600 ms arm on Send, the POST body, refusals in plain words
 * and Cancel handing back null.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

const h = vi.hoisted(() => ({
  getInvitable: vi.fn(),
  createGroup: vi.fn(),
  showToast: vi.fn(),
}))

vi.mock('@/store/apps', () => ({ getInvitable: h.getInvitable }))
vi.mock('@/store/appGroups', async (orig) => ({
  ...(await orig<typeof import('@/store/appGroups')>()),
  createGroup: h.createGroup,
}))
vi.mock('@/components/Toast', () => ({ showToast: h.showToast }))

import { ApiError } from '@/api'
import { GroupSheet } from './GroupSheet'
import { SEND_ARM_MS } from '../inviteCaps'
import type { GroupOpenRequest } from '../bridge'

type P = {
  instance_id: string, user_ref: string, display_name: string, person_name: string,
  household_name: string | null, is_local: boolean, online: boolean, groups?: boolean,
}
const person = (name: string, home: string | null, extra: Partial<P> = {}): P => ({
  instance_id: home ?? 'own',
  user_ref: name.toLowerCase(),
  display_name: home ? `${name} · ${home}` : name,
  person_name: name,
  household_name: home,
  is_local: home === null,
  online: false,
  groups: true,
  ...extra,
})
const BEN = person('Ben', 'Brookside')
const MILO = person('Milo', null)
const CLEO = person('Cleo', 'Krämer')
const OLD = person('Oskar', 'Oldtown', { groups: false })
const COUNTS = { open: 0, max_open: 10, today: 0, max_per_day: 30 }

const SESSION = {
  id: 't1', appId: 'quiz', state: 'lobby', me: 1, myState: 'seated', host: 1, isHost: true,
  min: 2, max: 8, lateJoin: false, seq: 0, players: [],
}

function refused(code: string, params?: Record<string, unknown>): ApiError {
  return new ApiError(409, '/api/apps/quiz/groups', { code, detail: 'x', ...(params ? { params } : {}) })
}

async function open(opts: {
  people?: P[]
  spaces?: { space_id: string, name: string, people: number[] }[]
  request?: GroupOpenRequest
  players?: { min: number, max: number }
  invites?: typeof COUNTS
} = {}) {
  const people = opts.people ?? [BEN, MILO, CLEO, OLD]
  h.getInvitable.mockResolvedValue({ people, invites: opts.invites ?? COUNTS, spaces: opts.spaces })
  const onDone = vi.fn()
  const view = render(
    <GroupSheet
      appId="quiz"
      appName="Quiz"
      request={opts.request ?? { suggest: [] }}
      players={opts.players}
      onDone={onDone}
    />,
  )
  await view.findByText(people.find(p => p.groups !== false)!.person_name)
  const send = () => view.getByTestId('group-send') as HTMLButtonElement
  const arm = () => vi.setSystemTime(Date.now() + SEND_ARM_MS)
  return { ...view, onDone, send, arm }
}

describe('GroupSheet', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-10-31T12:00:00Z'))
    h.getInvitable.mockReset()
    h.createGroup.mockReset()
    h.showToast.mockReset()
  })
  afterEach(() => { vi.useRealTimers() })

  it('lists only households that can play in a group, and says so once when some were left out', async () => {
    const { queryByText, getByText, getByRole } = await open()
    expect(getByRole('dialog').textContent).toContain('Quiz')
    expect(queryByText('Oskar')).toBeNull()
    expect(getByText('Some people can\'t join group games yet — their home needs an update.')).toBeTruthy()
  })

  it('no quiet line when nobody was left out', async () => {
    const { queryByText } = await open({ people: [BEN, MILO] })
    expect(queryByText(/can't join group games yet/)).toBeNull()
  })

  it('search is always shown and matches person or household', async () => {
    const { getByRole, queryByText } = await open({ people: [BEN, MILO] })
    const search = getByRole('searchbox') as HTMLInputElement
    fireEvent.input(search, { target: { value: 'brook' } })
    expect(queryByText('Ben')).not.toBeNull()
    expect(queryByText('Milo')).toBeNull()
    fireEvent.input(search, { target: { value: 'mil' } })
    expect(queryByText('Milo')).not.toBeNull()
    expect(queryByText('Ben')).toBeNull()
  })

  it('caps picks at the app\'s players.max − 1 and says when it\'s full', async () => {
    const { getByLabelText, getByText } = await open({ players: { min: 2, max: 3 } })
    expect(getByText('0 of 2 picked')).toBeTruthy()
    fireEvent.click(getByLabelText(/Ben/))
    fireEvent.click(getByLabelText(/Milo/))
    expect(getByText('2 of 2 picked')).toBeTruthy()
    expect((getByLabelText(/Cleo/) as HTMLInputElement).disabled).toBe(true)
    expect(getByText('Nobody else fits: Quiz is for up to 3 players.')).toBeTruthy()
    // Unticking frees a place again.
    fireEvent.click(getByLabelText(/Ben/))
    expect((getByLabelText(/Cleo/) as HTMLInputElement).disabled).toBe(false)
  })

  it('without a range from the app, the hard ceiling of 30 players (29 picks)', async () => {
    const { getByText } = await open()
    expect(getByText('0 of 29 picked')).toBeTruthy()
  })

  it('a range above 30 is cut to 30', async () => {
    const { getByText } = await open({ players: { min: 2, max: 99 } })
    expect(getByText('0 of 29 picked')).toBeTruthy()
  })

  it('"Everyone in <space>" adds the space\'s people; a second tap takes them out again', async () => {
    const { getByRole, getByLabelText, getByText } = await open({
      spaces: [{ space_id: 'sp1', name: 'Familie Müller', people: [0, 1, 3] }],
    })
    // Oskar's household can't play: the count leaves him out.
    const chip = getByRole('button', { name: 'Everyone in Familie Müller (2)' })
    expect(chip.getAttribute('aria-pressed')).toBe('false')
    fireEvent.click(chip)
    expect((getByLabelText(/Ben/) as HTMLInputElement).checked).toBe(true)
    expect((getByLabelText(/Milo/) as HTMLInputElement).checked).toBe(true)
    expect((getByLabelText(/Cleo/) as HTMLInputElement).checked).toBe(false)
    expect(chip.getAttribute('aria-pressed')).toBe('true')
    expect(getByText('2 of 29 picked')).toBeTruthy()
    fireEvent.click(chip)
    expect((getByLabelText(/Ben/) as HTMLInputElement).checked).toBe(false)
    expect((getByLabelText(/Milo/) as HTMLInputElement).checked).toBe(false)
  })

  it('a space that doesn\'t fit adds as many as fit and says so', async () => {
    const { getByRole, getByLabelText, getByText } = await open({
      players: { min: 2, max: 3 },
      spaces: [{ space_id: 'sp1', name: 'Club', people: [0, 1, 2] }],
    })
    fireEvent.click(getByLabelText(/Cleo/))
    fireEvent.click(getByRole('button', { name: 'Everyone in Club (3)' }))
    expect(getByText('Only 1 more fits.')).toBeTruthy()
    expect(getByText('2 of 2 picked')).toBeTruthy()
    expect((getByLabelText(/Ben/) as HTMLInputElement).checked).toBe(true)
  })

  it('ignores a space index that isn\'t a person, and spaces nobody of can play', async () => {
    const { queryByRole } = await open({
      spaces: [
        { space_id: 'sp1', name: 'Odd', people: [7, -1, 3] },
        { space_id: 'sp2', name: 'Pair', people: [0, 0] },
      ],
    })
    expect(queryByRole('button', { name: /Everyone in Odd/ })).toBeNull()
    expect(queryByRole('button', { name: 'Everyone in Pair (1)' })).not.toBeNull()
  })

  it('a suggested person starts ticked; one who isn\'t listed is ignored', async () => {
    const { getByLabelText, send } = await open({
      request: { suggest: [
        { instance_id: 'Brookside', user_ref: 'ben', is_local: false },
        { instance_id: 'Oldtown', user_ref: 'oskar', is_local: false },
      ] },
    })
    expect((getByLabelText(/Ben/) as HTMLInputElement).checked).toBe(true)
    expect(send().textContent).toBe('Invite Ben')
  })

  it('Send stays off until the app\'s fewest players can be reached', async () => {
    const { getByLabelText, send, getByText, queryByText } = await open({ players: { min: 3, max: 8 } })
    expect(send().disabled).toBe(true)
    fireEvent.click(getByLabelText(/Ben/))
    expect(send().disabled).toBe(true)
    expect(getByText('Pick at least 2 people')).toBeTruthy()
    fireEvent.click(getByLabelText(/Milo/))
    expect(send().disabled).toBe(false)
    expect(send().textContent).toBe('Invite 2 people')
    expect(queryByText('Pick at least 2 people')).toBeNull()
  })

  it('tap-jacking: a tap on Send within 600 ms of the list appearing does nothing', async () => {
    h.createGroup.mockResolvedValue(SESSION)
    const { send, onDone } = await open({
      request: { suggest: [{ instance_id: 'Brookside', user_ref: 'ben', is_local: false }] },
    })
    vi.setSystemTime(Date.now() + SEND_ARM_MS - 1)
    fireEvent.click(send())
    await Promise.resolve()
    expect(h.createGroup).not.toHaveBeenCalled()
    expect(onDone).not.toHaveBeenCalled()
  })

  it('Send posts the picked people, how long it stays open and the note, then hands back the game', async () => {
    h.createGroup.mockResolvedValue(SESSION)
    const { getByLabelText, send, onDone, arm } = await open()
    fireEvent.click(getByLabelText(/Ben/))
    fireEvent.click(getByLabelText(/Milo/))
    const days = getByLabelText('Open for') as HTMLSelectElement
    expect(days.value).toBe('3')
    fireEvent.change(days, { target: { value: '7' } })
    fireEvent.input(getByLabelText('Add a note (optional)'), { target: { value: '  Friday quiz night  ' } })
    arm()
    fireEvent.click(send())
    await waitFor(() => expect(onDone).toHaveBeenCalled())
    expect(h.createGroup).toHaveBeenCalledWith('quiz', {
      targets: [
        { instance_id: 'Brookside', user_ref: 'ben', is_local: false },
        { instance_id: 'own', user_ref: 'milo', is_local: true },
      ],
      lobby_days: 7,
      note: 'Friday quiz night',
    })
    expect(onDone).toHaveBeenCalledWith(SESSION)
    expect(h.showToast).toHaveBeenCalledWith('Invited 2 people to Quiz.')
  })

  it('no note: the field is left out; one person: the toast names them', async () => {
    h.createGroup.mockResolvedValue(SESSION)
    const { getByLabelText, send, onDone, arm } = await open()
    fireEvent.click(getByLabelText(/Cleo/))
    arm()
    fireEvent.click(send())
    await waitFor(() => expect(onDone).toHaveBeenCalled())
    expect(h.createGroup.mock.calls[0][1]).toEqual({
      targets: [{ instance_id: 'Krämer', user_ref: 'cleo', is_local: false }],
      lobby_days: 3,
    })
    expect(h.showToast).toHaveBeenCalledWith('Invited Cleo to Quiz.')
  })

  it('the note is one line of at most 80 characters, with a counter', async () => {
    const { getByLabelText, getByText } = await open()
    const note = getByLabelText('Add a note (optional)') as HTMLInputElement
    expect(note.maxLength).toBe(80)
    fireEvent.input(note, { target: { value: 'Bring snacks' } })
    expect(getByText('12/80')).toBeTruthy()
  })

  it.each([
    ['APP_GROUP_OPEN_LIMIT', { limit: 5 }, 'You already have 5 games waiting for people in Quiz.'],
    ['APP_GROUP_DAILY_LIMIT', { limit: 10 }, 'You\'ve started 10 games today. Try again tomorrow.'],
    ['APP_GROUP_HOUSEHOLD_TOO_OLD', undefined, 'Someone you picked can\'t join group games yet: their home needs an update.'],
    ['APP_GROUPS_NOT_SUPPORTED', undefined, 'Quiz can\'t be played as a group game.'],
    ['APP_INVITES_OPEN_LIMIT', undefined, 'You have 10 invites nobody has answered yet. Wait for an answer, or take one back in Quiz.'],
    ['APP_INVITES_DAILY_LIMIT', undefined, 'You\'ve sent 30 invites today. Try again tomorrow.'],
  ])('a %s refusal is said in plain words', async (code, params, text) => {
    h.createGroup.mockRejectedValue(refused(code, params))
    const { getByLabelText, send, findByText, onDone, arm } = await open()
    fireEvent.click(getByLabelText(/Ben/))
    arm()
    fireEvent.click(send())
    expect(await findByText(text)).toBeTruthy()
    expect(onDone).not.toHaveBeenCalled()
  })

  it('a size refusal says the range and takes it on as the cap', async () => {
    h.createGroup.mockRejectedValue(refused('APP_GROUP_SIZE', { min: 2, max: 2 }))
    const { getByLabelText, send, findByText, getByText, arm } = await open()
    fireEvent.click(getByLabelText(/Ben/))
    fireEvent.click(getByLabelText(/Milo/))
    arm()
    fireEvent.click(send())
    expect(await findByText('Quiz is for 2 to 2 players.')).toBeTruthy()
    expect(getByText('2 of 1 picked')).toBeTruthy()
    // Too many ticked now: Send waits until one is unticked.
    expect(send().disabled).toBe(true)
    fireEvent.click(getByLabelText(/Milo/))
    expect(send().disabled).toBe(false)
  })

  it('a network failure says so and lets the person try again', async () => {
    h.createGroup.mockRejectedValue(new TypeError('Failed to fetch'))
    const { getByLabelText, send, findByText, arm } = await open()
    fireEvent.click(getByLabelText(/Ben/))
    arm()
    fireEvent.click(send())
    expect(await findByText('Couldn\'t send the invites. Try again.')).toBeTruthy()
    expect(send().disabled).toBe(false)
  })

  it('Cancel hands back null', async () => {
    const { getByText, onDone } = await open()
    fireEvent.click(getByText('Cancel'))
    expect(onDone).toHaveBeenCalledWith(null)
  })

  it('Cancel after "can\'t be played as a group game" tells the app why', async () => {
    h.createGroup.mockRejectedValue(refused('APP_GROUPS_NOT_SUPPORTED'))
    const { getByLabelText, send, findByText, getByText, onDone, arm } = await open()
    fireEvent.click(getByLabelText(/Ben/))
    arm()
    fireEvent.click(send())
    await findByText('Quiz can\'t be played as a group game.')
    fireEvent.click(getByText('Cancel'))
    expect(onDone).toHaveBeenCalledWith(null, 'groups_not_supported')
  })

  it('nobody to invite: says so', async () => {
    h.getInvitable.mockResolvedValue({ people: [OLD], invites: COUNTS })
    const view = render(<GroupSheet appId="quiz" appName="Quiz" request={{ suggest: [] }} onDone={vi.fn()} />)
    expect(await view.findByText('There\'s nobody you can invite to a group game yet.')).toBeTruthy()
  })
})
