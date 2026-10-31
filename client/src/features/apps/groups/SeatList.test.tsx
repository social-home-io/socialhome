/**
 * SeatList — who's in a group game (shared with the space Games section).
 *
 * Pinned here: only seated people show, in seat order; the person who
 * started the game and "you" carry their chips; your own household reads
 * "Your household"; free places collapse after two rows; Remove shows
 * only when the caller hands ``onRemove`` (the starter's view), never on
 * your own row, and names the person for a screen reader.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'
import { SeatList, FREE_ROWS } from './SeatList'
import type { GroupPlayer } from '@/features/apps/groupBridge'

const P = (seat: number, name: string, household: string | null, state: GroupPlayer['state'] = 'seated'): GroupPlayer =>
  ({ seat, name, household, isLocal: household === null, state })

const PLAYERS = [
  P(3, 'Cleo', 'Krämer'),
  P(1, 'Anna', null),
  P(2, 'Ben', 'Okafor'),
  P(4, 'Dev', 'Müller', 'left'),
  P(5, 'Eli', 'Müller', 'removed'),
]

describe('SeatList', () => {
  it('lists the seated people in seat order, never those who left or were removed', () => {
    const { getAllByRole, queryByText } = render(<SeatList players={PLAYERS} max={3} host={1} me={2} />)
    const names = getAllByRole('listitem').map(li => li.querySelector('.sh-seat-list__name')?.textContent)
    expect(names).toEqual(['Anna', 'Ben', 'Cleo'])
    expect(queryByText('Dev')).toBeNull()
    expect(queryByText('Eli')).toBeNull()
  })

  it('marks who started the game and you; your own household reads "Your household"', () => {
    const { getAllByRole } = render(<SeatList players={PLAYERS} max={3} host={1} me={2} />)
    const [anna, ben, cleo] = getAllByRole('listitem')
    expect(anna.textContent).toContain('Started this game')
    expect(anna.textContent).toContain('Your household')
    expect(anna.textContent).not.toContain('You ')
    expect(ben.textContent).toContain('You')
    expect(ben.textContent).not.toContain('Started this game')
    expect(cleo.textContent).toContain('Krämer')
  })

  it('shows up to two free places, then how many more', () => {
    const { getAllByText, getByText } = render(<SeatList players={PLAYERS} max={12} host={1} me={1} />)
    expect(FREE_ROWS).toBe(2)
    expect(getAllByText('Free seat')).toHaveLength(2)
    expect(getByText('+ 7 more free seats')).toBeTruthy()
  })

  it('one more free place reads in the singular; exactly two shows no summary', () => {
    const one = render(<SeatList players={PLAYERS} max={6} host={1} me={1} />)
    expect(one.getByText('+ 1 more free seat')).toBeTruthy()
    one.unmount()
    const two = render(<SeatList players={PLAYERS} max={5} host={1} me={1} />)
    expect(two.getAllByText('Free seat')).toHaveLength(2)
    expect(two.queryByText(/more free/)).toBeNull()
  })

  it('a full game shows no free place', () => {
    const { queryByText } = render(<SeatList players={PLAYERS} max={3} host={1} me={1} />)
    expect(queryByText('Free seat')).toBeNull()
  })

  it('no Remove without onRemove', () => {
    const { queryByRole } = render(<SeatList players={PLAYERS} max={3} host={1} me={1} />)
    expect(queryByRole('button')).toBeNull()
  })

  it('Remove on every other player, named for a screen reader, never on your own row', () => {
    const onRemove = vi.fn()
    const { getAllByRole, getByRole } = render(
      <SeatList players={PLAYERS} max={3} host={1} me={1} onRemove={onRemove} />,
    )
    const buttons = getAllByRole('button')
    expect(buttons.map(b => b.getAttribute('aria-label'))).toEqual(['Remove Ben', 'Remove Cleo'])
    expect(buttons[0].textContent).toBe('Remove')
    fireEvent.click(getByRole('button', { name: 'Remove Cleo' }))
    expect(onRemove).toHaveBeenCalledWith(3)
  })

  it('is a labelled list', () => {
    const { getByRole } = render(<SeatList players={PLAYERS} max={3} host={1} me={1} label="Who's in" />)
    expect(getByRole('list', { name: "Who's in" })).toBeTruthy()
  })
})
