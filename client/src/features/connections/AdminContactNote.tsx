/**
 * AdminContactNote — the member's footnote in the hero: only admins can
 * pair, and who to ask. 1 admin → "Ask Dave", 2 → "Ask Dave or Alice",
 * 3+ or unknown → "Ask an admin".
 */
import type { ComponentChildren } from 'preact'
import { t } from '@/i18n/i18n'
import { ConnIcon } from './ConnIcon'
import { fillNodes } from './richText'

export function askAdmins(admins: string[] | null): ComponentChildren {
  if (admins && admins.length === 1) {
    return fillNodes(t('connections.hero.ask_one'), { name: <strong>{admins[0]}</strong> })
  }
  if (admins && admins.length === 2) {
    return fillNodes(t('connections.hero.ask_two'), {
      a: <strong>{admins[0]}</strong>,
      b: <strong>{admins[1]}</strong>,
    })
  }
  return t('connections.hero.ask_any')
}

export function AdminContactNote({ admins }: { admins: string[] | null }) {
  return (
    <p class="sh-conn-hero__note">
      <ConnIcon name="info" />
      <span>{fillNodes(t('connections.hero.member_note'), { ask: askAdmins(admins) })}</span>
    </p>
  )
}
