/**
 * SectionHeader — h2 (+ count pill) with an optional right-hand slot and
 * description; SubgroupDivider — the small-caps divider inside a list.
 */
import type { ComponentChildren } from 'preact'
import { t } from '@/i18n/i18n'

interface Props {
  id?: string
  title: string
  count?: number
  right?: ComponentChildren
  desc?: string
  /** Focus target after a row disappears (``tabIndex=-1``). */
  focusable?: boolean
}

export function SectionHeader({ id, title, count, right, desc, focusable }: Props) {
  return (
    <>
      <div class="sh-conn-sect-head">
        <div class="sh-conn-sect-head__title">
          <h2 id={id} tabIndex={focusable ? -1 : undefined}>{title}</h2>
          {count !== undefined && (
            <span class="sh-conn-count" aria-label={t('connections.count_label', { n: String(count) })}>
              {count}
            </span>
          )}
        </div>
        {right}
      </div>
      {desc && <p class="sh-conn-sect-desc">{desc}</p>}
    </>
  )
}

export function SubgroupDivider({ children }: { children: ComponentChildren }) {
  return <p class="sh-conn-subgroup">{children}</p>
}
