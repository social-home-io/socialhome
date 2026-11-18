import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/preact'

const showToast = vi.hoisted(() => vi.fn())
vi.mock('./Toast', () => ({ showToast }))

import { HouseholdId } from './HouseholdId'

const ID = 'ppsfwqhik3nxgtak6o6m3hxo5i6wwj2v'

describe('HouseholdId', () => {
  beforeEach(() => { showToast.mockReset() })

  it('shows the whole ID in groups of four, so two people can compare it', () => {
    const view = render(<HouseholdId id={ID} />)
    const code = view.getByTestId('household-id')
    // Selecting or copying the text gives the ID itself, without spaces.
    expect(code.textContent).toBe(ID)
    const groups = Array.from(code.querySelectorAll('span')).map(s => s.textContent)
    expect(groups).toEqual(['ppsf', 'wqhi', 'k3nx', 'gtak', '6o6m', '3hxo', '5i6w', 'wj2v'])
  })

  it('copies the plain ID', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    const view = render(<HouseholdId id={ID} />)
    fireEvent.click(view.getByRole('button', { name: 'Copy household ID' }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(ID))
    await waitFor(() => expect(showToast).toHaveBeenCalledWith(expect.any(String), 'success'))
  })

  it('says so when copying is not possible', async () => {
    Object.assign(navigator, { clipboard: { writeText: vi.fn().mockRejectedValue(new Error('no')) } })
    const view = render(<HouseholdId id={ID} />)
    fireEvent.click(view.getByRole('button', { name: 'Copy household ID' }))
    await waitFor(() => expect(showToast).toHaveBeenCalledWith(expect.any(String), 'error'))
  })

  it('can leave the copy button out', () => {
    const view = render(<HouseholdId id={ID} copy={false} />)
    expect(view.queryByRole('button')).toBeNull()
  })
})
