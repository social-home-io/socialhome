/**
 * SpacePublicDetailPage — public-style space landing on the SH side.
 *
 * Mirrors the GFS public ``/spaces/{id}`` page (see
 * ``socialhome/global_server/public.py``) but rendered inside the
 * SPA, so a household browser tap lands on a polished detail surface
 * before the user commits to joining.
 *
 * Data sources (in priority order):
 *   1. ``directoryCache`` — populated by :class:`SpaceBrowserPage` on
 *      load; covers every visible scope.
 *   2. ``GET /api/spaces/{id}`` — works for any authed user, returns
 *      public-shaped fields for local + already-known spaces.
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
import { useTitle } from '@/store/pageTitle'
import { directoryCache, getCachedEntry } from '@/store/spaceDirectory'
import type { DirectoryEntry, Space } from '@/types'
import { JoinRequestModal } from './JoinRequestModal'
import { contentIsGated, GATED_CHIP, joinModeChip } from './SpaceCard'

/** Shown when the space's owner has NOT opted into read-only followers
 *  (``SpaceFeatures.allow_subscribers`` off): the space is listed so people
 *  can find it and ask in, but none of its content is published. */
const GATED_CONTENT_NOTE
  = 'Only members can read this space. Nothing posted here is published '
  + 'to the directory.'

/** …paired with one sentence on how you actually get in. Independent of
 *  readability now: every join mode has its own way in, and any of them can
 *  sit alongside a private or a public content stream. */
function howToGetIn(mode: DirectoryEntry['join_mode']): string {
  switch (mode) {
    case 'open':
      return 'Anyone can join this space and start posting.'
    case 'invite_only':
      return 'A member has to invite you before you can join.'
    case 'request':
      return 'You can ask to join — a member decides, and you can post '
        + 'once they let you in.'
  }
}

const detail = signal<DirectoryEntry | null>(null)
const loading = signal<boolean>(true)
const error = signal<string | null>(null)

/** ``/api/spaces/{id}`` returns the canonical Space wire shape — flatten
 *  it into the DirectoryEntry shape this page renders. Member count is
 *  fetched on the side because Space doesn't include it. */
async function loadAsLocal(spaceId: string): Promise<DirectoryEntry | null> {
  try {
    const space = await api.get(`/api/spaces/${spaceId}`) as Space
    let memberCount = 0
    try {
      const members = await api.get(
        `/api/spaces/${spaceId}/members`,
      ) as Array<{ user_id: string }>
      memberCount = members.length
    } catch { /* visibility-gated; leave at 0 */ }
    return {
      space_id:           space.id,
      host_instance_id:   'local',
      host_display_name:  'Your household',
      host_is_paired:     true,
      name:               space.name,
      description:        space.description,
      emoji:              space.emoji,
      member_count:       memberCount,
      scope:              space.space_type as 'household' | 'public',
      join_mode:          space.join_mode,
      allow_subscribers:  !!space.features?.allow_subscribers,
      min_age:            0,
      category:           space.category,
      already_member:     false,
      already_subscribed: false,
    }
  } catch {
    return null
  }
}

export default function SpacePublicDetailPage() {
  const { params } = useRoute()
  const loc = useLocation()
  const spaceId = params.id
  const [activeModal, setActiveModal] = useState<DirectoryEntry | null>(null)

  useTitle(detail.value?.name ?? 'Space')

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
          error.value =
            "Couldn't load this space. It may not be public, or you may need "
            + "to open Browse spaces first to refresh the directory."
        }
      })
      .finally(() => { loading.value = false })
  }, [spaceId, directoryCache.value])

  const onPrimary = async (entry: DirectoryEntry) => {
    if (entry.already_member) {
      loc.route(`/spaces/${entry.space_id}`)
      return
    }
    const isLocalEntry =
      entry.scope === 'household' || entry.host_instance_id === 'local'
    // Remote (public peer or global GFS) — needs a paired host before
    // either the open send or the request modal can reach it.
    if (!isLocalEntry && !entry.host_is_paired) {
      showToast(`Pair with ${entry.host_display_name} first.`, 'info')
      return
    }
    // Open spaces join immediately with no message — match the browser's
    // ``onAction`` open branch (no JoinRequestModal). The modal is only
    // for ``request`` mode, where a message is appropriate.
    if (entry.join_mode === 'open') {
      try {
        if (isLocalEntry) {
          // Local open-to-join: the server auto-approves JoinMode.OPEN
          // through the join-request endpoint.
          await api.post(`/api/spaces/${entry.space_id}/join-requests`, {})
          showToast(`Joined ${entry.name}`, 'success')
          loc.route(`/spaces/${entry.space_id}`)
        } else {
          await api.post(
            `/api/public_spaces/${entry.space_id}/join-request`,
            { host_instance_id: entry.host_instance_id },
          )
          showToast(`Request sent to ${entry.host_display_name}`, 'success')
          if (detail.value) {
            detail.value = { ...detail.value, request_pending: true }
          }
        }
      } catch (exc) {
        showToast((exc as Error).message, 'error')
      }
      return
    }
    // ``request`` (approval-required) mode — collect a message.
    setActiveModal(entry)
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
    showToast(`Request sent for ${e.name}`, 'success')
    if (detail.value) {
      detail.value = { ...detail.value, request_pending: true }
    }
  }

  if (loading.value) return <Spinner />
  if (error.value || !detail.value) {
    return (
      <div class="sh-space-public" role="alert">
        <a class="sh-space-public__back" href="/spaces/browse"
           onClick={(ev) => { ev.preventDefault(); loc.route('/spaces/browse') }}>
          ← Browse spaces
        </a>
        <p class="sh-error">{error.value ?? 'Space not found.'}</p>
      </div>
    )
  }

  const entry = detail.value
  const isLocal = entry.host_instance_id === 'local'
  const scopeLabel =
    entry.scope === 'household' ? '🏠 Your household'
      : entry.scope === 'public' ? '🤝 Public'
      : '🌐 Global'
  // Two independent statements, two chips — same as the browser card, so
  // both surfaces tell the same story. The join-mode chip says how you get
  // in; the 🔒 chip says whether there is anything to read before you do.
  const jmode = joinModeChip(entry.join_mode)
  const joinModeLabel = `${jmode.icon} ${jmode.label}`
  const gated = contentIsGated(entry)

  const primaryLabel =
    entry.already_member ? 'Open space'
      : entry.request_pending ? 'Request pending'
      : entry.join_mode === 'invite_only'
        ? 'Invite required'
      : entry.scope !== 'household' && !entry.host_is_paired
        ? `Pair with ${entry.host_display_name} first`
      : entry.join_mode === 'open' ? 'Join space'
      : 'Request to join'

  const primaryDisabled =
    entry.request_pending
    || entry.join_mode === 'invite_only'
    || (entry.scope !== 'household' && !entry.host_is_paired)

  return (
    <div class="sh-space-public">
      <a
        class="sh-space-public__back"
        href="/spaces/browse"
        onClick={(ev) => { ev.preventDefault(); loc.route('/spaces/browse') }}
      >
        ← Browse spaces
      </a>
      <div class={`sh-space-public__hero sh-space-public__hero--${entry.scope}`}>
        <span class="sh-space-public__emoji" aria-hidden="true">
          {entry.emoji || '🗂'}
        </span>
        <div class="sh-space-public__title">
          <h1>{entry.name}</h1>
          <p class="sh-muted">
            {entry.member_count} {entry.member_count === 1 ? 'member' : 'members'}
            {!isLocal && (
              <> · hosted by <strong>{entry.host_display_name}</strong></>
            )}
          </p>
        </div>
      </div>

      <div class={`sh-space-public__accent sh-space-public__accent--${entry.scope}`} />

      <div class="sh-space-public__chips">
        <span class="sh-scope-chip">{scopeLabel}</span>
        <span class="sh-join-mode-chip">{joinModeLabel}</span>
        {gated && (
          <span class={GATED_CHIP.cls}>
            {GATED_CHIP.icon} {GATED_CHIP.label}
          </span>
        )}
        {entry.min_age > 0 && (
          <span class="sh-age-chip">{entry.min_age}+</span>
        )}
        {entry.already_subscribed && (
          <span class="sh-subscribed-pill">🔔 Subscribed</span>
        )}
      </div>

      <div class="sh-space-public__cta-row">
        <Button onClick={() => void onPrimary(entry)} disabled={primaryDisabled}>
          {primaryLabel}
        </Button>
      </div>

      {entry.description && (
        <section class="sh-space-public__section">
          <h2>About</h2>
          <p>{entry.description}</p>
        </section>
      )}

      {gated && (
        <section class="sh-space-public__section sh-muted">
          <p>
            {GATED_CONTENT_NOTE} {howToGetIn(entry.join_mode)}
          </p>
        </section>
      )}

      {entry.scope !== 'household' && !entry.host_is_paired && (
        <section class="sh-space-public__section sh-muted">
          <p>
            This space lives on another household.  Pair with
            <strong> {entry.host_display_name}</strong> from
            Settings → Connections to join, post, or read.
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
