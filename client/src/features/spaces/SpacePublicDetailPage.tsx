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
import { Spinner } from '@/components/Spinner'
import { showToast } from '@/components/Toast'
import { isOne, t } from '@/i18n/i18n'
import { useTitle } from '@/store/pageTitle'
import { directoryCache, getCachedEntry } from '@/store/spaceDirectory'
import type { DirectoryEntry, Space } from '@/types'
import { JoinRequestModal } from './JoinRequestModal'
import {
  contentIsGated, discoverAction, GATED_CHIP, hostLabel, joinModeChip, sourceOf, type DiscoverAction,
} from './discover'
import { addBase } from '@/baseUrl'

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
function howToGetIn(mode: DirectoryEntry['join_mode']): string {
  switch (mode) {
    case 'open':
      return t('space.public.how_open')
    case 'invite_only':
      return t('space.public.how_invite_only')
    case 'request':
      return t('space.public.how_request')
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

export default function SpacePublicDetailPage() {
  const { params } = useRoute()
  const loc = useLocation()
  const spaceId = params.id
  const [activeModal, setActiveModal] = useState<DirectoryEntry | null>(null)

  useTitle(detail.value?.name ?? t('spaces.space_title'))

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
          { host_instance_id: entry.host_instance_id },
        )
        showToast(t('spaces.browse.request_sent_to', { host: hostLabel(entry) }), 'success')
        patch({ request_pending: true })
      }
    } catch (exc) {
      showToast((exc as Error).message, 'error')
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
        { host_instance_id: e.host_instance_id, message },
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
  const jmode = entry.scope === 'household' ? null : joinModeChip(entry.join_mode)
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
  const action: AboutAction | null = entry.already_member ? 'open'
    : way === 'follow' && entry.already_subscribed ? null
    : way
  const primaryLabel = action === null ? null : ABOUT_LABEL[action]()

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
      {!action && !entry.already_subscribed && (
        <p class="sh-muted sh-space-public__invite-note">{t('spaces.discover.invite_link')}</p>
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
            {gatedContentNote()} {howToGetIn(entry.join_mode)}
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
          onSubmit={onSubmitJoinRequest}
        />
      )}
    </div>
  )
}
