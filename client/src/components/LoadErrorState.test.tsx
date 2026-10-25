import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'
import { LoadErrorState, StaleNotice } from './LoadErrorState'

describe('LoadErrorState', () => {
  it('announces the failure and offers Retry', () => {
    const onRetry = vi.fn()
    const { getByRole } = render(
      <LoadErrorState message="Couldn't load the shopping list." onRetry={onRetry} />,
    )
    const alert = getByRole('alert')
    expect(alert.className).toContain('sh-empty-state')
    expect(alert.textContent).toContain("Couldn't load the shopping list.")
    fireEvent.click(getByRole('button', { name: 'Retry' }))
    expect(onRetry).toHaveBeenCalledTimes(1)
  })

  it('falls back to a generic message', () => {
    const { getByRole } = render(<LoadErrorState onRetry={() => {}} />)
    expect(getByRole('alert').textContent).toContain("Couldn't load this list.")
  })

  it('takes an explanation and its own button label', () => {
    const onRetry = vi.fn()
    const { getByRole } = render(
      <LoadErrorState message="Couldn't load moments" detail="Nothing is lost." retryLabel="Try again" onRetry={onRetry} />,
    )
    expect(getByRole('alert').querySelector('p')?.textContent).toBe('Nothing is lost.')
    fireEvent.click(getByRole('button', { name: 'Try again' }))
    expect(onRetry).toHaveBeenCalledTimes(1)
  })
})

describe('StaleNotice', () => {
  it('says the list is kept and from when, with Try again', () => {
    const onRetry = vi.fn()
    const { getByRole } = render(
      <StaleNotice message="Couldn't refresh. Showing moments from 13:02." onRetry={onRetry} />,
    )
    const note = getByRole('status')
    expect(note.textContent).toContain('Showing moments from 13:02.')
    fireEvent.click(getByRole('button', { name: 'Try again' }))
    expect(onRetry).toHaveBeenCalledTimes(1)
  })

  it('can be an alert when nothing else on screen says it failed', () => {
    const { getByRole } = render(<StaleNotice message="Can't reach Demo GFS." onRetry={() => {}} urgent />)
    expect(getByRole('alert').textContent).toContain("Can't reach Demo GFS.")
  })
})
