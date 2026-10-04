import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'
import { signal } from '@preact/signals'
import { SpaceSubHeader, type SpaceTab } from './SpaceSubHeader'
import { setLocale } from '@/i18n/i18n'

const TABS: readonly SpaceTab[] = ['feed', 'members', 'pages', 'calendar', 'gallery']
const FULL_TABS: readonly SpaceTab[] = [
  'feed', 'members', 'pages', 'calendar', 'tasks', 'gallery', 'moderation',
]

// jsdom doesn't lay out, so the overflow detector reads 0 for both
// ``scrollWidth`` and ``clientWidth`` and the ⋯ button never appears.
// We monkey-patch ``scrollWidth`` on .sh-space-tabs to simulate an
// over-stuffed strip the user would see on a 390-px phone.
function forceOverflow() {
  Object.defineProperty(HTMLElement.prototype, 'scrollWidth', {
    configurable: true,
    get(this: HTMLElement) {
      return this.classList?.contains('sh-space-tabs') ? 9999 : 0
    },
  })
  Object.defineProperty(HTMLElement.prototype, 'clientWidth', {
    configurable: true,
    get() { return 300 },
  })
}

function restoreLayout() {
  // ``scrollWidth``/``clientWidth`` are not normally
  // configurable own properties on HTMLElement — the cast
  // sidesteps that so the next test starts from the jsdom default.
  delete (HTMLElement.prototype as unknown as Record<string, unknown>).scrollWidth
  delete (HTMLElement.prototype as unknown as Record<string, unknown>).clientWidth
}

beforeEach(() => {
  restoreLayout()
  // ResizeObserver isn't in jsdom — stub it so the component's
  // useLayoutEffect doesn't crash.
  ;(window as unknown as Record<string, unknown>).ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  HTMLElement.prototype.scrollIntoView = vi.fn()
})

describe('SpaceSubHeader', () => {
  it('renders one tab button per visibleTabs entry, marks the active one selected', () => {
    const activeTab = signal<SpaceTab>('members')
    const { container, getByText } = render(
      <SpaceSubHeader
        name="Garden"
        emoji="🌿"
        memberCount={3}
        activeTab={activeTab}
        visibleTabs={TABS}
        onSelectTab={() => {}}
      />,
    )
    const buttons = container.querySelectorAll('nav[role="tablist"] button[role="tab"]')
    expect(buttons.length).toBe(TABS.length)

    const active = getByText('Members').closest('button')!
    expect(active.getAttribute('aria-selected')).toBe('true')
    expect(active.classList.contains('sh-tab--active')).toBe(true)

    const feed = getByText('Feed').closest('button')!
    expect(feed.getAttribute('aria-selected')).toBe('false')
  })

  it('calls onSelectTab when a tab is clicked', () => {
    const onSelect = vi.fn()
    const activeTab = signal<SpaceTab>('feed')
    const { getByText } = render(
      <SpaceSubHeader
        name="Garden"
        emoji={null}
        memberCount={null}
        activeTab={activeTab}
        visibleTabs={TABS}
        onSelectTab={onSelect}
      />,
    )
    fireEvent.click(getByText('Pages'))
    expect(onSelect).toHaveBeenCalledWith('pages')
  })

  it('shows the member count when provided, hides when null', () => {
    const activeTab = signal<SpaceTab>('feed')
    const { container, rerender } = render(
      <SpaceSubHeader
        name="Garden"
        emoji={null}
        memberCount={5}
        activeTab={activeTab}
        visibleTabs={TABS}
        onSelectTab={() => {}}
      />,
    )
    expect(container.textContent).toContain('5 members')

    rerender(
      <SpaceSubHeader
        name="Garden"
        emoji={null}
        memberCount={1}
        activeTab={activeTab}
        visibleTabs={TABS}
        onSelectTab={() => {}}
      />,
    )
    expect(container.textContent).toContain('1 member')
    expect(container.textContent).not.toContain('1 members')

    rerender(
      <SpaceSubHeader
        name="Garden"
        emoji={null}
        memberCount={null}
        activeTab={activeTab}
        visibleTabs={TABS}
        onSelectTab={() => {}}
      />,
    )
    expect(container.querySelector('.sh-space-subheader-meta')).toBeNull()
  })

  it('renders the actions slot when provided', () => {
    const activeTab = signal<SpaceTab>('feed')
    const { getByText } = render(
      <SpaceSubHeader
        name="Garden"
        emoji={null}
        memberCount={null}
        activeTab={activeTab}
        visibleTabs={TABS}
        onSelectTab={() => {}}
        actions={<button type="button">⚙ Settings</button>}
      />,
    )
    expect(getByText('⚙ Settings')).toBeTruthy()
  })

  // ── Overflow menu ─────────────────────────────────────────────────────

  it('hides the ⋯ overflow trigger when the strip fits', () => {
    const activeTab = signal<SpaceTab>('feed')
    const { queryByLabelText } = render(
      <SpaceSubHeader
        name="Garden"
        emoji={null}
        memberCount={null}
        activeTab={activeTab}
        visibleTabs={TABS}
        onSelectTab={() => {}}
      />,
    )
    // jsdom layout = 0 width, so the detector reads "no overflow"
    // and the trigger never mounts — same as a desktop viewport
    // where every tab fits.
    expect(queryByLabelText('More sections')).toBeNull()
  })

  it('renders the ⋯ overflow trigger when the strip overflows', async () => {
    forceOverflow()
    const activeTab = signal<SpaceTab>('feed')
    const { findByLabelText } = render(
      <SpaceSubHeader
        name="Garden"
        emoji={null}
        memberCount={null}
        activeTab={activeTab}
        visibleTabs={FULL_TABS}
        onSelectTab={() => {}}
      />,
    )
    const trigger = await findByLabelText('More sections')
    expect(trigger).toBeTruthy()
    expect(trigger.getAttribute('aria-expanded')).toBe('false')
  })

  it('opens the popover with every tab when ⋯ is clicked', async () => {
    forceOverflow()
    const activeTab = signal<SpaceTab>('moderation')
    const { findByLabelText, getByRole } = render(
      <SpaceSubHeader
        name="Garden"
        emoji={null}
        memberCount={null}
        activeTab={activeTab}
        visibleTabs={FULL_TABS}
        onSelectTab={() => {}}
      />,
    )
    const trigger = await findByLabelText('More sections')
    fireEvent.click(trigger)
    await waitFor(() => {
      expect(trigger.getAttribute('aria-expanded')).toBe('true')
    })
    const menu = getByRole('menu', { name: 'All sections' })
    const items = menu.querySelectorAll('button[role="menuitemradio"]')
    expect(items.length).toBe(FULL_TABS.length)
    const moderationItem = Array.from(items).find(
      (el) => el.querySelector('.sh-space-tabs-overflow__label')?.textContent === 'Moderation',
    )!
    expect(moderationItem.getAttribute('aria-checked')).toBe('true')
  })

  it('selecting a tab in the popover fires onSelectTab and closes the menu', async () => {
    forceOverflow()
    const onSelect = vi.fn()
    const activeTab = signal<SpaceTab>('feed')
    const { findByLabelText, getByRole, queryByRole } = render(
      <SpaceSubHeader
        name="Garden"
        emoji={null}
        memberCount={null}
        activeTab={activeTab}
        visibleTabs={FULL_TABS}
        onSelectTab={onSelect}
      />,
    )
    fireEvent.click(await findByLabelText('More sections'))
    const menu = getByRole('menu', { name: 'All sections' })
    const gallery = Array.from(
      menu.querySelectorAll('button[role="menuitemradio"]'),
    ).find(
      (el) => el.querySelector('.sh-space-tabs-overflow__label')?.textContent === 'Gallery',
    ) as HTMLButtonElement
    fireEvent.click(gallery)
    expect(onSelect).toHaveBeenCalledWith('gallery')
    await waitFor(() => {
      expect(queryByRole('menu', { name: 'All sections' })).toBeNull()
    })
  })

  it('Escape closes the overflow menu', async () => {
    forceOverflow()
    const activeTab = signal<SpaceTab>('feed')
    const { findByLabelText, getByRole, queryByRole } = render(
      <SpaceSubHeader
        name="Garden"
        emoji={null}
        memberCount={null}
        activeTab={activeTab}
        visibleTabs={FULL_TABS}
        onSelectTab={() => {}}
      />,
    )
    fireEvent.click(await findByLabelText('More sections'))
    getByRole('menu', { name: 'All sections' })
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => {
      expect(queryByRole('menu', { name: 'All sections' })).toBeNull()
    })
  })
})

describe('SpaceSubHeader in German', () => {
  afterEach(async () => { await setLocale('en') })

  it('names the tabs and counts members with the right plural form', async () => {
    await setLocale('de')
    const props = {
      name: 'Familie', emoji: null, activeTab: signal<SpaceTab>('feed'),
      visibleTabs: ['feed', 'members', 'calendar', 'map', 'moderation'] as SpaceTab[],
      onSelectTab: () => {},
    }
    const { container, rerender } = render(<SpaceSubHeader {...props} memberCount={1} />)
    expect([...container.querySelectorAll('[role="tab"]')].map(b => b.textContent))
      .toEqual(['Feed', 'Mitglieder', 'Kalender', 'Karte', 'Moderation'])
    expect(container.querySelector('.sh-space-subheader-meta')?.textContent).toBe('1 Mitglied')
    rerender(<SpaceSubHeader {...props} memberCount={3} />)
    expect(container.querySelector('.sh-space-subheader-meta')?.textContent).toBe('3 Mitglieder')
  })
})
