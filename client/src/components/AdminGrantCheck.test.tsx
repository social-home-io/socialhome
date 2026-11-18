import { describe, it, expect, vi } from 'vitest'
import { render } from '@testing-library/preact'

vi.mock('./Toast', () => ({ showToast: vi.fn() }))

import { AdminGrantCheck } from './AdminGrantCheck'

const ID = 'ppsfwqhik3nxgtak6o6m3hxo5i6wwj2v'

describe('AdminGrantCheck', () => {
  it('names the household by its ID and says what an admin can do', () => {
    const view = render(<AdminGrantCheck householdId={ID} />)
    expect(view.getByText('Household ID')).toBeTruthy()
    expect(view.getByTestId('household-id').textContent).toBe(ID)
    expect(view.getByText(
      "As an admin, this household can act for the space while you're offline.",
    )).toBeTruthy()
    expect(view.getByText(
      'Check the household ID with them first — they find it on their Connections page.',
    )).toBeTruthy()
  })

  it('is one labelled group for screen readers', () => {
    const view = render(<AdminGrantCheck householdId={ID} />)
    expect(view.getByRole('group').getAttribute('aria-label')).toBe('Household ID')
  })
})
