/**
 * Discover › Friends — the people the Friends page lists (your own
 * household and your paired households), each with one-tap Follow.
 *
 * The follow is the household follow (``POST /api/moments/follows``): it
 * never leaves the household. Their moments already reach you through
 * pairing; a follow keeps them as long as their author chose instead of
 * 24 hours. The hint under the heading says so.
 *
 * * Order: your household first (without you), then paired households by
 *   name, people by name (``buildModel``, the Friends page's own shaping).
 *   Never re-sorted on a tap, so a row doesn't jump away under the finger.
 * * More than ``COLLAPSE_OVER`` people show ``COLLAPSED_SHOWN`` and an
 *   expander, as on the Friends page; a search shows every match.
 * * No one yet: one card pointing at Connections. A failed load: an inline
 *   error with Try again, for this section only.
 */
import { useRef, useState } from 'preact/hooks'
import { addBase } from '@/baseUrl'
import { Avatar } from '@/components/Avatar'
import { LoadErrorState } from '@/components/LoadErrorState'
import { openPerson } from '@/components/PersonSheet'
import { Spinner } from '@/components/Spinner'
import {
  COLLAPSE_OVER, COLLAPSED_SHOWN, buildModel, collatorFor, fold, matches, personSheetPerson,
  type FriendsPayload, type Person,
} from '@/features/friends/friendsModel'
import { t } from '@/i18n/i18n'
import { followUser, followedUserIds, unfollowUser } from '@/store/follows'
import { FollowToggle, toggleFollow } from './FollowToggle'

/** Everyone Discover offers to follow, in the order above. */
export function discoverFriends(
  data: FriendsPayload,
  opts: { selfId?: string | null, locale: string, hidden?: (userId: string) => boolean },
): Person[] {
  const model = buildModel(data, opts)
  // By household name only: the Friends page puts unreachable households
  // first, which is about the household, not about following its people.
  const collator = collatorFor(opts.locale)
  const groups = [...model.groups].sort((a, b) =>
    collator.compare(a.household.display_name, b.household.display_name))
  return [
    ...model.atHome.filter((p) => !p.isSelf),
    ...groups.flatMap((g) => g.people),
  ]
}

interface Props {
  /** ``null`` while loading (or after a failed load). */
  people: Person[] | null
  query: string
  failed?: boolean
  onRetry?: () => void
}

const HEADING_ID = 'sh-discover-friends-heading'

export function DiscoverFriends({ people, query, failed = false, onRetry }: Props) {
  const [expanded, setExpanded] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const inFlight = useRef(false)

  const heading = <h2 id={HEADING_ID} class="sh-momentum-discover__title">{t('moment.discover.friends_heading')}</h2>

  if (people === null) {
    return (
      <section class="sh-discover-section" aria-labelledby={HEADING_ID}>
        {heading}
        {failed
          ? (
            <LoadErrorState
              message={t('moment.discover.friends_failed')}
              retryLabel={t('load.try_again')}
              onRetry={() => onRetry?.()}
            />
          )
          : <Spinner />}
      </section>
    )
  }

  if (people.length === 0) {
    return (
      <section class="sh-discover-section sh-empty-state" aria-labelledby={HEADING_ID}>
        {heading}
        <p>{t('moment.discover.friends_empty')}</p>
        <div class="sh-empty-state__cta-row">
          <a class="sh-btn sh-btn--secondary" href={addBase('/connections')}>
            {t('moment.discover.open_connections')}
          </a>
        </div>
      </section>
    )
  }

  const following = followedUserIds.value
  const folded = fold(query.trim())
  const matching = folded ? people.filter((p) => matches(p, folded)) : people
  const collapsible = !folded && people.length > COLLAPSE_OVER
  const shown = collapsible && !expanded ? people.slice(0, COLLAPSED_SHOWN) : matching
  const followCount = people.filter((p) => following.has(p.user_id)).length
  const listId = `${HEADING_ID}-list`

  const onToggle = async (p: Person) => {
    // The ref answers at once; ``busy`` only after the next render.
    if (inFlight.current) return
    inFlight.current = true
    setBusy(p.user_id)
    try {
      await toggleFollow(
        p.user_id, p.shown, following.has(p.user_id),
        () => followUser(p.user_id), () => unfollowUser(p.user_id),
      )
    } finally {
      inFlight.current = false
      setBusy(null)
    }
  }

  const open = (p: Person) => openPerson(p.user_id, { person: personSheetPerson(p) })

  return (
    <section class="sh-discover-section" aria-labelledby={HEADING_ID}>
      <div class="sh-discover-section__head">
        {heading}
        <span class="sh-muted sh-discover-section__count">
          {t('moment.discover.friends_count', { n: String(followCount), total: String(people.length) })}
        </span>
      </div>
      <p class="sh-muted sh-momentum-discover__note">{t('moment.discover.friends_hint')}</p>
      {shown.length === 0
        ? <p class="sh-muted">{t('moment.discover.friends_no_match')}</p>
        : (
          <ul id={listId} class="sh-momentum-discover-list" aria-labelledby={HEADING_ID}>
            {shown.map((p) => (
              <li key={p.user_id} class="sh-momentum-discover-row">
                <Avatar src={p.member.picture_url} name={p.shown} size={40} />
                <div class="sh-momentum-discover-meta">
                  <button type="button" class="sh-person-btn sh-momentum-discover-name"
                    title={p.shown} onClick={() => open(p)}>
                    <bdi dir="auto">{p.shown}</bdi>
                  </button>
                  <span class="sh-muted sh-momentum-discover-sub" title={p.home.display_name}>
                    <bdi dir="auto">{p.home.display_name}</bdi>
                  </span>
                </div>
                <FollowToggle
                  name={p.shown}
                  following={following.has(p.user_id)}
                  busy={busy === p.user_id}
                  onClick={() => void onToggle(p)}
                />
              </li>
            ))}
          </ul>
        )}
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
    </section>
  )
}
