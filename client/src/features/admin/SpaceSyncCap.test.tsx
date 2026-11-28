import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

vi.mock('@/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))

import { api } from '@/api'
import { showToast } from '@/components/Toast'
import { SpaceSyncCap } from './SpaceSyncCap'

const GB = 1024 ** 3
const LABEL = 'Storage for synced space files'

describe('SpaceSyncCap', () => {
  beforeEach(() => {
    vi.mocked(api.get).mockReset()
    vi.mocked(api.put).mockReset()
    vi.mocked(showToast).mockReset()
  })

  it('shows the storage per space and saves a new one on change', async () => {
    vi.mocked(api.get).mockResolvedValue({ used_bytes: 0, quota_bytes: 10 * GB, space_sync_bytes: 2 * GB })
    vi.mocked(api.put).mockResolvedValue({ space_sync_bytes: 0 })
    const { findByLabelText } = render(<SpaceSyncCap />)
    const select = await findByLabelText(LABEL) as HTMLSelectElement
    expect(select.value).toBe(String(2 * GB))
    expect([...select.options].map(o => o.textContent)).toEqual(
      ['Only what we open', '500 MB', '1 GB', '2 GB', '5 GB', '10 GB'])
    fireEvent.change(select, { target: { value: '0' } })
    await waitFor(() => expect(api.put).toHaveBeenCalledWith(
      '/api/admin/storage/quota', { space_sync_bytes: 0 }))
    expect(showToast).toHaveBeenCalledWith('Saved', 'success')
  })

  it('keeps a size set some other way in the list', async () => {
    vi.mocked(api.get).mockResolvedValue({ space_sync_bytes: 3 * GB })
    const { findByLabelText } = render(<SpaceSyncCap />)
    const select = await findByLabelText(LABEL) as HTMLSelectElement
    expect(select.value).toBe(String(3 * GB))
    expect([...select.options].map(o => o.textContent)).toContain('3 GB')
  })

  it('a failed save goes back to the saved size and says why', async () => {
    vi.mocked(api.get).mockResolvedValue({ space_sync_bytes: GB })
    vi.mocked(api.put).mockRejectedValue(new Error('offline'))
    const { findByLabelText } = render(<SpaceSyncCap />)
    const select = await findByLabelText(LABEL) as HTMLSelectElement
    fireEvent.change(select, { target: { value: String(5 * GB) } })
    await waitFor(() => expect(showToast).toHaveBeenCalledWith(
      "Couldn't save: offline", 'error'))
    await waitFor(() => expect(select.value).toBe(String(GB)))
  })

  it('shows nothing when the setting cannot be read', async () => {
    vi.mocked(api.get).mockRejectedValue(new Error('offline'))
    const { container } = render(<SpaceSyncCap />)
    await waitFor(() => expect(api.get).toHaveBeenCalled())
    expect(container.textContent).toBe('')
  })
})
