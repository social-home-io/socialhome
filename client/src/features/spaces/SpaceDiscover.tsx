/**
 * SpaceDiscover — the Discover section of the Spaces page: spaces you can
 * join, from one of three sources picked with a chip (owner decision 5):
 *
 * - **Your household** — the spaces this household hosts that you're not
 *   in (``GET /api/household/spaces``);
 * - **Paired households** — the public spaces of households you're paired
 *   with (``GET /api/peer_spaces``, synced by §D1a). Public is paired
 *   households only — never on a GFS;
 * - **Global** — the directories of the GFSs this household added
 *   (``GET /api/public_spaces``). With no GFS, one inline row says so
 *   ("Add a GFS" for admins, "Ask …" for everyone else).
 *
 * A paired household's local community carries only "within X km" and the
 * host's verdict for our home (never its centre): Paired lists the ones
 * your home is in first, the ones you're outside of last and dimmed, and
 * "Near you" narrows the list to the first kind. Global lists the spaces
 * pinned nearest to our home first ("about 3 km away") — worked out in
 * this browser; our home goes nowhere.
 *
 * Invite-only spaces are never listed, nor a space you're already in or
 * follow. Each row opens the space's About page; its one button joins,
 * asks to join or follows, by the space's rules. The chosen chip lives in
 * the URL (``/spaces?discover=paired``), so Back and a shared link keep it.
 */
import { useEffect, useState } from 'preact/hooks'
import { useLocation } from 'preact-iso'
import { api } from '@/api'
import { addBase } from '@/baseUrl'
import { Button } from '@/components/Button'
import { LocalAreaBadge, localAreaVerdictText, verdictAllowsJoin } from '@/components/LocalAreaBadge'
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'
import { currentUser } from '@/store/auth'
import { ensureHouseholdHome, householdHome } from '@/store/householdHome'
import { cacheDirectoryEntries } from '@/store/spaceDirectory'
import { askAdmins } from '@/features/connections/AdminContactNote'
import { householdAdmins, loadHouseholdAdmins } from '@/features/connections/connectionsData'
import { ConnIcon } from '@/features/connections/ConnIcon'
import { areaShortLabel, type Home } from '@/utils/localArea'
import {
  distanceLabel, hasHint, hintLabel, hintVerdict, nearMe, pinDistanceKm, sortByDistance,
} from '@/utils/placePin'
import type { DirectoryEntry, GfsConnection, HouseholdSpaceRow } from '@/types'
import { JoinRequestModal } from './JoinRequestModal'
import {
  buildHouseholdEntries, discoverAction, discoverable, entryAreaVerdict, entryRadiusKm, hostLabel,
  matchesTerm, sortNearFirst, withMembership, DISCOVER_SOURCES, type DiscoverAction, type DiscoverSource,
} from './discover'
import { SpaceListRow, membersLabel } from './SpaceListRow'

interface Props {
  /** Ids of the spaces the caller holds a seat in (members and followers). */
  seatedIds: Set<string>
  /** The chip in the URL (``?discover=…``). */
  source: DiscoverSource
  term: string
  /** The caller joined or followed something — re-read their spaces. */
  onChanged: () => void
}

type Lists = Record<DiscoverSource, DirectoryEntry[]>

const EMPTY: Lists = { household: [], paired: [], global: [] }

interface MySubscription { space_id: string }

async function loadDiscover(): Promise<{ lists: Lists; failed: Set<DiscoverSource>; hasGfs: boolean }> {
  const failed = new Set<DiscoverSource>()
  const soft = <T,>(src: DiscoverSource | null, p: Promise<unknown>, fallback: T): Promise<T> =>
    (p as Promise<T>).catch(() => { if (src) failed.add(src); return fallback })
  const [rawHome, rawPaired, rawGlobal, rawSubs, rawPending, rawGfs] = await Promise.all([
    soft<HouseholdSpaceRow[]>('household', api.get('/api/household/spaces'), []),
    soft<DirectoryEntry[]>('paired', api.get('/api/peer_spaces'), []),
    soft<DirectoryEntry[]>('global', api.get('/api/public_spaces'), []),
    soft(null, api.get('/api/me/subscriptions'), { subscriptions: [] as MySubscription[] }),
    soft(null, api.get('/api/me/join-requests'), { pending_space_ids: [] as string[] }),
    soft<GfsConnection[] | null>(null, api.get('/api/gfs/connections'), null),
  ])
  const subIds = new Set(((rawSubs as { subscriptions?: MySubscription[] }).subscriptions ?? []).map((s) => s.space_id))
  const pendingIds = new Set((rawPending as { pending_space_ids?: string[] }).pending_space_ids ?? [])
  const household = buildHouseholdEntries(Array.isArray(rawHome) ? rawHome : [], { subIds, pendingIds })
  const homeIds = new Set(household.map((e) => e.space_id))
  const mine = { memberIds: new Set<string>(), subIds, pendingIds }
  // A space this household hosts is listed under Your household only.
  const remote = (rows: DirectoryEntry[]) => (Array.isArray(rows) ? rows : []).filter((e) => !homeIds.has(e.space_id))
  const lists: Lists = {
    household,
    paired: withMembership(remote(rawPaired), 'public', mine),
    global: withMembership(remote(rawGlobal), 'global', mine),
  }
  cacheDirectoryEntries([...lists.household, ...lists.paired, ...lists.global])
  // Unknown (the read failed) counts as "has one": no wrong "add a GFS".
  const hasGfs = rawGfs === null || (Array.isArray(rawGfs) && rawGfs.length > 0)
  return { lists, failed, hasGfs }
}

const CHIP_LABEL: Record<DiscoverSource, string> = {
  household: 'spaces.discover.chip_household',
  paired: 'spaces.discover.chip_paired',
  global: 'spaces.discover.chip_global',
}

function actionLabel(a: Exclude<DiscoverAction, null>): string {
  switch (a) {
    case 'join': return t('spaces.discover.join')
    case 'request': return t('spaces.discover.ask')
    case 'follow': return t('spaces.discover.follow')
    case 'pending': return t('spaces.discover.requested')
  }
}

/** "Sara's household · 6 members · ask to join" — and, for a pinned
 *  global space without a radius hint, "about 3 km away" from our home
 *  (with a hint the distance is in the row's hint chip). */
function entryMeta(e: DirectoryEntry, source: DiscoverSource, home: Home | null | undefined): string {
  const parts: string[] = []
  const km = source === 'global' && hintVerdict(e, home) === null ? pinDistanceKm(e, home) : null
  if (km !== null) parts.push(distanceLabel(km))
  if (source !== 'household') parts.push(hostLabel(e))
  parts.push(membersLabel(e.member_count))
  if (e.join_mode === 'request') parts.push(t('spaces.discover.mode_request'))
  else if (e.join_mode === 'open') {
    parts.push(e.scope === 'household' ? t('spaces.discover.mode_household') : t('spaces.discover.mode_open'))
  }
  return parts.join(' · ')
}

export function SpaceDiscover({ seatedIds, source, term, onChanged }: Props) {
  const loc = useLocation()
  const isAdmin = currentUser.value?.is_admin === true
  const [lists, setLists] = useState<Lists>(EMPTY)
  const [failed, setFailed] = useState<Set<DiscoverSource>>(() => new Set())
  const [hasGfs, setHasGfs] = useState(true)
  const [loaded, setLoaded] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const [asking, setAsking] = useState<DirectoryEntry | null>(null)
  /** Paired › "Near you": only the local communities our home is in. */
  const [nearOnly, setNearOnly] = useState(false)
  /** Global › "Near me": only the spaces whose suggested area (the owner's
   *  radius hint) our home is in — worked out here, sent nowhere. */
  const [nearMeOnly, setNearMeOnly] = useState(false)

  const reload = async () => {
    const r = await loadDiscover()
    setLists(r.lists)
    setFailed(r.failed)
    setHasGfs(r.hasGfs)
    setLoaded(true)
  }

  useEffect(() => { void reload() }, [])
  // The local-community badge is worked out against our own home.
  useEffect(() => { void ensureHouseholdHome() }, [])

  // "Ask Dave" needs the household's admins — only for a member who
  // could be pointed at one.
  useEffect(() => {
    if (!isAdmin && householdAdmins.value === null) void loadHouseholdAdmins()
  }, [isAdmin])

  const pick = (next: DiscoverSource) => {
    // Replace, not push: flipping chips shouldn't fill the Back stack.
    loc.route(`/spaces?discover=${next}`, true)
  }

  const markPending = (id: string) => {
    setLists((prev) => {
      const next = { ...prev }
      for (const k of DISCOVER_SOURCES) {
        next[k] = prev[k].map((e) => (e.space_id === id ? { ...e, request_pending: true } : e))
      }
      return next
    })
  }

  const isLocal = (e: DirectoryEntry) => e.host_instance_id === 'local' || e.scope === 'household'

  const act = async (e: DirectoryEntry, a: DiscoverAction) => {
    if (a === 'request') { setAsking(e); return }
    if (a !== 'join' && a !== 'follow') return
    setBusy(e.space_id)
    try {
      if (a === 'follow') {
        await api.post(`/api/spaces/${e.space_id}/subscribe`, {})
        showToast(t('spaces.browse.followed_toast', { name: e.name }), 'success')
        onChanged()
        await reload()
      } else if (isLocal(e)) {
        // Open to join: the join-request endpoint seats you right away
        // (``joined``); otherwise the request is pending — say so in place.
        const r = await api.post(`/api/spaces/${e.space_id}/join-requests`, {}) as { joined?: boolean } | undefined
        if (r?.joined === false) {
          showToast(t('spaces.browse.request_sent_for', { name: e.name }), 'success')
          markPending(e.space_id)
        } else {
          showToast(t('spaces.browse.joined_toast', { name: e.name }), 'success')
          onChanged()
          await reload()
        }
      } else {
        await api.post(`/api/public_spaces/${e.space_id}/join-request`, { host_instance_id: e.host_instance_id })
        showToast(t('spaces.browse.request_sent_to', { host: hostLabel(e) }), 'success')
        markPending(e.space_id)
      }
    } catch (exc) {
      showToast((exc as Error).message, 'error')
    } finally {
      setBusy(null)
    }
  }

  const submitAsk = async (message: string) => {
    const e = asking
    if (!e) return
    if (isLocal(e)) {
      await api.post(`/api/spaces/${e.space_id}/join-requests`, { message })
    } else {
      await api.post(`/api/public_spaces/${e.space_id}/join-request`, { host_instance_id: e.host_instance_id, message })
    }
    showToast(t('spaces.browse.request_sent_for', { name: e.name }), 'success')
    markPending(e.space_id)
  }

  const home = householdHome.value
  const paired = sortNearFirst(
    discoverable(lists.paired, seatedIds).filter((e) => matchesTerm(term, e.name, e.description)),
    home,
  )
  // The toggle only means something when a paired household hosts a
  // local community at all.
  const hasCommunity = paired.some((e) => entryRadiusKm(e) !== null)
  const near = nearOnly && hasCommunity
  const shown: Lists = {
    household: discoverable(lists.household, seatedIds).filter((e) => matchesTerm(term, e.name, e.description)),
    // Near the edge may be inside: the host checks when they ask.
    paired: near
      ? paired.filter((e) => ['inside', 'edge'].includes(entryAreaVerdict(e, home) ?? ''))
      : paired,
    global: sortByDistance(
      discoverable(lists.global, seatedIds).filter((e) => matchesTerm(term, e.name, e.description)),
      home,
    ),
  }
  // "Near me" only means something with our home and a hinted space.
  const canNearMe = home != null && hasHint(shown.global)
  if (nearMeOnly && canNearMe) shown.global = nearMe(shown.global, home)
  const rows = shown[source]
  const searching = term.trim() !== ''

  const emptyLine = () => {
    if (!loaded) return <p class="sh-spaces-note">{t('spaces.discover.loading')}</p>
    if (failed.has(source)) {
      return (
        <p class="sh-spaces-note" role="alert">
          {t('spaces.discover.load_failed')}{' '}
          <button type="button" class="sh-spaces-link" onClick={() => { void reload() }}>
            {t('common.retry')}
          </button>
        </p>
      )
    }
    if (source === 'paired' && near) {
      return <p class="sh-spaces-note">{t('local_area.discover.near_you_none')}</p>
    }
    if (searching) return <p class="sh-spaces-note">{t('spaces.discover.no_match', { term })}</p>
    if (source === 'global' && !hasGfs) {
      return (
        <div class="sh-spaces-card sh-spaces-inline" data-testid="discover-no-gfs">
          <span class="sh-spaces-inline__ic" aria-hidden="true"><ConnIcon name="globe" /></span>
          <p>
            {t('spaces.discover.no_gfs')}{' '}
            {isAdmin
              ? <a class="sh-spaces-link" href={addBase('/connections')} onClick={(ev) => { ev.preventDefault(); loc.route('/connections') }}>{t('spaces.discover.add_gfs')}</a>
              : <span>{askAdmins(householdAdmins.value)}</span>}
          </p>
        </div>
      )
    }
    if (source === 'paired') {
      return (
        <p class="sh-spaces-note">
          {t('spaces.discover.empty_paired')}{' '}
          {isAdmin
            ? <a class="sh-spaces-link" href={addBase('/connections')} onClick={(ev) => { ev.preventDefault(); loc.route('/connections') }}>{t('spaces.discover.pair')}</a>
            : null}
        </p>
      )
    }
    return (
      <p class="sh-spaces-note">
        {t(source === 'global' ? 'spaces.discover.empty_global' : 'spaces.discover.empty_household')}
      </p>
    )
  }

  return (
    <section class="sh-spaces-sect" aria-labelledby="sh-spaces-discover" id="discover">
      <div class="sh-conn-sect-head">
        <div class="sh-conn-sect-head__title"><h2 id="sh-spaces-discover">{t('spaces.discover.title')}</h2></div>
      </div>
      <p class="sh-conn-sect-desc">{t('spaces.discover.desc')}</p>
      <div class="sh-spaces-chips" role="group" aria-label={t('spaces.discover.sources')}>
        {DISCOVER_SOURCES.map((s) => (
          <button
            key={s}
            type="button"
            class="sh-spaces-chip"
            aria-pressed={s === source}
            onClick={() => pick(s)}
          >
            {t(CHIP_LABEL[s])}
            {loaded && shown[s].length > 0 && (
              <span class="sh-spaces-chip__n" aria-label={t('connections.count_label', { n: String(shown[s].length) })}>
                {shown[s].length}
              </span>
            )}
          </button>
        ))}
      </div>
      {source === 'paired' && hasCommunity && (
        <div class="sh-spaces-filter">
          <button
            type="button"
            class="sh-spaces-chip"
            aria-pressed={near}
            data-testid="discover-near-you"
            onClick={() => setNearOnly(!nearOnly)}
          >
            <span aria-hidden="true">📍</span> {t('local_area.discover.near_you')}
          </button>
        </div>
      )}
      {source === 'global' && canNearMe && (
        <div class="sh-spaces-filter">
          <button
            type="button"
            class="sh-spaces-chip"
            aria-pressed={nearMeOnly}
            data-testid="discover-near-me"
            onClick={() => setNearMeOnly(!nearMeOnly)}
          >
            <span aria-hidden="true">📍</span> {t('place_pin.discover.near_me')}
          </button>
        </div>
      )}
      {rows.length === 0 ? emptyLine() : (
        <ul class="sh-spaces-card sh-spaces-list" aria-label={t(CHIP_LABEL[source])}>
          {rows.map((e) => {
            const verdict = entryAreaVerdict(e, home)
            const radius = entryRadiusKm(e)
            // A local community you can't get into from here (outside, or
            // your home isn't known to its host): the badge says why, and
            // no button offers a join the host would refuse.
            const areaBlocks = verdict !== null && !verdictAllowsJoin(verdict)
            const way = discoverAction(e)
            const a = areaBlocks && way !== 'pending' ? null : way
            // No way in from here: say how people do get in.
            const meta = a === null && !areaBlocks
              ? `${entryMeta(e, source, home)} · ${t('spaces.discover.invite_link_short')}`
              : entryMeta(e, source, home)
            // A global space's radius hint: compared with OUR home, here.
            const hint = source === 'global' ? hintVerdict(e, home) : null
            const areaText = radius !== null
              ? [areaShortLabel(radius), verdict ? localAreaVerdictText(verdict, hostLabel(e)) : null]
                .filter(Boolean).join(' · ')
              : hint ? hintLabel(hint) : ''
            return (
              <SpaceListRow
                key={e.space_id}
                to={`/spaces/${e.space_id}/about`}
                emoji={e.emoji}
                name={e.name}
                meta={meta}
                muted={verdict === 'outside'}
                note={radius !== null ? (
                  <>
                    <span class="sh-local-area-chip">🏘️ {areaShortLabel(radius)}</span>
                    {verdict && <LocalAreaBadge verdict={verdict} hostName={hostLabel(e)} />}
                  </>
                ) : hint && (
                  <span class={`sh-place-hint sh-place-hint--${hint.kind}`} data-testid="place-hint">
                    <span aria-hidden="true">📍</span> {hintLabel(hint)}
                  </span>
                )}
                label={areaText ? `${e.name}, ${meta}, ${areaText}` : `${e.name}, ${meta}`}
                action={a && (
                  <Button
                    variant="secondary"
                    disabled={a === 'pending' || busy === e.space_id}
                    loading={busy === e.space_id}
                    aria-label={`${actionLabel(a)}: ${e.name}`}
                    onClick={() => { void act(e, a) }}
                  >
                    {actionLabel(a)}
                  </Button>
                )}
              />
            )
          })}
        </ul>
      )}
      {source === 'paired' && rows.length > 0 && (
        <p class="sh-spaces-note">{t('spaces.discover.paired_note')}</p>
      )}
      {asking && (
        <JoinRequestModal
          open={true}
          onClose={() => setAsking(null)}
          spaceName={asking.name}
          hostDisplayName={hostLabel(asking)}
          hostIsPaired={asking.host_is_paired}
          onSubmit={submitAsk}
        />
      )}
    </section>
  )
}
