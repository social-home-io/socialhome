/**
 * One person in a Settings › Privacy people list (Blocked, Following).
 *
 * Same anatomy as a Friends person row: avatar, name, a muted meta line
 * and one action. The viewer's nickname wins as the name; the real name
 * then leads the meta line ("Oma" over "Grandma Ruth · Vizeli · since
 * Oct 15"). An id the server can no longer resolve (its household was
 * unpaired) reads as "Unknown person", never as the raw id.
 */
import { Fragment, type ComponentChildren } from 'preact'
import { Avatar } from '@/components/Avatar'
import { formatLocale, t } from '@/i18n/i18n'
import type { PersonSummary } from '@/types'
import { normaliseTimestamp } from '@/utils/relativeTime'

/** The name to show for ``p`` — nickname, then real name, then
 *  "Unknown person". Also used for the confirm dialogs. */
export function privacyPersonName(p: PersonSummary): string {
  return p.personal_alias || p.display_name || t('settings.person.unknown')
}

/** "Oct 15" (plus the year when it isn't this year), in the UI locale. */
function shortDate(d: Date): string {
  const sameYear = d.getFullYear() === new Date().getFullYear()
  return d.toLocaleDateString(formatLocale(), {
    month: 'short',
    day: 'numeric',
    ...(sameYear ? {} : { year: 'numeric' }),
  })
}

interface Props {
  person: PersonSummary
  /** When the block / follow started (server timestamp). */
  since: string
  /** The trailing action (Unblock / Unfollow). */
  children: ComponentChildren
}

export function PrivacyPersonRow({ person, since, children }: Props) {
  const name = privacyPersonName(person)
  const known = Boolean(person.display_name)
  // Naive SQLite timestamps are UTC — normalise before ``Date`` math.
  const at = new Date(normaliseTimestamp(since))
  const valid = !Number.isNaN(at.getTime())
  const meta: string[] = []
  if (person.personal_alias && person.display_name) meta.push(person.display_name)
  if (person.household) meta.push(person.household)
  else if (!known) meta.push(t('settings.person.no_longer_connected'))
  return (
    <li class="sh-person-row">
      <Avatar name={name} src={person.picture_url ?? null} size={40} />
      <span class="sh-person-row__body">
        <bdi class="sh-person-row__name">{name}</bdi>
        <span class="sh-person-row__meta">
          {/* <bdi> keeps an Arabic household name from reordering the
              separators around it. */}
          {meta.map((part, i) => (
            <Fragment key={i}>{i > 0 && ' · '}<bdi>{part}</bdi></Fragment>
          ))}
          {valid && (
            <>
              {meta.length > 0 && ' · '}
              <time dateTime={at.toISOString()} title={at.toLocaleString(formatLocale())}>
                {t('settings.person.since', { date: shortDate(at) })}
              </time>
            </>
          )}
        </span>
      </span>
      {children}
    </li>
  )
}
