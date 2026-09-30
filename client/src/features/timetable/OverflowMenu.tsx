/**
 * OverflowMenu — a "···" / "⋯" trigger with a small ``role="menu"``.
 *
 * The first item takes focus on open, ArrowUp / ArrowDown move
 * (wrapping), Escape closes and returns focus to the trigger, an
 * outside press closes (Safari never focuses a clicked button, so
 * focusout alone can't be relied on). Picking an item parks focus on
 * the trigger first, so a dialog the action opens hands focus back to
 * it when it closes. Shared by the timetable header and the day
 * headings.
 */
import type { ComponentChildren } from 'preact'
import { useEffect, useLayoutEffect, useRef, useState } from 'preact/hooks'

export interface MenuItem {
  label: string
  onSelect: () => void
  danger?: boolean
  disabled?: boolean
  title?: string
}

interface Props {
  label: string
  items: MenuItem[]
  triggerClass?: string
  wrapClass?: string
  children: ComponentChildren
  /** Position the menu ``fixed`` under the trigger — for triggers
   *  inside a scroll container (the grid) that would clip it. It
   *  follows the trigger on scroll / resize. */
  floating?: boolean
}

const MENU_W = 200

export function OverflowMenu({
  label, items, triggerClass = '', wrapClass = '', children, floating = false,
}: Props) {
  const [open, setOpen] = useState(false)
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null)
  const triggerRef = useRef<HTMLButtonElement | null>(null)
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const menuRef = useRef<HTMLDivElement | null>(null)

  const menuItems = () => Array.from(
    menuRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]:not([aria-disabled="true"])') ?? [],
  )
  useEffect(() => {
    if (open) menuItems()[0]?.focus()
  }, [open])
  useEffect(() => {
    if (!open) return
    const onDown = (e: PointerEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('pointerdown', onDown)
    return () => document.removeEventListener('pointerdown', onDown)
  }, [open])

  useLayoutEffect(() => {
    if (!open || !floating) { setPos(null); return }
    // Follow the trigger while the page / grid scrolls; close once it
    // has scrolled out of view.
    const place = () => {
      const r = triggerRef.current?.getBoundingClientRect()
      if (!r) return
      if (r.bottom < 0 || r.top > window.innerHeight) { setOpen(false); return }
      const left = Math.max(8, Math.min(r.right - MENU_W, window.innerWidth - MENU_W - 8))
      setPos({ top: r.bottom + 4, left })
    }
    place()
    window.addEventListener('scroll', place, true)
    window.addEventListener('resize', place)
    return () => {
      window.removeEventListener('scroll', place, true)
      window.removeEventListener('resize', place)
    }
  }, [open, floating])

  const onKey = (e: KeyboardEvent) => {
    if (e.key === 'Escape' && open) {
      e.stopPropagation()
      setOpen(false)
      triggerRef.current?.focus()
      return
    }
    if (!open || (e.key !== 'ArrowDown' && e.key !== 'ArrowUp')) return
    const list = menuItems()
    if (list.length === 0) return
    e.preventDefault()
    const i = list.indexOf(document.activeElement as HTMLElement)
    const next = e.key === 'ArrowDown' ? (i + 1) % list.length : (i - 1 + list.length) % list.length
    list[next].focus()
  }
  const pick = (item: MenuItem) => () => {
    if (item.disabled) return
    triggerRef.current?.focus()
    setOpen(false)
    item.onSelect()
  }
  // Pressing an item must not blur the trigger / item first (focusout
  // would close the menu before the click lands).
  const keepFocus = (e: PointerEvent) => e.preventDefault()

  return (
    <div
      ref={wrapRef}
      class={`sh-post-overflow-wrap ${wrapClass}`.trim()}
      onFocusOut={(e) => {
        const next = e.relatedTarget as Node | null
        if (next && !(e.currentTarget as HTMLElement).contains(next)) setOpen(false)
      }}
      onKeyDown={onKey}
    >
      <button
        ref={triggerRef}
        type="button"
        class={`sh-post-overflow ${triggerClass}`.trim()}
        aria-label={label}
        title={label}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen(v => !v)}
      >
        {children}
      </button>
      {open && (
        <div ref={menuRef} class={`sh-post-menu${floating ? ' sh-timetable-menu--floating' : ''}`}
             role="menu"
             style={floating && pos ? { top: `${pos.top}px`, left: `${pos.left}px`, width: `${MENU_W}px` } : undefined}>
          {items.map(item => (
            <button key={item.label} type="button" role="menuitem" tabIndex={-1}
                    class={item.danger ? 'sh-post-menu-danger' : undefined}
                    aria-disabled={item.disabled ? 'true' : undefined}
                    title={item.title}
                    onPointerDown={keepFocus} onClick={pick(item)}>
              {item.label}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
