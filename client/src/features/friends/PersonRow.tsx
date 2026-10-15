/**
 * PersonRow — one person on the Friends page.
 *
 * Connections-card anatomy without the card: a 40 px avatar (presence dot
 * for local *and* remote people; an emoji-only status rides on it as a
 * badge), the name, then muted lines — the status, or "Seen …" for a
 * local person who is away. The whole row is the button (no chevron); it
 * opens the shared person sheet. Our own row opens the status editor
 * instead, so its "Add a status" nudge is never a dead end.
 */
import { Avatar } from '@/components/Avatar'
import { t } from '@/i18n/i18n'
import { relativeDocsTime } from '@/utils/relativeTime'
import { ConnIcon } from '@/features/connections/ConnIcon'
import { emojiOnly, statusLine, type Person } from './friendsModel'

interface Props {
  person: Person
  /** People view: the household goes on its own line under the status. */
  showHousehold?: boolean
  onOpen: (p: Person) => void
}

/** The muted line under the name: status text, else "Seen …" for a local
 *  person who is offline. Remote "last seen" is a best-effort cache, so a
 *  remote person without a status gets no second line at all. */
export function metaLine(p: Person): string | null {
  const status = statusLine(p.member.status)
  if (status) return status
  if (p.isSelf) return emojiOnly(p.member.status) ? null : t('friends.self_add_status')
  const m = p.member
  if (p.isLocal && !m.is_online && m.last_seen_at) {
    return t('friends.seen', { when: relativeDocsTime(m.last_seen_at) })
  }
  return null
}

export function presenceOf(p: Person): 'online' | 'idle' | null {
  if (!p.member.is_online) return null
  return p.member.is_idle ? 'idle' : 'online'
}

/** "Lina, away, 📚 Homework, then football, Okafor family" — every fact
 *  the row shows, in reading order. */
export function rowLabel(p: Person, showHousehold: boolean): string {
  const presence = presenceOf(p)
  const badge = emojiOnly(p.member.status)
  const parts = [
    p.isSelf ? t('friends.self_name', { name: p.shown }) : p.shown,
    presence ? t(presence === 'idle' ? 'friends.presence_idle' : 'friends.presence_online') : null,
    p.aliased ? t('friends.nickname_for', { name: p.real }) : null,
    badge,
    metaLine(p),
    showHousehold ? p.home.display_name : null,
    p.isSelf ? t('friends.self_edit_status') : null,
  ]
  return parts.filter(Boolean).join(', ')
}

export function PersonRow({ person: p, showHousehold = false, onOpen }: Props) {
  const badge = emojiOnly(p.member.status)
  const meta = metaLine(p)
  return (
    <button
      type="button"
      class={`sh-friends-row${p.isSelf ? ' sh-friends-row--self' : ''}`}
      aria-label={rowLabel(p, showHousehold)}
      data-user-id={p.user_id}
      onClick={() => onOpen(p)}
    >
      <span class="sh-friends-row__av" aria-hidden="true">
        <Avatar name={p.shown} src={p.member.picture_url} size={40} online={presenceOf(p)} />
        {badge && <span class="sh-friends-row__badge">{badge}</span>}
      </span>
      <span class="sh-friends-row__body" aria-hidden="true">
        <span class="sh-friends-row__name">
          <span class="sh-friends-row__text">{p.shown}</span>
          {p.aliased && (
            <span class="sh-friends-row__nick" title={t('friends.nickname_for', { name: p.real })}>
              <ConnIcon name="tag" />
            </span>
          )}
          {p.isSelf && <span class="sh-friends-row__you">{t('friends.you')}</span>}
        </span>
        {meta && <span class="sh-friends-row__meta">{meta}</span>}
        {showHousehold && <span class="sh-friends-row__meta">{p.home.display_name}</span>}
      </span>
    </button>
  )
}
