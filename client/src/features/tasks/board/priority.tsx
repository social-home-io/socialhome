/**
 * Task priority: the four levels, their names and the small icon a
 * card / row shows (none when the task has no priority). The icon is
 * decorative; the level is spoken through sr-only text.
 *
 * For ``i18n:check``: t('tasks.priority.urgent') t('tasks.priority.high')
 * t('tasks.priority.medium') t('tasks.priority.low')
 */
import { t } from '@/i18n/i18n'
import type { TaskPriority } from '@/types'

/** Most to least pressing (filters, the dialog's picker). */
export const PRIORITIES: readonly TaskPriority[] = ['urgent', 'high', 'medium', 'low']

export function priorityLabel(p: TaskPriority): string {
  return t(`tasks.priority.${p}`)
}

/** Chevrons like an issue tracker: ⇈ urgent, ↑ high, = medium, ↓ low. */
const PATHS: Record<TaskPriority, string> = {
  urgent: 'M3 9.5 8 5l5 4.5M3 13.5 8 9l5 4.5',
  high: 'M3 11 8 6l5 5',
  medium: 'M3 6.5h10M3 10.5h10',
  low: 'M3 6l5 5 5-5',
}

export function PriorityIcon({ priority, withText = true }: {
  priority: TaskPriority | null | undefined
  /** Speak "Priority: High" (off when a visible label says it). */
  withText?: boolean
}) {
  if (!priority) return null
  const name = priorityLabel(priority)
  return (
    <span class={`sh-task-priority sh-task-priority--${priority}`} title={t('tasks.priority.label', { name })}>
      <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" focusable="false">
        <path d={PATHS[priority]} fill="none" stroke="currentColor" stroke-width="2"
              stroke-linecap="round" stroke-linejoin="round" />
      </svg>
      {withText && <span class="sr-only">{t('tasks.priority.label', { name })}</span>}
    </span>
  )
}
