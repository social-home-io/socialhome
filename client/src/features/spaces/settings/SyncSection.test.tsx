import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

const { apiPost, toast } = vi.hoisted(() => ({ apiPost: vi.fn(), toast: vi.fn() }))
vi.mock('@/api', async () => {
  const real = await vi.importActual<typeof import('@/api')>('@/api')
  return { ...real, api: { post: apiPost } }
})
vi.mock('@/components/Toast', () => ({ showToast: toast }))
vi.mock('@/ws', () => ({ ws: { on: () => () => undefined } }))

import { ApiError } from '@/api'
import { SyncSection } from './SyncSection'

const ago = (min: number) => new Date(Date.now() - min * 60_000).toISOString()

function section(sync: unknown, hostedHere = false) {
  return render(<SyncSection spaceId="s1" sync={sync} hostedHere={hostedHere} />)
}

beforeEach(() => { apiPost.mockReset(); toast.mockReset() })

describe('SyncSection', () => {
  it('always shows the state — up to date too', () => {
    const { container } = section({ state: 'up_to_date', last_ok_at: ago(5), waiting_to_send: 0 })
    expect(container.textContent).toContain('Up to date')
    expect(container.textContent).toContain('You have everything the host household has.')
    expect(container.textContent).toContain('Last up to date 5 min ago')
  })

  it.each([
    ['catching_up', 'Catching up…'],
    ['behind', 'Behind'],
    ['host_offline', 'Host offline'],
    ['never', 'Not synced yet'],
  ])('%s', (state, label) => {
    const { container } = section({ state, last_ok_at: null, waiting_to_send: 2 })
    expect(container.querySelector('.sh-sync-card__state')?.textContent).toBe(label)
    expect(container.textContent).toContain('2 waiting to send')
  })

  it('a space hosted here says so and shows no last-up-to-date line', () => {
    const { container } = section({ state: 'up_to_date', last_ok_at: null, waiting_to_send: 0 }, true)
    expect(container.textContent).toContain('This household hosts the space')
    expect(container.textContent).not.toContain('Last up to date')
  })

  it('renders nothing without a status (a follower)', () => {
    expect(section(null).container.innerHTML).toBe('')
  })

  it('Sync now asks the server and confirms', async () => {
    apiPost.mockResolvedValue({ space_id: 's1', targets: ['h'] })
    const { getByTestId } = section({ state: 'behind', last_ok_at: ago(200), waiting_to_send: 0 })
    fireEvent.click(getByTestId('space-sync-now'))
    await waitFor(() => expect(toast).toHaveBeenCalledWith(
      "Asked for what's new.", 'success'))
    expect(apiPost).toHaveBeenCalledWith('/api/spaces/s1/sync')
  })

  it('Sync now asked too often says so', async () => {
    apiPost.mockRejectedValue(new ApiError(429, '/api/spaces/s1/sync', { code: 'RATE_LIMITED' }))
    const { getByTestId } = section({ state: 'behind', last_ok_at: ago(200), waiting_to_send: 0 })
    fireEvent.click(getByTestId('space-sync-now'))
    await waitFor(() => expect(toast).toHaveBeenCalledWith(
      'You asked a moment ago — try again later.', 'error'))
  })

  it('Sync now failing says so', async () => {
    apiPost.mockRejectedValue(new Error('offline'))
    const { getByTestId } = section({ state: 'never', last_ok_at: null, waiting_to_send: 0 })
    fireEvent.click(getByTestId('space-sync-now'))
    await waitFor(() => expect(toast).toHaveBeenCalledWith(
      "Couldn't ask for a sync. Try again.", 'error'))
  })
})
