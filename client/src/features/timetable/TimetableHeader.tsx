/**
 * TimetableHeader — the selected timetable's name (click to rename
 * inline: Enter or leaving the field saves, Esc cancels, an empty name
 * is refused), a chip with the school weeks it runs in (opens the
 * Weeks dialog), and one compact row of view controls: Fill (brush
 * mode), Picture view, List view, and an overflow menu (Settings,
 * School weeks & holidays, Print, Duplicate, New timetable, Delete).
 * Below 400 px the toggles switch to short labels ("Fill" / "Pictures"
 * / "List") without their emoji; the accessible name stays the full
 * label.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { t } from '@/i18n/i18n'
import { patchHeader } from '@/store/timetables'
import type { Timetable } from '@/types'
import { colorClass } from './colors'
import { validitySummary } from './dates'
import { OverflowMenu, type MenuItem } from './OverflowMenu'

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
  onWeeks: () => void
  onPrint: () => void
  /** Brush mode ("Fill"); the toggle is hidden when ``onBrush`` is
   *  absent (week mode edits one week, not the regular plan). */
  brush?: boolean
  onBrush?: (on: boolean) => void
}

export function TimetableHeader({
  tt, picture, onPicture, list, onList, onSettings, onDuplicate, onNew, onDelete,
  onWeeks, onPrint, brush = false, onBrush,
}: Props) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(tt.name)
  const [error, setError] = useState<string | null>(null)
  const inputRef = useRef<HTMLInputElement | null>(null)
  const nameBtnRef = useRef<HTMLButtonElement | null>(null)
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

  const menu: MenuItem[] = [
    { label: t('timetable.header.settings'), onSelect: onSettings },
    { label: t('timetable.header.weeks'), onSelect: onWeeks },
    { label: t('timetable.header.print'), onSelect: onPrint },
    { label: t('timetable.header.duplicate'), onSelect: onDuplicate },
    // Also here so creating one stays discoverable when the "+ New"
    // card is scrolled off-screen on a phone.
    { label: t('timetable.new'), onSelect: onNew },
    { label: t('timetable.header.delete'), onSelect: onDelete, danger: true },
  ]
  const summary = validitySummary(tt.validity)

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
        <button type="button" class="sh-timetable-head__weeks" onClick={onWeeks}
                aria-label={t('timetable.validity.chip_aria', { summary })}
                title={t('timetable.header.weeks')}>
          <span aria-hidden="true">🗓️</span>
          <span aria-hidden="true">{summary}</span>
        </button>
      </div>
      <div class="sh-timetable-head__actions">
        {onBrush && (
          <button
            type="button"
            class={`sh-timetable-toggle sh-timetable-toggle--brush${brush ? ' is-on' : ''}`}
            aria-pressed={brush}
            aria-label={t('timetable.brush.toggle')}
            title={t('timetable.brush.toggle_hint')}
            onClick={() => onBrush(!brush)}
          >
            <span class="sh-timetable-toggle__icon" aria-hidden="true">🖌️</span>
            <span class="sh-timetable-toggle__text" aria-hidden="true">{t('timetable.brush.toggle')}</span>
            <span class="sh-timetable-toggle__short" aria-hidden="true">{t('timetable.brush.toggle')}</span>
          </button>
        )}
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
        <OverflowMenu label={t('timetable.header.actions')} triggerClass="sh-timetable-head__more"
                      items={menu}>
          ···
        </OverflowMenu>
      </div>
    </div>
  )
}
