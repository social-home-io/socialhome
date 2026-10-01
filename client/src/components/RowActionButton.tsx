/**
 * RowActionButton — the small icon action at the end of a list row
 * (delete ✕, edit ✎, ⋯).
 *
 * Built on the shared ``.sh-icon-btn`` base. ``reveal`` hides it until
 * its row is hovered or holds focus — the row opts in by carrying the
 * ``ROW_REVEAL_HOST`` class (``sh-row-reveal-host``), at any nesting
 * depth. Only on devices that can hover: under ``(hover: none)`` it is
 * always visible, so touch users are never left with an invisible
 * control. 44 px under a coarse
 * pointer. ``danger`` tints the hover state for destructive actions.
 */
import type { ComponentChildren } from 'preact'

/** Put this class on the row that should reveal its ``reveal`` actions. */
export const ROW_REVEAL_HOST = 'sh-row-reveal-host'

interface RowActionButtonProps {
  label: string
  icon: ComponentChildren
  onClick: (e: MouseEvent) => void
  danger?: boolean
  /** Hide until the parent row is hovered / focused (hover devices). */
  reveal?: boolean
  disabled?: boolean
  class?: string
}

export function RowActionButton({
  label, icon, onClick, danger, reveal, disabled, class: extra,
}: RowActionButtonProps) {
  const cls = [
    'sh-icon-btn sh-row-action',
    danger ? 'sh-row-action--danger' : '',
    reveal ? 'sh-row-action--reveal' : '',
    extra ?? '',
  ].filter(Boolean).join(' ')
  return (
    <button
      type="button"
      class={cls}
      aria-label={label}
      title={label}
      disabled={disabled}
      onClick={onClick}
    >
      <span aria-hidden="true">{icon}</span>
    </button>
  )
}
