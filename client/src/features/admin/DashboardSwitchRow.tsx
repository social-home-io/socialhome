/**
 * A labelled on/off switch row for the Admin › Dashboard settings
 * (``DashboardTab.tsx``, ``DashboardPanelsCard.tsx``). The hint is wired
 * as the switch's description, not part of its name.
 */
import { Switch } from '@/components/Switch'

export function SwitchRow({ id, label, hint, checked, disabled, onChange }: {
  id: string, label: string, hint?: string, checked: boolean, disabled?: boolean,
  onChange: (next: boolean) => void
}) {
  // The hint is a description, not part of the switch's name.
  const hintId = hint ? `${id}-hint` : undefined
  return (
    <div class={`sh-dashboard-row${disabled ? ' sh-dashboard-row--disabled' : ''}`}>
      <div class="sh-dashboard-row__label">
        <label for={id}>{label}</label>
        {hint && <span id={hintId} class="sh-form-hint">{hint}</span>}
      </div>
      <Switch id={id} checked={checked} disabled={disabled} aria-describedby={hintId}
        onChange={onChange} />
    </div>
  )
}
