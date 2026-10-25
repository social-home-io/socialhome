import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, fireEvent, waitFor, act } from '@testing-library/preact'
import {
  MomentumComposerDialog,
  openMomentumComposer,
} from './MomentumComposerDialog'

const mockApi = {
  get: vi.fn(),
  post: vi.fn(),
}
vi.mock('@/api', () => ({
  get api() {
    return mockApi
  },
}))

vi.mock('./Toast', () => ({
  showToast: vi.fn(),
}))

/** A registration whose "share by default" is on. */
const REG = { gfs_id: 'g1', default_share: true }

async function openAndSettle(...args: Parameters<typeof openMomentumComposer>) {
  await act(async () => {
    openMomentumComposer(...args)
    // loadRegistrations resolves after a couple of microtasks.
    await new Promise((r) => setTimeout(r, 0))
  })
}

function publicBox(container: Element): HTMLInputElement | null {
  return container.querySelector('.sh-momentum-composer-public input')
}

describe('MomentumComposerDialog', () => {
  beforeEach(() => {
    mockApi.get.mockReset()
    mockApi.post.mockReset()
    mockApi.get.mockResolvedValue([REG])
    mockApi.post.mockResolvedValue({ id: 'm-new' })
  })

  it('exports the dialog component + open helper', () => {
    expect(typeof MomentumComposerDialog).toBe('function')
    expect(typeof openMomentumComposer).toBe('function')
  })

  it('a new moment follows the "share by default" setting', async () => {
    const { container } = render(<MomentumComposerDialog />)
    await openAndSettle()
    await waitFor(() => expect(publicBox(container)?.checked).toBe(true))
  })

  it('a reply to a moment that is not public offers no public toggle', async () => {
    const { container } = render(<MomentumComposerDialog />)
    await openAndSettle('m-parent', { parentIsPublic: false })
    expect(publicBox(container)).toBeNull()
    const text = container.querySelector('textarea')!
    fireEvent.input(text, { target: { value: 'hi' } })
    fireEvent.submit(container.querySelector('form')!)
    await waitFor(() => expect(mockApi.post).toHaveBeenCalled())
    const body = mockApi.post.mock.calls[0][1] as Record<string, unknown>
    expect(body.parent_moment_id).toBe('m-parent')
    expect(body.is_public).not.toBe(true)
  })

  it('a reply whose parent publicness is unknown (deep link) stays private', async () => {
    const { container } = render(<MomentumComposerDialog />)
    await openAndSettle('m-parent')
    expect(publicBox(container)).toBeNull()
  })

  it('a reply to a public moment offers the toggle but never pre-ticks it', async () => {
    const { container } = render(<MomentumComposerDialog />)
    await openAndSettle('m-parent', { parentIsPublic: true })
    await waitFor(() => expect(publicBox(container)).not.toBeNull())
    expect(publicBox(container)!.checked).toBe(false)
  })
})
