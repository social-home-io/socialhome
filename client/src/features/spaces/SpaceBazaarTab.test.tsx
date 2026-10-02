import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'

const apiGet = vi.fn()
const openBazaarCreate = vi.fn()

vi.mock('@/api', () => ({ api: { get: (...a: unknown[]) => apiGet(...a) } }))
vi.mock('@/ws', () => ({ ws: { on: () => () => {} } }))
vi.mock('@/components/Skeleton', () => ({ BazaarSkeleton: () => null }))
vi.mock('@/components/BazaarPostBody', () => ({
  BazaarPostBody: () => null,
  formatBazaarAmount: () => '',
}))
vi.mock('@/components/BazaarCreateDialog', () => ({
  BazaarCreateDialog: () => null,
  openBazaarCreate: (...a: unknown[]) => openBazaarCreate(...a),
}))
vi.mock('@/features/bazaar/BazaarPage', () => ({
  BazaarCard: ({ listing }: { listing: { title: string } }) => (
    <div data-testid="bazaar-card">{listing.title}</div>
  ),
}))

const tick = () => new Promise((r) => setTimeout(r, 0))

describe('SpaceBazaarTab', () => {
  beforeEach(() => {
    apiGet.mockReset()
    openBazaarCreate.mockReset()
  })

  it('queries the space-scoped bazaar endpoint on mount', async () => {
    apiGet.mockResolvedValue([])
    const { SpaceBazaarTab } = await import('./SpaceBazaarTab')
    render(<SpaceBazaarTab spaceId="sp-1" />)
    await tick()
    expect(apiGet).toHaveBeenCalledWith('/api/spaces/sp-1/bazaar')
  })

  it('shows an empty state with a New listing CTA when there are none', async () => {
    apiGet.mockResolvedValue([])
    const { SpaceBazaarTab } = await import('./SpaceBazaarTab')
    const { getByText, getAllByText } = render(<SpaceBazaarTab spaceId="sp-1" />)
    await tick()
    expect(getByText('Nothing listed yet')).toBeTruthy()
    // Both the header and the empty-state render a "+ New listing" CTA;
    // either opens the create dialog pre-targeted to this space.
    fireEvent.click(getAllByText('+ New listing')[0])
    expect(openBazaarCreate).toHaveBeenCalledWith('sp-1')
  })

  it('renders a card per listing in the space', async () => {
    apiGet.mockResolvedValue([
      { post_id: 'p1', title: 'Bike', status: 'active', mode: 'fixed',
        image_urls: [], currency: 'EUR' },
      { post_id: 'p2', title: 'Lamp', status: 'active', mode: 'fixed',
        image_urls: [], currency: 'EUR' },
    ])
    const { SpaceBazaarTab } = await import('./SpaceBazaarTab')
    const { getAllByTestId } = render(<SpaceBazaarTab spaceId="sp-2" />)
    await tick()
    expect(getAllByTestId('bazaar-card')).toHaveLength(2)
  })
})

describe('SpaceBazaarTab — ADMIN_ONLY posts (§4.3)', () => {
  it('a member gets the admin-only note instead of New listing', async () => {
    apiGet.mockResolvedValue([])
    const { SpaceBazaarTab } = await import('./SpaceBazaarTab')
    const { queryAllByText, getByText } = render(
      <SpaceBazaarTab spaceId="sp-1" canSell={false} />,
    )
    await tick()
    expect(queryAllByText('+ New listing')).toHaveLength(0)
    expect(getByText('Only admins can post here.')).toBeTruthy()
  })
})
