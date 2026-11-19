import { describe, it, expect, vi } from 'vitest'
import { render } from '@testing-library/preact'

vi.mock('./Toast', () => ({ showToast: vi.fn() }))

import { AdminGrantCheck } from './AdminGrantCheck'

const ID = 'ppsfwqhik3nxgtak6o6m3hxo5i6wwj2v'
const WARNING = "As an admin, this household can act for the space while you're offline."

describe('AdminGrantCheck', () => {
  it('names the household by its ID and says what an admin can do', () => {
    const view = render(<AdminGrantCheck householdId={ID} actsForSpace />)
    expect(view.getByText('Household ID')).toBeTruthy()
    expect(view.getByTestId('household-id').textContent).toBe(ID)
    expect(view.getByText(WARNING)).toBeTruthy()
    expect(view.getByText(
      'Check the household ID with them first — they find it on their Connections page.',
    )).toBeTruthy()
  })

  it('a space whose admins do not act for the owner shows the ID without that line', () => {
    const view = render(<AdminGrantCheck householdId={ID} actsForSpace={false} />)
    expect(view.getByTestId('household-id').textContent).toBe(ID)
    expect(view.queryByText(WARNING)).toBeNull()
  })

  it('is one group, labelled by the "Household ID" caption', () => {
    const view = render(<AdminGrantCheck householdId={ID} actsForSpace />)
    const labelId = view.getByRole('group').getAttribute('aria-labelledby')
    expect(labelId && view.container.querySelector(`[id="${labelId}"]`)?.textContent).toBe('Household ID')
  })
})
