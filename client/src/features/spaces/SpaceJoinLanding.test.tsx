import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, waitFor, cleanup } from '@testing-library/preact'
import { LocationProvider } from 'preact-iso'
import { decodeInviteCode } from '@/lib/spaceInviteCode'

vi.mock('@/api', () => {
  class ApiError extends Error {
    constructor(public status: number, msg = 'api error') { super(msg) }
  }
  return { api: { post: vi.fn() }, ApiError }
})
vi.mock('@/baseUrl', () => ({
  basePath: '/',
  addBase: (p: string) => p,
  stripBase: (p: string) => p,
}))
vi.mock('qrcode', () => ({
  default: { toDataURL: vi.fn(async () => 'data:fake') },
}))
// Our own instance id — this page is served BY the issuer, so it is
// also the issuer id the fallback code has to carry.
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

const { api, ApiError } = await import('@/api') as unknown as {
  api: { post: ReturnType<typeof vi.fn> }
  ApiError: new (status: number, msg?: string) => Error & { status: number }
}

const TOKEN = 'a1b2c3d4e5f60718'

beforeEach(() => {
  api.post.mockReset()
  // Inject ?token=… into the location so SpaceJoinLanding's effect
  // picks it up; useLocation here is fine because it reads from
  // window.location directly.
  window.history.replaceState({}, '', '/join?token=' + TOKEN)
})

afterEach(() => {
  cleanup()
})

async function renderLanding() {
  const { default: SpaceJoinLanding } = await import('./SpaceJoinLanding')
  return render(
    <LocationProvider>
      <SpaceJoinLanding />
    </LocationProvider>,
  )
}

describe('SpaceJoinLanding', () => {
  it('exports a default component', async () => {
    const mod = await import('./SpaceJoinLanding')
    expect(mod.default).toBeTruthy()
    expect(typeof mod.default).toBe('function')
  })

  it('renders the wrong-instance fallback panel when the API returns 404', async () => {
    api.post.mockRejectedValueOnce(new ApiError(404, 'unknown token'))
    const { container } = await renderLanding()
    await waitFor(() => {
      expect(container.querySelector('[data-testid="join-landing-wrong-instance"]'))
        .not.toBeNull()
    })
    const codeEl = container.querySelector('[data-testid="fallback-code"]')!
    // The fallback panel renders the same token back as an invite
    // code so the receiver can paste it into their own instance.
    expect(codeEl.textContent).toMatch(/^socialhome:\/\/invite#/)
  })

  it('renders the wrong-instance fallback on 403 (revoked / forbidden)', async () => {
    api.post.mockRejectedValueOnce(new ApiError(403, 'forbidden'))
    const { container } = await renderLanding()
    await waitFor(() => {
      expect(container.querySelector('[data-testid="join-landing-wrong-instance"]'))
        .not.toBeNull()
    })
  })

  it('renders the bare error card for other failures (500)', async () => {
    api.post.mockRejectedValueOnce(new ApiError(500, 'boom'))
    const { container } = await renderLanding()
    await waitFor(() => {
      expect(container.querySelector('.sh-error h2')?.textContent)
        .toBe("Couldn't join")
    })
    expect(container.querySelector('[data-testid="join-landing-wrong-instance"]'))
      .toBeNull()
  })
})

describe('SpaceJoinLanding — the fallback code is redeemable', () => {
  it('stamps our own instance id as the issuer so the paste can route', async () => {
    // Without it the receiver's Social Home can only try the LOCAL
    // redeem path, which fails exactly the way this panel is trying to
    // recover from.
    api.post.mockRejectedValueOnce(new ApiError(404, 'unknown token'))
    const { container } = await renderLanding()
    await waitFor(() => {
      expect(container.querySelector('[data-testid="fallback-code"]'))
        .not.toBeNull()
    })
    const decoded = decodeInviteCode(
      container.querySelector('[data-testid="fallback-code"]')!.textContent!,
    )!
    expect(decoded.token).toBe(TOKEN)
    expect(decoded.issuer_instance_id).toBe(OUR_INSTANCE_ID)
  })

  it('carries the space id when the link supplied one', async () => {
    window.history.replaceState(
      {}, '', `/join?token=${TOKEN}&space_id=sp-42`,
    )
    api.post.mockRejectedValueOnce(new ApiError(410, 'gone'))
    const { container } = await renderLanding()
    await waitFor(() => {
      expect(container.querySelector('[data-testid="fallback-code"]'))
        .not.toBeNull()
    })
    const decoded = decodeInviteCode(
      container.querySelector('[data-testid="fallback-code"]')!.textContent!,
    )!
    expect(decoded.space_id).toBe('sp-42')
  })

  it('leaves space_id null when the link had none', async () => {
    api.post.mockRejectedValueOnce(new ApiError(403, 'forbidden'))
    const { container } = await renderLanding()
    await waitFor(() => {
      expect(container.querySelector('[data-testid="fallback-code"]'))
        .not.toBeNull()
    })
    const decoded = decodeInviteCode(
      container.querySelector('[data-testid="fallback-code"]')!.textContent!,
    )!
    expect(decoded.space_id).toBeNull()
  })
})
