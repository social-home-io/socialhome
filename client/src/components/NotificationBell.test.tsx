import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, act, waitFor } from '@testing-library/preact'
import { LocationProvider } from 'preact-iso'

const apiGet = vi.fn()
const apiPost = vi.fn()
vi.mock('@/api', () => ({
  api: {
    get: (...a: unknown[]) => apiGet(...a),
    post: (...a: unknown[]) => apiPost(...a),
  },
}))

import { NotificationBell, panelOpen } from './NotificationBell'
import { unreadCount, recent } from '@/store/notifications'

const original = window.matchMedia
/** ``phone`` decides whether the bottom-bar breakpoint query matches. */
function viewport(phone: boolean) {
  window.matchMedia = ((q: string) => ({
    matches: q.includes('max-width: 768px') ? phone : false,
    media: q,
    addEventListener: () => {},
    removeEventListener: () => {},
  })) as unknown as typeof window.matchMedia
}

const rows = [
  { id: 'n1', type: 'post_created', title: 'Ben posted a new update', body: null,
    link_url: '/post/p1', read_at: null, created_at: '2026-10-27T10:00:00+00:00' },
  { id: 'n2', type: 'space_join_denied', title: 'Your join request was declined', body: null,
    link_url: null, read_at: '2026-10-27T09:00:00+00:00', created_at: '2026-10-27T08:00:00+00:00' },
]

beforeEach(() => {
  apiGet.mockReset().mockResolvedValue(rows)
  apiPost.mockReset().mockResolvedValue({})
  unreadCount.value = 0
  recent.value = []
  panelOpen.value = false
  history.replaceState(null, '', '/')
})
afterEach(() => {
  window.matchMedia = original
  panelOpen.value = false
  document.body.innerHTML = ''
})

const mount = () => render(
  <LocationProvider><NotificationBell /></LocationProvider>,
  { container: document.body.appendChild(document.createElement('div')) },
)

describe('NotificationBell on phones', () => {
  it('is a link to the Notifications page, not a dropdown', () => {
    viewport(true)
    unreadCount.value = 8
    const { container } = mount()
    const link = container.querySelector<HTMLAnchorElement>('a.sh-notif-btn')!
    expect(link).not.toBeNull()
    expect(link.getAttribute('href')).toBe('/notifications')
    expect(link.getAttribute('aria-haspopup')).toBeNull()
    expect(link.getAttribute('aria-label')).toBe('Notifications (8 unread)')
    // The count is visible on the bell itself.
    expect(container.querySelector('.sh-notif-badge')!.textContent).toBe('8')
    expect(container.querySelector('button.sh-notif-btn')).toBeNull()
    act(() => { link.click() })
    expect(container.querySelector('.sh-notif-panel')).toBeNull()
  })

  it('is the current page on /notifications and a second tap scrolls to the top', () => {
    viewport(true)
    history.replaceState(null, '', '/notifications')
    const scroll = vi.fn()
    window.scrollTo = scroll as unknown as typeof window.scrollTo
    const { container } = mount()
    const link = container.querySelector<HTMLAnchorElement>('a.sh-notif-btn')!
    expect(link.getAttribute('aria-current')).toBe('page')
    const ev = new MouseEvent('click', { bubbles: true, cancelable: true })
    act(() => { link.dispatchEvent(ev) })
    expect(ev.defaultPrevented).toBe(true)
    expect(scroll).toHaveBeenCalledWith({ top: 0, behavior: 'smooth' })
  })

  it('hides the badge at zero', () => {
    viewport(true)
    const { container } = mount()
    expect(container.querySelector('.sh-notif-badge')).toBeNull()
    expect(container.querySelector('a.sh-notif-btn')!.getAttribute('aria-label')).toBe('Notifications')
  })
})

describe('NotificationBell on desktop', () => {
  it('the bell is a popup button: aria-haspopup, aria-expanded, aria-controls → a labelled dialog', async () => {
    viewport(false)
    unreadCount.value = 1
    const { container } = mount()
    const bell = container.querySelector<HTMLButtonElement>('button.sh-notif-btn')!
    expect(bell.getAttribute('aria-haspopup')).toBe('dialog')
    expect(bell.getAttribute('aria-expanded')).toBe('false')
    await act(async () => { bell.click() })
    expect(bell.getAttribute('aria-expanded')).toBe('true')
    const panel = container.querySelector<HTMLElement>('.sh-notif-panel')!
    expect(panel.getAttribute('role')).toBe('dialog')
    expect(bell.getAttribute('aria-controls')).toBe(panel.id)
    const heading = document.getElementById(panel.getAttribute('aria-labelledby')!)!
    expect(heading.textContent).toBe('Notifications')
  })

  it('opening moves focus to the first row; Escape closes and returns focus to the bell', async () => {
    viewport(false)
    const { container } = mount()
    await waitFor(() => expect(apiGet).toHaveBeenCalled())
    const bell = container.querySelector<HTMLButtonElement>('button.sh-notif-btn')!
    bell.focus()
    await act(async () => { bell.click() })
    await waitFor(() => expect(container.querySelectorAll('.sh-notif-item').length).toBe(2))
    expect(document.activeElement).toBe(container.querySelector('.sh-notif-item'))
    act(() => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    })
    expect(container.querySelector('.sh-notif-panel')).toBeNull()
    expect(document.activeElement).toBe(bell)
  })

  it('each row is one named control; unread is spoken, not only coloured', async () => {
    viewport(false)
    const { container, getByRole } = mount()
    await waitFor(() => expect(apiGet).toHaveBeenCalled())
    await act(async () => { container.querySelector<HTMLButtonElement>('button.sh-notif-btn')!.click() })
    await waitFor(() => expect(container.querySelectorAll('.sh-notif-item').length).toBe(2))
    const [unread, read] = [...container.querySelectorAll<HTMLElement>('.sh-notif-item')]
    expect(unread.tagName).toBe('A')
    expect(unread.getAttribute('href')).toBe('/post/p1')
    expect(getByRole('link', { name: /^Unread, Ben posted a new update \S/ })).toBe(unread)
    // A row with nothing to open is still one real control.
    expect(read.tagName).toBe('BUTTON')
    expect(read.querySelector('.sr-only')).toBeNull()
    expect(read.textContent).toContain('Your join request was declined')
  })

  it('ends with "See all notifications", a link to the page that closes the panel', async () => {
    viewport(false)
    const { container } = mount()
    await act(async () => { container.querySelector<HTMLButtonElement>('button.sh-notif-btn')!.click() })
    const all = container.querySelector<HTMLAnchorElement>('a.sh-notif-see-all')!
    expect(all.textContent).toBe('See all notifications')
    expect(all.getAttribute('href')).toBe('/notifications')
    act(() => { all.click() })
    expect(panelOpen.value).toBe(false)
  })

  it('a panel that would spill off the left edge is nudged back on screen', async () => {
    viewport(false)
    const rect = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
      if (this.classList.contains('sh-notif-panel')) {
        return { left: -38, right: 342, top: 60, bottom: 400, width: 380, height: 340, x: -38, y: 60, toJSON() {} } as DOMRect
      }
      return { left: 0, right: 0, top: 0, bottom: 0, width: 0, height: 0, x: 0, y: 0, toJSON() {} } as DOMRect
    })
    const { container } = mount()
    await act(async () => { container.querySelector<HTMLButtonElement>('button.sh-notif-btn')!.click() })
    const panel = container.querySelector<HTMLElement>('.sh-notif-panel')!
    expect(panel.style.transform).toBe('translateX(46px)')
    rect.mockRestore()
  })

  it('Tab past the panel closes it', async () => {
    viewport(false)
    const { container } = mount()
    const outside = document.body.appendChild(document.createElement('button'))
    await act(async () => { container.querySelector<HTMLButtonElement>('button.sh-notif-btn')!.click() })
    const all = container.querySelector<HTMLAnchorElement>('a.sh-notif-see-all')!
    all.focus()
    act(() => { outside.focus() })
    expect(container.querySelector('.sh-notif-panel')).toBeNull()
  })

  it('clicking an unread row marks it read and lowers the count', async () => {
    viewport(false)
    unreadCount.value = 3
    const { container } = mount()
    await waitFor(() => expect(apiGet).toHaveBeenCalled())
    await act(async () => { container.querySelector<HTMLButtonElement>('button.sh-notif-btn')!.click() })
    await waitFor(() => expect(container.querySelectorAll('.sh-notif-item').length).toBe(2))
    const row = container.querySelector<HTMLAnchorElement>('a.sh-notif-item')!
    row.addEventListener('click', e => e.preventDefault())
    await act(async () => { row.click() })
    expect(apiPost).toHaveBeenCalledWith('/api/notifications/n1/read')
    expect(unreadCount.value).toBe(2)
  })
})

describe('notification polling lifecycle', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  it('start is idempotent — repeated calls never leak overlapping intervals', async () => {
    const { startNotificationPolling, stopNotificationPolling } =
      await import('./NotificationBell')
    const setSpy = vi.spyOn(globalThis, 'setInterval')
    const clearSpy = vi.spyOn(globalThis, 'clearInterval')

    // Reproduce the old render-body bug: many "starts" back to back.
    // Each start must clear the prior timer so only one interval is
    // ever live (the bug left one orphaned interval per call).
    startNotificationPolling()
    startNotificationPolling()
    startNotificationPolling()

    expect(setSpy).toHaveBeenCalledTimes(3)
    // 2nd + 3rd start each cleared the previous timer → net one live.
    expect(clearSpy.mock.calls.length).toBeGreaterThanOrEqual(2)

    stopNotificationPolling()
    // A redundant stop after the handle is nulled is a no-op.
    const clearsAfterStop = clearSpy.mock.calls.length
    stopNotificationPolling()
    expect(clearsAfterStop).toBe(clearSpy.mock.calls.length)
  })
})

describe('NotificationBell under the HA ingress prefix', () => {
  let baseEl: HTMLBaseElement
  beforeEach(() => {
    baseEl = document.createElement('base')
    baseEl.href = '/api/hassio_ingress/tok/'
    document.head.prepend(baseEl)
    vi.resetModules()
  })
  afterEach(() => {
    baseEl.remove()
    vi.resetModules()
  })

  it('the phone bell links to /api/hassio_ingress/<token>/notifications', async () => {
    viewport(true)
    const { NotificationBell: Bell } = await import('./NotificationBell')
    const iso = await import('preact-iso')
    const { container } = render(<iso.LocationProvider><Bell /></iso.LocationProvider>)
    expect(container.querySelector('a.sh-notif-btn')!.getAttribute('href'))
      .toBe('/api/hassio_ingress/tok/notifications')
  })

  it('"See all notifications" keeps the ingress prefix on desktop', async () => {
    viewport(false)
    const { NotificationBell: Bell } = await import('./NotificationBell')
    const iso = await import('preact-iso')
    const { container } = render(<iso.LocationProvider><Bell /></iso.LocationProvider>)
    await act(async () => { container.querySelector<HTMLButtonElement>('button.sh-notif-btn')!.click() })
    expect(container.querySelector('a.sh-notif-see-all')!.getAttribute('href'))
      .toBe('/api/hassio_ingress/tok/notifications')
  })
})
