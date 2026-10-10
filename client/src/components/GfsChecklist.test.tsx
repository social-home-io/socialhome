import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'

vi.mock('@/i18n/i18n', () => ({ t: (k: string) => k }))

import { GfsChecklist, defaultGfsSelection, gfsHost } from './GfsChecklist'

const A = { id: 'a', display_name: 'Town GFS', inbox_url: 'https://town.example/' }
const B = { id: 'b', display_name: 'Club GFS', inbox_url: 'https://club.example:8443' }
const C = { id: 'c', display_name: 'Odd GFS', inbox_url: 'not a url' }

describe('gfsHost', () => {
  it('shows the host, port included, and falls back to the raw text', () => {
    expect(gfsHost(A.inbox_url)).toBe('town.example')
    expect(gfsHost(B.inbox_url)).toBe('club.example:8443')
    expect(gfsHost(C.inbox_url)).toBe('not a url')
  })
})

describe('defaultGfsSelection', () => {
  it('ticks the only GFS, and nothing when there is a real choice', () => {
    expect(defaultGfsSelection([A])).toEqual(['a'])
    expect(defaultGfsSelection([A, B])).toEqual([])
    expect(defaultGfsSelection([])).toEqual([])
  })
})

describe('GfsChecklist', () => {
  it('renders one labelled checkbox per GFS, ticked per the selection', () => {
    const { container, getByLabelText } = render(
      <GfsChecklist name="t" choices={[A, B]} selected={['b']} onChange={() => {}} />,
    )
    expect(container.querySelector('legend')!.textContent).toContain('space.gfs_choice.legend')
    expect((getByLabelText(/Town GFS/) as HTMLInputElement).checked).toBe(false)
    expect((getByLabelText(/Club GFS/) as HTMLInputElement).checked).toBe(true)
  })

  it('adds and removes ids through onChange', () => {
    const onChange = vi.fn()
    const { getByLabelText, rerender } = render(
      <GfsChecklist name="t" choices={[A, B]} selected={[]} onChange={onChange} />,
    )
    fireEvent.click(getByLabelText(/Town GFS/))
    expect(onChange).toHaveBeenLastCalledWith(['a'])
    rerender(<GfsChecklist name="t" choices={[A, B]} selected={['a', 'b']} onChange={onChange} />)
    fireEvent.click(getByLabelText(/Town GFS/))
    expect(onChange).toHaveBeenLastCalledWith(['b'])
  })

  it('disables unticked boxes once the cap is reached', () => {
    const { getByLabelText } = render(
      <GfsChecklist name="t" choices={[A, B]} selected={['a']} max={1} onChange={() => {}} />,
    )
    expect((getByLabelText(/Town GFS/) as HTMLInputElement).disabled).toBe(false)
    expect((getByLabelText(/Club GFS/) as HTMLInputElement).disabled).toBe(true)
  })

  it('ties the error to the group for screen readers', () => {
    const { container } = render(
      <GfsChecklist name="t" choices={[A]} selected={[]} onChange={() => {}} error="pick one" />,
    )
    const fs = container.querySelector('fieldset')!
    expect(fs.getAttribute('aria-describedby')).toBe('t-error')
    expect(container.querySelector('#t-error')!.textContent).toBe('pick one')
    expect(container.querySelector('input')!.getAttribute('aria-invalid')).toBe('true')
  })
})
