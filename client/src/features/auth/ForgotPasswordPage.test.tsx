import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/preact'
import { ForgotPasswordPage } from './ForgotPasswordPage'
import { setLocale } from '@/i18n/i18n'

function submitReset() {
  const view = render(<ForgotPasswordPage token="reset-token" />)
  const [pass, confirm] = Array.from(
    view.container.querySelectorAll('input[type="password"]'),
  ) as HTMLInputElement[]
  fireEvent.input(pass, { target: { value: 'fresh-pass-9' } })
  fireEvent.input(confirm, { target: { value: 'fresh-pass-9' } })
  fireEvent.submit(view.container.querySelector('form') as HTMLFormElement)
  return view
}

describe('ForgotPasswordPage — too many wrong reset links', () => {
  beforeEach(() => {
    const res = {
      ok: false,
      status: 429,
      headers: new Headers({ 'content-type': 'application/json', 'retry-after': '300' }),
      json: vi.fn().mockResolvedValue({
        error: { code: 'RATE_LIMITED', detail: 'Too many attempts.', retry_after: 300 },
      }),
    } as unknown as Response
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(res))
  })
  afterEach(async () => {
    vi.unstubAllGlobals()
    await setLocale('en')
  })

  it('says how many minutes to wait', async () => {
    const view = submitReset()
    await waitFor(() => {
      expect(view.container.querySelector('#reset-error')?.textContent)
        .toBe('Too many tries. Wait 5 minutes, then try again.')
    })
  })

  it('falls back to the plain line without a wait', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 429,
      headers: new Headers(),
      json: vi.fn().mockRejectedValue(new Error('not json')),
    } as unknown as Response))
    const view = submitReset()
    await waitFor(() => {
      expect(view.container.querySelector('#reset-error')?.textContent)
        .toBe('Too many attempts — wait a few minutes and try again.')
    })
  })
})
