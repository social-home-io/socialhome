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
 *     each home.
 *
 * Endpoint: ``GET /api/friends`` returns the whole tree in one shot
 * (no follow-up fetches per household).
 */
import { useEffect, useState } from 'preact/hooks'
import { useLocation } from 'preact-iso'
import { useTitle } from '@/store/pageTitle'
import { currentUser } from '@/store/auth'
import { Avatar } from '@/components/Avatar'
import { Button } from '@/components/Button'
import { Spinner } from '@/components/Spinner'
import { LocationMap, type LocationMarker } from '@/components/LocationMap'
import { openPairing, PairingFlow } from '@/components/PairingFlow'
import { showToast } from '@/components/Toast'
import { AliasDialog } from '@/components/AliasDialog'
import { FriendActionSheet, openFriendActions } from './FriendActionSheet'
import { api } from '@/api'

interface LocalMember {
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
}

interface RemoteMember {
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
}

interface LocalInstance {
  instance_id: string | null
  display_name: string
  home_lat: number | null
  home_lon: number | null
  members: LocalMember[]
  member_count: number
}

interface Household {
  instance_id: string
  display_name: string
  home_lat: number | null
  home_lon: number | null
  paired_at: string | null
  reachable: boolean
  members: RemoteMember[]
  member_count: number
}

interface FriendsPayload {
  instance: LocalInstance
  households: Household[]
  totals: { households: number; people: number }
}

/** "5 min ago" / "3 d ago" — enough precision for a paired-since hint. */
function humanizeAgo(iso: string | null | undefined): string | null {
  if (!iso) return null
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return null
  const sec = Math.max(0, Math.round((Date.now() - t) / 1000))
  if (sec < 60)         return 'just now'
  if (sec < 3600)       return `${Math.floor(sec / 60)} min ago`
  if (sec < 86400)      return `${Math.floor(sec / 3600)} h ago`
  if (sec < 86400 * 30) return `${Math.floor(sec / 86400)} d ago`
  return new Date(t).toLocaleDateString()
}

/** Earliest paired_at across all households — used in the hero
 *  sub-line to give a sense of how long the network has existed. */
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
  useTitle('Friends')
  const [data, setData] = useState<FriendsPayload | null>(null)
  const [loading, setLoading] = useState(true)
  /** Per-user "starting DM" tracking so a double-click can't spawn
   *  two POSTs. The DM service is idempotent server-side (returns the
   *  existing conversation), but a flashing spinner reassures the
   *  user that the click is in flight. */
  const [dmBusy, setDmBusy] = useState<Set<string>>(new Set())
  const location = useLocation()

  /** Open (or create) a 1:1 DM with the chosen household member and
   *  navigate straight into the thread. Local members route via
   *  ``{username}``; remote members route via ``{user_id}`` so the
   *  conversation rides the federation envelope path. The service
   *  layer is idempotent — clicking the same chip again jumps back
   *  to the same conversation. */
  const startDmWith = async (target: {
    user_id: string
    username: string
    is_local: boolean
  }) => {
    if (dmBusy.has(target.user_id)) return
    setDmBusy(b => new Set(b).add(target.user_id))
    try {
      const body = target.is_local
        ? { username: target.username }
        : { user_id: target.user_id }
      const conv = await api.post('/api/conversations/dm', body) as {
        id: string
      }
      location.route(`/dms/${conv.id}`)
    } catch (e: any) {
      showToast(e?.message || 'Couldn’t start the DM', 'error')
    } finally {
      setDmBusy(b => {
        const n = new Set(b)
        n.delete(target.user_id)
        return n
      })
    }
  }

  useEffect(() => {
    let cancelled = false
    api.get('/api/friends').then((rows) => {
      if (cancelled) return
      setData(rows as FriendsPayload)
      setLoading(false)
    }).catch(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [])

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
      sub_label: `${instance.member_count} member${
        instance.member_count === 1 ? '' : 's'} · your household`,
      state: 'home',
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
      sub_label: `${h.member_count} member${
        h.member_count === 1 ? '' : 's'}${ago ? ` · paired ${ago}` : ''}`,
      state: h.reachable ? 'zone' : 'away',
    })
  }

  const isAdmin = !!currentUser.value?.is_admin

  return (
    <div class="sh-friends">
      <header class="sh-friends-hero">
        <div class="sh-friends-hero-headline">
          <strong>{totals.people}</strong>{' '}
          {totals.people === 1 ? 'person' : 'people'} across{' '}
          <strong>{totals.households}</strong>{' '}
          {totals.households === 1 ? 'household' : 'households'}
        </div>
        <div class="sh-friends-hero-sub sh-muted">
          {households.length === 0
            ? 'Just your household for now — pair another to grow your network.'
            : (
              <>
                {oldest && <>Connected since {humanizeAgo(oldest)} · </>}
                {reachableCount} of {households.length} reachable now
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
              + Pair a household
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
          <span class="sh-friends-tag sh-muted">your household</span>
        </header>
        <div class="sh-friends-members">
          {instance.members.map(m => {
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
                  title={`${m.display_name} (you)`}
                >
                  <Avatar
                    name={m.display_name}
                    src={m.picture_url}
                    size={28}
                    online={m.is_online ? (m.is_idle ? 'idle' : 'online') : null}
                  />
                  <span class="sh-friends-member-name">{m.display_name}</span>
                  <span class="sh-friends-tag sh-muted">you</span>
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
                title={`Open actions for ${shown}`}
                aria-label={`Open actions for ${shown}`}
                disabled={dmBusy.has(m.user_id)}
                onClick={() => openFriendActions({
                  user_id: m.user_id,
                  username: m.username,
                  display_name: m.display_name,
                  personal_alias: m.personal_alias ?? null,
                  picture_url: m.picture_url,
                  household: instance.display_name,
                  is_local: true,
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
                    aria-label="Your nickname"
                    title={`Your nickname · their name: ${m.display_name}`}
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
          <h3>No connected households yet</h3>
          <p>Pair with another household to see them here.</p>
          {isAdmin ? (
            <Button onClick={() => openPairing('household')}>
              + Pair a household
            </Button>
          ) : (
            <p class="sh-muted">
              Ask a household admin to pair with another household.
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
                    paired {pairedAgo}
                  </span>
                )}
                {/* Reachable is the default expectation — only paint a
                 * dot when something is wrong, so the green session-
                 * presence dot on the avatars is the only "good news"
                 * indicator on the row. */}
                {!h.reachable && (
                  <span
                    class="sh-friends-status-dot sh-friends-status-dot--unreachable"
                    aria-label="Unreachable"
                    title="Unreachable"
                  />
                )}
              </header>
              {h.members.length === 0 ? (
                <p class="sh-muted sh-friends-empty-members">
                  We haven't synced their members yet.
                </p>
              ) : (
                <div class="sh-friends-members">
                  {h.members.map(m => {
                    const shown = m.personal_alias || m.display_name
                    const isAliased = !!m.personal_alias
                      && m.personal_alias !== m.display_name
                    return (
                      <button
                        key={m.user_id}
                        type="button"
                        class="sh-friends-member-chip sh-friends-member-chip--button"
                        title={`Open actions for ${shown}`}
                        aria-label={`Open actions for ${shown}`}
                        disabled={dmBusy.has(m.user_id)}
                        onClick={() => openFriendActions({
                          user_id: m.user_id,
                          username: m.remote_username,
                          display_name: m.display_name,
                          personal_alias: m.personal_alias ?? null,
                          picture_url: m.picture_url,
                          household: h.display_name,
                          is_local: false,
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
                            aria-label="Your nickname"
                            title={`Your nickname · their name: ${m.display_name}`}
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
      {/* Friend chip click opens this action sheet (Message / Rename).
       *  The alias dialog is its sibling so saving a new nickname
       *  flips back into the main view without leaving a stale modal
       *  z-stack. */}
      <FriendActionSheet
        onStartDm={async (t) => {
          await startDmWith({
            user_id: t.user_id,
            username: t.username ?? '',
            is_local: t.is_local,
          })
        }}
        onAliasChanged={(user_id, newAlias) => {
          // Optimistic patch — both local + remote member rows live in
          // the same payload; just walk the structure once.
          setData(prev => {
            if (!prev) return prev
            const patch = <T extends { user_id: string; personal_alias?: string | null }>(
              rows: T[],
            ): T[] =>
              rows.map(r =>
                r.user_id === user_id
                  ? { ...r, personal_alias: newAlias }
                  : r,
              )
            return {
              ...prev,
              instance: {
                ...prev.instance,
                members: patch(prev.instance.members),
              },
              households: prev.households.map(h => ({
                ...h,
                members: patch(h.members),
              })),
            }
          })
        }}
      />
      <AliasDialog />
    </div>
  )
}
