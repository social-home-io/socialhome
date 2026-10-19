import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, fireEvent, waitFor, cleanup } from '@testing-library/preact'
import { JoinRequestModal } from './JoinRequestModal'

afterEach(() => { cleanup() })

const base = {
  open: true,
  onClose: () => {},
  spaceName: 'Quartier',
  hostDisplayName: 'Keller household',
  hostIsPaired: true,
}

describe('JoinRequestModal — a local community', () => {
  it('shows the consent line beside Send; sending is agreeing', async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    const view = render(
      <JoinRequestModal {...base} onSubmit={onSubmit} locationNote="Your approximate location goes along." />,
    )
    expect(view.getByTestId('join-request-location-note').textContent)
      .toContain('Your approximate location goes along.')
    fireEvent.click(view.getByText('Send request'))
    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith(''))
  })

  it('without a household home: says why and Send stays off', () => {
    const onSubmit = vi.fn()
    const view = render(
      <JoinRequestModal {...base} onSubmit={onSubmit} locationNote="x" locationBlocked="No home yet." />,
    )
    expect(view.getByTestId('join-request-location-blocked').textContent).toContain('No home yet.')
    expect(view.queryByTestId('join-request-location-note')).toBeNull()
    const send = view.getByText('Send request').closest('button') as HTMLButtonElement
    expect(send.disabled).toBe(true)
  })

  it('an ordinary space shows neither', () => {
    const view = render(<JoinRequestModal {...base} onSubmit={vi.fn()} />)
    expect(view.queryByTestId('join-request-location-note')).toBeNull()
    expect(view.queryByTestId('join-request-location-blocked')).toBeNull()
  })
})
