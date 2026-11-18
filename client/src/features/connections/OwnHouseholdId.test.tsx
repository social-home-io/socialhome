import { describe, it, expect, vi } from 'vitest'
import { render } from '@testing-library/preact'

vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))

import { instanceConfig } from '@/store/instance'
import { OwnHouseholdId } from './OwnHouseholdId'

const ID = 'ab2cd3ef4gh5ij6kl7mn2op3qr4st5uv'

function setId(instance_id: string | null) {
  instanceConfig.value = {
    mode: 'standalone', instance_name: 'Test', instance_id, capabilities: [], setup_required: false,
  }
}

describe('OwnHouseholdId', () => {
  it('shows this household\'s own ID, with what it is for', () => {
    setId(ID)
    const view = render(<OwnHouseholdId />)
    expect(view.getByRole('heading', { name: 'Your household ID' })).toBeTruthy()
    expect(view.getByTestId('household-id').textContent).toBe(ID)
    expect(view.getByText(
      "Another household may ask for this to check it's really you — for example before making you an admin of their space.",
    )).toBeTruthy()
    expect(view.getByRole('button', { name: 'Copy household ID' })).toBeTruthy()
  })

  it('renders nothing before the household has an ID', () => {
    setId(null)
    expect(render(<OwnHouseholdId />).container.innerHTML).toBe('')
    instanceConfig.value = null
    expect(render(<OwnHouseholdId />).container.innerHTML).toBe('')
  })
})
