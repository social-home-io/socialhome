/**
 * ConnCard — the shared row shell for households, pending pairings,
 * space-only peers and GFS connections.
 *
 * A row that opens something (Manage, admin only) is a ``<button>`` with
 * a chevron, a full-status ``aria-label`` and a hover lift; every other
 * row is a static ``<div>`` with no chevron and no lift. Static rows that
 * a hero chip can jump to get ``tabIndex=-1`` so focus can land on them.
 */
import type { ComponentChildren } from 'preact'
import { Fragment } from 'preact'
import { ConnIcon } from './ConnIcon'

/** DOM id of a household's card — the hero's problem chips focus it. */
export function cardDomId(instanceId: string): string {
  return `sh-conn-card-${instanceId}`
}

interface Props {
  avatar: ComponentChildren
  name: string
  /** Chips after the name (e.g. "2 updates behind", "Space only"). */
  chips?: ComponentChildren
  /** ConnMeta items, separated by middots on wide screens. */
  meta: ComponentChildren[]
  /** Right-hand slot for static rows (actions, overflow menu). */
  trailing?: ComponentChildren
  /** Opens Manage; makes the row a button with a chevron. */
  onOpen?: () => void
  ariaLabel?: string
  id?: string
  /** Extra modifiers: ``down`` / ``warn`` edge, ``dashed``, ``action``. */
  mods?: string[]
}

export function ConnCard({ avatar, name, chips, meta, trailing, onOpen, ariaLabel, id, mods = [] }: Props) {
  const body = (
    <>
      {avatar}
      <span class="sh-conn-card__body">
        <span class="sh-conn-card__line1">
          <span class="sh-conn-card__name" title={name}>{name}</span>
          {chips}
        </span>
        <span class="sh-conn-card__line2">
          {meta.filter(Boolean).map((m, i) => (
            <Fragment key={i}>
              {i > 0 && <span class="sh-conn-sep" aria-hidden="true">·</span>}
              {m}
            </Fragment>
          ))}
        </span>
      </span>
    </>
  )
  const modCls = mods.map((m) => ` sh-conn-card--${m}`).join('')
  if (onOpen) {
    return (
      <button
        type="button"
        id={id}
        class={`sh-conn-card${modCls}`}
        aria-label={ariaLabel ?? name}
        onClick={onOpen}
      >
        {body}
        <span class="sh-conn-chev" aria-hidden="true"><ConnIcon name="chev" /></span>
      </button>
    )
  }
  return (
    <div id={id} class={`sh-conn-card sh-conn-card--static${modCls}`} tabIndex={id ? -1 : undefined}>
      {body}
      {trailing}
    </div>
  )
}
