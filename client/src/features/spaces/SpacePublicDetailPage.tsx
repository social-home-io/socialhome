/**
 * SpacePublicDetailPage — public-style space landing on the SH side.
 *
 * Mirrors the GFS public ``/spaces/{id}`` page (see
 * ``socialhome/global_server/public.py``) but rendered inside the
 * SPA, so a household browser tap lands on a polished detail surface
 * before the user commits to joining.
 *
 * Data sources (in priority order):
 *   1. ``directoryCache`` — populated by the Spaces page's Discover on
 *      load; covers every visible scope.
 *   2. ``GET /api/spaces/{id}`` — the landing fields for a caller with no
 *      seat in a discoverable space; the full space for a seated caller
 *      (a member opening their own space, private ones included).
 *
 * The CTA flips to match the viewer's relationship:
 * already-member → "Open space"; pending request → disabled "Pending";
 * open → "Join"; request → "Request to join"; remote unpaired host →
 * "Pair {household} first".
 */
import { useEffect, useState } from 'preact/hooks'
import { signal } from '@preact/signals'
import { useLocation, useRoute } from 'preact-iso'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { LocalAreaBadge, verdictAllowsJoin } from '@/components/LocalAreaBadge'
import { LocalAreaMap } from '@/components/LocalAreaMap'
import { Spinner } from '@/components/Spinner'
import { showToast } from '@/components/Toast'
import { isOne, t } from '@/i18n/i18n'
import { useTitle } from '@/store/pageTitle'
import { ensureHouseholdHome, householdHome } from '@/store/householdHome'
import { directoryCache, getCachedEntry } from '@/store/spaceDirectory'
import type { DirectoryEntry, Space } from '@/types'
import { JoinRequestModal } from './JoinRequestModal'
import { askLocationConsent, isApiCode, locationConsentText, noHomeText } from '@/components/localConsent'
import {
  contentIsGated, discoverAction, entryAreaVerdict, entryRadiusKm, GATED_CHIP, hostLabel,
  joinCarriesLocation, joinModeChip,
  sourceOf, type DiscoverAction,
} from './discover'
import { addBase } from '@/baseUrl'
import { areaLabel, areaShortLabel, peerAreaLabel, radiusLabel, type AreaVerdict } from '@/utils/localArea'
import { distanceLabel, hintLabel, hintVerdict, pinDistanceKm } from '@/utils/placePin'
import { PIN_DOT_KM } from '@/components/LocalAreaPicker'

type AboutAction = 'open' | Exclude<DiscoverAction, null>

const ABOUT_LABEL: Record<AboutAction, () => string> = {
  open: () => t('space.public.open'),
  join: () => t('space.public.join'),
  request: () => t('space.public.request_join'),
  follow: () => t('spaces.discover.follow'),
  pending: () => t('space.public.request_pending'),
}

/** Shown when the space's owner has NOT opted into read-only followers
 *  (``SpaceFeatures.allow_subscribers`` off): the space is listed so people
 *  can find it and ask in, but none of its content is published. */
function gatedContentNote(): string {
  return t('space.public.gated_note')
}

/** …paired with one sentence on how you actually get in. Independent of
 *  readability now: every join mode has its own way in, and any of them can
 *  sit alongside a private or a public content stream. */
function howToGetIn(mode: DirectoryEntry['join_mode'], local: boolean): string {
  // A local community lets in only households in the area (near the edge,
  // an admin decides) — never "anyone".
  switch (mode) {
    case 'open':
      return local ? t('space.public.how_open_local') : t('space.public.how_open')
    case 'invite_only':
      return local ? t('space.public.how_invite_only_local') : t('space.public.how_invite_only')
    case 'request':
      return local ? t('space.public.how_request_local') : t('space.public.how_request')
  }
}

const detail = signal<DirectoryEntry | null>(null)
const loading = signal<boolean>(true)
const error = signal<string | null>(null)

/** ``/api/spaces/{id}`` returns the canonical Space wire shape — flatten
 *  it into the DirectoryEntry shape this page renders. A seated caller's
 *  answer carries ``my_role`` (a ``subscriber`` follows); a caller with no
 *  seat gets a discoverable space's landing fields. Both carry the one
 *  ``member_count`` (members, never followers). */
async function loadAsLocal(spaceId: string): Promise<DirectoryEntry | null> {
  try {
    const space = await api.get(`/api/spaces/${spaceId}`) as Space
    const seated = typeof space.my_role === 'string'
    const following = seated && space.my_role === 'subscriber'
    return {
      space_id:           space.id,
      host_instance_id:   'local',
      host_display_name:  t('spaces.browse.your_household'),
      host_is_paired:     true,
      name:               space.name,
      description:        space.description,
      emoji:              space.emoji,
      member_count:       space.member_count ?? 0,
      scope:              space.space_type,
      join_mode:          space.join_mode,
      allow_subscribers:  !!space.features?.allow_subscribers,
      min_age:            0,
      category:           space.category,
      already_member:     seated && !following,
      already_subscribed: following,
      local_area:         space.local_area ?? null,
      // A global space's place pin and radius hint (listing metadata).
      lat:                space.place?.lat ?? null,
      lon:                space.place?.lon ?? null,
      radius_km:          space.place?.radius_km ?? null,
    }
  } catch {
    return null
  }
}

/** The scope chip in the hero: which audience the space is for. */
function scopeLabel(scope: DirectoryEntry['scope']): string {
  switch (scope) {
    case 'private':
      return `🔐 ${t('space.visibility.private')}`
    case 'household':
      return `🏠 ${t('spaces.browse.your_household')}`
    case 'public':
      return `🤝 ${t('space.public.scope_public')}`
    case 'global':
      return `🌐 ${t('space.public.scope_global')}`
  }
}

/** What the area means for you — one sentence under the badge (our own
 *  community) or the rule (another household's). ``null`` when there is
 *  nothing to add. */
function areaNextStep(verdict: AreaVerdict | null, isLocal: boolean): string | null {
  switch (verdict) {
    case 'outside': return t('local_area.about.outside_next')
    case 'edge': return t('local_area.about.edge_next')
    case 'no_home': return t('local_area.about.no_home_next')
    default: return isLocal ? null : t('local_area.about.host_checks')
  }
}

export default function SpacePublicDetailPage() {
  const { params } = useRoute()
  const loc = useLocation()
  const spaceId = params.id
  const [activeModal, setActiveModal] = useState<DirectoryEntry | null>(null)

  useTitle(detail.value?.name ?? t('spaces.space_title'))
  useEffect(() => { void ensureHouseholdHome() }, [])

  useEffect(() => {
    loading.value = true
    error.value = null
    detail.value = null
    const cached = getCachedEntry(spaceId)
    if (cached) {
      detail.value = cached
      loading.value = false
      return
    }
    void loadAsLocal(spaceId)
      .then(d => {
        if (d) {
          detail.value = d
        } else {
          error.value = t('space.public.load_failed')
        }
      })
      .finally(() => { loading.value = false })
  }, [spaceId, directoryCache.value])

  const patch = (p: Partial<DirectoryEntry>) => {
    if (detail.value) detail.value = { ...detail.value, ...p }
  }

  const onPrimary = async (entry: DirectoryEntry, action: AboutAction) => {
    if (action === 'open') {
      loc.route(`/spaces/${entry.space_id}`)
      return
    }
    // ``request`` (approval-required) mode — collect a message.
    if (action === 'request') {
      setActiveModal(entry)
      return
    }
    // A local community another household hosts: the request carries our
    // approximate location — ask first, send nothing without a yes.
    const carries = joinCarriesLocation(entry)
    if (action === 'join' && carries && !await askLocationConsent(hostLabel(entry))) return
    try {
      if (action === 'follow') {
        await api.post(`/api/spaces/${entry.space_id}/subscribe`, {})
        showToast(t('spaces.browse.followed_toast', { name: entry.name }), 'success')
        patch({ already_subscribed: true })
      } else if (entry.scope === 'household' || entry.host_instance_id === 'local') {
        // Open to join here: seated at once (``joined``) — or, when the
        // space takes requests after all, the request is now pending.
        const r = await api.post(`/api/spaces/${entry.space_id}/join-requests`, {}) as { joined?: boolean } | undefined
        if (r?.joined === false) {
          showToast(t('spaces.browse.request_sent_for', { name: entry.name }), 'success')
          patch({ request_pending: true })
        } else {
          showToast(t('spaces.browse.joined_toast', { name: entry.name }), 'success')
          loc.route(`/spaces/${entry.space_id}`)
        }
      } else {
        await api.post(
          `/api/public_spaces/${entry.space_id}/join-request`,
          { host_instance_id: entry.host_instance_id, ...(carries ? { share_location: true } : {}) },
        )
        showToast(t('spaces.browse.request_sent_to', { host: hostLabel(entry) }), 'success')
        patch({ request_pending: true })
      }
    } catch (exc) {
      if (isApiCode(exc, 'JOIN_REQUEST_SENT')) {
        // A follow near the edge of our own local community went to the
        // admins as a request — the same answer as any request.
        showToast(t('spaces.browse.request_sent_for', { name: entry.name }), 'success')
        patch({ request_pending: true })
      } else {
        showToast((exc as Error).message, 'error')
      }
    }
  }

  const onSubmitJoinRequest = async (message: string) => {
    if (!activeModal) return
    const e = activeModal
    if (e.scope === 'household' || e.host_instance_id === 'local') {
      await api.post(`/api/spaces/${e.space_id}/join-requests`, { message })
    } else {
      await api.post(
        `/api/public_spaces/${e.space_id}/join-request`,
        {
          host_instance_id: e.host_instance_id,
          message,
          ...(joinCarriesLocation(e) ? { share_location: true } : {}),
        },
      )
    }
    showToast(t('spaces.browse.request_sent_for', { name: e.name }), 'success')
    if (detail.value) {
      detail.value = { ...detail.value, request_pending: true }
    }
  }

  if (loading.value) return <Spinner />
  if (error.value || !detail.value) {
    return (
      <div class="sh-space-public" role="alert">
        <a class="sh-space-public__back" href={addBase('/spaces')}
           onClick={(ev) => { ev.preventDefault(); loc.route('/spaces') }}>
          ← {t('nav.spaces')}
        </a>
        <p class="sh-error">{error.value ?? t('space.public.not_found')}</p>
      </div>
    )
  }

  const entry = detail.value
  const isLocal = entry.host_instance_id === 'local'
  // Back to the list — to the Discover chip this space came from, unless
  // the viewer is in it already.
  const back = entry.already_member ? '/spaces' : `/spaces?discover=${sourceOf(entry)}`
  // Two independent statements, two chips — same as the browser card, so
  // both surfaces tell the same story. The join-mode chip says how you get
  // in; the 🔒 chip says whether there is anything to read before you do.
  // A household space has no join mode to speak of (everyone at home is a
  // member), so it shows no join-mode chip — like its browser card.
  const radius = entryRadiusKm(entry)
  const jmode = entry.scope === 'household' ? null : joinModeChip(entry.join_mode, radius !== null)
  const joinModeLabel = jmode ? `${jmode.icon} ${jmode.label}` : null
  // How to get in, and that only members can read it, is for people
  // outside the space — a member is already in.
  const gated = !entry.already_member && contentIsGated(entry)

  // The one way in from here — or none: an invite-only space, or one on a
  // GFS whose household we never paired with (households met through a
  // GFS never talk directly, so pairing is never suggested) and that takes
  // no followers. Then a neutral line says how people do get in.
  // A follower may still ask to become a member; Follow is never offered
  // twice.
  const way = discoverAction(entry)
  // Our own local community: worked out against our own home with the
  // server's rule. Outside (or no home) offers no join that would be
  // refused. Another household's shows no verdict — its host decides.
  const verdict = entry.already_member ? null : entryAreaVerdict(entry, householdHome.value)
  const areaBlocks = verdict !== null && !verdictAllowsJoin(verdict) && way !== 'pending'
  const action: AboutAction | null = entry.already_member ? 'open'
    : areaBlocks ? null
    : way === 'follow' && entry.already_subscribed ? null
    : way
  const primaryLabel = action === null ? null : ABOUT_LABEL[action]()
  // A local community: its radius whoever hosts it — but the centre (and
  // so the map) only for a space this household hosts. A paired
  // household's centre never leaves it.
  const ownArea = isLocal ? entry.local_area ?? null : null
  // A global space's place pin: "about 3 km away" from our home, worked
  // out here. Sorting / a hint only — joining never checks it.
  const pinKm = entry.scope === 'global' ? pinDistanceKm(entry, householdHome.value) : null
  // …and the owner's radius hint, compared with our home here too.
  const place = entry.scope === 'global' && entry.lat != null && entry.lon != null
    ? { lat: entry.lat, lon: entry.lon, radius_km: entry.radius_km ?? null }
    : null
  const hint = entry.scope === 'global' ? hintVerdict(entry, householdHome.value) : null
  const joining = action === 'join' || action === 'request'

  return (
    <div class="sh-space-public">
      <a
        class="sh-space-public__back"
        href={addBase(back)}
        onClick={(ev) => { ev.preventDefault(); loc.route(back) }}
      >
        ← {t('nav.spaces')}
      </a>
      <div class={`sh-space-public__hero sh-space-public__hero--${entry.scope}`}>
        <span class="sh-space-public__emoji" aria-hidden="true">
          {entry.emoji || '🗂'}
        </span>
        <div class="sh-space-public__title">
          <h1>{entry.name}</h1>
          <p class="sh-muted">
            {t(isOne(entry.member_count) ? 'space.header.members_one' : 'space.header.members',
              { count: String(entry.member_count) })}
            {!isLocal && (
              <> · {t('space.public.hosted_by', { host: hostLabel(entry) })}</>
            )}
            {pinKm !== null && (
              <span data-testid="place-distance"> · 📍 {distanceLabel(pinKm)}</span>
            )}
          </p>
        </div>
      </div>

      <div class={`sh-space-public__accent sh-space-public__accent--${entry.scope}`} />

      <div class="sh-space-public__chips">
        <span class="sh-scope-chip">{scopeLabel(entry.scope)}</span>
        {entry.already_member && (
          <span class="sh-member-pill">✓ {t('space.public.you_are_member')}</span>
        )}
        {joinModeLabel && <span class="sh-join-mode-chip">{joinModeLabel}</span>}
        {gated && (
          <span class={GATED_CHIP.cls}>
            {GATED_CHIP.icon} {GATED_CHIP.label}
          </span>
        )}
        {entry.min_age > 0 && (
          <span class="sh-age-chip">{entry.min_age}+</span>
        )}
        {radius !== null && (
          <span class="sh-local-area-chip">🏘️ {areaShortLabel(radius)}</span>
        )}
        {entry.already_subscribed && (
          <span class="sh-subscribed-pill">🔔 {t('spaces.list.subscribed_pill')}</span>
        )}
      </div>

      {action && primaryLabel && (
        <div class="sh-space-public__cta-row">
          <Button onClick={() => void onPrimary(entry, action)} disabled={action === 'pending'}>
            {primaryLabel}
          </Button>
        </div>
      )}
      {joining && entry.scope === 'global' && (
        <p class="sh-muted sh-space-public__rules-note">{t('space.public.follow_rules')}</p>
      )}
      {!action && !entry.already_subscribed && !areaBlocks && (
        <p class="sh-muted sh-space-public__invite-note">{t('spaces.discover.invite_link')}</p>
      )}

      {radius !== null && (
        <section class="sh-space-public__section sh-space-public__local" aria-labelledby="sh-about-local">
          <h2 id="sh-about-local">🏘️ {t('local_area.about.title')}</h2>
          <p>{t('local_area.about.rule')}</p>
          <p class="sh-space-public__local-where" data-testid="local-area-where">
            {ownArea ? areaLabel(ownArea) : peerAreaLabel(radius)}
          </p>
          {verdict && (
            <p class="sh-space-public__local-verdict" role="status">
              <LocalAreaBadge verdict={verdict} />
            </p>
          )}
          {ownArea && <LocalAreaMap area={ownArea} home={householdHome.value} height={200} />}
          {!entry.already_member && areaNextStep(verdict, isLocal) && (
            <p class="sh-muted" data-testid="local-area-next">{areaNextStep(verdict, isLocal)}</p>
          )}
        </section>
      )}

      {place && (
        <section class="sh-space-public__section sh-space-public__local" aria-labelledby="sh-about-place">
          <h2 id="sh-about-place">📍 {t('place_pin.settings.title')}</h2>
          {place.radius_km !== null && (
            <p class="sh-space-public__local-where" data-testid="place-hint-where">
              {t('place_pin.hint.within', { radius: radiusLabel(place.radius_km) })}
            </p>
          )}
          {hint && (
            <p class="sh-space-public__local-verdict" role="status">
              <span class={`sh-place-hint sh-place-hint--${hint.kind}`} data-testid="place-hint">
                <span aria-hidden="true">📍</span> {hintLabel(hint)}
              </span>
            </p>
          )}
          <LocalAreaMap area={{ lat: place.lat, lon: place.lon, radius_km: place.radius_km ?? PIN_DOT_KM }}
                        home={householdHome.value} height={180}
                        zoneName={t('place_pin.map_zone')} ariaLabel={t('place_pin.map_label')} />
          {place.radius_km !== null && (
            <p class="sh-muted">{t('place_pin.hint.only_a_hint')}</p>
          )}
        </section>
      )}

      {entry.description && (
        <section class="sh-space-public__section">
          <h2>{t('space.public.about')}</h2>
          <p>{entry.description}</p>
        </section>
      )}

      {gated && (
        <section class="sh-space-public__section sh-muted">
          <p>
            {gatedContentNote()} {howToGetIn(entry.join_mode, radius !== null)}
          </p>
        </section>
      )}

      {activeModal && (
        <JoinRequestModal
          open={true}
          onClose={() => setActiveModal(null)}
          spaceName={activeModal.name}
          hostDisplayName={activeModal.host_display_name}
          hostIsPaired={activeModal.host_is_paired}
          locationNote={joinCarriesLocation(activeModal)
            ? locationConsentText(hostLabel(activeModal)) : null}
          locationBlocked={joinCarriesLocation(activeModal) && householdHome.value === null
            ? noHomeText() : null}
          onSubmit={onSubmitJoinRequest}
        />
      )}
    </div>
  )
}
