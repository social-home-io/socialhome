/**
 * TimetableHeader — the selected timetable's name (click to rename
 * inline: Enter or leaving the field saves, Esc cancels, an empty name
 * is refused) and one compact row of view controls: Picture view, List
 * view, and an overflow menu (Settings, Duplicate, New timetable,
 * Delete). Below 400 px the two toggles switch to short labels
 * ("Pictures" / "List") without their emoji; the accessible name stays
 * the full label.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { t } from '@/i18n/i18n'
import { patchHeader } from '@/store/timetables'
import type { Timetable } from '@/types'
import { colorClass } from './colors'

interface Props {
  tt: Timetable
  picture: boolean
  onPicture: (on: boolean) => void
  list: boolean
  onList: (on: boolean) => void
  onSettings: () => void
  onDuplicate: () => void
  onNew: () => void
  onDelete: () => void
}

export function TimetableHeader({
  tt, picture, onPicture, list, onList, onSettings, onDuplicate, onNew, onDelete,
}: Props) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(tt.name)
  const [error, setError] = useState<string | null>(null)
  const [menuOpen, setMenuOpen] = useState(false)
  const inputRef = useRef<HTMLInputElement | null>(null)
  const nameBtnRef = useRef<HTMLButtonElement | null>(null)
  const moreRef = useRef<HTMLButtonElement | null>(null)
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const menuRef = useRef<HTMLDivElement | null>(null)
  // Esc and a finished save unmount the input — its blur must not
  // commit (again).
  const closing = useRef(false)
  const saving = useRef(false)

  useEffect(() => {
    if (editing) {
      inputRef.current?.focus()
      inputRef.current?.select()
    }
  }, [editing])

  const start = () => {
    setDraft(tt.name)
    setError(null)
    closing.current = false
    setEditing(true)
  }
  const stop = () => {
    closing.current = true
    setEditing(false)
    setError(null)
    queueMicrotask(() => nameBtnRef.current?.focus())
  }
  const commit = async (fromBlur = false) => {
    if (closing.current || saving.current) return
    const name = draft.trim()
    if (!name) {
      if (fromBlur) stop() // leaving an empty field just reverts
      else setError(t('timetable.header.name_empty'))
      return
    }
    if (name === tt.name) { stop(); return }
    saving.current = true
    try {
      if (await patchHeader(tt.id, { name })) stop()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      saving.current = false
    }
  }

  // Menu: first item focused on open, ArrowUp / ArrowDown move (wrapping).
  const items = () => Array.from(menuRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? [])
  useEffect(() => {
    if (menuOpen) items()[0]?.focus()
  }, [menuOpen])
  // Outside press closes (Safari never focuses a clicked button, so
  // focusout alone can't be relied on).
  useEffect(() => {
    if (!menuOpen) return
    const onDown = (e: PointerEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setMenuOpen(false)
    }
    document.addEventListener('pointerdown', onDown)
    return () => document.removeEventListener('pointerdown', onDown)
  }, [menuOpen])
  const onMenuKey = (e: KeyboardEvent) => {
    if (e.key === 'Escape' && menuOpen) {
      e.stopPropagation()
      setMenuOpen(false)
      moreRef.current?.focus()
      return
    }
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return
    const list = items()
    if (list.length === 0) return
    e.preventDefault()
    const i = list.indexOf(document.activeElement as HTMLElement)
    const next = e.key === 'ArrowDown' ? (i + 1) % list.length : (i - 1 + list.length) % list.length
    list[next].focus()
  }
  // Pick an item: park focus on the trigger first, so a dialog the
  // action opens hands focus back to it when it closes.
  const pick = (action: () => void) => () => {
    moreRef.current?.focus()
    setMenuOpen(false)
    action()
  }
  // Pressing an item must not blur the trigger / item first (focusout
  // would close the menu before the click lands).
  const keepFocus = (e: PointerEvent) => e.preventDefault()

  return (
    <div class={`sh-timetable-head ${colorClass(tt.color)}`}>
      <span class="sh-timetable-head__stripe" aria-hidden="true" />
      <div class="sh-timetable-head__title">
        {editing ? (
          <form class="sh-timetable-head__rename"
                onSubmit={(e) => { e.preventDefault(); void commit() }}>
            <input
              ref={inputRef}
              value={draft}
              maxLength={60}
              aria-label={t('timetable.header.name_label')}
              aria-invalid={error ? 'true' : undefined}
              aria-describedby={error ? `sh-tt-${tt.id}-rename-err` : undefined}
              onInput={(e) => { setDraft((e.target as HTMLInputElement).value); setError(null) }}
              onBlur={() => void commit(true)}
              onKeyDown={(e) => {
                if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); stop() }
              }}
            />
            {error && (
              <p id={`sh-tt-${tt.id}-rename-err`} class="sh-form-error" role="alert">{error}</p>
            )}
          </form>
        ) : (
          <h2 class="sh-timetable-head__h">
            <button ref={nameBtnRef} type="button" class="sh-timetable-head__name"
                    title={t('timetable.header.rename')} onClick={start}>
              {tt.name}
              <span class="sh-timetable-head__pen" aria-hidden="true">✎</span>
              <span class="sr-only">{`, ${t('timetable.header.rename')}`}</span>
            </button>
          </h2>
        )}
      </div>
      <div class="sh-timetable-head__actions">
        <button
          type="button"
          class={`sh-timetable-toggle sh-timetable-toggle--picture${picture ? ' is-on' : ''}`}
          aria-pressed={picture}
          aria-label={t('timetable.view.picture')}
          title={t('timetable.view.picture')}
          onClick={() => onPicture(!picture)}
        >
          <span class="sh-timetable-toggle__icon" aria-hidden="true">🖼️</span>
          <span class="sh-timetable-toggle__text" aria-hidden="true">{t('timetable.view.picture')}</span>
          <span class="sh-timetable-toggle__short" aria-hidden="true">{t('timetable.view.picture_short')}</span>
        </button>
        <button
          type="button"
          class={`sh-timetable-toggle sh-timetable-toggle--list${list ? ' is-on' : ''}`}
          aria-pressed={list}
          aria-label={t('timetable.view.list')}
          title={t('timetable.view.list')}
          onClick={() => onList(!list)}
        >
          <span class="sh-timetable-toggle__icon" aria-hidden="true">☰</span>
          <span class="sh-timetable-toggle__text" aria-hidden="true">{t('timetable.view.list')}</span>
          <span class="sh-timetable-toggle__short" aria-hidden="true">{t('timetable.view.list_short')}</span>
        </button>
        <div
          ref={wrapRef}
          class="sh-post-overflow-wrap"
          onFocusOut={(e) => {
            const next = e.relatedTarget as Node | null
            if (next && !(e.currentTarget as HTMLElement).contains(next)) setMenuOpen(false)
          }}
          onKeyDown={onMenuKey}
        >
          <button
            ref={moreRef}
            type="button"
            class="sh-post-overflow sh-timetable-head__more"
            aria-label={t('timetable.header.actions')}
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen(v => !v)}
          >
            ···
          </button>
          {menuOpen && (
            <div ref={menuRef} class="sh-post-menu" role="menu">
              <button type="button" role="menuitem" tabIndex={-1} onPointerDown={keepFocus}
                      onClick={pick(onSettings)}>
                {t('timetable.header.settings')}
              </button>
              <button type="button" role="menuitem" tabIndex={-1} onPointerDown={keepFocus}
                      onClick={pick(onDuplicate)}>
                {t('timetable.header.duplicate')}
              </button>
              {/* Also here so creating one stays discoverable when the
               *  "+ New" card is scrolled off-screen on a phone. */}
              <button type="button" role="menuitem" tabIndex={-1} onPointerDown={keepFocus}
                      onClick={pick(onNew)}>
                {t('timetable.new')}
              </button>
              <button type="button" role="menuitem" tabIndex={-1} class="sh-post-menu-danger"
                      onPointerDown={keepFocus} onClick={pick(onDelete)}>
                {t('timetable.header.delete')}
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
