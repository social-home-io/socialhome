/**
 * The rows a space-settings card is made of: a labelled switch that saves
 * on flip, and a link row (title + meta + chevron) that opens another
 * screen. One look for every section, so "saves now" and "opens
 * something" read differently at a glance.
 */
import type { ComponentChildren } from 'preact'
import { Switch } from '@/components/Switch'

export function SwitchRow({ id, label, hint, checked, disabled, onChange, sub = false }: {
  id: string
  label: ComponentChildren
  hint?: string
  checked: boolean
  disabled?: boolean
  onChange: (next: boolean) => void
  /** Indented under the row above (it only applies while that one is on). */
  sub?: boolean
}) {
  const hintId = hint ? `${id}-hint` : undefined
  return (
    <div class={'sh-ss-switch' + (sub ? ' sh-ss-switch--sub' : '')
      + (disabled ? ' sh-ss-switch--disabled' : '')}>
      <div class="sh-ss-switch__text">
        <label for={id} class="sh-ss-switch__label">{label}</label>
        {hint && <span id={hintId} class="sh-ss-switch__hint">{hint}</span>}
      </div>
      <Switch id={id} checked={checked} disabled={disabled}
              aria-describedby={hintId} onChange={onChange} />
    </div>
  )
}

export function LinkRow({ href, title, meta, onClick, testId }: {
  href?: string
  title: string
  meta?: string | null
  onClick?: () => void
  testId?: string
}) {
  const body = (
    <>
      <span class="sh-ss-link__text">
        <span class="sh-ss-link__title">{title}</span>
        {meta && <span class="sh-ss-link__meta">{meta}</span>}
      </span>
      <span class="sh-ss-link__chevron sh-flip-rtl" aria-hidden="true">›</span>
    </>
  )
  return href ? (
    <a class="sh-ss-link" href={href} data-testid={testId}>{body}</a>
  ) : (
    <button type="button" class="sh-ss-link" onClick={onClick} data-testid={testId}>
      {body}
    </button>
  )
}
