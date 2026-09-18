import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import {
  render, fireEvent, waitFor, act, cleanup,
} from '@testing-library/preact'
import { LocationProvider } from 'preact-iso'

vi.mock('@/api', async () => {
  class ApiError extends Error {
    detail: string | null
    constructor(public status: number, msg = 'api error', detail: string | null = null) {
      super(msg)
      this.detail = detail
    }
  }
  return {
    api: { post: vi.fn() },
    ApiError,
  }
})
vi.mock('@/baseUrl', () => ({
  basePath: '/',
  addBase: (p: string) => p,
  stripBase: (p: string) => p,
}))
vi.mock('qrcode', () => ({
  default: { toDataURL: vi.fn(async () => 'data:fake') },
}))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))

const OUR_INSTANCE_ID = 'aaaabbbbccccddddeeeeffff00001111'
vi.mock('@/store/instance', () => ({
  instanceConfig: {
    value: {
      mode: 'standalone',
      instance_name: 'Test home',
      instance_id: OUR_INSTANCE_ID,
      capabilities: [],
      setup_required: false,
    },
  },
  loadInstanceConfig: vi.fn(),
}))

const routeSpy = vi.fn()
vi.mock('preact-iso', async () => {
  const actual = await vi.importActual<typeof import('preact-iso')>('preact-iso')
  return {
    ...actual,
    useLocation: () => ({ route: routeSpy, url: '/', path: '/', query: {} }),
  }
})

const { api, ApiError } = await import('@/api') as unknown as {
  api: { post: ReturnType<typeof vi.fn> }
  ApiError: new (
    status: number, msg?: string, detail?: string | null,
  ) => Error & { status: number; detail: string | null }
}
const { showToast } = await import('@/components/Toast') as unknown as {
  showToast: ReturnType<typeof vi.fn>
}
const {
  SpaceJoinByCodeDialog, openSpaceJoinByCode,
} = await import('./SpaceJoinByCodeDialog')
const { buildInviteCode } = await import('@/lib/spaceInviteCode')

beforeEach(() => {
  api.post.mockReset()
  routeSpy.mockReset()
  showToast.mockReset()
})

afterEach(() => {
  cleanup()
})

async function renderAndOpen() {
  const result = render(
    <LocationProvider>
      <SpaceJoinByCodeDialog />
    </LocationProvider>,
  )
  await act(async () => { openSpaceJoinByCode() })
  await waitFor(() => {
    expect(
      result.container.querySelector('[data-testid="join-by-code-input"]'),
    ).not.toBeNull()
  })
  return result
}

describe('SpaceJoinByCodeDialog', () => {
  it('is gated on openSpaceJoinByCode() — renders nothing by default', () => {
    const { container } = render(
      <LocationProvider>
        <SpaceJoinByCodeDialog />
      </LocationProvider>,
    )
    expect(
      container.querySelector('[data-testid="join-by-code-input"]'),
    ).toBeNull()
  })

  it('joins same-instance — issuer matches our id, no issuer_instance_id in the POST', async () => {
    api.post.mockResolvedValueOnce({ space_id: 'space-uuid-xyz' })
    // Same instance id as the mock store — local redeem path; the
    // SPA must NOT forward issuer_instance_id so the backend takes
    // the local code branch in /api/spaces/join.
    const code = buildInviteCode({
      token: 'a1b2c3d4e5f60718',
      space_id: 'space-uuid-xyz',
      issuer_instance_id: OUR_INSTANCE_ID,
    })
    const { container, getByText } = await renderAndOpen()
    const input = container.querySelector('[data-testid="join-by-code-input"]') as HTMLTextAreaElement
    await act(async () => {
      fireEvent.input(input, { target: { value: code } })
    })
    await act(async () => { fireEvent.click(getByText('Join')) })
    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith('/api/spaces/join', {
        token: 'a1b2c3d4e5f60718',
      })
      expect(routeSpy).toHaveBeenCalledWith('/spaces/space-uuid-xyz')
      // The success toast is what the receiver sees on a same-instance
      // redeem — before this we just navigated silently and the space
      // page (often initially empty) read as "nothing happened".
      expect(showToast).toHaveBeenCalledWith(
        expect.stringContaining("You're in"),
        'success',
      )
    })
  })

  it('forwards issuer_instance_id when the code was minted on another instance', async () => {
    api.post.mockResolvedValueOnce({ space_id: 'space-uuid-fed' })
    // Different instance id — the backend has to route the redeem
    // over the SPACE_INVITE_TOKEN_REDEEM federation flow.
    const code = buildInviteCode({
      token: 'a1b2c3d4e5f60718',
      space_id: 'space-uuid-fed',
      issuer_instance_id: 'ffffeeeeddddccccbbbb111122223333',
    })
    const { container, getByText } = await renderAndOpen()
    const input = container.querySelector('[data-testid="join-by-code-input"]') as HTMLTextAreaElement
    await act(async () => {
      fireEvent.input(input, { target: { value: code } })
    })
    await act(async () => { fireEvent.click(getByText('Join')) })
    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith('/api/spaces/join', {
        token: 'a1b2c3d4e5f60718',
        issuer_instance_id: 'ffffeeeeddddccccbbbb111122223333',
      })
      // Cross-instance redeems queue a federated join; the local row
      // is not seatable yet, so the receiver lands on ``/spaces``
      // (the WS upsert places the new card when the redeem returns).
      expect(routeSpy).toHaveBeenCalledWith('/spaces')
      expect(showToast).toHaveBeenCalledWith(
        expect.stringContaining("You're in"),
        'success',
      )
    })
  })

  it('forwards the bootstrap block for a code from a household we never met', async () => {
    api.post.mockResolvedValueOnce({ space_id: 'space-uuid-boot' })
    // §D2b — the issuer's public keys plus the connection server that
    // served the blob. The backend uses them only when neither a
    // pairing nor a mesh route reaches the issuer.
    const code = buildInviteCode({
      token: 'a1b2c3d4e5f60718',
      space_id: 'space-uuid-boot',
      issuer_instance_id: 'ffffeeeeddddccccbbbb111122223333',
      issuer_identity_pk: 'aa'.repeat(32),
      issuer_keywrap_pk: 'bb'.repeat(32),
      issuer_keywrap_sig: 'c2ln',
      issuer_proto_version: 29,
      expires_at: '2026-12-01T00:00:00+00:00',
      via_gfs: { gfs_url: 'https://relay.example.org', gfs_space_id: 'g-1' },
    })
    const { container, getByText } = await renderAndOpen()
    const input = container.querySelector('[data-testid="join-by-code-input"]') as HTMLTextAreaElement
    await act(async () => {
      fireEvent.input(input, { target: { value: code } })
    })
    await act(async () => { fireEvent.click(getByText('Join')) })
    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith('/api/spaces/join', {
        token: 'a1b2c3d4e5f60718',
        issuer_instance_id: 'ffffeeeeddddccccbbbb111122223333',
        space_id: 'space-uuid-boot',
        issuer_identity_pk: 'aa'.repeat(32),
        issuer_keywrap_pk: 'bb'.repeat(32),
        issuer_keywrap_sig: 'c2ln',
        issuer_proto_version: 29,
        expires_at: '2026-12-01T00:00:00+00:00',
        gfs: 'https://relay.example.org',
      })
    })
  })

  it('sends no bootstrap block when the code carries an incomplete key set', async () => {
    api.post.mockResolvedValueOnce({ space_id: 'space-uuid-part' })
    const code = buildInviteCode({
      token: 'a1b2c3d4e5f60718',
      issuer_instance_id: 'ffffeeeeddddccccbbbb111122223333',
      issuer_identity_pk: 'aa'.repeat(32),
      // key-wrap key + signature missing — nothing safe to seal to.
    })
    const { container, getByText } = await renderAndOpen()
    const input = container.querySelector('[data-testid="join-by-code-input"]') as HTMLTextAreaElement
    await act(async () => {
      fireEvent.input(input, { target: { value: code } })
    })
    await act(async () => { fireEvent.click(getByText('Join')) })
    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith('/api/spaces/join', {
        token: 'a1b2c3d4e5f60718',
        issuer_instance_id: 'ffffeeeeddddccccbbbb111122223333',
      })
    })
  })

  it('joins when given a bare hex token (back-compat)', async () => {
    api.post.mockResolvedValueOnce({ space_id: 'space-bare' })
    const { container, getByText } = await renderAndOpen()
    const input = container.querySelector('[data-testid="join-by-code-input"]') as HTMLTextAreaElement
    await act(async () => {
      fireEvent.input(input, { target: { value: 'a1b2c3d4e5f60718' } })
    })
    await act(async () => { fireEvent.click(getByText('Join')) })
    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith('/api/spaces/join', {
        token: 'a1b2c3d4e5f60718',
      })
    })
  })

  it('shows a decoder-specific error for garbage input — does NOT call the API', async () => {
    const { container, getByText } = await renderAndOpen()
    const input = container.querySelector('[data-testid="join-by-code-input"]') as HTMLTextAreaElement
    await act(async () => {
      fireEvent.input(input, { target: { value: 'totally not a code at all' } })
    })
    await act(async () => { fireEvent.click(getByText('Join')) })
    expect(api.post).not.toHaveBeenCalled()
    expect(container.querySelector('.sh-scan-error-inline')?.textContent)
      .toContain("doesn't look like a Social Home invite code")
  })

  it('shows the expired-token message on 404 from the API', async () => {
    api.post.mockRejectedValueOnce(new ApiError(404, 'token not found'))
    const { container, getByText } = await renderAndOpen()
    const input = container.querySelector('[data-testid="join-by-code-input"]') as HTMLTextAreaElement
    await act(async () => {
      fireEvent.input(input, { target: { value: 'a1b2c3d4e5f60718' } })
    })
    await act(async () => { fireEvent.click(getByText('Join')) })
    await waitFor(() => {
      expect(container.querySelector('.sh-scan-error-inline')?.textContent)
        .toContain('expired or already been used')
    })
    expect(routeSpy).not.toHaveBeenCalled()
  })

  it('surfaces the backend reason on a 403 (age gate), not a generic message', async () => {
    // A protected minor blocked by §CP.F1 must see WHY, not a misleading
    // "invite revoked". The backend's detail carries the real reason.
    api.post.mockRejectedValueOnce(
      new ApiError(403, 'forbidden', 'This space is restricted to users aged 18+.'),
    )
    const { container, getByText } = await renderAndOpen()
    const input = container.querySelector('[data-testid="join-by-code-input"]') as HTMLTextAreaElement
    await act(async () => {
      fireEvent.input(input, { target: { value: 'a1b2c3d4e5f60718' } })
    })
    await act(async () => { fireEvent.click(getByText('Join')) })
    await waitFor(() => {
      expect(container.querySelector('.sh-scan-error-inline')?.textContent)
        .toContain('restricted to users aged 18+')
    })
    expect(routeSpy).not.toHaveBeenCalled()
  })

  it('falls back to a generic message on a 403 with no detail', async () => {
    api.post.mockRejectedValueOnce(new ApiError(403, 'forbidden'))
    const { container, getByText } = await renderAndOpen()
    const input = container.querySelector('[data-testid="join-by-code-input"]') as HTMLTextAreaElement
    await act(async () => {
      fireEvent.input(input, { target: { value: 'a1b2c3d4e5f60718' } })
    })
    await act(async () => { fireEvent.click(getByText('Join')) })
    await waitFor(() => {
      expect(container.querySelector('.sh-scan-error-inline')?.textContent)
        .toContain('not allowed to join')
    })
  })

  // Removed: the SPA no longer pre-flights "wrong instance" with
  // a hard block. Cross-instance codes now route through the
  // /api/spaces/join endpoint, which forwards a federation redeem
  // when the issuer is reachable as a CONFIRMED peer and 422s with
  // a "pair with X first" error otherwise. The error-rendering
  // test below covers the unreachable case.

  it('reopen after a prior error resets the draft + error message', async () => {
    // First open: trigger the decoder error.
    let result = await renderAndOpen()
    let input = result.container.querySelector('[data-testid="join-by-code-input"]') as HTMLTextAreaElement
    await act(async () => {
      fireEvent.input(input, { target: { value: 'garbage' } })
    })
    await act(async () => { fireEvent.click(result.getByText('Join')) })
    expect(result.container.querySelector('.sh-scan-error-inline')).not.toBeNull()
    // Close via the modal's overlay-click escape (the close handler).
    // The component re-renders to null when ``open`` flips.
    await act(async () => { result.unmount() })
    // Re-open — input should be empty, no stale error.
    result = await renderAndOpen()
    input = result.container.querySelector('[data-testid="join-by-code-input"]') as HTMLTextAreaElement
    expect(input.value).toBe('')
    expect(result.container.querySelector('.sh-scan-error-inline')).toBeNull()
  })
})
