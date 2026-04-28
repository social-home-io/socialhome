import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'

const { apiMock } = vi.hoisted(() => ({
  apiMock: { post: vi.fn(), delete: vi.fn() },
}))

vi.mock('@/api', () => ({ api: apiMock }))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))

import { SubscribeFeed } from './SubscribeFeed'

beforeEach(() => {
  apiMock.post.mockReset()
  apiMock.delete.mockReset()
})

// Module-level signal cache in SubscribeFeed leaks between tests; use
// distinct space IDs per test so each starts from a clean state.
describe('SubscribeFeed', () => {
  it('renders the empty state with a "Create" button initially', () => {
    const { container } = render(<SubscribeFeed spaceId="sp-empty" />)
    expect(container.textContent).toContain('No feed URL yet')
    expect(container.textContent).toContain('Create feed URL')
  })

  it('mints a token on Create click', async () => {
    apiMock.post.mockResolvedValueOnce({
      token: 'abc',
      url: '/api/spaces/sp-mint/calendar/export.ics?token=abc',
    })
    const { container, findByText } = render(<SubscribeFeed spaceId="sp-mint" />)
    const create = container.querySelector('button')!
    fireEvent.click(create)
    await Promise.resolve()
    expect(apiMock.post).toHaveBeenCalledWith(
      '/api/spaces/sp-mint/calendar/feed-token',
      {},
    )
    await findByText('Copy')
  })

  it('reveals the masked URL on initial render after mint', async () => {
    apiMock.post.mockResolvedValueOnce({
      token: 'spaceXYZ',
      url: '/api/spaces/sp-reveal/calendar/export.ics?token=spaceXYZ',
    })
    const { container, findByLabelText } = render(
      <SubscribeFeed spaceId="sp-reveal" />,
    )
    fireEvent.click(container.querySelector('button')!)
    const input = (await findByLabelText('Calendar feed URL')) as HTMLInputElement
    expect(input.value).toContain('token=spaceXYZ')
  })

  it('shows per-app instruction tabs in the accordion', () => {
    const { container } = render(<SubscribeFeed spaceId="sp-tabs" />)
    expect(container.textContent).toContain('Apple Calendar')
    expect(container.textContent).toContain('Google Calendar')
    expect(container.textContent).toContain('Outlook')
    expect(container.textContent).toContain('Thunderbird')
  })
})
