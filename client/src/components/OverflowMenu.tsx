/**
 * OverflowMenu — a "···" / "⋯" trigger with a small ``role="menu"``.
 *
 * The first item takes focus on open, ArrowUp / ArrowDown move
 * (wrapping), Home / End jump to the ends, a letter jumps to the next
 * item starting with it, Escape closes and returns focus to the trigger, an
 * outside press closes (Safari never focuses a clicked button, so
 * focusout alone can't be relied on). Picking an item parks focus on
 * the trigger first, so a dialog the action opens hands focus back to
 * it when it closes. Shared by the timetable header and the day
 * headings, and by the shopping row's store picker.
 *
 * Items with ``checked`` set render as ``menuitemradio`` (a pick-one
 * menu such as "which store"). ``bareTrigger`` drops the round ⋯
 * button chrome so the trigger can look like a pill; ``open`` /
 * ``onOpenChange`` make the open state controllable.
 */
import type { ComponentChildren } from 'preact'
import { useEffect, useLayoutEffect, useRef, useState } from 'preact/hooks'

export interface MenuItem {
  label: string
  onSelect: () => void
  danger?: boolean
  disabled?: boolean
  title?: string
  /** Pick-one menus: ``true`` / ``false`` renders a ``menuitemradio``
   *  with ``aria-checked`` and a trailing ✓ when checked. Leave unset
   *  for a plain action. */
  checked?: boolean
  /** Extra class on the item button. */
  class?: string
  /** React key — defaults to ``label`` (set it when labels repeat). */
  key?: string
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
  /** Extra class on the ``role="menu"`` popup. */
  menuClass?: string
  /** Render the trigger with only ``triggerClass`` (no ⋯ chrome). */
  bareTrigger?: boolean
  /** Tooltip; defaults to ``label``. */
  title?: string
  /** Controlled open state (pair with ``onOpenChange``). */
  open?: boolean
  onOpenChange?: (open: boolean) => void
}

const MENU_W = 200

export function OverflowMenu({
  label, items, triggerClass = '', wrapClass = '', children, floating = false,
  menuClass = '', bareTrigger = false, title, open: openProp, onOpenChange,
}: Props) {
  const [openState, setOpenState] = useState(false)
  const open = openProp ?? openState
  const setOpen = (next: boolean | ((v: boolean) => boolean)) => {
    const value = typeof next === 'function' ? next(open) : next
    if (openProp === undefined) setOpenState(value)
    onOpenChange?.(value)
  }
  /** Effects close through this ref so they see the current props. */
  const setOpenRef = useRef(setOpen)
  setOpenRef.current = setOpen
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null)
  const triggerRef = useRef<HTMLButtonElement | null>(null)
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const menuRef = useRef<HTMLDivElement | null>(null)

  const menuItems = () => Array.from(
    menuRef.current?.querySelectorAll<HTMLElement>(
      ':is([role="menuitem"], [role="menuitemradio"]):not([aria-disabled="true"])',
    ) ?? [],
  )
  // A pick-one menu opens on the current choice, like a native select.
  useEffect(() => {
    if (!open) return
    const list = menuItems()
    ;(list.find(el => el.getAttribute('aria-checked') === 'true') ?? list[0])?.focus()
  }, [open])
  useEffect(() => {
    if (!open) return
    const onDown = (e: PointerEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setOpenRef.current(false)
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
      if (r.bottom < 0 || r.top > window.innerHeight) { setOpenRef.current(false); return }
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
    if (!open) return
    const list = menuItems()
    if (list.length === 0) return
    const i = list.indexOf(document.activeElement as HTMLElement)
    let next = -1
    if (e.key === 'ArrowDown') next = (i + 1) % list.length
    else if (e.key === 'ArrowUp') next = (i - 1 + list.length) % list.length
    else if (e.key === 'Home') next = 0
    else if (e.key === 'End') next = list.length - 1
    else if (e.key.length === 1 && /\S/.test(e.key) && !e.ctrlKey && !e.metaKey && !e.altKey) {
      // Type-ahead: the next item (after the focused one, wrapping)
      // whose label starts with that letter.
      const ch = e.key.toLocaleLowerCase()
      for (let k = 1; k <= list.length; k++) {
        const cand = list[(i + k + list.length) % list.length]
        if ((cand.textContent ?? '').trim().toLocaleLowerCase().startsWith(ch)) {
          next = (i + k + list.length) % list.length
          break
        }
      }
    }
    if (next < 0) return
    e.preventDefault()
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
        class={bareTrigger ? triggerClass : `sh-post-overflow ${triggerClass}`.trim()}
        aria-label={label}
        title={title ?? label}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen(v => !v)}
      >
        {children}
      </button>
      {open && (
        <div ref={menuRef}
             class={`sh-post-menu${floating ? ' sh-timetable-menu--floating' : ''} ${menuClass}`.trim()}
             role="menu" aria-label={label}
             style={floating && pos ? { top: `${pos.top}px`, left: `${pos.left}px`, width: `${MENU_W}px` } : undefined}>
          {items.map(item => {
            const radio = item.checked !== undefined
            const cls = [item.danger ? 'sh-post-menu-danger' : '', item.class ?? '']
              .filter(Boolean).join(' ')
            return (
              <button key={item.key ?? item.label} type="button"
                      role={radio ? 'menuitemradio' : 'menuitem'}
                      aria-checked={radio ? (item.checked ? 'true' : 'false') : undefined}
                      tabIndex={-1}
                      class={cls || undefined}
                      aria-disabled={item.disabled ? 'true' : undefined}
                      title={item.title}
                      onPointerDown={keepFocus} onClick={pick(item)}>
                {item.label}
                {item.checked && <span aria-hidden="true" class="sh-post-menu__check">✓</span>}
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}
