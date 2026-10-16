import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { render, act } from '@testing-library/preact'
import { LocationProvider } from 'preact-iso'
import { MobileNav, mobileSidebarOpen } from './MobileNav'
import { shortcutsHelpOpen } from '@/lib/shortcuts'
import { dmUnreadTotal } from '@/store/dms'
import { KBD_OPEN_CLASS, keyboardOpen } from '@/hooks/useKeyboardOpen'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

function dmTab(container: Element): HTMLAnchorElement {
  const bar = container.querySelector('.sh-mobile-nav')!
  return [...bar.querySelectorAll('a.sh-mobile-tab')].find(
    (a) => a.getAttribute('href') === '/dms',
  ) as HTMLAnchorElement
}

const wrap = (ui: any) => (
  <LocationProvider>{ui}</LocationProvider>
)

describe('MobileNav', () => {
  afterEach(() => { dmUnreadTotal.value = 0 })

  it('renders 5 bottom tabs (4 link tabs + 1 More button)', () => {
    const { container } = render(wrap(<MobileNav />))
    // Only the bottom-bar tabs — the drawer hosts the full SideNav,
    // whose many links would otherwise inflate the count.
    const bar = container.querySelector('.sh-mobile-nav')!
    const tabs = bar.querySelectorAll('.sh-mobile-tab')
    expect(tabs.length).toBe(5)
    // 4 are anchor links, 1 is a button (the "More" toggle).
    expect(bar.querySelectorAll('a.sh-mobile-tab').length).toBe(4)
    expect(bar.querySelectorAll('button.sh-mobile-tab').length).toBe(1)
  })

  it('closes the drawer when the keyboard-shortcuts dialog opens', () => {
    render(wrap(<MobileNav />))
    act(() => { mobileSidebarOpen.value = true })
    expect(mobileSidebarOpen.value).toBe(true)
    act(() => { shortcutsHelpOpen.value = true })
    expect(mobileSidebarOpen.value).toBe(false)
    shortcutsHelpOpen.value = false
  })

  it('has navigation role', () => {
    const { container } = render(wrap(<MobileNav />))
    expect(container.querySelector('nav[role="navigation"]')).toBeTruthy()
  })

  it('contains Home and Spaces tabs', () => {
    const { container } = render(wrap(<MobileNav />))
    const bar = container.querySelector('.sh-mobile-nav')!
    expect(bar.textContent).toContain('Home')
    expect(bar.textContent).toContain('Spaces')
  })

  it('marks the matching tab active via aria-current + sh-active', () => {
    // jsdom navigates to "/" by default, so the Feed tab should be the
    // active one without any extra setup.
    const { container } = render(wrap(<MobileNav />))
    const bar = container.querySelector('.sh-mobile-nav')!
    const active = bar.querySelector('a.sh-active') as HTMLAnchorElement | null
    expect(active).toBeTruthy()
    expect(active?.getAttribute('aria-current')).toBe('page')
    expect(active?.getAttribute('href')).toBe('/')
  })

  it('badges the DMs tab with the live unread count', () => {
    dmUnreadTotal.value = 3
    const { container } = render(wrap(<MobileNav />))
    const badge = dmTab(container).querySelector('.sh-mobile-tab__badge')
    expect(badge?.textContent).toBe('3')
  })

  it('hides the DMs badge when there are no unread messages', () => {
    dmUnreadTotal.value = 0
    const { container } = render(wrap(<MobileNav />))
    expect(dmTab(container).querySelector('.sh-mobile-tab__badge')).toBeNull()
  })

  it('caps the DMs badge at 99+', () => {
    dmUnreadTotal.value = 150
    const { container } = render(wrap(<MobileNav />))
    expect(dmTab(container).querySelector('.sh-mobile-tab__badge')?.textContent)
      .toBe('99+')
  })

  it('More tab toggles the sidebar drawer', () => {
    const { container, rerender } = render(wrap(<MobileNav />))
    const drawer = container.querySelector('.sh-mobile-drawer')!
    expect(drawer.classList.contains('sh-mobile-drawer--open')).toBe(false)
    const more = container.querySelector('button.sh-mobile-tab') as HTMLButtonElement
    more.click()
    rerender(wrap(<MobileNav />))
    expect(
      container.querySelector('.sh-mobile-drawer')!.classList.contains('sh-mobile-drawer--open'),
    ).toBe(true)
  })
})

describe('MobileNav — links under the HA ingress prefix', () => {
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

  it('bottom tabs carry the ingress prefix (/api/hassio_ingress/<token>/)', async () => {
    const { MobileNav: PrefixedNav } = await import('./MobileNav')
    const iso = await import('preact-iso')
    const { container } = render(<iso.LocationProvider><PrefixedNav /></iso.LocationProvider>)
    const bar = container.querySelector('.sh-mobile-nav')!
    const hrefs = [...bar.querySelectorAll('a.sh-mobile-tab')].map(a => a.getAttribute('href'))
    expect(hrefs).toEqual([
      '/api/hassio_ingress/tok/', '/api/hassio_ingress/tok/spaces',
      '/api/hassio_ingress/tok/dms', '/api/hassio_ingress/tok/notifications',
    ])
  })
})

describe('MobileNav — steps aside while the on-screen keyboard is up', () => {
  /** A phone: coarse pointer + a controllable visual viewport. */
  let vv: EventTarget & { height: number, scale: number, offsetTop: number }
  beforeEach(() => {
    vv = Object.assign(new EventTarget(), { height: 844, scale: 1, offsetTop: 0 })
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 390 })
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 844 })
    Object.defineProperty(window, 'visualViewport', { configurable: true, value: vv })
    Object.defineProperty(window, 'matchMedia', {
      configurable: true,
      value: (q: string) => ({ matches: q.includes('coarse') }),
    })
  })
  afterEach(() => { document.body.innerHTML = '' })

  function openKeyboard(input: HTMLInputElement) {
    act(() => { input.focus() })
    act(() => {
      vv.height = 500
      vv.dispatchEvent(new Event('resize'))
    })
  }

  it('hides the tab bar (inert + aria-hidden) and restores it once the keyboard is gone', async () => {
    const { container } = render(wrap(<><MobileNav /><input id="f" /></>))
    const nav = container.querySelector('.sh-mobile-nav') as HTMLElement
    expect(nav.classList.contains('sh-mobile-nav--kbd')).toBe(false)
    expect(nav.hasAttribute('inert')).toBe(false)
    expect(nav.getAttribute('aria-hidden')).toBeNull()

    const input = container.querySelector('#f') as HTMLInputElement
    openKeyboard(input)
    expect(nav.classList.contains('sh-mobile-nav--kbd')).toBe(true)
    expect(nav.hasAttribute('inert')).toBe(true)
    expect(nav.getAttribute('aria-hidden')).toBe('true')
    // The html class is what hands the bar's reserved padding back.
    expect(document.documentElement.classList.contains(KBD_OPEN_CLASS)).toBe(true)

    await act(async () => {
      input.blur()
      await new Promise((r) => setTimeout(r, 0))
    })
    // Still hidden while the keyboard animates away — it must not pop
    // up over the composer under a finger that's mid-tap.
    expect(nav.classList.contains('sh-mobile-nav--kbd')).toBe(true)
    act(() => {
      vv.height = 844
      vv.dispatchEvent(new Event('resize'))
    })
    expect(nav.classList.contains('sh-mobile-nav--kbd')).toBe(false)
    expect(nav.hasAttribute('inert')).toBe(false)
    expect(nav.getAttribute('aria-hidden')).toBeNull()
    expect(document.documentElement.classList.contains(KBD_OPEN_CLASS)).toBe(false)
  })

  it('stays put for a checkbox (no keyboard)', () => {
    const { container } = render(wrap(<><MobileNav /><input id="c" type="checkbox" /></>))
    openKeyboard(container.querySelector('#c') as HTMLInputElement)
    expect(container.querySelector('.sh-mobile-nav--kbd')).toBeNull()
  })

  it('clears the keyboard state when the nav unmounts', () => {
    const { container, unmount } = render(wrap(<><MobileNav /><input id="f" /></>))
    openKeyboard(container.querySelector('#f') as HTMLInputElement)
    expect(keyboardOpen.value).toBe(true)
    act(() => { unmount() })
    expect(keyboardOpen.value).toBe(false)
    expect(document.documentElement.classList.contains(KBD_OPEN_CLASS)).toBe(false)
  })

  it('app.css hands the reserved bar space back to the content', () => {
    // jsdom doesn't apply media-queried stylesheet rules, so guard the
    // rules themselves: the bar slides away and the content's 84 px
    // bottom clearance (+ the chat hosts' bar subtraction) drops.
    const css = readFileSync(join(__dirname, '..', 'styles', 'app.css'), 'utf8')
    expect(css).toMatch(/\.sh-mobile-nav\.sh-mobile-nav--kbd \{[^}]*transform: translateY\(100%\);[^}]*visibility: hidden;/)
    expect(css).toMatch(/html\.sh-kbd-open \.sh-main \{ padding-bottom: var\(--sh-space-md\); \}/)
    const chat = css.match(/html\.sh-kbd-open \.sh-space-feed--chat \{([^}]*)\}/)
    expect(chat).not.toBeNull()
    expect(chat![1]).not.toContain('--sh-mobile-nav-height')
    expect(css).toMatch(/html\.sh-kbd-open \.sh-back-to-top \{ display: none; \}/)
  })
})
