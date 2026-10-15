/**
 * HouseholdPeople — a paired household as a Connections-style card with
 * its people inside.
 *
 * Header: the household's monogram on its hash tint (the same
 * ``ConnAvatar`` Connections uses — an unreachable one gets the dashed
 * danger ring + "!" badge and the card a danger left edge), the name and a
 * muted "N people" (or "Unreachable since Wed · N people"). Below, one
 * ``PersonRow`` per person. More than ``COLLAPSE_OVER`` people show
 * ``COLLAPSED_SHOWN`` plus an "N more" expander, which flips to "Show
 * fewer" once open. A search shows every match (no collapse).
 */
import { useState } from 'preact/hooks'
import { formatLocale, isOne, t } from '@/i18n/i18n'
import { ConnAvatar } from '@/features/connections/ConnAvatar'
import { formatSince, initialOf, tintFor } from '@/features/connections/model'
import { PersonRow } from './PersonRow'
import { COLLAPSE_OVER, COLLAPSED_SHOWN, type HouseholdGroup, type Person } from './friendsModel'

export function peopleCount(n: number): string {
  return t(isOne(n) ? 'friends.people_count_one' : 'friends.people_count', { n: String(n) })
}

/** "Unreachable since Wed" / "Unreachable" (no timestamp known). */
export function unreachableText(since: string | null | undefined, style: 'short' | 'long' = 'short'): string {
  const when = formatSince(since, style, formatLocale())
  return when ? t('friends.unreachable_since', { when }) : t('friends.unreachable')
}

interface Props {
  group: HouseholdGroup
  /** Everyone the household shared — the header count, even while a
   *  search narrows the rows. */
  total: number
  /** A search is on: every match shows, no expander. */
  filtering: boolean
  onOpen: (p: Person) => void
}

export function HouseholdPeople({ group, total, filtering, onOpen }: Props) {
  const { household: h, people } = group
  const [expanded, setExpanded] = useState(false)
  const down = !h.reachable
  const collapsible = !filtering && people.length > COLLAPSE_OVER
  const shown = collapsible && !expanded ? people.slice(0, COLLAPSED_SHOWN) : people
  const headId = `sh-friends-hh-${h.instance_id}`
  const listId = `${headId}-people`
  return (
    <li
      class={`sh-friends-hh${down ? ' sh-friends-hh--down' : ''}`}
      data-instance-id={h.instance_id}
      aria-labelledby={headId}
    >
      <div class="sh-friends-hh__head">
        <ConnAvatar
          letter={initialOf(h.display_name)}
          tint={tintFor(h.instance_id)}
          status={down ? 'down' : undefined}
        />
        <div class="sh-conn-card__body">
          <h3 id={headId} class="sh-conn-card__name sh-friends-hh__name">{h.display_name}</h3>
          <p class="sh-conn-card__line2 sh-friends-hh__line2">
            {down && (
              <>
                <span class="sh-conn-down">{unreachableText(h.unreachable_since)}</span>
                <span class="sh-conn-sep" aria-hidden="true">·</span>
              </>
            )}
            <span>{peopleCount(total)}</span>
          </p>
        </div>
      </div>
      <div class="sh-friends-people" id={listId}>
        {shown.map((p) => <PersonRow key={p.user_id} person={p} onOpen={onOpen} />)}
      </div>
      {collapsible && (
        <button
          type="button"
          class="sh-friends-more"
          aria-expanded={expanded}
          aria-controls={listId}
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded
            ? t('friends.show_fewer')
            : t('friends.n_more', { n: String(people.length - COLLAPSED_SHOWN) })}
        </button>
      )}
    </li>
  )
}
