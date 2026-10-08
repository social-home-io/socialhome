import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor, screen } from '@testing-library/preact'

const { apiMock, userMock } = vi.hoisted(() => ({
  apiMock: {
    get: vi.fn(),
    post: vi.fn(),
  },
  userMock: {
    value: {
      user_id: 'u1', username: 'admin', display_name: 'Admin',
      is_admin: true, is_new_member: true,
    } as Record<string, unknown> | null,
  },
}))

vi.mock('@/api', async () => {
  const real = await vi.importActual<typeof import('@/api')>('@/api')
  return { ApiError: real.ApiError, api: apiMock }
})
vi.mock('@/store/auth', () => ({ currentUser: userMock }))

import { ApiError } from '@/api'
import { setLocale } from '@/i18n/i18n'
import { OnboardingFlow } from './OnboardingFlow'

const DEFAULT_PATH = '/api/gfs/connections/default'

function offer(over: Record<string, unknown> = {}) {
  return {
    url: 'https://gfs.social-home.io',
    available: true,
    reason: null,
    connection: null,
    ...over,
  }
}

function postsTo(path: string) {
  return apiMock.post.mock.calls.filter(c => c[0] === path)
}

async function stepCount() {
  const bar = await screen.findByRole('progressbar')
  return Number(bar.getAttribute('aria-valuemax'))
}

/** Click Next until the GFS step (the last one) is on screen. */
async function goToGfsStep() {
  for (let i = 0; i < 6; i++) {
    if (screen.queryByRole('heading', { name: 'Connect to the GFS' })) return
    fireEvent.click(screen.getByRole('button', { name: 'Next' }))
  }
  await screen.findByRole('heading', { name: 'Connect to the GFS' })
}

function gfsBox() {
  return screen.getByRole('checkbox', { name: /Connect this household to/ }) as HTMLInputElement
}

beforeEach(() => {
  apiMock.get.mockReset()
  apiMock.post.mockReset()
  apiMock.post.mockResolvedValue({})
  userMock.value = {
    user_id: 'u1', username: 'admin', display_name: 'Admin',
    is_admin: true, is_new_member: true,
  }
})

describe('OnboardingFlow — tour', () => {
  it('walks every tour step in plain English', async () => {
    userMock.value = { ...userMock.value!, is_admin: false }
    render(<OnboardingFlow onComplete={() => {}} />)
    const titles = [
      'Welcome to Social Home',
      'A feed for the people you live with',
      'Shared lists, calendar and chores',
      'Connect with other households, privately',
    ]
    for (const [i, title] of titles.entries()) {
      expect(await screen.findByRole('heading', { name: title })).toBeTruthy()
      if (i < titles.length - 1) fireEvent.click(screen.getByRole('button', { name: 'Next' }))
    }
    // No protocol names in the tour.
    expect(document.body.textContent).not.toMatch(/Ed25519|Federated/)
  })

  it('shows the tour steps in German', async () => {
    userMock.value = { ...userMock.value!, is_admin: false }
    await setLocale('de')
    try {
      render(<OnboardingFlow onComplete={() => {}} />)
      expect(await screen.findByRole('heading', { name: 'Willkommen bei Social Home' })).toBeTruthy()
      expect(screen.getByText('Familie Vizeli')).toBeTruthy()
      fireEvent.click(screen.getByRole('button', { name: 'Weiter' }))
      expect(await screen.findByRole('heading', {
        name: 'Ein Feed für die Menschen, mit denen du lebst',
      })).toBeTruthy()
      fireEvent.click(screen.getByRole('button', { name: 'Weiter' }))
      expect(screen.getByText('Einkaufsliste')).toBeTruthy()
      fireEvent.click(screen.getByRole('button', { name: 'Weiter' }))
      expect(screen.getByText(/Verschlüsselt · noch 5:00/)).toBeTruthy()
    } finally {
      await setLocale('en')
    }
  })

  it('a member sees the four-step tour and the GFS is never asked about', async () => {
    userMock.value = { ...userMock.value!, is_admin: false }
    render(<OnboardingFlow onComplete={() => {}} />)
    expect(await stepCount()).toBe(4)
    expect(apiMock.get).not.toHaveBeenCalled()
  })

  it('Skip tour finishes onboarding without contacting a GFS', async () => {
    apiMock.get.mockResolvedValue(offer())
    const onComplete = vi.fn()
    render(<OnboardingFlow onComplete={onComplete} />)
    await waitFor(async () => expect(await stepCount()).toBe(5))
    fireEvent.click(screen.getByRole('button', { name: 'Skip tour' }))
    expect(onComplete).toHaveBeenCalled()
    expect(postsTo(DEFAULT_PATH)).toHaveLength(0)
    expect(postsTo('/api/me/onboarding-complete')).toHaveLength(1)
  })
})

describe('OnboardingFlow — GFS step visibility', () => {
  it('admins get the step when a default GFS is configured and pairing can work', async () => {
    apiMock.get.mockResolvedValue(offer())
    render(<OnboardingFlow onComplete={() => {}} />)
    await waitFor(async () => expect(await stepCount()).toBe(5))
    expect(apiMock.get).toHaveBeenCalledWith(DEFAULT_PATH)
    await goToGfsStep()
    expect(screen.getByText(/GFS \(Global Federation Server\)/)).toBeTruthy()
    expect(screen.getByText(/Join people with GFS links/)).toBeTruthy()
    expect(screen.getByText(/Which households take part/)).toBeTruthy()
    expect(screen.getByText(/Settings → Connections/)).toBeTruthy()
    expect(gfsBox().checked).toBe(true)
    expect(screen.getAllByText(/gfs\.social-home\.io/).length).toBeGreaterThan(0)
  })

  it.each([
    ['disabled', offer({ url: '', available: false, reason: 'disabled' })],
    ['already connected', offer({
      available: false, reason: 'already_connected',
      connection: { id: 'c1', status: 'active' },
    })],
    // Connecting no longer needs an External URL. An older backend that
    // still answers with this reason gets no step and no URL hint.
    ['an older backend says no_external_url', offer({ available: false, reason: 'no_external_url' })],
  ])('hides the step when %s', async (_label, body) => {
    apiMock.get.mockResolvedValue(body)
    render(<OnboardingFlow onComplete={() => {}} />)
    await waitFor(() => expect(apiMock.get).toHaveBeenCalled())
    expect(await stepCount()).toBe(4)
    expect(screen.queryByText(/External URL/)).toBeNull()
  })

  it('hides the step when the household cannot tell (request failed)', async () => {
    apiMock.get.mockRejectedValue(new ApiError(500, DEFAULT_PATH, null))
    render(<OnboardingFlow onComplete={() => {}} />)
    await waitFor(() => expect(apiMock.get).toHaveBeenCalled())
    expect(await stepCount()).toBe(4)
  })
})

describe('OnboardingFlow — GFS step choice', () => {
  beforeEach(() => {
    apiMock.get.mockResolvedValue(offer())
  })

  async function open(onComplete = vi.fn()) {
    render(<OnboardingFlow onComplete={onComplete} />)
    await waitFor(async () => expect(await stepCount()).toBe(5))
    await goToGfsStep()
    return onComplete
  }

  it('is ticked by default, so the primary button connects', async () => {
    await open()
    expect(gfsBox().checked).toBe(true)
    expect(screen.getByRole('button', { name: 'Connect' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: "Let's go" })).toBeNull()
    expect(postsTo(DEFAULT_PATH)).toHaveLength(0)
  })

  it('unticking it opts out: finishing leaves the GFS alone', async () => {
    const onComplete = await open()
    fireEvent.click(gfsBox())
    expect(gfsBox().checked).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: "Let's go" }))
    expect(onComplete).toHaveBeenCalled()
    expect(postsTo(DEFAULT_PATH)).toHaveLength(0)
  })

  it('connecting with the default tick pairs, then shows success', async () => {
    apiMock.post.mockImplementation(async (path: string) =>
      path === DEFAULT_PATH ? { id: 'c1', status: 'active' } : {})
    const onComplete = await open()
    expect(gfsBox().checked).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    expect(await screen.findByText(/You're connected to the GFS/)).toBeTruthy()
    expect(postsTo(DEFAULT_PATH)).toHaveLength(1)
    expect(onComplete).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: "Let's go" }))
    expect(onComplete).toHaveBeenCalled()
    expect(postsTo(DEFAULT_PATH)).toHaveLength(1)
  })

  it('says it is waiting for approval when the GFS approves by hand', async () => {
    apiMock.post.mockImplementation(async (path: string) =>
      path === DEFAULT_PATH ? { id: 'c1', status: 'pending' } : {})
    await open()
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    expect(await screen.findByText(/Waiting for the GFS to approve/)).toBeTruthy()
  })

  it.each([
    ['GFS_SIGNUP_CLOSED', 409, /isn't taking sign-ups/],
    ['GFS_IDENTITY_MISMATCH', 422, /doesn't look like the Social Home GFS/],
  ])('%s: plain words, no pointless retry, the box unticks', async (code, status, text) => {
    apiMock.post.mockImplementation(async (path: string) => {
      if (path === DEFAULT_PATH) {
        throw new ApiError(status, path, { code, detail: 'server words' })
      }
      return {}
    })
    const onComplete = await open()
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.getByText(text)).toBeTruthy()
    expect(screen.queryByText('server words')).toBeNull()
    // The onboarding-only follow-up: the pairing modal in Settings is
    // where they can pick this up again.
    expect(screen.getByText(/connect later in Settings → Connections/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull()
    expect(gfsBox().checked).toBe(false)
    expect(gfsBox().disabled).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: "Let's go" }))
    expect(onComplete).toHaveBeenCalled()
    expect(postsTo(DEFAULT_PATH)).toHaveLength(1)
  })

  it.each([
    ['GFS_UNREACHABLE', 502, /Couldn't reach the GFS/],
    ['GFS_BUSY', 503, /The GFS is busy/],
    ['GFS_PAIRING_FAILED', 422, /didn't accept this household/],
  ])('maps %s to plain words and lets them try again', async (code, status, text) => {
    apiMock.post.mockImplementation(async (path: string) => {
      if (path === DEFAULT_PATH) {
        throw new ApiError(status, path, { code, detail: 'server words' })
      }
      return {}
    })
    await open()
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.getByText(text)).toBeTruthy()
    expect(screen.queryByText('server words')).toBeNull()
    expect(screen.getByText(/connect later in Settings → Connections/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Try again' })).toBeTruthy()
  })

  it('a refusal it has no words for shows the server reason and lets them try again', async () => {
    apiMock.post.mockImplementation(async (path: string) => {
      if (path === DEFAULT_PATH) {
        throw new ApiError(422, path, { code: 'GFS_TOKEN_EXPIRED', detail: 'That code has expired.' })
      }
      return {}
    })
    await open()
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.getByText('That code has expired.')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Try again' })).toBeTruthy()
  })

  it('after an error, unticking lets them finish without connecting', async () => {
    apiMock.post.mockImplementation(async (path: string) => {
      if (path === DEFAULT_PATH) {
        throw new ApiError(502, path, { code: 'GFS_UNREACHABLE' })
      }
      return {}
    })
    const onComplete = await open()
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    await screen.findByRole('alert')
    fireEvent.click(gfsBox())
    fireEvent.click(screen.getByRole('button', { name: "Let's go" }))
    expect(onComplete).toHaveBeenCalled()
    expect(postsTo(DEFAULT_PATH)).toHaveLength(1)
  })
})
