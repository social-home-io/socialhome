import { describe, it, expect } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'
import { useState } from 'preact/hooks'
import { DaysPicker } from './DaysPicker'
import type { WeekStart } from '@/utils/week'

function Harness({ initial = [0, 1, 2, 3, 4], weekStart = 0 as WeekStart }) {
  const [days, setDays] = useState<number[]>(initial)
  return (
    <>
      <DaysPicker value={days} onChange={setDays} weekStart={weekStart} />
      <output data-testid="out">{days.join(',')}</output>
    </>
  )
}

describe('DaysPicker', () => {
  it('defaults to the Mon–Fri preset, pressed', () => {
    const { getByRole, queryByRole } = render(<Harness />)
    expect(getByRole('button', { name: 'Mon–Fri' }).getAttribute('aria-pressed')).toBe('true')
    expect(queryByRole('group', { name: 'Pick the days' })).toBeNull()
  })

  it('applies a preset', () => {
    const { getByRole, getByTestId } = render(<Harness />)
    fireEvent.click(getByRole('button', { name: 'Mon–Sat' }))
    expect(getByTestId('out').textContent).toBe('0,1,2,3,4,5')
    fireEvent.click(getByRole('button', { name: 'Every day' }))
    expect(getByTestId('out').textContent).toBe('0,1,2,3,4,5,6')
  })

  it('Custom shows seven day toggles in week-start order', () => {
    const { getByRole } = render(<Harness weekStart={6} />)
    fireEvent.click(getByRole('button', { name: 'Custom' }))
    const group = getByRole('group', { name: 'Pick the days' })
    const buttons = Array.from(group.querySelectorAll('button'))
    expect(buttons.map(b => b.textContent)).toEqual(['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'])
    expect(buttons.map(b => b.getAttribute('aria-pressed'))).toEqual(
      ['false', 'true', 'true', 'true', 'true', 'true', 'false'])
  })

  it('toggles single days', () => {
    const { getByRole, getByTestId } = render(<Harness />)
    fireEvent.click(getByRole('button', { name: 'Custom' }))
    fireEvent.click(getByRole('button', { name: 'Wednesday' }))
    expect(getByTestId('out').textContent).toBe('0,1,3,4')
    fireEvent.click(getByRole('button', { name: 'Saturday' }))
    expect(getByTestId('out').textContent).toBe('0,1,3,4,5')
  })

  it('never allows an empty selection', () => {
    const { getByRole, getByTestId, getByText } = render(<Harness initial={[2]} />)
    // A non-preset selection opens straight into Custom.
    fireEvent.click(getByRole('button', { name: 'Wednesday' }))
    expect(getByTestId('out').textContent).toBe('2')
    expect(getByText('A timetable needs at least one day.')).toBeTruthy()
  })
})
