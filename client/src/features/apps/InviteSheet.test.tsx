/**
 * InviteSheet — Social Home's own invite sheet (D3).
 *
 * Pinned here: the tap-jacking guard (Send ignores taps for its first
 * 600 ms), Send always naming who gets the invite, a suggestion only
 * starting selected, the caps said in plain words (before Send and when the
 * server refuses), and the app getting back only who was picked + the
 * session ids Social Home opened.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

const h = vi.hoisted(() => ({
  getInvitable: vi.fn(),
  sendInvite: vi.fn(),
  continueGame: vi.fn(),
  showToast: vi.fn(),
}))

vi.mock('@/store/apps', () => ({
  getInvitable: h.getInvitable,
  sendInvite: h.sendInvite,
  continueGame: h.continueGame,
}))
vi.mock('@/components/Toast', () => ({ showToast: h.showToast }))

import { ApiError } from '@/api'
import { InviteSheet, SEND_ARM_MS } from './InviteSheet'
import type { PickRequest } from './bridge'

const BEN = {
  instance_id: 'brookside', user_ref: 'ben', display_name: 'Ben · Brookside',
  person_name: 'Ben', household_name: 'Brookside', is_local: false, online: false,
}
const MILO = {
  instance_id: 'own', user_ref: 'u-milo', display_name: 'Milo',
  person_name: 'Milo', household_name: null, is_local: true, online: true,
}
const COUNTS = { open: 0, max_open: 10, today: 0, max_per_day: 30 }

function refused(code: string): ApiError {
  const err = new ApiError(429, 'x') as ApiError & { code: string }
  Object.assign(err, { code })
  return err
}

async function open(request: PickRequest = { purpose: 'play', max: 1 }, people = [BEN, MILO], invites = COUNTS) {
  h.getInvitable.mockResolvedValue({ people, invites })
  const onDone = vi.fn()
  const view = render(<InviteSheet appId="chess" appName="Chess" request={request} onDone={onDone} />)
  await view.findByText('Milo')
  return { ...view, onDone, send: () => view.getByTestId('invite-send') }
}

describe('InviteSheet', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-10-29T12:00:00Z'))
    h.getInvitable.mockReset()
    h.sendInvite.mockReset()
    h.continueGame.mockReset()
    h.showToast.mockReset()
  })
  afterEach(() => { vi.useRealTimers() })

  it('is Social Home talking: the app name, never the app\'s own text', async () => {
    const { getByRole, getByText } = await open()
    expect(getByRole('dialog').textContent).toContain('Invite someone to play Chess')
    expect(getByText('Social Home sends the invite. Chess only learns who you pick.')).toBeTruthy()
    expect(getByText('Brookside')).toBeTruthy()
    expect(getByText('Your household')).toBeTruthy()
  })

  it('Send names who the invite goes to, and is off until someone is picked', async () => {
    const { send, getByLabelText } = await open()
    expect(send().textContent).toBe('Send invite')
    expect((send() as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(getByLabelText(/Ben/))
    expect(send().textContent).toBe('Send invite to Ben')
  })

  it('tap-jacking: a tap on Send within 600 ms of the sheet appearing does nothing', async () => {
    h.sendInvite.mockResolvedValue('sess-1')
    const { send, onDone } = await open({ purpose: 'play', max: 1, suggest: { instance_id: 'brookside', user_ref: 'ben', is_local: false } })
    // The app suggested Ben (a rematch) and opened the sheet under the
    // person's double tap: Send is already labelled, but the tap is ignored.
    expect(send().textContent).toBe('Send invite to Ben')
    vi.setSystemTime(Date.now() + SEND_ARM_MS - 1)
    fireEvent.click(send())
    await Promise.resolve()
    expect(h.sendInvite).not.toHaveBeenCalled()
    expect(onDone).not.toHaveBeenCalled()
    // A deliberate tap after that sends.
    vi.setSystemTime(Date.now() + 1)
    fireEvent.click(send())
    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1))
    expect(h.sendInvite).toHaveBeenCalledWith('chess', BEN)
    // The app gets only who was picked and the game Social Home opened.
    expect(onDone).toHaveBeenCalledWith([{
      instance_id: 'brookside', user_ref: 'ben', is_local: false,
      display_name: 'Ben · Brookside', session_id: 'sess-1',
    }])
  })

  it('tap-jacking: the 600 ms count from when the list (and a suggestion) appears, not from mount', async () => {
    h.sendInvite.mockResolvedValue('sess-1')
    let arrive!: (v: unknown) => void
    h.getInvitable.mockReturnValue(new Promise(r => { arrive = r }))
    const onDone = vi.fn()
    const view = render(<InviteSheet appId="chess" appName="Chess"
      request={{ purpose: 'play', max: 1, suggest: { instance_id: 'brookside', user_ref: 'ben', is_local: false } }}
      onDone={onDone} />)
    // A slow list: it arrives 2 s after the sheet opened, Ben preselected.
    vi.setSystemTime(Date.now() + 2000)
    arrive({ people: [BEN, MILO], invites: COUNTS })
    await view.findByText('Milo')
    fireEvent.click(view.getByTestId('invite-send'))
    await Promise.resolve()
    expect(h.sendInvite).not.toHaveBeenCalled()
    vi.setSystemTime(Date.now() + SEND_ARM_MS)
    fireEvent.click(view.getByTestId('invite-send'))
    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1))
  })

  it('a suggestion that isn\'t someone the sheet lists selects nobody', async () => {
    const { send } = await open({ purpose: 'play', max: 1, suggest: { instance_id: 'elsewhere', user_ref: 'mallory', is_local: false } })
    expect(send().textContent).toBe('Send invite')
  })

  it('Cancel sends nothing and hands the app an empty list', async () => {
    const { getByText, onDone } = await open()
    fireEvent.click(getByText('Cancel'))
    expect(onDone).toHaveBeenCalledWith([])
    expect(h.sendInvite).not.toHaveBeenCalled()
  })

  it('at the open-invite cap it says so before Send', async () => {
    const { send, getByRole, getByLabelText } = await open(undefined, [BEN, MILO], { ...COUNTS, open: 10 })
    fireEvent.click(getByLabelText(/Ben/))
    expect((send() as HTMLButtonElement).disabled).toBe(true)
    expect(getByRole('alert').textContent).toBe(
      'You have 10 invites nobody has answered yet. Wait for an answer, or take one back in Chess.',
    )
  })

  it('a server refusal at the daily cap reads in plain words and keeps the sheet', async () => {
    h.sendInvite.mockRejectedValue(refused('APP_INVITES_DAILY_LIMIT'))
    const { send, getByLabelText, findByRole, onDone } = await open()
    fireEvent.click(getByLabelText(/Milo/))
    vi.setSystemTime(Date.now() + SEND_ARM_MS)
    fireEvent.click(send())
    expect((await findByRole('alert')).textContent).toBe("You've sent 30 invites today. Try again tomorrow.")
    expect(onDone).not.toHaveBeenCalled()
  })

  it('several people (max > 1): checkboxes, capped at max, one invite each', async () => {
    h.sendInvite.mockResolvedValueOnce('s-ben').mockResolvedValueOnce('s-milo')
    const { send, getByLabelText, onDone } = await open({ purpose: 'play', max: 2 })
    fireEvent.click(getByLabelText(/Ben/))
    fireEvent.click(getByLabelText(/Milo/))
    expect(send().textContent).toBe('Send 2 invites')
    vi.setSystemTime(Date.now() + SEND_ARM_MS)
    fireEvent.click(send())
    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1))
    expect(onDone.mock.calls[0][0].map((p: { session_id: string }) => p.session_id)).toEqual(['s-ben', 's-milo'])
  })

  it('nobody to invite says so', async () => {
    h.getInvitable.mockResolvedValue({ people: [], invites: COUNTS })
    const view = render(<InviteSheet appId="chess" appName="Chess" request={{ purpose: 'play', max: 1 }} onDone={vi.fn()} />)
    expect(await view.findByText("There's nobody you can invite yet.")).toBeTruthy()
  })

  it('a failed load offers Try again', async () => {
    h.getInvitable.mockRejectedValueOnce(new Error('offline')).mockResolvedValue({ people: [MILO], invites: COUNTS })
    const view = render(<InviteSheet appId="chess" appName="Chess" request={{ purpose: 'play', max: 1 }} onDone={vi.fn()} />)
    fireEvent.click(await view.findByText('Try again'))
    expect(await view.findByText('Milo')).toBeTruthy()
  })

  describe('continue an old game', () => {
    const CONTINUE: PickRequest = {
      purpose: 'continue', max: 1, sessionId: 'old-game',
      suggest: { instance_id: 'brookside', user_ref: 'ben', is_local: false },
    }

    it('names the game and only the person the app gave, and Social Home re-registers it', async () => {
      h.continueGame.mockResolvedValue('old-game')
      const { getByRole, queryByText, send, onDone } = await openContinue()
      expect(getByRole('dialog').textContent).toContain('Continue a game in Chess')
      expect(queryByText('Milo')).toBeNull() // nobody else is offered
      expect(send().textContent).toBe('Continue game with Ben')
      // The same tap-jacking guard.
      fireEvent.click(send())
      await Promise.resolve()
      expect(h.continueGame).not.toHaveBeenCalled()
      vi.setSystemTime(Date.now() + SEND_ARM_MS)
      fireEvent.click(send())
      await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1))
      expect(h.continueGame).toHaveBeenCalledWith('chess', 'old-game', BEN)
      expect(h.sendInvite).not.toHaveBeenCalled()
      expect(onDone.mock.calls[0][0][0].session_id).toBe('old-game')
    })

    it('someone you can no longer play with: says so, nothing to send', async () => {
      h.getInvitable.mockResolvedValue({ people: [MILO], invites: COUNTS })
      const view = render(<InviteSheet appId="chess" appName="Chess" request={CONTINUE} onDone={vi.fn()} />)
      expect(await view.findByText(/You can't continue this game/)).toBeTruthy()
      expect((view.getByTestId('invite-send') as HTMLButtonElement).disabled).toBe(true)
    })

    it('a game already going with someone else reads in plain words', async () => {
      const err = new ApiError(409, 'x') as ApiError & { code: string }
      Object.assign(err, { code: 'APP_SESSION_TAKEN' })
      h.continueGame.mockRejectedValue(err)
      const { send, findByRole, onDone } = await openContinue()
      vi.setSystemTime(Date.now() + SEND_ARM_MS)
      fireEvent.click(send())
      expect((await findByRole('alert')).textContent).toBe('That game is already going with someone else.')
      expect(onDone).not.toHaveBeenCalled()
    })

    async function openContinue() {
      h.getInvitable.mockResolvedValue({ people: [BEN, MILO], invites: COUNTS })
      const onDone = vi.fn()
      const view = render(<InviteSheet appId="chess" appName="Chess" request={CONTINUE} onDone={onDone} />)
      await view.findByText('Ben')
      return { ...view, onDone, send: () => view.getByTestId('invite-send') }
    }
  })
})
