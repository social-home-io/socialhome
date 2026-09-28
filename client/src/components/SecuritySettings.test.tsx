import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

const { apiMock, confirmMock, platformMock } = vi.hoisted(() => ({
  apiMock: { get: vi.fn(), post: vi.fn(), delete: vi.fn() },
  confirmMock: vi.fn(),
  platformMock: { addon: false },
}))

vi.mock('@/api', async () => {
  const real = await vi.importActual<typeof import('@/api')>('@/api')
  return { api: apiMock, ApiError: real.ApiError }
})
vi.mock('./Toast', () => ({ showToast: vi.fn() }))
vi.mock('./confirm', () => ({ confirmDialog: confirmMock }))
vi.mock('@/platform', () => ({ isSupervisorAddon: () => platformMock.addon }))

import { ApiError } from '@/api'
import { SecuritySettings } from './SecuritySettings'

const WEB_ROW = {
  token_id: 'tid-web', label: 'web', created_at: '2026-09-01 10:00:00',
  last_used_at: '2026-09-28 09:00:00', expires_at: null,
}
const SCRIPT_ROW = {
  token_id: 'tid-script', label: 'Backup script', created_at: '2026-08-01 10:00:00',
  last_used_at: null, expires_at: '2020-01-01T00:00:00+00:00',
}

beforeEach(() => {
  apiMock.get.mockReset()
  apiMock.post.mockReset()
  apiMock.delete.mockReset()
  confirmMock.mockReset()
  platformMock.addon = false
})

describe('SecuritySettings', () => {
  it('GETs /api/me/tokens and shows the empty state', async () => {
    apiMock.get.mockResolvedValueOnce({ base_url: null, tokens: [] })
    const { findByText } = render(<SecuritySettings />)
    await findByText(/No tokens yet/)
    expect(apiMock.get).toHaveBeenCalledWith('/api/me/tokens')
  })

  it('lists rows from the {tokens} envelope; a "web" row reads as a browser sign-in', async () => {
    apiMock.get.mockResolvedValueOnce({ base_url: null, tokens: [WEB_ROW, SCRIPT_ROW] })
    const { findByText, getByRole, container } = render(<SecuritySettings />)
    await findByText('Browser sign-in')
    expect(container.textContent).toContain('Backup script')
    expect(container.textContent).toContain('Never used')
    // Expired rows are flagged rather than hidden.
    expect(container.querySelector('.sh-token-row__badge')?.textContent).toBe('Expired')
    expect(getByRole('button', { name: 'Sign out Browser sign-in' })).toBeTruthy()
    expect(getByRole('button', { name: 'Revoke Backup script' })).toBeTruthy()
  })

  it('creates a token with a 90-day default expiry and reveals it once with the public base URL', async () => {
    apiMock.get
      .mockResolvedValueOnce({ base_url: 'https://home.example.com', tokens: [] })
      .mockResolvedValueOnce({
        base_url: 'https://home.example.com',
        tokens: [{ ...SCRIPT_ROW, expires_at: null }],
      })
    apiMock.post.mockResolvedValueOnce({ token_id: 'tid-new', token: 'raw-secret-token' })
    const { findByText, getByLabelText, getByRole, findByLabelText, container } = render(
      <SecuritySettings />,
    )
    await findByText(/No tokens yet/)
    fireEvent.input(getByLabelText('Name'), { target: { value: '  Backup script ' } })
    const before = Date.now()
    fireEvent.click(getByRole('button', { name: 'Create token' }))
    const secret = await findByLabelText('New API token')
    expect(secret.textContent).toBe('raw-secret-token')
    const [path, body] = apiMock.post.mock.calls[0]
    expect(path).toBe('/api/me/tokens')
    expect(body.label).toBe('Backup script')
    const days = (Date.parse(body.expires_at) - before) / 86_400_000
    expect(days).toBeGreaterThan(89.9)
    expect(days).toBeLessThan(90.1)
    // Usage example uses the server's public origin, not document.baseURI.
    expect(container.textContent).toContain('https://home.example.com/api/me')
    expect(container.textContent).toContain("you won't see it again")
    fireEvent.click(getByRole('button', { name: "I've saved it" }))
    await waitFor(() => expect(getByRole('button', { name: 'Create token' })).toBeTruthy())
  })

  it('"Never" sends expires_at: null', async () => {
    apiMock.get.mockResolvedValue({ base_url: null, tokens: [] })
    apiMock.post.mockResolvedValueOnce({ token_id: 't', token: 'raw' })
    const { findByText, getByLabelText, getByRole } = render(<SecuritySettings />)
    await findByText(/No tokens yet/)
    fireEvent.input(getByLabelText('Name'), { target: { value: 'ci' } })
    fireEvent.change(getByLabelText('Expires'), { target: { value: 'never' } })
    fireEvent.click(getByRole('button', { name: 'Create token' }))
    await waitFor(() =>
      expect(apiMock.post).toHaveBeenCalledWith('/api/me/tokens', { label: 'ci', expires_at: null }),
    )
  })

  it('with no public address, explains it instead of printing a URL', async () => {
    apiMock.get.mockResolvedValue({ base_url: null, tokens: [] })
    apiMock.post.mockResolvedValueOnce({ token_id: 't', token: 'raw' })
    const { findByText, getByLabelText, getByRole, container } = render(<SecuritySettings />)
    await findByText(/No tokens yet/)
    fireEvent.input(getByLabelText('Name'), { target: { value: 'ci' } })
    fireEvent.click(getByRole('button', { name: 'Create token' }))
    await findByText(/no public web address set/)
    expect(container.textContent).toContain('<your-social-home>')
  })

  it('an empty name is rejected inline without calling the API', async () => {
    apiMock.get.mockResolvedValueOnce({ base_url: null, tokens: [] })
    const { findByText, getByRole, findByRole } = render(<SecuritySettings />)
    await findByText(/No tokens yet/)
    fireEvent.click(getByRole('button', { name: 'Create token' }))
    expect((await findByRole('alert')).textContent).toMatch(/Give the token a name/)
    expect(apiMock.post).not.toHaveBeenCalled()
  })

  it('surfaces the backend detail when creating fails', async () => {
    apiMock.get.mockResolvedValueOnce({ base_url: null, tokens: [] })
    apiMock.post.mockRejectedValueOnce(
      new ApiError(429, '/api/me/tokens', { code: 'RATE_LIMITED', detail: 'Too many requests.' }),
    )
    const { findByText, getByLabelText, getByRole, findByRole } = render(<SecuritySettings />)
    await findByText(/No tokens yet/)
    fireEvent.input(getByLabelText('Name'), { target: { value: 'x' } })
    fireEvent.click(getByRole('button', { name: 'Create token' }))
    expect((await findByRole('alert')).textContent).toBe('Too many requests.')
  })

  it('revoke confirms first; cancel keeps the row, confirm DELETEs it', async () => {
    apiMock.get.mockResolvedValueOnce({ base_url: null, tokens: [SCRIPT_ROW] })
    apiMock.delete.mockResolvedValueOnce(undefined)
    const { findByRole, queryByText } = render(<SecuritySettings />)
    const btn = await findByRole('button', { name: 'Revoke Backup script' })
    confirmMock.mockResolvedValueOnce(false)
    fireEvent.click(btn)
    await waitFor(() => expect(confirmMock).toHaveBeenCalledTimes(1))
    expect(apiMock.delete).not.toHaveBeenCalled()
    confirmMock.mockResolvedValueOnce(true)
    fireEvent.click(btn)
    await waitFor(() =>
      expect(apiMock.delete).toHaveBeenCalledWith('/api/me/tokens/tid-script'),
    )
    await waitFor(() => expect(queryByText('Backup script')).toBeNull())
  })

  it('signing out a browser warns it may be this browser', async () => {
    apiMock.get.mockResolvedValueOnce({ base_url: null, tokens: [WEB_ROW] })
    confirmMock.mockResolvedValueOnce(false)
    const { findByRole } = render(<SecuritySettings />)
    fireEvent.click(await findByRole('button', { name: 'Sign out Browser sign-in' }))
    await waitFor(() => expect(confirmMock).toHaveBeenCalled())
    expect(confirmMock.mock.calls[0][0]).toMatch(/you will be signed out/)
    expect(confirmMock.mock.calls[0][1]).toMatchObject({ destructive: true })
  })

  it('shows a retry when the list fails to load', async () => {
    apiMock.get
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce({ base_url: null, tokens: [] })
    const { findByRole, findByText } = render(<SecuritySettings />)
    fireEvent.click(await findByRole('button', { name: 'Try again' }))
    await findByText(/No tokens yet/)
    expect(apiMock.get).toHaveBeenCalledTimes(2)
  })
})
