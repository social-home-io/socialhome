import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'
import { LoginPage } from './LoginPage'
import { setLocale } from '@/i18n/i18n'
import { token } from '@/store/token'

/** A wrong password answers ``401 UNAUTHENTICATED`` with the canonical
 *  error body. No token is stashed yet (the user is signing in), so the
 *  api client must not toast "Session expired" — it must hand the status
 *  to the form, which shows the translated "invalid" message. */
function stubLogin401() {
  const res = {
    ok: false,
    status: 401,
    headers: new Headers({ 'content-type': 'application/json' }),
    json: vi.fn().mockResolvedValue({
      error: { code: 'UNAUTHENTICATED', detail: 'Invalid credentials.' },
    }),
  } as unknown as Response
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(res))
}

async function submitWrongPassword() {
  const view = render(<LoginPage />)
  const user = view.container.querySelector('input[name="username"]') as HTMLInputElement
  const pass = view.container.querySelector('input[name="password"]') as HTMLInputElement
  fireEvent.input(user, { target: { value: 'anna' } })
  fireEvent.input(pass, { target: { value: 'wrong' } })
  fireEvent.submit(view.container.querySelector('form') as HTMLFormElement)
  return view
}

describe('LoginPage — wrong password', () => {
  beforeEach(() => {
    token.value = null
    stubLogin401()
  })
  afterEach(async () => {
    vi.unstubAllGlobals()
    await setLocale('en')
  })

  it('shows the translated "invalid" message, not the raw "Unauthorized"', async () => {
    const view = await submitWrongPassword()
    await waitFor(() => {
      expect(view.container.querySelector('#login-error')?.textContent)
        .toContain('Invalid credentials.')
    })
    expect(view.container.textContent).not.toContain('Unauthorized')
  })

  it('shows the German message in German', async () => {
    await setLocale('de')
    const view = await submitWrongPassword()
    await waitFor(() => {
      expect(view.container.querySelector('#login-error')?.textContent)
        .toContain('Ungültige Anmeldedaten.')
    })
  })
})
