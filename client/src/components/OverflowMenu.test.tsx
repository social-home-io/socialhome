import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, fireEvent, cleanup } from '@testing-library/preact'
import { useState } from 'preact/hooks'
import { createRef } from 'preact'
import { OverflowMenu, visibleBottom, type MenuItem } from './OverflowMenu'

const items = (onSelect = vi.fn()): MenuItem[] => [
  { label: 'Aldi', onSelect: () => onSelect('Aldi') },
  { label: 'Migros', onSelect: () => onSelect('Migros'), checked: true },
  { label: 'Delete', onSelect: () => onSelect('Delete'), danger: true },
]

describe('OverflowMenu', () => {
  it('puts a ref on its trigger and still returns focus there on Escape', () => {
    const ref = createRef<HTMLButtonElement>()
    const seen: (HTMLButtonElement | null)[] = []
    const { getByRole, rerender } = render(
      <OverflowMenu ref={ref} label="More" items={items()}>⋯</OverflowMenu>,
    )
    const trigger = getByRole('button', { name: 'More' })
    expect(ref.current).toBe(trigger)
    // A callback ref works too.
    rerender(<OverflowMenu ref={(el) => { seen.push(el) }} label="More" items={items()}>⋯</OverflowMenu>)
    expect(seen.at(-1)).toBe(trigger)
    fireEvent.click(trigger)
    fireEvent.keyDown(document.activeElement!, { key: 'Escape' })
    expect(document.activeElement).toBe(trigger)
  })

  it('opens on click, focuses the checked item, and moves with the arrow keys', () => {
    const { getByRole, getAllByRole } = render(
      <OverflowMenu label="More" items={items()}>⋯</OverflowMenu>,
    )
    const trigger = getByRole('button', { name: 'More' })
    expect(trigger.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(trigger)
    expect(trigger.getAttribute('aria-expanded')).toBe('true')
    const opts = getAllByRole(/menuitem/)
    // Opens on the checked choice (Migros), like a native select.
    expect(document.activeElement).toBe(opts[1])
    fireEvent.keyDown(opts[1], { key: 'ArrowDown' })
    expect(document.activeElement).toBe(opts[2])
    fireEvent.keyDown(opts[2], { key: 'ArrowDown' })
    expect(document.activeElement).toBe(opts[0])
    fireEvent.keyDown(opts[0], { key: 'ArrowUp' })
    expect(document.activeElement).toBe(opts[2])
  })

  it('focuses the first item when nothing is checked', () => {
    const plain: MenuItem[] = [
      { label: 'Edit', onSelect: () => {} },
      { label: 'Delete', onSelect: () => {} },
    ]
    const { getByRole, getAllByRole } = render(
      <OverflowMenu label="More" items={plain}>⋯</OverflowMenu>,
    )
    fireEvent.click(getByRole('button', { name: 'More' }))
    expect(document.activeElement).toBe(getAllByRole('menuitem')[0])
  })

  it('Escape closes and returns focus to the trigger', () => {
    const { getByRole, queryByRole } = render(
      <OverflowMenu label="More" items={items()}>⋯</OverflowMenu>,
    )
    const trigger = getByRole('button', { name: 'More' })
    fireEvent.click(trigger)
    fireEvent.keyDown(document.activeElement!, { key: 'Escape' })
    expect(queryByRole('menu')).toBeNull()
    expect(document.activeElement).toBe(trigger)
  })

  it('picking an item runs it, closes, and parks focus on the trigger', () => {
    const onSelect = vi.fn()
    const { getByRole, queryByRole } = render(
      <OverflowMenu label="More" items={items(onSelect)}>⋯</OverflowMenu>,
    )
    const trigger = getByRole('button', { name: 'More' })
    fireEvent.click(trigger)
    fireEvent.click(getByRole('menuitemradio', { name: /Migros/ }))
    expect(onSelect).toHaveBeenCalledWith('Migros')
    expect(queryByRole('menu')).toBeNull()
    expect(document.activeElement).toBe(trigger)
  })

  it('renders checked items as menuitemradio with aria-checked', () => {
    const { getByRole } = render(
      <OverflowMenu label="More" items={items()}>⋯</OverflowMenu>,
    )
    fireEvent.click(getByRole('button', { name: 'More' }))
    const migros = getByRole('menuitemradio', { name: /Migros/ })
    expect(migros.getAttribute('aria-checked')).toBe('true')
    // A plain action stays a menuitem.
    expect(getByRole('menuitem', { name: 'Delete' })).toBeTruthy()
  })

  it('a bare trigger drops the round ⋯ chrome, keeping its own classes', () => {
    const { getByRole } = render(
      <OverflowMenu label="Store" items={items()} bareTrigger triggerClass="pill" menuClass="sheet">
        Aldi
      </OverflowMenu>,
    )
    const trigger = getByRole('button', { name: 'Store' })
    expect(trigger.className).toBe('pill')
    fireEvent.click(trigger)
    expect(getByRole('menu').className).toContain('sheet')
  })

  it('can be controlled through open / onOpenChange', () => {
    function Host() {
      const [open, setOpen] = useState(true)
      return (
        <>
          <span data-testid="state">{String(open)}</span>
          <OverflowMenu label="More" items={items()} open={open} onOpenChange={setOpen}>⋯</OverflowMenu>
        </>
      )
    }
    const { getByRole, getByTestId, queryByRole } = render(<Host />)
    expect(getByRole('menu')).toBeTruthy()
    fireEvent.keyDown(document.activeElement!, { key: 'Escape' })
    expect(queryByRole('menu')).toBeNull()
    expect(getByTestId('state').textContent).toBe('false')
  })
})

describe('OverflowMenu — Home / End and type-ahead', () => {
  const list: MenuItem[] = [
    { label: 'Aldi', onSelect: () => {} },
    { label: 'Bäckerei', onSelect: () => {} },
    { label: 'Migros', onSelect: () => {} },
    { label: 'Metzgerei', onSelect: () => {} },
  ]
  function openMenu() {
    const r = render(<OverflowMenu label="Store" items={list}>⋯</OverflowMenu>)
    fireEvent.click(r.getByRole('button', { name: 'Store' }))
    return r
  }

  it('Home / End jump to the first / last item', () => {
    const { getAllByRole } = openMenu()
    const opts = getAllByRole('menuitem')
    fireEvent.keyDown(opts[0], { key: 'End' })
    expect(document.activeElement).toBe(opts[3])
    fireEvent.keyDown(opts[3], { key: 'Home' })
    expect(document.activeElement).toBe(opts[0])
  })

  it('a letter jumps to the next item starting with it, wrapping, case-insensitive', () => {
    const { getAllByRole } = openMenu()
    const opts = getAllByRole('menuitem')
    fireEvent.keyDown(opts[0], { key: 'm' })
    expect(document.activeElement).toBe(opts[2])
    fireEvent.keyDown(opts[2], { key: 'M' })
    expect(document.activeElement).toBe(opts[3])
    fireEvent.keyDown(opts[3], { key: 'm' })
    expect(document.activeElement).toBe(opts[2])
    fireEvent.keyDown(opts[2], { key: 'b' })
    expect(document.activeElement).toBe(opts[1])
  })

  it('ignores letters with a modifier and letters nothing starts with', () => {
    const { getAllByRole } = openMenu()
    const opts = getAllByRole('menuitem')
    fireEvent.keyDown(opts[0], { key: 'm', ctrlKey: true })
    expect(document.activeElement).toBe(opts[0])
    fireEvent.keyDown(opts[0], { key: 'z' })
    expect(document.activeElement).toBe(opts[0])
  })
})

describe('OverflowMenu near the bottom of a phone screen (#1190 follow-up)', () => {
  const rect = (top: number, height: number, left = 300, width = 40) => ({
    top, bottom: top + height, height, left, right: left + width, width, x: left, y: top,
    toJSON: () => ({}),
  }) as DOMRect
  const MENU_H = 160
  let nav: HTMLElement | null = null

  /** A 390×844 phone with the bottom tab bar (64 px) at y 780. */
  function phone(withNav = true) {
    vi.stubGlobal('innerHeight', 844)
    vi.stubGlobal('innerWidth', 390)
    if (withNav) {
      nav = document.createElement('nav')
      nav.className = 'sh-mobile-nav'
      document.body.appendChild(nav)
      vi.spyOn(nav, 'getBoundingClientRect').mockReturnValue(rect(780, 64, 0, 390))
    }
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
      if (this === nav) return rect(780, 64, 0, 390)
      if (this.classList.contains('sh-post-menu')) return rect(0, MENU_H, 0, 200)
      return rect(this.dataset.top ? Number(this.dataset.top) : 100, 40)
    })
  }

  afterEach(() => {
    cleanup()
    nav?.remove()
    nav = null
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('the visible bottom stops above the bottom tab bar', () => {
    phone()
    expect(visibleBottom()).toBe(780 - 8)
    nav!.remove()
    expect(visibleBottom()).toBe(844 - 8)
  })

  it('opens upward when it would end under the tab bar', () => {
    phone()
    const { getByRole, container } = render(
      <OverflowMenu label="More" items={items()}>⋯</OverflowMenu>,
    )
    const trigger = getByRole('button', { name: 'More' })
    trigger.dataset.top = '700'
    fireEvent.click(trigger)
    const menu = container.querySelector('.sh-post-menu')!
    expect(menu.classList.contains('sh-post-menu--up')).toBe(true)
  })

  it('opening never scrolls the page to the first item (it may be flipping upward)', () => {
    phone()
    const focus = vi.spyOn(HTMLElement.prototype, 'focus')
    const { getByRole } = render(
      <OverflowMenu label="More" items={items()}>⋯</OverflowMenu>,
    )
    const trigger = getByRole('button', { name: 'More' })
    trigger.dataset.top = '700'
    fireEvent.click(trigger)
    const itemFocus = focus.mock.calls.find((_, i) => (focus.mock.instances[i] as unknown as HTMLElement)?.getAttribute?.('role')?.startsWith('menuitem'))
    expect(itemFocus?.[0]).toEqual({ preventScroll: true })
  })

  it('opens downward when there is room', () => {
    phone()
    const { getByRole, container } = render(
      <OverflowMenu label="More" items={items()}>⋯</OverflowMenu>,
    )
    const trigger = getByRole('button', { name: 'More' })
    trigger.dataset.top = '200'
    fireEvent.click(trigger)
    expect(container.querySelector('.sh-post-menu')!.classList.contains('sh-post-menu--up')).toBe(false)
  })

  it('a floating menu is placed above its trigger when below would be covered', () => {
    phone()
    const { getByRole, container } = render(
      <OverflowMenu label="More" items={items()} floating>⋯</OverflowMenu>,
    )
    const trigger = getByRole('button', { name: 'More' })
    trigger.dataset.top = '720'
    fireEvent.click(trigger)
    const menu = container.querySelector('.sh-post-menu') as HTMLElement
    // Trigger top 720 − 4 px gap − 160 px menu.
    expect(menu.style.top).toBe(`${720 - 4 - MENU_H}px`)
  })
})
