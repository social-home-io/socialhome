import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'
import { PeoplePicker } from './PeoplePicker'

const people = [
  { user_id: 'a', name: 'Ann' },
  { user_id: 'b', name: 'Ben', detail: 'Millers' },
  { user_id: 'c', name: 'Cat' },
]

describe('PeoplePicker', () => {
  it('toggles people in pick order, with aria-pressed', () => {
    const onChange = vi.fn()
    const r = render(<PeoplePicker people={people} value={['c']} onChange={onChange} legend="Who" />)
    expect(r.getByRole('group', { name: 'Who' })).toBeTruthy()
    expect(r.getByRole('button', { name: /Cat/ }).getAttribute('aria-pressed')).toBe('true')
    fireEvent.click(r.getByRole('button', { name: /Ann/ }))
    expect(onChange).toHaveBeenLastCalledWith(['c', 'a'])
    fireEvent.click(r.getByRole('button', { name: /Cat/ }))
    expect(onChange).toHaveBeenLastCalledWith([])
  })

  it('shows a remote member’s household', () => {
    const r = render(<PeoplePicker people={people} value={[]} onChange={vi.fn()} legend="Who" />)
    expect(r.getByRole('button', { name: /Ben/ }).textContent).toContain('Millers')
  })

  it('at max, the unpicked disable and the hint says why', () => {
    const r = render(<PeoplePicker people={people} value={['a', 'b']} onChange={vi.fn()} legend="Who"
                                   max={2} maxHint="Up to 2" />)
    expect((r.getByRole('button', { name: /Cat/ }) as HTMLButtonElement).disabled).toBe(true)
    expect((r.getByRole('button', { name: /Ann/ }) as HTMLButtonElement).disabled).toBe(false)
    expect(r.getByText('Up to 2')).toBeTruthy()
  })

  it('an empty roster says so', () => {
    const r = render(<PeoplePicker people={[]} value={[]} onChange={vi.fn()} legend="Who" emptyText="Nobody" />)
    expect(r.getByText('Nobody')).toBeTruthy()
  })
})
