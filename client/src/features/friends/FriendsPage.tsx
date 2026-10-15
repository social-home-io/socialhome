/**
 * FriendsPage — connected-people dashboard under Browse.
 *
 * Reframes the federation pairing data as a social-shape view of "us
 * + every household we've paired with":
 *
 *   • Hero: "X people across Y households · paired since {oldest}".
 *   • Map: a constellation of household pins, one per paired
 *     instance with coords. The local household lands as a
 *     terracotta "home" pin; reachable peers are zone-coloured;
 *     unreachable peers fade. Hidden when no household has coords.
 *   • Cards: our household first, then one card per paired household,
 *     each carrying the member chips so a viewer can scan who's in
 *     each home. A chip opens the shared person sheet (``openPerson``)
 *     with the row it already holds, so no extra fetch.
 *
 * Endpoint: ``GET /api/friends`` returns the whole tree in one shot
 * (no follow-up fetches per household).
 */
import { useEffect, useState } from 'preact/hooks'
import { t, formatLocale, isOne } from '@/i18n/i18n'
import { useTitle } from '@/store/pageTitle'
import { currentUser } from '@/store/auth'
import { Avatar } from '@/components/Avatar'
import { Button } from '@/components/Button'
import { Spinner } from '@/components/Spinner'
import { LocationMap, type LocationMarker } from '@/components/LocationMap'
import { openPairing, PairingFlow } from '@/components/PairingFlow'
import { openPerson } from '@/components/PersonSheet'
import { isBlocked } from '@/store/blocks'
import { api } from '@/api'
import type { UserStatus } from '@/types'

export interface LocalMember {
  user_id: string
  username: string
  display_name: string
  /** Viewer's private nickname (§4.1.6 personal_alias). Renders in
   *  place of ``display_name`` when set; only visible to this viewer.
   *  Edited via the action sheet on each chip. */
  personal_alias?: string | null
  picture_url: string | null
  is_online?: boolean
  is_idle?: boolean
  last_seen_at?: string | null
  /** Emoji + text status; null when unset or expired (the server drops
   *  expired ones). */
  status?: UserStatus | null
  /** Profile bio; null when empty. */
  bio?: string | null
}

export interface RemoteMember {
  user_id: string
  instance_id: string
  remote_username: string
  display_name: string
  /** See :type:`LocalMember.personal_alias`. */
  personal_alias?: string | null
  picture_url: string | null
  is_online?: boolean
  is_idle?: boolean
  last_seen_at?: string | null
  /** Emoji + text status; null when unset or expired (the server drops
   *  expired ones). */
  status?: UserStatus | null
  /** Profile bio; null when empty. */
  bio?: string | null
}

export interface LocalInstance {
  instance_id: string | null
  display_name: string
  home_lat: number | null
  home_lon: number | null
  members: LocalMember[]
  member_count: number
}

export interface Household {
  instance_id: string
  display_name: string
  home_lat: number | null
  home_lon: number | null
  paired_at: string | null
  reachable: boolean
  members: RemoteMember[]
  member_count: number
}

export interface FriendsPayload {
  instance: LocalInstance
  households: Household[]
  totals: { households: number; people: number }
}

/** "5 min ago" / "3 d ago" — enough precision for a paired-since hint. */
function humanizeAgo(iso: string | null | undefined): string | null {
  if (!iso) return null
  const ts = Date.parse(iso)
  if (Number.isNaN(ts)) return null
  const sec = Math.max(0, Math.round((Date.now() - ts) / 1000))
  if (sec < 60)         return t('time.just_now')
  if (sec < 3600)       return t('time.minutes_ago', { n: String(Math.floor(sec / 60)) })
  if (sec < 86400)      return t('time.hours_ago', { n: String(Math.floor(sec / 3600)) })
  if (sec < 86400 * 30) return t('time.days_ago', { n: String(Math.floor(sec / 86400)) })
  return new Date(ts).toLocaleDateString(formatLocale())
}

/** Earliest paired_at across all households — used in the hero
 *  sub-line to give a sense of how long the network has existed. */
/** "3 members" / "1 member" for a map pin's sub-label. */
function memberCount(n: number): string {
  return t(isOne(n) ? 'friends.members_one' : 'friends.members', { n: String(n) })
}

function earliestPairedAt(households: Household[]): string | null {
  let earliest: number | null = null
  for (const h of households) {
    if (!h.paired_at) continue
    const t = Date.parse(h.paired_at)
    if (Number.isNaN(t)) continue
    if (earliest === null || t < earliest) earliest = t
  }
  return earliest === null ? null : new Date(earliest).toISOString()
}

export default function FriendsPage() {
  useTitle(t('nav.friends'))
  const [data, setData] = useState<FriendsPayload | null>(null)
  const [loading, setLoading] = useState(true)
  /** The last ``GET /api/friends`` failed. Kept separate from
   *  ``loading`` so a failed load renders the retry card instead of a
   *  spinner that never stops; ``attempt`` bumps to re-run the load. */
  const [loadFailed, setLoadFailed] = useState(false)
  const [attempt, setAttempt] = useState(0)
  /** Patch the viewer's private nickname into the payload after the
   *  person sheet saves one — local and remote rows share the shape. */
  const onAliasChanged = (user_id: string, newAlias: string | null) => {
    setData(prev => {
      if (!prev) return prev
      const patch = <T extends { user_id: string; personal_alias?: string | null }>(
        rows: T[],
      ): T[] =>
        rows.map(r => (r.user_id === user_id ? { ...r, personal_alias: newAlias } : r))
      return {
        ...prev,
        instance: { ...prev.instance, members: patch(prev.instance.members) },
        households: prev.households.map(h => ({ ...h, members: patch(h.members) })),
      }
    })
  }

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    api.get('/api/friends').then((rows) => {
      if (cancelled) return
      setData(rows as FriendsPayload)
      setLoadFailed(false)
      setLoading(false)
    }).catch(() => {
      if (cancelled) return
      setLoadFailed(true)
      setLoading(false)
    })
    return () => { cancelled = true }
  }, [attempt])

  // A failed load gets its own card with a retry — never an endless
  // spinner. The card stays up while a retry is in flight (the button
  // shows the spinner) so focus and the announcement don't jump.
  if (!data && loadFailed) {
    return (
      <div class="sh-friends">
        <section class="sh-friends-error" role="alert">
          <span class="sh-friends-error__icon" aria-hidden="true">!</span>
          <h2>{t('friends.load_failed_title')}</h2>
          <p>{t('friends.load_failed_body')}</p>
          <Button variant="secondary" loading={loading}
                  onClick={() => setAttempt(n => n + 1)}>
            {t('common.try_again')}
          </Button>
        </section>
      </div>
    )
  }
  if (loading || !data) return <Spinner />

  const { instance, households, totals } = data
  const oldest = earliestPairedAt(households)
  const reachableCount = households.filter(h => h.reachable).length

  // Build the map pins: us + every paired household with coords. The
  // map is hidden entirely when no household has a pin so we don't
  // render a "lost in the ocean" empty world map.
  const markers: LocationMarker[] = []
  if (instance.home_lat !== null && instance.home_lon !== null) {
    markers.push({
      id: instance.instance_id ?? 'local',
      lat: instance.home_lat,
      lon: instance.home_lon,
      label: instance.display_name,
      sub_label: `${memberCount(instance.member_count)} · ${t('friends.your_household')}`,
      state: 'home',
      // A household is a place: a house pin with a name popup, never a
      // person bubble that merges with its neighbours.
      glyph: '🏠',
    })
  }
  for (const h of households) {
    if (h.home_lat === null || h.home_lon === null) continue
    const ago = humanizeAgo(h.paired_at)
    markers.push({
      id: h.instance_id,
      lat: h.home_lat,
      lon: h.home_lon,
      label: h.display_name,
      sub_label: memberCount(h.member_count)
        + (ago ? ` · ${t('friends.paired_ago', { ago })}` : ''),
      state: h.reachable ? 'zone' : 'away',
      glyph: '🏠',
    })
  }

  const isAdmin = !!currentUser.value?.is_admin

  return (
    <div class="sh-friends">
      <header class="sh-friends-hero">
        <div class="sh-friends-hero-headline">
          <strong>{totals.people}</strong>{' '}
          {t(isOne(totals.people) ? 'friends.hero.people_one' : 'friends.hero.people')}{' '}
          {t('friends.hero.across')}{' '}
          <strong>{totals.households}</strong>{' '}
          {t(isOne(totals.households) ? 'friends.hero.households_one' : 'friends.hero.households')}
        </div>
        <div class="sh-friends-hero-sub sh-muted">
          {households.length === 0
            ? t('friends.hero.alone')
            : (
              <>
                {oldest && <>{t('friends.hero.connected_since', { ago: humanizeAgo(oldest) ?? '' })} · </>}
                {t('friends.hero.reachable', { n: String(reachableCount), total: String(households.length) })}
              </>
            )}
        </div>
        {/* Admin-only inline pairing CTA — turns the hero from a stat
         *  card into an action card. Non-admins see the same hint copy
         *  in the empty state below ("ask an admin"); putting the CTA
         *  here saves admins a dig through the sidebar to ``/connections``. */}
        {isAdmin && (
          <div class="sh-friends-hero-actions">
            <Button onClick={() => openPairing('household')}>
              + {t('friends.pair_household')}
            </Button>
          </div>
        )}
      </header>

      {markers.length > 0 && (
        <div class="sh-friends-map">
          <LocationMap markers={markers} height={320} />
        </div>
      )}

      <section
        class="sh-friends-household sh-friends-household--mine"
        data-instance-id={instance.instance_id ?? ''}
      >
        <header class="sh-friends-household-head">
          <span class="sh-friends-house-icon" aria-hidden="true">🏠</span>
          <strong>{instance.display_name}</strong>
          <span class="sh-friends-tag sh-muted">{t('friends.your_household')}</span>
        </header>
        <div class="sh-friends-members">
          {instance.members.filter(m => !isBlocked(m.user_id)).map(m => {
            const isSelf = currentUser.value?.user_id === m.user_id
            // Self: render a passive card — a button leading nowhere
            // would be worse UX than no button. Everyone else: whole
            // chip clicks to start a DM, matching the remote-household
            // chips so the interaction model is symmetric.
            if (isSelf) {
              return (
                <div
                  key={m.user_id}
                  class="sh-friends-member-chip sh-friends-member-chip--self"
                  title={`${m.display_name} ${t('dms.group.you')}`}
                >
                  <Avatar
                    name={m.display_name}
                    src={m.picture_url}
                    size={28}
                    online={m.is_online ? (m.is_idle ? 'idle' : 'online') : null}
                  />
                  <span class="sh-friends-member-name">{m.display_name}</span>
                  <span class="sh-friends-tag sh-muted">{t('friends.you')}</span>
                </div>
              )
            }
            const shown = m.personal_alias || m.display_name
            const isAliased = !!m.personal_alias
              && m.personal_alias !== m.display_name
            return (
              <button
                key={m.user_id}
                type="button"
                class="sh-friends-member-chip sh-friends-member-chip--button"
                title={t('friends.open_actions', { name: shown })}
                aria-label={t('friends.open_actions', { name: shown })}
                onClick={() => openPerson(m.user_id, {
                  person: {
                    ...m,
                    is_local: true,
                    household: {
                      instance_id: instance.instance_id,
                      display_name: instance.display_name,
                      is_own: true,
                    },
                  },
                  onAliasChanged,
                })}
              >
                <Avatar
                  name={shown}
                  src={m.picture_url}
                  size={28}
                  online={m.is_online ? (m.is_idle ? 'idle' : 'online') : null}
                />
                <span class="sh-friends-member-name">{shown}</span>
                {isAliased && (
                  <span
                    class="sh-friends-member-chip__alias-hint"
                    aria-label={t('alias.label')}
                    title={t('friends.nickname_hint', { name: m.display_name })}
                  >
                    ✏
                  </span>
                )}
              </button>
            )
          })}
        </div>
      </section>

      {households.length === 0 ? (
        <div class="sh-empty-state">
          <div aria-hidden="true">🤝</div>
          <h3>{t('friends.empty_title')}</h3>
          <p>{t('friends.empty_body')}</p>
          {isAdmin ? (
            <Button onClick={() => openPairing('household')}>
              + {t('friends.pair_household')}
            </Button>
          ) : (
            <p class="sh-muted">
              {t('friends.empty_ask_admin')}
            </p>
          )}
        </div>
      ) : (
        households.map(h => {
          const pairedAgo = humanizeAgo(h.paired_at)
          return (
            <section
              key={h.instance_id}
              class="sh-friends-household"
              data-instance-id={h.instance_id}
            >
              <header class="sh-friends-household-head">
                <strong>{h.display_name}</strong>
                {pairedAgo && (
                  <span class="sh-friends-tag sh-muted">
                    {t('friends.paired_ago', { ago: pairedAgo })}
                  </span>
                )}
                {/* Reachable is the default expectation — only paint a
                 * dot when something is wrong, so the green session-
                 * presence dot on the avatars is the only "good news"
                 * indicator on the row. */}
                {!h.reachable && (
                  <span
                    class="sh-friends-status-dot sh-friends-status-dot--unreachable"
                    aria-label={t('friends.unreachable')}
                    title={t('friends.unreachable')}
                  />
                )}
              </header>
              {h.members.length === 0 ? (
                <p class="sh-muted sh-friends-empty-members">
                  {t('friends.members_not_synced')}
                </p>
              ) : (
                <div class="sh-friends-members">
                  {h.members.filter(m => !isBlocked(m.user_id)).map(m => {
                    const shown = m.personal_alias || m.display_name
                    const isAliased = !!m.personal_alias
                      && m.personal_alias !== m.display_name
                    return (
                      <button
                        key={m.user_id}
                        type="button"
                        class="sh-friends-member-chip sh-friends-member-chip--button"
                        title={t('friends.open_actions', { name: shown })}
                        aria-label={t('friends.open_actions', { name: shown })}
                        onClick={() => openPerson(m.user_id, {
                          person: {
                            ...m,
                            username: null,
                            is_local: false,
                            household: {
                              instance_id: h.instance_id,
                              display_name: h.display_name,
                              is_own: false,
                            },
                          },
                          onAliasChanged,
                        })}
                      >
                        <Avatar
                          name={shown}
                          src={m.picture_url}
                          size={28}
                          online={m.is_online ? (m.is_idle ? 'idle' : 'online') : null}
                        />
                        <span class="sh-friends-member-name">
                          {shown}
                        </span>
                        {isAliased && (
                          <span
                            class="sh-friends-member-chip__alias-hint"
                            aria-label={t('alias.label')}
                            title={t('friends.nickname_hint', { name: m.display_name })}
                          >
                            ✏
                          </span>
                        )}
                      </button>
                    )
                  })}
                </div>
              )}
            </section>
          )
        })
      )}
      {/* Mount the pairing dialog so the hero / empty-state CTAs can
       *  open it inline, rather than routing through ``/connections``. */}
      <PairingFlow />
    </div>
  )
}
