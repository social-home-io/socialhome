/**
 * ComposerAttachMenu — the DM composer's paperclip, opening a small
 * menu of things to attach: a photo / video / file (the hidden file
 * input) or a location (the LocationPicker).
 *
 * One round button keeps the composer row to paperclip · input · send
 * on a phone; the menu opens upward because the composer sits at the
 * bottom of the thread. Keyboard: Enter/Space opens and focuses the
 * first item, ↑/↓ move, Escape closes and returns focus to the
 * paperclip; a tap outside closes it.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { t } from '@/i18n/i18n'

interface ComposerAttachMenuProps {
  /** A file is staged or uploading — only one at a time, so the file
   *  item is disabled (a location can still be shared). */
  fileDisabled: boolean
  onPickFile: () => void
  onPickLocation: () => void
}

export function ComposerAttachMenu({
  fileDisabled, onPickFile, onPickLocation,
}: ComposerAttachMenuProps) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement | null>(null)
  const triggerRef = useRef<HTMLButtonElement | null>(null)
  const menuRef = useRef<HTMLDivElement | null>(null)

  const items = (): HTMLButtonElement[] =>
    Array.from(
      menuRef.current?.querySelectorAll<HTMLButtonElement>(
        '[role="menuitem"]:not([disabled])',
      ) ?? [],
    )

  useEffect(() => {
    if (!open) return
    items()[0]?.focus()
    const onDown = (e: PointerEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('pointerdown', onDown)
    return () => document.removeEventListener('pointerdown', onDown)
  }, [open])

  const close = (refocus: boolean) => {
    setOpen(false)
    if (refocus) triggerRef.current?.focus()
  }

  const onMenuKey = (e: KeyboardEvent) => {
    if (e.key === 'Escape') {
      e.preventDefault()
      e.stopPropagation()
      close(true)
      return
    }
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return
    e.preventDefault()
    const list = items()
    if (list.length === 0) return
    const at = list.indexOf(document.activeElement as HTMLButtonElement)
    const step = e.key === 'ArrowDown' ? 1 : -1
    list[(at + step + list.length) % list.length].focus()
  }

  const choose = (fn: () => void) => {
    close(false)
    fn()
  }

  return (
    <div class="sh-dm-attach" ref={rootRef}>
      <button
        ref={triggerRef}
        type="button"
        class="sh-dm-attach-btn"
        title={t('dms.attach.menu')}
        aria-label={t('dms.attach.menu')}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        <span aria-hidden="true">📎</span>
      </button>
      {open && (
        <div
          ref={menuRef}
          class="sh-dm-attach-menu"
          role="menu"
          aria-label={t('dms.attach.menu')}
          onKeyDown={onMenuKey}
        >
          <button
            type="button"
            role="menuitem"
            class="sh-dm-attach-menu__item"
            disabled={fileDisabled}
            onClick={() => choose(onPickFile)}
          >
            <span aria-hidden="true">🖼️</span>
            {t('dms.attach.file')}
          </button>
          <button
            type="button"
            role="menuitem"
            class="sh-dm-attach-menu__item"
            onClick={() => choose(onPickLocation)}
          >
            <span aria-hidden="true">📍</span>
            {t('dms.attach.location')}
          </button>
        </div>
      )}
    </div>
  )
}
