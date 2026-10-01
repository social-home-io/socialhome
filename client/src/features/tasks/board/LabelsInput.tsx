/**
 * LabelsInput — a task's labels as removable chips plus a field: Enter
 * or a comma adds what was typed (so does leaving the field), Backspace
 * in the empty field removes the last chip. Suggestions are the list's
 * other labels matching what's typed, one tap to add. At most
 * ``MAX_LABELS`` labels of at most ``MAX_LABEL_LEN`` characters, unique
 * whatever the case; a refused label says why.
 */
import { useState } from 'preact/hooks'
import { t } from '@/i18n/i18n'
import { addLabel, labelColorClass, labelKey, MAX_LABEL_LEN, MAX_LABELS, type AddLabelError } from './labels'

const MAX_SUGGESTIONS = 8

interface Props {
  id: string
  value: readonly string[]
  onChange: (labels: string[]) => void
  suggestions: readonly string[]
}

/** For ``i18n:check``: t('tasks.labels.error.too_long')
 *  t('tasks.labels.error.too_many') t('tasks.labels.error.duplicate') */
function errorText(e: AddLabelError): string {
  return t(`tasks.labels.error.${e}`, { max: String(MAX_LABELS), len: String(MAX_LABEL_LEN) })
}

export function LabelsInput({ id, value, onChange, suggestions }: Props) {
  const [draft, setDraft] = useState('')
  const [error, setError] = useState<AddLabelError | null>(null)
  const full = value.length >= MAX_LABELS
  const chosen = new Set(value.map(labelKey))
  const q = labelKey(draft)
  const offered = full ? [] : suggestions
    .filter(s => !chosen.has(labelKey(s)) && (!q || labelKey(s).includes(q)))
    .slice(0, MAX_SUGGESTIONS)

  const add = (raw: string): boolean => {
    const res = addLabel(value, raw)
    if (res.error === 'empty') return false
    if (res.error) {
      setError(res.error)
      return false
    }
    setError(null)
    setDraft('')
    onChange(res.labels)
    return true
  }
  const remove = (label: string) => {
    setError(null)
    onChange(value.filter(l => l !== label))
  }

  const hintId = `${id}-hint`
  const errId = `${id}-err`
  return (
    <div class="sh-labels-input">
      <label class="sh-form-label" for={id}>{t('tasks.labels.title')}</label>
      {value.length > 0 && (
        <ul class="sh-labels-input__chips" aria-label={t('tasks.labels.chosen')}>
          {value.map(l => (
            <li key={l} class={`sh-task-label sh-task-label--removable ${labelColorClass(l)}`}>
              <span>{l}</span>
              <button type="button" class="sh-task-label__remove"
                      aria-label={t('tasks.labels.remove', { name: l })}
                      onClick={() => remove(l)}>
                <span aria-hidden="true">✕</span>
              </button>
            </li>
          ))}
        </ul>
      )}
      <input
        id={id}
        type="text"
        value={draft}
        maxLength={MAX_LABEL_LEN}
        disabled={full}
        autoComplete="off"
        placeholder={full ? t('tasks.labels.full', { max: String(MAX_LABELS) }) : t('tasks.labels.placeholder')}
        aria-describedby={error ? `${hintId} ${errId}` : hintId}
        aria-invalid={error ? 'true' : undefined}
        onInput={(e) => { setDraft((e.target as HTMLInputElement).value); setError(null) }}
        onKeyDown={(e) => {
          if ((e.key === 'Enter' || e.key === ',') && draft.trim()) {
            e.preventDefault()
            add(draft)
          } else if (e.key === ',') {
            e.preventDefault()
          } else if (e.key === 'Backspace' && !draft && value.length > 0) {
            e.preventDefault()
            remove(value[value.length - 1])
          }
        }}
        onBlur={() => { if (draft.trim()) add(draft) }}
      />
      <p class="sh-form-hint" id={hintId}>
        {t('tasks.labels.hint', { n: String(value.length), max: String(MAX_LABELS), len: String(MAX_LABEL_LEN) })}
      </p>
      {error && <p class="sh-form-error" id={errId} role="alert">{errorText(error)}</p>}
      {offered.length > 0 && (
        <div class="sh-labels-input__suggest" role="group" aria-label={t('tasks.labels.suggestions')}>
          {offered.map(s => (
            <button key={s} type="button" class={`sh-task-label sh-task-label--suggest ${labelColorClass(s)}`}
                    aria-label={t('tasks.labels.add', { name: s })}
                    // Keep focus in the field: a tap mustn't blur-add the draft first.
                    onPointerDown={e => e.preventDefault()}
                    onClick={() => add(s)}>
              <span aria-hidden="true">+ </span>{s}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
