/**
 * ViewToggle — List / Map switch for the Households section. Two toggle
 * buttons (``aria-pressed``) in the shared ``.sh-segmented`` pill, each
 * 42 px tall inside a 2 px pad so the control is ≥ 44 px.
 */
import { t } from '@/i18n/i18n'
import { ConnIcon } from './ConnIcon'

export type ConnView = 'list' | 'map'

export function ViewToggle({ value, onChange }: { value: ConnView; onChange: (v: ConnView) => void }) {
  const opt = (v: ConnView, label: string) => (
    <button
      type="button"
      class={`sh-segmented__opt${value === v ? ' sh-segmented__opt--active' : ''}`}
      aria-pressed={value === v}
      onClick={() => onChange(v)}
    >
      <ConnIcon name={v} />
      {label}
    </button>
  )
  return (
    <div class="sh-segmented" role="group" aria-label={t('connections.view')}>
      {opt('list', t('connections.view_list'))}
      {opt('map', t('connections.view_map'))}
    </div>
  )
}
