/**
 * ConnAvatar — a household's 44 px initial (or glyph) on its hash tint,
 * with an optional status ring (colour) + badge (shape), so status is
 * never conveyed by colour alone. Decorative: the card's text names the
 * household and its state.
 */
import { ConnIcon, type ConnIconName } from './ConnIcon'
import type { Tint } from './model'

export type AvatarStatus = 'ok' | 'warn' | 'down' | 'pending'

const BADGE: Partial<Record<AvatarStatus, ConnIconName>> = {
  warn: 'up',
  down: 'bang',
  pending: 'dots',
}

interface Props {
  /** A letter, or an icon for non-household rows (GFS cloud). */
  letter?: string
  icon?: ConnIconName
  tint: Tint | 'paper' | 'ghost'
  status?: AvatarStatus
}

export function ConnAvatar({ letter, icon, tint, status }: Props) {
  const badge = status ? BADGE[status] : undefined
  return (
    <span
      class={`sh-conn-av sh-conn-av--${tint}`}
      data-s={status}
      aria-hidden="true"
    >
      {icon ? <ConnIcon name={icon} /> : letter}
      {badge && (
        <span class={`sh-conn-badge sh-conn-badge--${status}`}>
          <ConnIcon name={badge} />
        </span>
      )}
    </span>
  )
}
