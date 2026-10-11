/**
 * GfsChecklist — pick the GFSs a global space is listed on.
 *
 * A global space is listed only on the GFSs its owner chooses (never on
 * every connected one), so creating a global space, or proposing the move
 * into the global tier from the owner household, asks which. Each row is a
 * real ``<input type="checkbox">`` inside its ``<label>`` so keyboard and
 * screen-reader use come for free. Controlled: ``selected`` + ``onChange``.
 */
import type { JSX } from 'preact'
import { t } from '@/i18n/i18n'

/** One active GFS connection the owner can list the space on. */
export interface GfsChoice {
  id: string
  display_name: string
  inbox_url: string
}

/** The host part of a GFS URL — enough to tell two servers with the same
 *  name apart, short enough for a phone row. Falls back to the raw text. */
export function gfsHost(inboxUrl: string): string {
  try {
    return new URL(inboxUrl).host || inboxUrl
  } catch {
    return inboxUrl
  }
}

/** The choice a checklist starts with: the only server when there is
 *  exactly one (the 90% case — nothing to decide), else none. */
export function defaultGfsSelection(choices: GfsChoice[]): string[] {
  return choices.length === 1 ? [choices[0].id] : []
}

/** Bring the checklist named *name* into view and focus its first box —
 *  its error may sit below the fold (a long dialog on a phone). Runs after
 *  the error has rendered. */
export function revealGfsChecklist(name: string): void {
  setTimeout(() => {
    const fs = document.querySelector<HTMLElement>(`[data-testid="${name}-checklist"]`)
    if (!fs) return
    fs.scrollIntoView?.({ block: 'center', behavior: 'smooth' })
    fs.querySelector<HTMLInputElement>('input:not(:disabled)')?.focus({ preventScroll: true })
  }, 0)
}

export function GfsChecklist({
  name,
  choices,
  selected,
  onChange,
  max = 8,
  error = null,
  intro,
  legend,
  icon = '🌐',
}: {
  /** Prefix for element ids (two checklists may share a page). */
  name: string
  choices: GfsChoice[]
  selected: string[]
  onChange: (ids: string[]) => void
  /** The server's cap; further boxes disable once it is reached. */
  max?: number
  /** Shown under the list (e.g. "choose at least one"). */
  error?: string | null
  intro?: string
  /** Replaces the default "List it on" legend (a private channel). */
  legend?: string
  /** The legend's icon (a private channel isn't the public globe). */
  icon?: string
}): JSX.Element {
  const errorId = `${name}-error`
  const atMax = selected.length >= max
  const toggle = (id: string, on: boolean) => {
    const next = on
      ? [...selected.filter(s => s !== id), id]
      : selected.filter(s => s !== id)
    onChange(next)
  }
  return (
    <fieldset
      class="sh-form-fieldset sh-gfs-checklist"
      data-testid={`${name}-checklist`}
      aria-describedby={error ? errorId : undefined}
    >
      <legend><span aria-hidden="true">{icon}</span> {legend ?? t('space.gfs_choice.legend')}</legend>
      <p class="sh-muted sh-gfs-checklist__intro">
        {intro ?? t('space.gfs_choice.intro')}
      </p>
      {choices.map((c) => {
        const checked = selected.includes(c.id)
        return (
          <label key={c.id} class="sh-toggle-row sh-gfs-checklist__item">
            <input
              type="checkbox"
              name={`${name}-gfs`}
              value={c.id}
              checked={checked}
              disabled={!checked && atMax}
              aria-invalid={error ? true : undefined}
              onChange={(e) => toggle(c.id, (e.target as HTMLInputElement).checked)}
            />
            <span class="sh-toggle-row__text">
              <span>{c.display_name}</span>
              <span class="sh-toggle-row__sub">{gfsHost(c.inbox_url)}</span>
            </span>
          </label>
        )
      })}
      {atMax && choices.length > selected.length && (
        // Why the remaining boxes are greyed out.
        <p class="sh-form-hint" data-testid={`${name}-at-max`}>
          {t('error.gfs_selection.too_many', { max: String(max) })}
        </p>
      )}
      {error && (
        <p id={errorId} class="sh-form-error" role="alert">{error}</p>
      )}
    </fieldset>
  )
}
