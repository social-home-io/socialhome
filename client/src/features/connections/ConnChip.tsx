/**
 * ConnChip — the page's one chip component. Neutral by default; ``ok`` /
 * ``warn`` / ``danger`` / ``muted`` variants; ``md`` size in the hero.
 *
 * With ``onClick`` it renders a button (a "jump to that card" chip) whose
 * hit area grows to 44 px through ``::after`` without growing the pill.
 */
import type { ComponentChildren } from 'preact'
import { ConnIcon, type ConnIconName } from './ConnIcon'

export type ChipTone = 'neutral' | 'ok' | 'warn' | 'danger' | 'muted'

interface Props {
  tone?: ChipTone
  size?: 'sm' | 'md'
  icon?: ConnIconName
  title?: string
  onClick?: () => void
  children: ComponentChildren
}

export function ConnChip({ tone = 'neutral', size = 'sm', icon, title, onClick, children }: Props) {
  const cls = 'sh-conn-chip'
    + (tone !== 'neutral' ? ` sh-conn-chip--${tone}` : '')
    + (size === 'md' ? ' sh-conn-chip--md' : '')
  if (onClick) {
    return (
      <button type="button" class={`${cls} sh-conn-chip--link`} title={title} onClick={onClick}>
        {icon && <ConnIcon name={icon} />}
        {children}
      </button>
    )
  }
  return (
    <span class={cls} title={title}>
      {icon && <ConnIcon name={icon} />}
      {children}
    </span>
  )
}
