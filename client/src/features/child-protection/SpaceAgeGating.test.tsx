import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

const apiGet = vi.fn()
const apiPatch = vi.fn()

vi.mock('@/api', () => ({
  api: {
    get: (...args: unknown[]) => apiGet(...args),
    patch: (...args: unknown[]) => apiPatch(...args),
    post: vi.fn().mockResolvedValue({}),
    delete: vi.fn().mockResolvedValue(undefined),
  },
}))

vi.mock('@/ws', () => ({
  ws: { on: vi.fn(() => () => {}) },
}))

vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))

import { SpaceAgeGating } from './SpaceAgeGating'

const SPACE_ID = 'space-123'

beforeEach(() => {
  apiGet.mockReset()
  apiPatch.mockReset()
  apiPatch.mockResolvedValue({})
  // Default: cp age-gate returns min_age only; space returns a category.
  apiGet.mockImplementation((url: string) => {
    if (url === `/api/cp/spaces/${SPACE_ID}/age-gate`) {
      return Promise.resolve({ min_age: 13 })
    }
    if (url === `/api/spaces/${SPACE_ID}`) {
      return Promise.resolve({ category: 'gaming' })
    }
    return Promise.resolve({})
  })
})

describe('SpaceAgeGating', () => {
  it('titles the tab with an h2 — one level under the settings page h1', async () => {
    const { findByText, container } = render(<SpaceAgeGating spaceId={SPACE_ID} />)
    await findByText('Minimum age')
    expect(container.querySelector('h2.sh-settings-section-heading')).not.toBeNull()
    expect(container.querySelector('h3, h4')).toBeNull()
  })

  it('does not render the old audience age-band options', async () => {
    const { findByText, queryByText } = render(<SpaceAgeGating spaceId={SPACE_ID} />)
    await findByText('Minimum age')
    expect(queryByText('Audience')).toBeNull()
    expect(queryByText('Family')).toBeNull()
    expect(queryByText('Teen')).toBeNull()
    expect(queryByText('Adult')).toBeNull()
  })

  it('saves min_age (only) via the cp age-gate endpoint', async () => {
    const { findByLabelText, getByText } = render(<SpaceAgeGating spaceId={SPACE_ID} />)
    const minAge = await findByLabelText('Minimum age') as HTMLSelectElement
    fireEvent.change(minAge, { target: { value: '18' } })
    fireEvent.click(getByText('Save'))
    await waitFor(() => {
      expect(apiPatch).toHaveBeenCalledWith(
        `/api/cp/spaces/${SPACE_ID}/age-gate`,
        { min_age: 18 },
      )
    })
  })

  it('renders a Category select and saves it via the space PATCH', async () => {
    const { findByLabelText, getByText } = render(<SpaceAgeGating spaceId={SPACE_ID} />)
    const category = await findByLabelText('Category') as HTMLSelectElement
    // initial value loaded from GET /api/spaces/{id}
    expect(category.value).toBe('gaming')
    fireEvent.change(category, { target: { value: 'tech' } })
    fireEvent.click(getByText('Save'))
    await waitFor(() => {
      expect(apiPatch).toHaveBeenCalledWith(
        `/api/spaces/${SPACE_ID}`,
        { category: 'tech' },
      )
    })
  })

  it('defaults category to general when the space has none', async () => {
    apiGet.mockImplementation((url: string) => {
      if (url === `/api/cp/spaces/${SPACE_ID}/age-gate`) {
        return Promise.resolve({ min_age: 0 })
      }
      if (url === `/api/spaces/${SPACE_ID}`) {
        return Promise.resolve({})
      }
      return Promise.resolve({})
    })
    const { findByLabelText } = render(<SpaceAgeGating spaceId={SPACE_ID} />)
    const category = await findByLabelText('Category') as HTMLSelectElement
    expect(category.value).toBe('general')
  })
})
