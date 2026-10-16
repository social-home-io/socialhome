/**
 * FriendsPage — "your people": everyone in our household and in every
 * household we paired with, under Browse.
 *
 *   • Hero (text only — Connections keeps the constellation): "N people in
 *     M households", one line on how long we've been connected, and one
 *     CTA — "Pair a household" for admins, "Ask <admin>" for members.
 *   • Toolbar: "Find someone" (only past ``FIND_OVER`` people) and a
 *     Households / People / Map switch; the map replaces the list.
 *   • Households view: "At home" (us), then one card per paired household
 *     — unreachable ones first, then A–Z. Households that shared nobody
 *     yet are a muted footer line, not an empty card.
 *   • People view: everyone A–Z with letter dividers.
 *   • Footer: how many people the viewer blocked (they are hidden here),
 *     with a link to manage them.
 *
 * Every row opens the shared person sheet (``openPerson``) with the row it
 * already holds, so no extra fetch; our own row opens the status editor.
 *
 * Endpoint: ``GET /api/friends`` returns the whole tree in one shot.
 */
import { useEffect, useRef, useState } from 'preact/hooks'
import { t, formatLocale, isOne } from '@/i18n/i18n'
import { useTitle } from '@/store/pageTitle'
import { currentUser } from '@/store/auth'
import { addBase } from '@/baseUrl'
import { Button } from '@/components/Button'
import { Modal } from '@/components/Modal'
import { Spinner } from '@/components/Spinner'
import { StatusEditor } from '@/components/StatusEditor'
import { LocationMap, type LocationMarker } from '@/components/LocationMap'
import { openPairing, PairingFlow } from '@/components/PairingFlow'
import { openPerson } from '@/components/PersonSheet'
import { blockedUsers, isBlocked, loadBlocks } from '@/store/blocks'
import { ConnIcon } from '@/features/connections/ConnIcon'
import { askAdmins } from '@/features/connections/AdminContactNote'
import { householdAdmins, loadHouseholdAdmins } from '@/features/connections/connectionsData'
import { fillNodes, joinNames } from '@/features/connections/richText'
import { api } from '@/api'
import type { User } from '@/types'
import {
  FIND_OVER, buildModel, fold, matches, pairingSpan, peopleByLetter,
  type FriendsModel, type FriendsPayload, type FriendsView, type Household, type Person,
} from './friendsModel'
import { PersonRow } from './PersonRow'
import { HouseholdPeople, peopleCount, unreachableText } from './HouseholdPeople'

function plural(key: string, n: number): string {
  return t(isOne(n) ? `${key}_one` : key, { n: String(n) })
}

// ── Hero ────────────────────────────────────────────────────────────────

function heroSub(households: Household[]): string | null {
  const { since, newest } = pairingSpan(households)
  if (since === null) return null
  const date = new Date(since).toLocaleDateString(formatLocale(), { month: 'long', year: 'numeric' })
  const together = t('friends.hero.together_since', { date })
  return households.length > 1 && newest
    ? `${together} · ${t('friends.hero.newest', { name: newest.display_name })}`
    : together
}

function MemberAsk() {
  return (
    <p class="sh-conn-hero__note">
      <ConnIcon name="info" />
      <span>{fillNodes(t('friends.hero.member_ask'), { ask: askAdmins(householdAdmins.value) })}</span>
    </p>
  )
}

function PairButton() {
  return (
    <div class="sh-conn-hero__actions">
      <button type="button" class="sh-btn sh-btn--primary" onClick={() => openPairing('household')}>
        <ConnIcon name="qr" />
        {t('friends.pair_household')}
      </button>
    </div>
  )
}

function FriendsHero({ model, households, isAdmin }: {
  model: FriendsModel
  households: Household[]
  isAdmin: boolean
}) {
  const sub = heroSub(households)
  return (
    <section class="sh-conn-hero sh-friends-hero" aria-labelledby="sh-friends-hero-title">
      <div class="sh-conn-hero__top">
        <span class="sh-conn-eyebrow">{t('friends.hero.eyebrow')}</span>
        <h2 id="sh-friends-hero-title" class="sh-conn-hero__headline">
          {fillNodes(t('friends.hero.headline'), {
            people: plural('friends.hero.people_n', model.peopleCount),
            households: <em>{plural('friends.hero.households_n', model.householdCount)}</em>,
          })}
        </h2>
      </div>
      <div class="sh-conn-hero__rest">
        {sub && <p class="sh-conn-hero__sub">{sub}</p>}
        {isAdmin ? <PairButton /> : <MemberAsk />}
      </div>
    </section>
  )
}

/** No paired household yet: the one place to start pairing. */
function FriendsEmpty({ isAdmin }: { isAdmin: boolean }) {
  return (
    <section class="sh-friends-empty" aria-labelledby="sh-friends-empty-title">
      <svg class="sh-friends-empty__art" viewBox="0 0 220 110" aria-hidden="true" focusable="false">
        <path class="sh-friends-empty__h" d="M22 100V52L64 18l42 34v48z" />
        <rect class="sh-friends-empty__door" x="56" y="72" width="16" height="28" rx="1" />
        <path class="sh-friends-empty__path" d="M108 94c14-24 34-24 48-4" />
        <path class="sh-friends-empty__h2" d="M160 100V60l28-26 28 26v40z" />
      </svg>
      <h2 id="sh-friends-empty-title">{t('friends.empty.title')}</h2>
      <p>{t('friends.empty.body')}</p>
      {isAdmin ? <PairButton /> : <MemberAsk />}
    </section>
  )
}

// ── Toolbar ─────────────────────────────────────────────────────────────

const VIEW_ICON = { households: 'home', people: 'users', map: 'map' } as const
const VIEW_LABEL: Record<FriendsView, () => string> = {
  households: () => t('friends.view.households'),
  people: () => t('friends.view.people'),
  map: () => t('friends.view.map'),
}

function ViewSwitch({ value, withMap, onChange }: {
  value: FriendsView
  withMap: boolean
  onChange: (v: FriendsView) => void
}) {
  const views: FriendsView[] = withMap ? ['households', 'people', 'map'] : ['households', 'people']
  return (
    <div class="sh-segmented sh-friends-switch" role="group" aria-label={t('friends.view.label')}>
      {views.map((v) => (
        <button
          key={v}
          type="button"
          class={`sh-segmented__opt${value === v ? ' sh-segmented__opt--active' : ''}`}
          aria-pressed={value === v}
          onClick={() => onChange(v)}
        >
          <ConnIcon name={VIEW_ICON[v]} />
          {VIEW_LABEL[v]()}
        </button>
      ))}
    </div>
  )
}

function FindBox({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const input = useRef<HTMLInputElement | null>(null)
  return (
    <div class="sh-friends-find">
      <ConnIcon name="search" />
      <input
        ref={input}
        type="search"
        class="sh-friends-find__input"
        placeholder={t('friends.find.placeholder')}
        aria-label={t('friends.find.placeholder')}
        value={value}
        autoComplete="off"
        spellcheck={false}
        onInput={(e) => onChange((e.currentTarget as HTMLInputElement).value)}
        onKeyDown={(e) => {
          if (e.key === 'Escape' && value) {
            e.preventDefault()
            onChange('')
          }
        }}
      />
      {value && (
        <button
          type="button"
          class="sh-friends-find__clear"
          aria-label={t('friends.find.clear')}
          onClick={() => { onChange(''); input.current?.focus() }}
        >
          <ConnIcon name="x" />
        </button>
      )}
    </div>
  )
}

// ── Map ─────────────────────────────────────────────────────────────────

function markersFor(data: FriendsPayload, model: FriendsModel): LocationMarker[] {
  const out: LocationMarker[] = []
  const { instance } = data
  if (instance.home_lat !== null && instance.home_lon !== null) {
    out.push({
      id: instance.instance_id ?? 'local',
      lat: instance.home_lat,
      lon: instance.home_lon,
      label: instance.display_name,
      sub_label: `${peopleCount(model.atHome.length)} · ${t('friends.at_home')}`,
      state: 'home',
      // A household is a place: a house pin with a name popup, never a
      // person bubble that merges with its neighbours.
      glyph: '🏠',
    })
  }
  const counts = new Map(model.groups.map((g) => [g.household.instance_id, g.people.length]))
  for (const h of data.households) {
    if (h.home_lat === null || h.home_lon === null) continue
    const n = counts.get(h.instance_id) ?? 0
    out.push({
      id: h.instance_id,
      lat: h.home_lat,
      lon: h.home_lon,
      label: h.display_name,
      sub_label: h.reachable
        ? peopleCount(n)
        : `${peopleCount(n)} · ${unreachableText(h.unreachable_since)}`,
      state: h.reachable ? 'zone' : 'away',
      glyph: '🏠',
    })
  }
  return out
}

// ── Page ────────────────────────────────────────────────────────────────

export default function FriendsPage() {
  useTitle(t('nav.friends'))
  const [data, setData] = useState<FriendsPayload | null>(null)
  const [loading, setLoading] = useState(true)
  /** The last ``GET /api/friends`` failed. Kept separate from ``loading``
   *  so a failed load renders the retry card instead of a spinner that
   *  never stops; ``attempt`` bumps to re-run the load. */
  const [loadFailed, setLoadFailed] = useState(false)
  const [attempt, setAttempt] = useState(0)
  const [view, setView] = useState<FriendsView>('households')
  const [query, setQuery] = useState('')
  const [editingStatus, setEditingStatus] = useState(false)
  const isAdmin = !!currentUser.value?.is_admin
  const selfId = currentUser.value?.user_id ?? null

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

  useEffect(() => {
    void loadBlocks()
    if (!isAdmin) void loadHouseholdAdmins()
  }, [isAdmin])

  /** Patch the viewer's private nickname into the payload after the
   *  person sheet saves one — local and remote rows share the shape. */
  const onAliasChanged = (userId: string, alias: string | null) => {
    setData((prev) => {
      if (!prev) return prev
      const patch = <T extends { user_id: string; personal_alias?: string | null }>(rows: T[]): T[] =>
        rows.map((r) => (r.user_id === userId ? { ...r, personal_alias: alias } : r))
      return {
        ...prev,
        instance: { ...prev.instance, members: patch(prev.instance.members) },
        households: prev.households.map((h) => ({ ...h, members: patch(h.members) })),
      }
    })
  }

  /** Our own status changed in the editor — show it without a reload. */
  const onStatusSaved = (user: User) => {
    setEditingStatus(false)
    setData((prev) => prev && {
      ...prev,
      instance: {
        ...prev.instance,
        members: prev.instance.members.map((m) =>
          m.user_id === user.user_id ? { ...m, status: user.status ?? null } : m),
      },
    })
  }

  const open = (p: Person) => {
    if (p.isSelf) {
      setEditingStatus(true)
      return
    }
    openPerson(p.user_id, {
      person: {
        ...p.member,
        username: p.isLocal ? (p.member as { username: string }).username : null,
        is_local: p.isLocal,
        household: p.home,
      },
      onAliasChanged,
    })
  }

  // A failed load gets its own card with a retry — never an endless
  // spinner. The card stays up while a retry is in flight (the button
  // shows the spinner) so focus and the announcement don't jump.
  if (!data && loadFailed) {
    return (
      <div class="sh-conn sh-friends">
        <section class="sh-friends-error" role="alert">
          <span class="sh-friends-error__icon" aria-hidden="true">!</span>
          <h2>{t('friends.load_failed_title')}</h2>
          <p>{t('friends.load_failed_body')}</p>
          <Button variant="secondary" loading={loading} onClick={() => setAttempt((n) => n + 1)}>
            {t('common.try_again')}
          </Button>
        </section>
      </div>
    )
  }
  if (!data) return <Spinner />

  // Reads the block cache during render, so a block made from the person
  // sheet drops the row (and the counts) at once.
  const blockedCount = blockedUsers.value.length
  const model = buildModel(data, { selfId, locale: formatLocale(), hidden: isBlocked })
  const alone = data.households.length === 0
  const markers = markersFor(data, model)
  const withMap = markers.length > 0
  const activeView: FriendsView = view === 'map' && !withMap ? 'households' : view
  const showFind = model.peopleCount > FIND_OVER && activeView !== 'map'
  const q = showFind ? fold(query.trim()) : ''
  const filtering = q.length > 0
  const keep = (p: Person) => matches(p, q)

  const atHome = model.atHome.filter(keep)
  const totals = new Map(model.groups.map((g) => [g.household.instance_id, g.people.length]))
  const groups = model.groups
    .map((g) => ({ ...g, people: g.people.filter(keep) }))
    .filter((g) => g.people.length > 0)
  const everyone = [...atHome, ...groups.flatMap((g) => g.people)]
  const noMatch = filtering && everyone.length === 0
  const offMap = data.households
    .filter((h) => h.home_lat === null || h.home_lon === null)
    .map((h) => h.display_name)

  return (
    <div class="sh-conn sh-friends">
      {alone
        ? <FriendsEmpty isAdmin={isAdmin} />
        : <FriendsHero model={model} households={data.households} isAdmin={isAdmin} />}

      {(!alone || showFind) && (
        <div class="sh-friends-toolbar">
          {showFind && <FindBox value={query} onChange={setQuery} />}
          {!alone && <ViewSwitch value={activeView} withMap={withMap} onChange={setView} />}
        </div>
      )}
      {filtering && (
        <p class="sr-only" role="status">{plural('friends.find.results', everyone.length)}</p>
      )}

      {noMatch && (
        <p class="sh-friends-nomatch">
          {t('friends.find.none', { q: query.trim() })}{' '}
          <button type="button" class="sh-friends-link" onClick={() => setQuery('')}>
            {t('friends.find.clear')}
          </button>
        </p>
      )}

      {activeView === 'map' && (
        <section class="sh-conn-sect" aria-label={t('friends.view.map')}>
          <div class="sh-friends-map">
            <LocationMap markers={markers} height={480} />
          </div>
          {offMap.length > 0 && (
            <p class="sh-friends-foot">
              {t('friends.map.off_map', { names: joinNames(offMap, formatLocale()) })}
            </p>
          )}
        </section>
      )}

      {activeView === 'households' && atHome.length > 0 && (
        <section class="sh-conn-sect sh-friends-sect" aria-labelledby="sh-friends-athome">
          <div class="sh-conn-sect-head">
            <div class="sh-conn-sect-head__title">
              <h2 id="sh-friends-athome">{t('friends.at_home')}</h2>
              <span class="sh-conn-count" aria-hidden="true">{atHome.length}</span>
            </div>
          </div>
          <div class="sh-friends-hh sh-friends-hh--home">
            <div class="sh-friends-people">
              {atHome.map((p) => <PersonRow key={p.user_id} person={p} onOpen={open} />)}
            </div>
          </div>
        </section>
      )}

      {activeView === 'households' && (groups.length > 0 || (!filtering && model.quiet.length > 0)) && (
        <section class="sh-conn-sect sh-friends-sect" aria-labelledby="sh-friends-households">
          <div class="sh-conn-sect-head">
            <div class="sh-conn-sect-head__title">
              <h2 id="sh-friends-households">{t('friends.households')}</h2>
              <span class="sh-conn-count" aria-hidden="true">
                {filtering ? groups.length : data.households.length}
              </span>
            </div>
          </div>
          {groups.length > 0 && (
            <ul class="sh-friends-hh-list">
              {groups.map((g) => (
                <HouseholdPeople
                  key={g.household.instance_id}
                  group={g}
                  total={totals.get(g.household.instance_id) ?? g.people.length}
                  filtering={filtering}
                  onOpen={open}
                />
              ))}
            </ul>
          )}
          {!filtering && model.quiet.map((h) => (
            <p key={h.instance_id} class="sh-friends-foot" data-instance-id={h.instance_id}>
              {t('friends.not_shared', { name: h.display_name })}
              {!h.reachable && ` ${unreachableText(h.unreachable_since)}.`}
            </p>
          ))}
        </section>
      )}

      {activeView === 'people' && everyone.length > 0 && (
        <section class="sh-conn-sect" aria-label={t('friends.view.people')}>
          <div class="sh-friends-hh sh-friends-hh--az">
            {peopleByLetter(everyone, formatLocale()).map((g) => (
              <div key={g.letter} class="sh-friends-letter" role="group" aria-label={g.letter}>
                <p class="sh-conn-subgroup sh-friends-letter__head" aria-hidden="true">{g.letter}</p>
                <div class="sh-friends-people">
                  {g.people.map((p) => <PersonRow key={p.user_id} person={p} showHousehold onOpen={open} />)}
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      {blockedCount > 0 && (
        <p class="sh-friends-foot sh-friends-foot--blocked">
          {plural('friends.blocked_hidden', blockedCount)}{' '}
          <a class="sh-friends-link" href={addBase('/settings#privacy')}>{t('friends.blocked_manage')}</a>
        </p>
      )}

      <Modal open={editingStatus} onClose={() => setEditingStatus(false)} title={t('friends.status_title')}>
        {editingStatus && (
          <StatusEditor onSave={onStatusSaved} onCancel={() => setEditingStatus(false)} />
        )}
      </Modal>
      {/* Mount the pairing dialog so the hero / empty-state CTAs can open
       *  it inline, rather than routing through ``/connections``. */}
      <PairingFlow />
    </div>
  )
}
