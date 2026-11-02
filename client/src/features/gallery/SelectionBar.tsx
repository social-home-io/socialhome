/**
 * SelectionBar — what to do with the photos picked in a grid.
 *
 * Desktop: one sticky bar on top of the grid (× · "12 selected" · the
 * actions). Phone: the "× 12 selected" header stays on top and the
 * actions sit in a bar at the bottom, in thumb reach, every button at
 * least 44 px tall. The count is a polite live region, so a screen
 * reader hears "12 selected" as tiles are picked. Escape (handled by
 * ``useSelection``) or × leaves selection.
 */
import type { ComponentChildren } from 'preact'
import { t } from '@/i18n/i18n'
import { HEART_PATH } from './HeartIcon'

export interface SelectionAction {
  key: string
  label: string
  icon: string
  onClick: () => void
  danger?: boolean
  disabled?: boolean
  busy?: boolean
}

const ICONS: Record<string, string> = {
  share: 'M12 15V4m0 0L8 8m4-4 4 4M6 11.5H5v8.5h14v-8.5h-1',
  move: 'M3.5 7.5h6l2 2h9v9h-17zM12 13h5m-2-2 2 2-2 2',
  download: 'M12 4v11m0 0-4.5-4.5M12 15l4.5-4.5M5 19.5h14',
  delete: 'M4.5 6.5h15M9.5 6.5V4.5h5v2m-8 0 1 13h9l1-13M10 10v6m4-6v6',
  restore: 'M5 12a7 7 0 1 0 2.05-4.95M5 4.5v3.5h3.5',
  purge: 'M6 6l12 12M18 6 6 18',
  favourite: HEART_PATH,
}

function Icon({ name }: { name: string }) {
  return (
    <svg class="sh-selbar-icon" viewBox="0 0 24 24" width="22" height="22" aria-hidden="true"
         fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
      <path d={ICONS[name] ?? ''} />
    </svg>
  )
}

export function SelectionBar({ count, onClose, actions, note }: {
  count: number
  onClose: () => void
  actions: SelectionAction[]
  /** A short line under the count (e.g. "3 added by Ben stay"). */
  note?: ComponentChildren
}) {
  return (
    <div class="sh-selbar" role="region" aria-label={t('gallery.manage.selection_region')}>
      <div class="sh-selbar-head">
        <button
          type="button"
          class="sh-selbar-close"
          onClick={onClose}
          aria-label={t('gallery.manage.select_done')}
          title={t('gallery.manage.select_done')}
        ><span aria-hidden="true">✕</span></button>
        <p class="sh-selbar-count" aria-live="polite" aria-atomic="true">
          {count === 0
            ? t('gallery.manage.select_hint')
            : t('gallery.manage.selected', { n: String(count) })}
        </p>
        {note && <p class="sh-selbar-note">{note}</p>}
      </div>
      <div class="sh-selbar-actions" role="toolbar" aria-label={t('gallery.manage.selection_actions')}>
        {actions.map(a => (
          <button
            key={a.key}
            type="button"
            class={'sh-selbar-action' + (a.danger ? ' sh-selbar-action--danger' : '')}
            onClick={a.onClick}
            disabled={a.disabled || a.busy || count === 0}
            aria-busy={a.busy || undefined}
          >
            {a.busy ? <span class="sh-spinner-sm" aria-hidden="true" /> : <Icon name={a.icon} />}
            <span>{a.label}</span>
          </button>
        ))}
      </div>
    </div>
  )
}
