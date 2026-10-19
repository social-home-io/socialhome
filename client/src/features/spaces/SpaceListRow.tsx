/**
 * SpaceListRow — one space on the Spaces page, in the Connections / Friends
 * card anatomy (flat — no tape, no rotation): the space's picture (its
 * uploaded icon, else its emoji, else its initial), the name clamped to
 * two lines (the distinguishing part of a space's name is often at the
 * end — "Schuljahr 2026/27"), one muted meta line, and an end slot.
 *
 * The row is a link when it opens something (``to``, an in-app path; the ingress prefix is added here); a Discover row's
 * Join / Ask / Follow button sits beside the link, never inside it.
 */
import type { ComponentChildren } from 'preact'
import { addBase } from '@/baseUrl'
import { isOne, t } from '@/i18n/i18n'
import { relativeDocsTime } from '@/utils/relativeTime'
import { spaceGlyph } from './spaceGlyph'

/** The space's picture: the uploaded icon, else emoji, else initial. */
export function SpaceIcon({ iconUrl, emoji, name }: {
  iconUrl?: string | null; emoji?: string | null; name: string
}) {
  return (
    <span class="sh-spaces-row__ic" aria-hidden="true">
      {iconUrl ? <img src={iconUrl} alt="" loading="lazy" /> : spaceGlyph(emoji, name)}
    </span>
  )
}

/** "Just you" / "8 members". */
export function membersLabel(n: number): string {
  if (n <= 1) return t('spaces.list.just_you')
  return t(isOne(n) ? 'space.members.count_one' : 'space.members.count', { n: String(n) })
}

/** "8 members · active 2 h ago" — either part may be missing. */
export function activityMeta(memberCount: number | null | undefined, lastActivityAt: string | null | undefined): string {
  const parts: string[] = []
  if (memberCount != null) parts.push(membersLabel(memberCount))
  if (lastActivityAt) parts.push(t('spaces.list.active', { when: relativeDocsTime(lastActivityAt) }))
  return parts.join(' · ')
}

interface Props {
  /** In-app path the row opens (``/spaces/…``); none → a static row. */
  to?: string
  iconUrl?: string | null
  emoji?: string | null
  name: string
  meta?: string
  /** Inside the link, after the text: a count. */
  end?: ComponentChildren
  /** Beside the link: the row's own button (Discover's Join / Ask). */
  action?: ComponentChildren
  /** Everything the row says, in reading order, for assistive tech. */
  label?: string
  muted?: boolean
}

export function SpaceListRow({ to, iconUrl, emoji, name, meta, end, action, label, muted }: Props) {
  const inner = (
    <>
      <SpaceIcon iconUrl={iconUrl} emoji={emoji} name={name} />
      <span class="sh-spaces-row__body">
        <span class="sh-spaces-row__t" dir="auto">{name}</span>
        {meta && <span class="sh-spaces-row__m" dir="auto">{meta}</span>}
      </span>
      {end && <span class="sh-spaces-row__end">{end}</span>}
    </>
  )
  return (
    <li class={`sh-spaces-row${muted ? ' sh-spaces-row--muted' : ''}`}>
      {to
        ? <a class="sh-spaces-row__main" href={addBase(to)} aria-label={label}>{inner}</a>
        : <div class="sh-spaces-row__main">{inner}</div>}
      {action && <span class="sh-spaces-row__action">{action}</span>}
    </li>
  )
}
