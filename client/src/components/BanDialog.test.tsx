import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor, fireEvent, cleanup } from '@testing-library/preact'

const { post, toast } = vi.hoisted(() => ({ post: vi.fn(), toast: vi.fn() }))
vi.mock('@/api', async (orig) => ({ ...(await orig<object>()), api: { post } }))
vi.mock('./Toast', () => ({ showToast: toast }))

import { ApiError } from '@/api'
import { BAN_REASON_MAX, BanDialog, banErrorCopy } from './BanDialog'

beforeEach(() => { cleanup(); post.mockReset(); toast.mockReset() })

describe('BanDialog', () => {
  it('closed renders nothing', () => {
    const r = render(<BanDialog target={null} onClose={vi.fn()} onBanned={vi.fn()} />)
    expect(r.baseElement.textContent).toBe('')
  })

  it('a household with nobody seated: what the ban blocks, no people line', () => {
    const r = render(<BanDialog target={{ kind: 'household', spaceId: 's', instanceId: 'h', household: 'Spam Farm' }}
                                onClose={vi.fn()} onBanned={vi.fn()} />)
    expect(r.baseElement.textContent).toContain('Ban Spam Farm?')
    expect(r.baseElement.textContent).toContain('Nobody from Spam Farm can join, follow or be invited')
    expect(r.baseElement.textContent).toContain('Only the owner and admins see it.')
  })

  it('one seated person reads in the singular', () => {
    const r = render(<BanDialog target={{ kind: 'household', spaceId: 's', instanceId: 'h', household: 'K', people: ['Ben'] }}
                                onClose={vi.fn()} onBanned={vi.fn()} />)
    expect(r.baseElement.textContent).toContain('Ben leaves this space.')
  })

  it('the button is busy while the ban is sent: pressed twice, one ban', async () => {
    let finish: (v: unknown) => void = () => {}
    post.mockImplementation(() => new Promise((res) => { finish = res }))
    const onBanned = vi.fn()
    const r = render(<BanDialog target={{ kind: 'person', spaceId: 's', userId: 'u', name: 'Ann' }}
                                onClose={vi.fn()} onBanned={onBanned} />)
    const btn = r.baseElement.querySelector('[data-action="ban-confirm"]') as HTMLButtonElement
    fireEvent.click(btn)
    fireEvent.click(btn)
    await waitFor(() => expect(btn.disabled).toBe(true))
    finish({})
    await waitFor(() => expect(onBanned).toHaveBeenCalledTimes(1))
    expect(post).toHaveBeenCalledTimes(1)
    expect(post).toHaveBeenCalledWith('/api/spaces/s/ban', { user_id: 'u' })
    expect(toast).toHaveBeenCalledWith('Ann is banned.', 'info')
  })

  it('caps the reason at the server limit', () => {
    const r = render(<BanDialog target={{ kind: 'person', spaceId: 's', userId: 'u' }}
                                onClose={vi.fn()} onBanned={vi.fn()} />)
    expect((r.baseElement.querySelector('textarea') as HTMLTextAreaElement).maxLength).toBe(BAN_REASON_MAX)
  })

  it('banErrorCopy: an API refusal in its words, anything else as a network failure', () => {
    expect(banErrorCopy(new ApiError(403, '/x', { code: 'OWNER_BAN', detail: 'x' })))
      .toBe('Only the space owner can lift this ban.')
    expect(banErrorCopy(new TypeError('Failed to fetch')))
      .toBe("Couldn't reach your Social Home. Check the connection and try again.")
  })
})
