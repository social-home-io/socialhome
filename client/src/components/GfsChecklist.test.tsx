import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'

vi.mock('@/i18n/i18n', () => ({ t: (k: string) => k }))

import { createRef } from 'preact'
import { GfsChecklist, defaultGfsSelection, gfsHost, revealGfsChecklist } from './GfsChecklist'

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

  it('takes another legend (a private channel is never "listed")', () => {
    const { container } = render(
      <GfsChecklist
        name="t" choices={[A]} selected={[]} onChange={() => {}}
        legend="space.private_gfs.channels_legend" icon="🛰️"
      />,
    )
    const legend = container.querySelector('legend')!.textContent
    expect(legend).toContain('space.private_gfs.channels_legend')
    expect(legend).toContain('🛰️')
    expect(legend).not.toContain('🌐')
    expect(legend).not.toContain('space.gfs_choice.legend')
  })

  it('composes two clicks that land before the parent re-renders', () => {
    // Both clicks fire in one tick: the second must build on the first's
    // result, not on the stale ``selected`` prop, or a choice is dropped.
    const onChange = vi.fn()
    const { getByLabelText } = render(
      <GfsChecklist name="t" choices={[A, B]} selected={['a', 'b']} onChange={onChange} />,
    )
    fireEvent.click(getByLabelText(/Town GFS/))
    fireEvent.click(getByLabelText(/Club GFS/))
    expect(onChange.mock.calls).toEqual([[['b']], [[]]])
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
    const { getByLabelText, getByTestId } = render(
      <GfsChecklist name="t" choices={[A, B]} selected={['a']} max={1} onChange={() => {}} />,
    )
    expect((getByLabelText(/Town GFS/) as HTMLInputElement).disabled).toBe(false)
    expect((getByLabelText(/Club GFS/) as HTMLInputElement).disabled).toBe(true)
    // …and says why the rest are greyed out.
    expect(getByTestId('t-at-max').textContent).toBe('error.gfs_selection.too_many')
  })

  it('shows no cap hint while every box can still be ticked', () => {
    const { queryByTestId } = render(
      <GfsChecklist name="t" choices={[A, B]} selected={['a', 'b']} max={2} onChange={() => {}} />,
    )
    expect(queryByTestId('t-at-max')).toBeNull()
  })

  it('keeps the decorative globe away from screen readers', () => {
    const { container } = render(
      <GfsChecklist name="t" choices={[A]} selected={[]} onChange={() => {}} />,
    )
    expect(container.querySelector('legend [aria-hidden="true"]')!.textContent).toBe('🌐')
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

describe('revealGfsChecklist', () => {
  it('focuses the first free box of the checklist its ref points at, not a namesake', () => {
    vi.useFakeTimers()
    try {
      const own = createRef<HTMLFieldSetElement>()
      const { getAllByRole } = render(
        <>
          {/* Same name earlier in the page (e.g. a second dialog). */}
          <GfsChecklist name="space-tier" choices={[A]} selected={[]} onChange={() => {}} />
          <GfsChecklist ref={own} name="space-tier" choices={[A, B]} selected={['a']}
                        max={1} onChange={() => {}} />
        </>,
      )
      expect(own.current?.tagName).toBe('FIELDSET')
      const boxes = getAllByRole('checkbox') as HTMLInputElement[]
      revealGfsChecklist(own)
      vi.advanceTimersByTime(0)
      // Box 2 ("a", ticked) is the first enabled one: "b" is greyed at max.
      expect(document.activeElement).toBe(boxes[1])
    } finally {
      vi.useRealTimers()
    }
  })

  it('does nothing while the checklist is not shown', () => {
    vi.useFakeTimers()
    try {
      const before = document.activeElement
      revealGfsChecklist(createRef<HTMLFieldSetElement>())
      vi.advanceTimersByTime(0)
      expect(document.activeElement).toBe(before)
    } finally {
      vi.useRealTimers()
    }
  })
})
