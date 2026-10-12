/**
 * OverflowMenu — a 44 px "⋯" button opening a small action menu (reuses
 * ``.sh-post-menu``).
 *
 * Keyboard: opening focuses the first item; ↑/↓ (and Home/End) move
 * between items; Esc closes and returns focus to the button; Tab or a
 * click outside closes.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { ConnIcon, type ConnIconName } from './ConnIcon'

export interface MenuAction {
  label: string
  icon: ConnIconName
  danger?: boolean
  onSelect: () => void
}

interface Props {
  label: string
  actions: MenuAction[]
}

export function OverflowMenu({ label, actions }: Props) {
  const [open, setOpen] = useState(false)
  const wrap = useRef<HTMLSpanElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)

  const items = (): HTMLElement[] =>
    Array.from(wrap.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? [])

  useEffect(() => {
    if (!open) return
    items()[0]?.focus()
    const onDoc = (e: MouseEvent) => {
      if (!wrap.current?.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [open])

  const close = (refocus: boolean) => {
    setOpen(false)
    if (refocus) trigger.current?.focus()
  }

  const onMenuKey = (e: KeyboardEvent) => {
    const list = items()
    const idx = list.indexOf(document.activeElement as HTMLElement)
    switch (e.key) {
      case 'Escape':
        e.preventDefault()
        e.stopPropagation()
        close(true)
        return
      case 'Tab':
        close(false)
        return
      case 'ArrowDown':
        e.preventDefault()
        list[(idx + 1) % list.length]?.focus()
        return
      case 'ArrowUp':
        e.preventDefault()
        list[(idx - 1 + list.length) % list.length]?.focus()
        return
      case 'Home':
        e.preventDefault()
        list[0]?.focus()
        return
      case 'End':
        e.preventDefault()
        list[list.length - 1]?.focus()
        return
    }
  }

  return (
    <span class="sh-conn-more-wrap" ref={wrap}>
      <button
        ref={trigger}
        type="button"
        class="sh-conn-more"
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        <ConnIcon name="more" />
      </button>
      {open && (
        <div class="sh-post-menu sh-conn-menu" role="menu" aria-label={label} onKeyDown={onMenuKey}>
          {actions.map((a) => (
            <button
              key={a.label}
              type="button"
              role="menuitem"
              class={a.danger ? 'sh-post-menu-danger' : undefined}
              onClick={() => { close(true); a.onSelect() }}
            >
              <ConnIcon name={a.icon} />
              {a.label}
            </button>
          ))}
        </div>
      )}
    </span>
  )
}
