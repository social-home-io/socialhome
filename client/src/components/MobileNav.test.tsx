import { describe, it, expect, afterEach } from 'vitest'
import { render, act } from '@testing-library/preact'
import { LocationProvider } from 'preact-iso'
import { MobileNav, mobileSidebarOpen } from './MobileNav'
import { shortcutsHelpOpen } from '@/lib/shortcuts'
import { dmUnreadTotal } from '@/store/dms'

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
