/**
 * AdvancedRow — one disclosure in the Advanced card: icon, label + one
 * line of description, an optional status chip, and a 44 px caret.
 *
 * A native ``<details>``; open state lives in :data:`advancedOpen` so the
 * hero's SetupWarning can open + focus a row (:func:`revealAdvancedRow`).
 * The summary carries an explicit ``aria-expanded`` mirroring the state.
 */
import type { ComponentChildren } from 'preact'
import { signal } from '@preact/signals'
import { ConnIcon, type ConnIconName } from './ConnIcon'

export type AdvancedRowId = 'ext' | 'relay' | 'diag'

export const advancedOpen = signal<Partial<Record<AdvancedRowId, boolean>>>({})

export function setAdvancedOpen(id: AdvancedRowId, open: boolean): void {
  if (!!advancedOpen.value[id] === open) return
  advancedOpen.value = { ...advancedOpen.value, [id]: open }
}

export function advancedRowDomId(id: AdvancedRowId): string {
  return `sh-conn-adv-${id}`
}

/** Open a row, scroll it into view and move focus to its summary. */
export function revealAdvancedRow(id: AdvancedRowId): void {
  setAdvancedOpen(id, true)
  requestAnimationFrame(() => {
    const summary = document.querySelector<HTMLElement>(`#${advancedRowDomId(id)} > summary`)
    summary?.scrollIntoView?.({ block: 'center' })
    summary?.focus()
  })
}

interface Props {
  id: AdvancedRowId
  icon: ConnIconName
  label: string
  desc: string
  chip?: ComponentChildren
  children: ComponentChildren
}

export function AdvancedRow({ id, icon, label, desc, chip, children }: Props) {
  const open = !!advancedOpen.value[id]
  return (
    <details
      id={advancedRowDomId(id)}
      class="sh-conn-adv__row"
      open={open}
      onToggle={(e) => setAdvancedOpen(id, (e.currentTarget as HTMLDetailsElement).open)}
    >
      <summary aria-expanded={open}>
        <span class="sh-conn-adv__ico" aria-hidden="true"><ConnIcon name={icon} /></span>
        <span class="sh-conn-adv__label">
          <b>{label}</b>
          <span>{desc}</span>
        </span>
        {chip}
        <span class="sh-conn-caret" aria-hidden="true"><ConnIcon name="chev" /></span>
      </summary>
      <div class="sh-conn-adv__panel">{children}</div>
    </details>
  )
}
