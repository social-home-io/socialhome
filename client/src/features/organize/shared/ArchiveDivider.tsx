/**
 * ArchiveDivider — the quiet "Already bought (3) · Clear all" line
 * between a list's open items and its finished pile. Shopping uses it
 * above the bought items; Tasks reuses it for the done archive.
 */
interface ArchiveDividerProps {
  label: string
  /** Optional trailing action, e.g. "Clear all". */
  actionLabel?: string
  /** Accessible name for the action when the visible label is terse. */
  actionAriaLabel?: string
  onAction?: () => void
  class?: string
}

export function ArchiveDivider({
  label, actionLabel, actionAriaLabel, onAction, class: extra,
}: ArchiveDividerProps) {
  return (
    <div class={extra ? `sh-archive-divider ${extra}` : 'sh-archive-divider'}>
      <span class="sh-archive-divider__label">{label}</span>
      {actionLabel && onAction && (
        <button
          type="button"
          class="sh-link sh-archive-divider__action"
          aria-label={actionAriaLabel}
          onClick={onAction}
        >
          {actionLabel}
        </button>
      )}
    </div>
  )
}
