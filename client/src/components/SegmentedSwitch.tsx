/**
 * SegmentedSwitch — a small pill-shaped "A | B" switch between views of
 * one surface (a space's Events | Timetable, Feed | Chat).
 *
 * A ``role="group"`` of toggle buttons (``aria-pressed``), not a tab
 * strip: it sits inside a tab and swaps what that tab shows. Purely
 * presentational — the host owns the choice. An optional unread count
 * per option renders the same pill as ``TabHeader``'s ``badges``.
 */
import { t } from '@/i18n/i18n'

interface SegmentedSwitchProps<T extends string> {
  /** Options in display order. */
  options: readonly T[]
  /** The option shown now. */
  value: T
  /** Localised label per option. */
  labels: Readonly<Record<T, string>>
  onChange: (next: T) => void
  /** Accessible name of the group. */
  ariaLabel: string
  /** Extra class on the group (placement / sizing). */
  class?: string
  /** Unread count per option — a pill after the label (hidden at zero),
   *  announced as "N unread". */
  badges?: Partial<Readonly<Record<T, number>>>
}

export function SegmentedSwitch<T extends string>({
  options, value, labels, onChange, ariaLabel, class: extra, badges,
}: SegmentedSwitchProps<T>) {
  return (
    <div class={extra ? `sh-timetable-seg ${extra}` : 'sh-timetable-seg'}
         role="group" aria-label={ariaLabel}>
      {options.map((o) => {
        const count = badges?.[o] ?? 0
        return (
          <button key={o} type="button"
                  class={`sh-timetable-seg__btn${value === o ? ' is-on' : ''}`}
                  aria-pressed={value === o}
                  onClick={() => { if (o !== value) onChange(o) }}>
            {labels[o]}
            {count > 0 && (
              <>
                <span class="sh-tab-unread" aria-hidden="true">
                  {count > 99 ? '99+' : count}
                </span>
                <span class="sr-only">
                  {` (${t('nav.unread', { count: String(count) })})`}
                </span>
              </>
            )}
          </button>
        )
      })}
    </div>
  )
}
