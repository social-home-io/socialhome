import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

describe('ReportDialog', () => {
  beforeEach(() => {
    vi.resetModules()
  })

  it('module exports exist', async () => {
    const mod = await import('./ReportDialog')
    expect(mod).toBeTruthy()
    expect(Object.keys(mod).length).toBeGreaterThan(0)
  })

  it('names who receives the report', async () => {
    const { reportSentMessage } = await import('./ReportDialog')
    expect(reportSentMessage({ space_id: 'sp' })).toMatch(/space's moderators/)
    expect(reportSentMessage({ space_id: null, forwarded_to_gfs: true })).toMatch(/Global Server/)
    expect(reportSentMessage({ forwarded_to_gfs: false })).toMatch(/household admin\.$/)
  })

  async function mount(post: ReturnType<typeof vi.fn>) {
    vi.doMock('@/api', () => ({
      api: { post },
      ApiError: class extends Error { status = 0 },
    }))
    const mod = await import('./ReportDialog')
    const view = render(<mod.ReportDialog />)
    return { mod, ...view }
  }

  it('a member report inside a space names the space and hides the GFS opt-out', async () => {
    const post = vi.fn(async () => ({ id: 'r', space_id: 'sp-1' }))
    const { mod, findByText, queryByText, getByRole } = await mount(post)
    mod.openReport('user', 'uid-x', 'sp-1')
    expect(await findByText(/goes to the space's moderators/)).toBeTruthy()
    expect(queryByText(/beyond my household/)).toBeNull()
    fireEvent.change(getByRole('combobox'), { target: { value: 'harassment' } })
    fireEvent.click(await findByText('Submit report'))
    await waitFor(() => expect(post).toHaveBeenCalled())
    const body = (post.mock.calls[0] as unknown[])[1] as Record<string, unknown>
    expect(body.space_id).toBe('sp-1')
    expect(body.target_type).toBe('user')
  })

  it('content in a space is not sent a space_id (the server derives it)', async () => {
    const post = vi.fn(async () => ({ id: 'r', space_id: 'sp-1' }))
    const { mod, findByText, getByRole } = await mount(post)
    mod.openReport('post', 'p1', 'sp-1')
    fireEvent.change(await waitFor(() => getByRole('combobox')), { target: { value: 'spam' } })
    fireEvent.click(await findByText('Submit report'))
    await waitFor(() => expect(post).toHaveBeenCalled())
    const body = (post.mock.calls[0] as unknown[])[1] as Record<string, unknown>
    expect(body.space_id).toBeUndefined()
  })

  it('a household report shows the GFS opt-out', async () => {
    const { mod, findByText } = await mount(vi.fn())
    mod.openReport('moment', 'm1')
    expect(await findByText(/beyond my household/)).toBeTruthy()
  })
})
