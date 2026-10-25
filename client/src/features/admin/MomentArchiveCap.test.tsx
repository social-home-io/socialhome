import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

vi.mock('@/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))

import { api } from '@/api'
import { showToast } from '@/components/Toast'
import { MomentArchiveCap } from './MomentArchiveCap'

const GB = 1024 ** 3

describe('MomentArchiveCap', () => {
  beforeEach(() => {
    vi.mocked(api.get).mockReset()
    vi.mocked(api.put).mockReset()
    vi.mocked(showToast).mockReset()
  })

  it('shows the household limit and saves a new one on change', async () => {
    vi.mocked(api.get).mockResolvedValue({ used_bytes: 0, cap_bytes: 2 * GB })
    vi.mocked(api.put).mockResolvedValue({ cap_bytes: 0 })
    const { findByLabelText } = render(<MomentArchiveCap />)
    const select = await findByLabelText('Pictures and videos per person') as HTMLSelectElement
    expect(select.value).toBe(String(2 * GB))
    expect([...select.options].map(o => o.textContent)).toEqual(
      ['500 MB', '1 GB', '2 GB', '5 GB', '10 GB', 'No limit'])
    fireEvent.change(select, { target: { value: '0' } })
    await waitFor(() => expect(api.put).toHaveBeenCalledWith(
      '/api/admin/moments/archive-cap', { cap_bytes: 0 }))
    expect(showToast).toHaveBeenCalledWith('Limit saved', 'success')
  })

  it('keeps a limit set some other way in the list', async () => {
    vi.mocked(api.get).mockResolvedValue({ used_bytes: 0, cap_bytes: 3 * GB })
    const { findByLabelText } = render(<MomentArchiveCap />)
    const select = await findByLabelText('Pictures and videos per person') as HTMLSelectElement
    expect(select.value).toBe(String(3 * GB))
    expect([...select.options].map(o => o.textContent)).toContain('3 GB')
  })

  it('a failed save goes back to the saved limit and says why', async () => {
    vi.mocked(api.get).mockResolvedValue({ used_bytes: 0, cap_bytes: GB })
    vi.mocked(api.put).mockRejectedValue(new Error('offline'))
    const { findByLabelText } = render(<MomentArchiveCap />)
    const select = await findByLabelText('Pictures and videos per person') as HTMLSelectElement
    fireEvent.change(select, { target: { value: String(5 * GB) } })
    await waitFor(() => expect(showToast).toHaveBeenCalledWith(
      "Couldn't save the limit: offline", 'error'))
    await waitFor(() => expect(select.value).toBe(String(GB)))
  })

  it('shows nothing when the limit cannot be read', async () => {
    vi.mocked(api.get).mockRejectedValue(new Error('offline'))
    const { container } = render(<MomentArchiveCap />)
    await waitFor(() => expect(api.get).toHaveBeenCalled())
    expect(container.textContent).toBe('')
  })
})
