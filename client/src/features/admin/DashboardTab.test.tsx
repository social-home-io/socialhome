import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor, screen, act, cleanup } from '@testing-library/preact'
import type { DashboardState } from './DashboardTab'

vi.mock('@/api', async (importOriginal) => {
  const real = await importOriginal<typeof import('@/api')>()
  return {
    ...real,
    api: {
      get: vi.fn(),
      put: vi.fn(),
      post: vi.fn(),
      patch: vi.fn(),
      delete: vi.fn().mockResolvedValue(null),
    },
  }
})
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))
vi.mock('@/components/confirm', () => ({ confirmDialog: vi.fn().mockResolvedValue(true) }))
vi.mock('@/components/QrCodeImg', () => ({
  QrCodeImg: ({ data, alt }: { data: string, alt?: string }) => (
    <img data-testid="qr" data-payload={data} alt={alt} />
  ),
}))
vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u1', username: 'admin', display_name: 'Admin', is_admin: true } },
}))

import { api, ApiError } from '@/api'
import { showToast } from '@/components/Toast'
import { confirmDialog } from '@/components/confirm'
import { DashboardTab } from './DashboardTab'
import { isWeakPassword } from './DashboardDialogs'

const mGet = vi.mocked(api.get)
const mPut = vi.mocked(api.put)
const mPost = vi.mocked(api.post)
const mDelete = vi.mocked(api.delete)

function state(over: Partial<DashboardState> = {}): DashboardState {
  return {
    enabled: false,
    running: false,
    running_port: null,
    port: 8100,
    port_fixed: false,
    password_set: true,
    password_set_at: '2026-10-11T08:00:00+00:00',
    pin_set: false,
    address: null,
    url: 'http://homeassistant.local:8100',
    url_host_allowed: true,
    locale: 'en',
    idle_return_minutes: 3,
    night_start: '22:00',
    night_end: '06:00',
    keep_screen_on: true,
    show_status_text: false,
    show_household_messages: false,
    mention_sound: true,
    household_chat_enabled: true,
    last_seen_at: null,
    people: [
      { user_id: 'u1', username: 'pascal', display_name: 'Pascal', picture_url: null,
        show: true, picture: null, picture_effective: false },
      { user_id: 'u2', username: 'mia', display_name: 'Mia', picture_url: null,
        show: true, picture: null, picture_effective: true },
      { user_id: 'u3', username: 'leo', display_name: 'Leo', picture_url: null,
        show: true, picture: true, picture_effective: true },
    ],
    displays: [],
    ...over,
  }
}

const running = () => state({ enabled: true, running: true, running_port: 8100 })

async function mount(s: DashboardState) {
  mGet.mockResolvedValue(s)
  const utils = render(<DashboardTab />)
  await screen.findByRole('heading', { name: 'Household dashboard' })
  return utils
}

const enableSwitch = () => screen.getByRole('switch', { name: 'Household dashboard' }) as HTMLInputElement

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn())
  // The server answers with the full state; a ``people`` patch is a map,
  // not the list, so it isn't echoed back here.
  mPut.mockImplementation(async (_path: string, body: unknown) => {
    const { people: _people, ...rest } = (body ?? {}) as Record<string, unknown>
    return { ...state(), ...rest }
  })
})

afterEach(() => {
  // No raw fetch anywhere: every call goes through the api client.
  expect(fetch).not.toHaveBeenCalled()
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

describe('DashboardTab — first run', () => {
  it('loads the settings through the api client', async () => {
    await mount(state())
    expect(mGet).toHaveBeenCalledWith('/api/admin/dashboard')
  })

  it('greys the on/off switch until a password is set', async () => {
    await mount(state({ password_set: false, password_set_at: null }))
    expect(enableSwitch().disabled).toBe(true)
    expect(screen.getByText(/Set a password first/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Set password' })).toBeTruthy()
  })

  it('offers a retry when the settings fail to load', async () => {
    mGet.mockRejectedValueOnce(new Error('down'))
    render(<DashboardTab />)
    await screen.findByText("Couldn't load the dashboard settings.")
    mGet.mockResolvedValueOnce(state())
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(enableSwitch()).toBeTruthy())
  })
})

describe('DashboardTab — password dialog', () => {
  async function openDialog() {
    await mount(state({ password_set: false, password_set_at: null }))
    fireEvent.click(screen.getByRole('button', { name: 'Set password' }))
    await screen.findByRole('dialog')
  }

  it('refuses a short password without calling the server', async () => {
    await openDialog()
    fireEvent.input(screen.getByLabelText('New password'), { target: { value: 'short' } })
    fireEvent.input(screen.getByLabelText('Repeat password'), { target: { value: 'short' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save password' }))
    expect((await screen.findByRole('alert')).textContent).toBe('Use at least 8 characters.')
    expect(mPut).not.toHaveBeenCalled()
  })

  it('refuses two passwords that differ', async () => {
    await openDialog()
    fireEvent.input(screen.getByLabelText('New password'), { target: { value: 'kitchen-tablet' } })
    fireEvent.input(screen.getByLabelText('Repeat password'), { target: { value: 'kitchen-tablex' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save password' }))
    expect((await screen.findByRole('alert')).textContent).toBe("The two passwords don't match.")
    expect(mPut).not.toHaveBeenCalled()
  })

  it('warns about a weak password but still saves it, then reloads', async () => {
    await openDialog()
    fireEvent.input(screen.getByLabelText('New password'), { target: { value: 'abcdefgh' } })
    expect(screen.getByText(/a longer password is safer/)).toBeTruthy()
    fireEvent.input(screen.getByLabelText('Repeat password'), { target: { value: 'abcdefgh' } })
    mPut.mockResolvedValueOnce(null)
    mGet.mockResolvedValueOnce(state())
    fireEvent.click(screen.getByRole('button', { name: 'Save password' }))
    await waitFor(() => expect(mPut).toHaveBeenCalledWith(
      '/api/admin/dashboard/password', { password: 'abcdefgh' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(mGet).toHaveBeenCalledTimes(2)
    expect(await screen.findByRole('button', { name: 'Change password' })).toBeTruthy()
  })

  it('says that changing the password signs every tablet out', async () => {
    await mount(state())
    fireEvent.click(screen.getByRole('button', { name: 'Change password' }))
    expect(await screen.findByText('Changing it signs out every tablet and unpairs every wall panel.')).toBeTruthy()
  })

  it('isWeakPassword flags short single-kind passwords only', () => {
    expect(isWeakPassword('short')).toBe(false)
    expect(isWeakPassword('abcdefgh')).toBe(true)
    expect(isWeakPassword('Abcdef1!')).toBe(false)
    expect(isWeakPassword('a much longer passphrase')).toBe(false)
  })
})

describe('DashboardTab — switching on', () => {
  it('turns the dashboard on and shows the port it runs on', async () => {
    await mount(state())
    mPut.mockResolvedValueOnce(running())
    fireEvent.click(enableSwitch())
    await waitFor(() => expect(mPut).toHaveBeenCalledWith('/api/admin/dashboard', { enabled: true }))
    expect(await screen.findByText('Running on port 8100')).toBeTruthy()
    expect(enableSwitch().checked).toBe(true)
  })

  it('shows a taken port inline and leaves the switch off', async () => {
    await mount(state())
    mPut.mockRejectedValueOnce(new ApiError(409, '/api/admin/dashboard', {
      code: 'DASHBOARD_PORT_IN_USE', detail: 'busy', params: { port: 8100 },
    }))
    fireEvent.click(enableSwitch())
    expect(await screen.findByText('Port 8100 is in use by another program.')).toBeTruthy()
    expect(enableSwitch().checked).toBe(false)
    expect(showToast).not.toHaveBeenCalled()
  })
})

describe('DashboardTab — open on the tablet', () => {
  it('makes the port read-only on the Home Assistant App', async () => {
    await mount(state({ port_fixed: true }))
    const port = screen.getByLabelText('Port') as HTMLInputElement
    expect(port.readOnly).toBe(true)
    expect(screen.getByText(/Managed by the Home Assistant App/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Save port' })).toBeNull()
    expect(document.body.textContent).not.toMatch(/add-on/i)
  })

  it('saves a new port and checks the range first', async () => {
    await mount(state())
    const port = screen.getByLabelText('Port')
    fireEvent.input(port, { target: { value: '80' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save port' }))
    expect(await screen.findByText('Pick a port from 1024 to 65535.')).toBeTruthy()
    expect(mPut).not.toHaveBeenCalled()
    fireEvent.input(port, { target: { value: '9000' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save port' }))
    await waitFor(() => expect(mPut).toHaveBeenCalledWith('/api/admin/dashboard', { port: 9000 }))
  })

  it('shows a taken port inline and keeps the typed value', async () => {
    await mount(running())
    mPut.mockRejectedValueOnce(new ApiError(409, '/api/admin/dashboard', {
      code: 'DASHBOARD_PORT_IN_USE', detail: 'busy', params: { port: 9000 },
    }))
    const port = screen.getByLabelText('Port') as HTMLInputElement
    fireEvent.input(port, { target: { value: '9000' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save port' }))
    expect(await screen.findByText('Port 9000 is in use by another program.')).toBeTruthy()
    expect((screen.getByLabelText('Port') as HTMLInputElement).value).toBe('9000')
    expect(screen.getByText('Running on port 8100')).toBeTruthy()
  })

  it('shows a one-time sign-in QR code with its countdown', async () => {
    await mount(running())
    const expires = new Date(Date.now() + 600_000).toISOString()
    mPost.mockResolvedValueOnce({ url: 'http://homeassistant.local:8100/enter?t=abc', expires_at: expires })
    fireEvent.click(screen.getByRole('button', { name: 'Show sign-in QR' }))
    const qr = await screen.findByTestId('qr')
    expect(mPost).toHaveBeenCalledWith('/api/admin/dashboard/setup-link')
    expect(qr.getAttribute('data-payload')).toBe('http://homeassistant.local:8100/enter?t=abc')
    expect(screen.getByText(/Valid for (10:00|9:5\d) · works once/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'New sign-in link' })).toBeTruthy()
  })

  it('counts down from when the link arrived, not the server clock', async () => {
    await mount(running())
    // The server's clock runs 5 minutes ahead: its expires_at is already
    // "past" for us, yet the link is fresh.
    mPost.mockResolvedValueOnce({
      url: 'http://homeassistant.local:8100/enter?t=skew',
      expires_at: new Date(Date.now() - 1000).toISOString(),
    })
    fireEvent.click(screen.getByRole('button', { name: 'Show sign-in QR' }))
    expect(await screen.findByTestId('qr')).toBeTruthy()
    expect(screen.getByText(/Valid for 10:00 · works once/)).toBeTruthy()
  })

  it('drops the QR at 0 and stops ticking', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      await mount(running())
      mPost.mockResolvedValueOnce({
        url: 'http://homeassistant.local:8100/enter?t=abc',
        expires_at: new Date(Date.now() + 600_000).toISOString(),
      })
      fireEvent.click(screen.getByRole('button', { name: 'Show sign-in QR' }))
      await screen.findByTestId('qr')
      const cleared = vi.spyOn(globalThis, 'clearInterval')
      await act(async () => { vi.advanceTimersByTime(601_000) })
      expect(screen.getByText('The sign-in link has expired.')).toBeTruthy()
      expect(screen.queryByTestId('qr')).toBeNull()
      expect(screen.getByRole('button', { name: 'New sign-in link' })).toBeTruthy()
      expect(cleared).toHaveBeenCalled()
      expect(vi.getTimerCount()).toBe(0)
    } finally {
      vi.useRealTimers()
    }
  })

  it('drops the QR when the port changes', async () => {
    await mount(running())
    mPost.mockResolvedValueOnce({
      url: 'http://homeassistant.local:8100/enter?t=abc',
      expires_at: new Date(Date.now() + 600_000).toISOString(),
    })
    fireEvent.click(screen.getByRole('button', { name: 'Show sign-in QR' }))
    await screen.findByTestId('qr')
    mPut.mockResolvedValueOnce({
      ...running(), port: 9000, running_port: 9000, url: 'http://homeassistant.local:9000',
    })
    fireEvent.input(screen.getByLabelText('Port'), { target: { value: '9000' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save port' }))
    await screen.findByText('Running on port 9000')
    expect(screen.queryByTestId('qr')).toBeNull()
    expect(screen.getByRole('button', { name: 'Show sign-in QR' })).toBeTruthy()
  })

  it('explains a 409 when the dashboard is not running and refreshes the tab', async () => {
    await mount(running())
    mPost.mockRejectedValueOnce(new ApiError(409, '/api/admin/dashboard/setup-link', {
      code: 'DASHBOARD_NOT_RUNNING', detail: 'x',
    }))
    // The listener stopped behind the tab's back.
    mGet.mockResolvedValueOnce(state({ enabled: true, running: false }))
    fireEvent.click(screen.getByRole('button', { name: 'Show sign-in QR' }))
    expect((await screen.findByRole('alert')).textContent)
      .toBe('Turn the dashboard on before creating a sign-in link.')
    expect(screen.queryByTestId('qr')).toBeNull()
    await waitFor(() => expect(mGet).toHaveBeenCalledTimes(2))
    expect(await screen.findByText(/Switched on, but not running/)).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Show sign-in QR' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('disables the QR button while the dashboard is off', async () => {
    await mount(state())
    const btn = screen.getByRole('button', { name: 'Show sign-in QR' }) as HTMLButtonElement
    expect(btn.disabled).toBe(true)
    expect(screen.getByText(/Turn the dashboard on to create a sign-in link/)).toBeTruthy()
  })

  it('asks for the address when the guessed host would be refused', async () => {
    await mount(state({ url_host_allowed: false }))
    expect(screen.getByText(/This address won't open the dashboard/)).toBeTruthy()
    fireEvent.input(screen.getByLabelText('Dashboard address'),
      { target: { value: ' http://192.168.1.20:8100 ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(mPut).toHaveBeenCalledWith(
      '/api/admin/dashboard', { address: 'http://192.168.1.20:8100' }))
  })

  it('asks to turn the Home Assistant App port on when it is not mapped', async () => {
    await mount(state({ port_fixed: true, host_port_mapped: false }))
    expect(screen.getByText(/The dashboard port isn't turned on yet/)).toBeTruthy()
  })

  it('says nothing about the App port when it is mapped or not applicable', async () => {
    await mount(state({ port_fixed: true, host_port_mapped: true }))
    expect(screen.queryByText(/The dashboard port isn't turned on yet/)).toBeNull()
    cleanup()
    await mount(state({ host_port_mapped: null }))
    expect(screen.queryByText(/The dashboard port isn't turned on yet/)).toBeNull()
  })

  it('clears a set address back to the guess (null)', async () => {
    await mount(state({ address: 'http://tablet.lan:8100', url: 'http://tablet.lan:8100' }))
    fireEvent.click(screen.getByRole('button', { name: 'Edit address' }))
    fireEvent.click(screen.getByRole('button', { name: 'Use the best guess' }))
    await waitFor(() => expect(mPut).toHaveBeenCalledWith('/api/admin/dashboard', { address: null }))
  })

  it('shows the Home Assistant App hint for a guessed address', async () => {
    await mount(state({ port_fixed: true }))
    expect(screen.getByText(/Settings › Apps › Social Home › Network/, { selector: 'p' })).toBeTruthy()
  })
})

describe('DashboardTab — privacy lock PIN', () => {
  it('validates and sets a PIN', async () => {
    await mount(state())
    fireEvent.click(screen.getByRole('button', { name: 'Set PIN' }))
    await screen.findByRole('dialog')
    const pin = screen.getByLabelText('PIN') as HTMLInputElement
    expect(pin.getAttribute('inputmode')).toBe('numeric')
    fireEvent.input(pin, { target: { value: '12' } })
    fireEvent.input(screen.getByLabelText('Repeat PIN'), { target: { value: '12' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save PIN' }))
    expect((await screen.findByRole('alert')).textContent).toBe('The PIN must be 4 to 6 digits.')
    expect(mPut).not.toHaveBeenCalled()

    // Letters are dropped as they are typed.
    fireEvent.input(pin, { target: { value: '12a34' } })
    expect(pin.value).toBe('1234')
    fireEvent.input(screen.getByLabelText('Repeat PIN'), { target: { value: '1234' } })
    mPut.mockResolvedValueOnce(null)
    mGet.mockResolvedValueOnce(state({ pin_set: true }))
    fireEvent.click(screen.getByRole('button', { name: 'Save PIN' }))
    await waitFor(() => expect(mPut).toHaveBeenCalledWith('/api/admin/dashboard/pin', { pin: '1234' }))
    expect(await screen.findByRole('button', { name: 'Remove PIN' })).toBeTruthy()
  })

  it('removes the PIN after confirming', async () => {
    await mount(state({ pin_set: true }))
    mGet.mockResolvedValueOnce(state())
    fireEvent.click(screen.getByRole('button', { name: 'Remove PIN' }))
    await waitFor(() => expect(mDelete).toHaveBeenCalledWith('/api/admin/dashboard/pin'))
    expect(confirmDialog).toHaveBeenCalled()
    expect(await screen.findByRole('button', { name: 'Set PIN' })).toBeTruthy()
  })
})

describe('DashboardTab — display settings', () => {
  it('saves each setting as it changes', async () => {
    await mount(state())
    fireEvent.change(screen.getByLabelText('Language'), { target: { value: 'de' } })
    await waitFor(() => expect(mPut).toHaveBeenLastCalledWith('/api/admin/dashboard', { locale: 'de' }))
    fireEvent.change(screen.getByLabelText(/Back to Home after/), { target: { value: '10' } })
    await waitFor(() => expect(mPut).toHaveBeenLastCalledWith(
      '/api/admin/dashboard', { idle_return_minutes: 10 }))
    fireEvent.change(screen.getByLabelText('Night mode starts'), { target: { value: '21:30' } })
    await waitFor(() => expect(mPut).toHaveBeenLastCalledWith(
      '/api/admin/dashboard', { night_start: '21:30' }))
    fireEvent.change(screen.getByLabelText('Night mode ends'), { target: { value: '06:30' } })
    await waitFor(() => expect(mPut).toHaveBeenLastCalledWith(
      '/api/admin/dashboard', { night_end: '06:30' }))
    fireEvent.click(screen.getByRole('switch', { name: /Keep screen on/ }))
    await waitFor(() => expect(mPut).toHaveBeenLastCalledWith(
      '/api/admin/dashboard', { keep_screen_on: false }))
    fireEvent.click(screen.getByRole('switch', { name: /Show status text/ }))
    await waitFor(() => expect(mPut).toHaveBeenLastCalledWith(
      '/api/admin/dashboard', { show_status_text: true }))
  })

  it('turns the household messages on with a privacy note', async () => {
    await mount(state())
    const sw = screen.getByRole('switch', { name: 'Show household messages' }) as HTMLInputElement
    expect(sw.disabled).toBe(false)
    expect(sw.checked).toBe(false)
    expect(document.getElementById(sw.getAttribute('aria-describedby')!)?.textContent).toBe(
      'Shows the last 3 household-chat messages (up to 6 h) on Home and on picture-view pages. '
      + "@here and @name mentions show up too: @here as a banner on Home, @name as a dot on that person's face, "
      + 'until someone taps "Got it" (the chat gets a ✅) or an hour passes. '
      + "Anyone at the tablet can see them — call if it's urgent.")
    mPut.mockResolvedValueOnce(state({ show_household_messages: true }))
    fireEvent.click(sw)
    await waitFor(() => expect(mPut).toHaveBeenLastCalledWith(
      '/api/admin/dashboard', { show_household_messages: true }))
    await waitFor(() => expect(sw.checked).toBe(true))
  })

  it('greys the household messages switch while the household chat is off', async () => {
    await mount(state({ household_chat_enabled: false, show_household_messages: true }))
    const sw = screen.getByRole('switch', { name: 'Show household messages' }) as HTMLInputElement
    expect(sw.disabled).toBe(true)
    expect(sw.checked).toBe(true)
    expect(document.getElementById(sw.getAttribute('aria-describedby')!)?.textContent)
      .toMatch(/^Turn on household chat first\. Shows the last 3/)
    expect(sw.closest('.sh-dashboard-row')?.classList.contains('sh-dashboard-row--disabled')).toBe(true)
  })

  it('plays a sound for mentions by default and saves the switch', async () => {
    await mount(state({ show_household_messages: true }))
    const sw = screen.getByRole('switch', { name: 'Play a sound for mentions' }) as HTMLInputElement
    expect(sw.disabled).toBe(false)
    expect(sw.checked).toBe(true)
    expect(document.getElementById(sw.getAttribute('aria-describedby')!)?.textContent).toBe(
      'A short, soft tone on the tablet when a new @here or @name mention appears. Never at night.')
    mPut.mockResolvedValueOnce(state({ show_household_messages: true, mention_sound: false }))
    fireEvent.click(sw)
    await waitFor(() => expect(mPut).toHaveBeenLastCalledWith(
      '/api/admin/dashboard', { mention_sound: false }))
    await waitFor(() => expect(sw.checked).toBe(false))
  })

  it('greys the mention sound switch while household messages are off', async () => {
    await mount(state())
    const sw = screen.getByRole('switch', { name: 'Play a sound for mentions' }) as HTMLInputElement
    expect(sw.disabled).toBe(true)
    expect(sw.checked).toBe(true)
  })

  it('greys the mention sound switch while the household chat is off', async () => {
    await mount(state({ household_chat_enabled: false, show_household_messages: true }))
    const sw = screen.getByRole('switch', { name: 'Play a sound for mentions' }) as HTMLInputElement
    expect(sw.disabled).toBe(true)
  })

  it('lists the languages by their own names', async () => {
    await mount(state())
    const options = Array.from((screen.getByLabelText('Language') as HTMLSelectElement).options)
    expect(options.map(o => o.textContent)).toEqual(['English', 'Deutsch', 'Español', 'Français', 'Nederlands'])
  })

  it('flips a switch at once and reverts it when the save fails', async () => {
    await mount(state())
    let reject!: (e: Error) => void
    mPut.mockImplementationOnce(() => new Promise((_res, rej) => { reject = rej }))
    const sw = screen.getByRole('switch', { name: /Keep screen on/ }) as HTMLInputElement
    fireEvent.click(sw)
    await waitFor(() => expect(sw.checked).toBe(false))
    reject(new Error('Network down'))
    await waitFor(() => expect(sw.checked).toBe(true))
    expect(showToast).toHaveBeenCalledWith('Network down', 'error')
  })
})

describe('DashboardTab — overlapping saves', () => {
  /** A PUT the test resolves or rejects by hand. */
  function deferredPut() {
    let resolve!: (v: DashboardState) => void
    let reject!: (e: Error) => void
    mPut.mockImplementationOnce(() => new Promise((res, rej) => { resolve = res; reject = rej }))
    return { resolve: (v: DashboardState) => resolve(v), reject: (e: Error) => reject(e) }
  }
  const keepOn = () => screen.getByRole('switch', { name: /Keep screen on/ }) as HTMLInputElement

  it('an older save failing does not undo a newer one', async () => {
    await mount(state())
    const first = deferredPut()
    fireEvent.click(keepOn())           // → off
    await waitFor(() => expect(keepOn().checked).toBe(false))
    const second = deferredPut()
    fireEvent.click(keepOn())           // → on again
    await waitFor(() => expect(keepOn().checked).toBe(true))

    await act(async () => { second.resolve(state({ keep_screen_on: true })) })
    await act(async () => { first.reject(new Error('Network down')) })
    expect(keepOn().checked).toBe(true)
  })

  it("an older save's late answer does not overwrite a newer one", async () => {
    await mount(state())
    const first = deferredPut()
    fireEvent.click(keepOn())           // → off
    const second = deferredPut()
    await waitFor(() => expect(keepOn().checked).toBe(false))
    fireEvent.click(keepOn())           // → on again
    await act(async () => { second.resolve(state({ keep_screen_on: true })) })
    await act(async () => { first.resolve(state({ keep_screen_on: false })) })
    expect(keepOn().checked).toBe(true)
  })

  it('a newer save failing keeps an older pending value until it answers', async () => {
    await mount(state())
    const first = deferredPut()
    fireEvent.click(screen.getByRole('switch', { name: /Show status text/ }))   // status text on
    const second = deferredPut()
    fireEvent.click(keepOn())                                                   // keep screen off
    await act(async () => { second.reject(new Error('nope')) })
    expect(keepOn().checked).toBe(true)
    expect((screen.getByRole('switch', { name: /Show status text/ }) as HTMLInputElement).checked).toBe(true)
    await act(async () => { first.resolve(state({ show_status_text: true })) })
    expect((screen.getByRole('switch', { name: /Show status text/ }) as HTMLInputElement).checked).toBe(true)
  })
})

describe('DashboardTab — sessions', () => {
  it('a double click sends one sign-out', async () => {
    let confirm!: (ok: boolean) => void
    vi.mocked(confirmDialog).mockImplementationOnce(() => new Promise((res) => { confirm = res }))
    await mount(state())
    const btn = screen.getByRole('button', { name: 'Sign out all tablets' }) as HTMLButtonElement
    fireEvent.click(btn)
    fireEvent.click(btn)
    await waitFor(() => expect(btn.disabled).toBe(true))
    await act(async () => { confirm(true) })
    await waitFor(() => expect(btn.disabled).toBe(false))
    expect(confirmDialog).toHaveBeenCalledTimes(1)
    expect(mDelete).toHaveBeenCalledTimes(1)
  })

  it('describes switches with their hint, outside the name', async () => {
    await mount(state())
    const sw = screen.getByRole('switch', { name: 'Keep screen on' })
    expect(document.getElementById(sw.getAttribute('aria-describedby')!)?.textContent)
      .toMatch(/going to sleep/)
  })

  it('shows when a tablet last opened it', async () => {
    await mount(state({ last_seen_at: new Date(Date.now() - 5 * 60_000).toISOString() }))
    expect(screen.getByText(/Last opened on a tablet: 5 min ago/)).toBeTruthy()
  })

  it('signs out every tablet after confirming', async () => {
    await mount(state())
    expect(screen.getByText(/No tablet has opened it/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Sign out all tablets' }))
    await waitFor(() => expect(mDelete).toHaveBeenCalledWith('/api/admin/dashboard/sessions'))
    expect(vi.mocked(confirmDialog).mock.calls[0][0]).toBe('Every tablet will need the password again, and every wall panel a new code.')
  })

  it('does nothing when the confirm is cancelled', async () => {
    vi.mocked(confirmDialog).mockResolvedValueOnce(false)
    await mount(state())
    fireEvent.click(screen.getByRole('button', { name: 'Sign out all tablets' }))
    await waitFor(() => expect(confirmDialog).toHaveBeenCalled())
    expect(mDelete).not.toHaveBeenCalled()
  })
})

describe('DashboardTab — people', () => {
  it('marks a default picture view, including the child-account default', async () => {
    await mount(state())
    expect(screen.getByText('Default')).toBeTruthy()
    expect(screen.getByText('Default (child account)')).toBeTruthy()
    expect((screen.getByRole('switch', { name: 'Picture view for Mia' }) as HTMLInputElement).checked).toBe(true)
  })

  it('explains picture view once and describes every picture switch with it', async () => {
    await mount(state())
    const hint = screen.getByText("Picture view: an emoji page with an analog clock for children who can't read yet.")
    expect(hint.id).toBe('sh-dash-picture-hint')
    for (const name of ['Pascal', 'Mia', 'Leo']) {
      const sw = screen.getByRole('switch', { name: `Picture view for ${name}` })
      expect(sw.getAttribute('aria-describedby')).toBe('sh-dash-picture-hint')
    }
  })

  it('leaves the picture-view hint out when there is nobody to list', async () => {
    await mount(state({ people: [] }))
    expect(screen.getByText('No household members yet.')).toBeTruthy()
    expect(document.getElementById('sh-dash-picture-hint')).toBeNull()
  })

  it('hides a person from the dashboard', async () => {
    await mount(state())
    fireEvent.click(screen.getByRole('switch', { name: 'Show Pascal on the dashboard' }))
    await waitFor(() => expect(mPut).toHaveBeenCalledWith('/api/admin/dashboard', {
      people: { u1: { show: false, picture: null } },
    }))
  })

  it('turns picture view on and off explicitly', async () => {
    await mount(state())
    fireEvent.click(screen.getByRole('switch', { name: 'Picture view for Mia' }))
    await waitFor(() => expect(mPut).toHaveBeenCalledWith('/api/admin/dashboard', {
      people: { u2: { show: true, picture: false } },
    }))
  })

  it('resets an explicit picture view to the default (null)', async () => {
    await mount(state())
    fireEvent.click(screen.getByRole('button', { name: 'Reset picture view for Leo to default' }))
    await waitFor(() => expect(mPut).toHaveBeenCalledWith('/api/admin/dashboard', {
      people: { u3: { show: true, picture: null } },
    }))
  })

  it('greys picture view for a hidden person', async () => {
    await mount(state({ people: [
      { user_id: 'u1', username: 'pascal', display_name: 'Pascal', picture_url: null,
        show: false, picture: null, picture_effective: false },
    ] }))
    expect((screen.getByRole('switch', { name: 'Picture view for Pascal' }) as HTMLInputElement).disabled).toBe(true)
  })
})

describe('AdminPage — Dashboard tab', () => {
  it('lists the Dashboard tab and opens it', async () => {
    mGet.mockImplementation(async (path: string) => {
      if (path === '/api/admin/dashboard') return state()
      if (path === '/api/admin/tokens') return { tokens: [] }
      return []
    })
    const { default: AdminPage } = await import('./AdminPage')
    render(<AdminPage />)
    fireEvent.click(await screen.findByRole('tab', { name: 'Dashboard' }))
    expect(await screen.findByRole('heading', { name: 'Household dashboard' })).toBeTruthy()
    expect(mGet).toHaveBeenCalledWith('/api/admin/dashboard')
  })
})
