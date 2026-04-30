import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/preact'
import { signal } from '@preact/signals'
import { SpaceSubHeader, type SpaceTab } from './SpaceSubHeader'

const TABS: readonly SpaceTab[] = ['feed', 'members', 'pages', 'calendar', 'gallery']

describe('SpaceSubHeader', () => {
  it('renders one tab button per visibleTabs entry, marks the active one selected', () => {
    const activeTab = signal<SpaceTab>('members')
    const { container, getByText } = render(
      <SpaceSubHeader
        name="Garden"
        emoji="🌿"
        coverUrl={null}
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
        coverUrl={null}
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
        coverUrl={null}
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
        coverUrl={null}
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
        coverUrl={null}
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
        coverUrl={null}
        memberCount={null}
        activeTab={activeTab}
        visibleTabs={TABS}
        onSelectTab={() => {}}
        actions={<button type="button">⚙ Settings</button>}
      />,
    )
    expect(getByText('⚙ Settings')).toBeTruthy()
  })
})
