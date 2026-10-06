import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

const { apiMock, confirmMock, platformMock } = vi.hoisted(() => ({
  apiMock: { post: vi.fn(), delete: vi.fn() },
  confirmMock: vi.fn(),
  platformMock: { addon: false },
}))

vi.mock('@/api', async () => {
  const real = await vi.importActual<typeof import('@/api')>('@/api')
  return { api: apiMock, ApiError: real.ApiError }
})
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))
vi.mock('@/components/confirm', () => ({ confirmDialog: confirmMock }))
vi.mock('@/platform', () => ({ isSupervisorAddon: () => platformMock.addon }))

import { ApiError } from '@/api'
import { SubscribeFeed, webcalUrl } from './SubscribeFeed'
import { currentUser } from '@/store/auth'
import type { User } from '@/types'

const FEED_URL = '/api/spaces/sp1/calendar/export.ics?token=tok123'

beforeEach(() => {
  apiMock.post.mockReset()
  apiMock.delete.mockReset()
  confirmMock.mockReset()
  platformMock.addon = false
})

describe('SubscribeFeed', () => {
  it('starts with a Create button and a way to turn off an earlier link', () => {
    const { getByRole } = render(<SubscribeFeed spaceId="sp1" />)
    expect(getByRole('button', { name: 'Create my link' })).toBeTruthy()
    expect(getByRole('button', { name: 'Turn off my existing link' })).toBeTruthy()
  })

  it('POSTs the feed-token route and shows the server external_url once, with copy + webcal', async () => {
    apiMock.post.mockResolvedValueOnce({
      token: 'tok123',
      url: FEED_URL,
      external_url: `https://home.example.com${FEED_URL}`,
    })
    const { getByRole, findByLabelText, getByText } = render(<SubscribeFeed spaceId="sp1" />)
    fireEvent.click(getByRole('button', { name: 'Create my link' }))
    const code = await findByLabelText('Calendar feed link')
    expect(apiMock.post).toHaveBeenCalledWith('/api/spaces/sp1/calendar/feed-token', {})
    // The copy-out value is the server's absolute public URL — never
    // anchored on document.baseURI (an ingress path under HA).
    expect(code.textContent).toBe(`https://home.example.com${FEED_URL}`)
    expect(getByText(/you won't see it again/)).toBeTruthy()
    expect(getByRole('button', { name: 'Copy' })).toBeTruthy()
    const open = getByRole('link', { name: 'Open in my calendar app' })
    expect(open.getAttribute('href')).toBe(`webcal://home.example.com${FEED_URL}`)
  })

  it('explains there is no public address when external_url is null (no broken link)', async () => {
    apiMock.post.mockResolvedValueOnce({ token: 'tok123', url: FEED_URL, external_url: null })
    const { getByRole, findByText, queryByLabelText, container } = render(
      <SubscribeFeed spaceId="sp1" />,
    )
    fireEvent.click(getByRole('button', { name: 'Create my link' }))
    await findByText("Calendar apps can't reach this Social Home")
    expect(queryByLabelText('Calendar feed link')).toBeNull()
    expect(container.textContent).toContain('Connections page')
    expect(container.querySelector('.sh-subscribe-path')?.textContent).toBe(FEED_URL)
    expect(container.querySelector('a[href^="webcal:"]')).toBeNull()
  })

  it('under the Home Assistant App, says the app is only reachable through HA (no admin hint)', async () => {
    platformMock.addon = true
    apiMock.post.mockResolvedValueOnce({ token: 't', url: FEED_URL, external_url: null })
    const { getByRole, findByText, container } = render(<SubscribeFeed spaceId="sp1" />)
    fireEvent.click(getByRole('button', { name: 'Create my link' }))
    await findByText(/runs as the Home Assistant App/)
    expect(container.textContent).not.toContain('add-on')
    expect(container.textContent).not.toContain('Connections page')
  })

  it('creating a new link asks first and does nothing on cancel', async () => {
    apiMock.post.mockResolvedValue({ token: 't', url: FEED_URL, external_url: `https://h.example${FEED_URL}` })
    const { getByRole, findByRole } = render(<SubscribeFeed spaceId="sp1" />)
    fireEvent.click(getByRole('button', { name: 'Create my link' }))
    const regen = await findByRole('button', { name: 'Create a new link' })
    confirmMock.mockResolvedValueOnce(false)
    fireEvent.click(regen)
    await waitFor(() => expect(confirmMock).toHaveBeenCalledTimes(1))
    expect(apiMock.post).toHaveBeenCalledTimes(1)
    confirmMock.mockResolvedValueOnce(true)
    fireEvent.click(regen)
    await waitFor(() => expect(apiMock.post).toHaveBeenCalledTimes(2))
  })

  it('turning the link off confirms, DELETEs, and returns to the empty state', async () => {
    apiMock.post.mockResolvedValueOnce({ token: 't', url: FEED_URL, external_url: `https://h.example${FEED_URL}` })
    apiMock.delete.mockResolvedValueOnce(undefined)
    confirmMock.mockResolvedValueOnce(true)
    const { getByRole, findByRole } = render(<SubscribeFeed spaceId="sp1" />)
    fireEvent.click(getByRole('button', { name: 'Create my link' }))
    fireEvent.click(await findByRole('button', { name: 'Turn off link' }))
    await waitFor(() =>
      expect(apiMock.delete).toHaveBeenCalledWith('/api/spaces/sp1/calendar/feed-token'),
    )
    await findByRole('button', { name: 'Create my link' })
  })

  it('shows the server error detail inline when minting fails', async () => {
    apiMock.post.mockRejectedValueOnce(
      new ApiError(403, '/api/spaces/sp1/calendar/feed-token', {
        code: 'FORBIDDEN', detail: 'Not a space member.',
      }),
    )
    const { getByRole, findByRole } = render(<SubscribeFeed spaceId="sp1" />)
    fireEvent.click(getByRole('button', { name: 'Create my link' }))
    expect((await findByRole('alert')).textContent).toBe('Not a space member.')
  })

  it('lists setup steps per calendar app', () => {
    const { container } = render(<SubscribeFeed spaceId="sp1" />)
    for (const app of ['Apple Calendar', 'Google Calendar', 'Outlook', 'Thunderbird']) {
      expect(container.textContent).toContain(app)
    }
  })
})

describe('webcalUrl', () => {
  it('maps https to webcal and refuses plain http', () => {
    expect(webcalUrl('https://h.example/x.ics?token=a')).toBe('webcal://h.example/x.ics?token=a')
    expect(webcalUrl('http://h.example/x.ics')).toBeNull()
  })
})

describe('SubscribeFeed — protected account', () => {
  it('offers no new link, explains why, and can still turn an old one off', async () => {
    currentUser.value = {
      user_id: 'u-kid', username: 'kid', display_name: 'Kid', is_admin: false,
      picture_url: null, picture_hash: null, bio: null, is_new_member: false,
      protected: true, restrictions: ['calendar_feeds'],
    } as User
    try {
      confirmMock.mockResolvedValueOnce(true)
      apiMock.delete.mockResolvedValueOnce(undefined)
      const { queryByRole, getByRole } = render(<SubscribeFeed spaceId="sp1" />)
      expect(queryByRole('button', { name: 'Create my link' })).toBeNull()
      expect(getByRole('note').textContent).toContain('Calendar subscription links')
      fireEvent.click(getByRole('button', { name: 'Turn off my existing link' }))
      await waitFor(() =>
        expect(apiMock.delete).toHaveBeenCalledWith('/api/spaces/sp1/calendar/feed-token'),
      )
      expect(apiMock.post).not.toHaveBeenCalled()
    } finally {
      currentUser.value = null
    }
  })
})
