import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'
import { LoadErrorState } from './LoadErrorState'

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
})
