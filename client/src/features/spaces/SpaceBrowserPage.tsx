/**
 * SpaceBrowserPage — browse + join spaces across three scopes.
 *
 *   1. Your household     — local spaces (HOUSEHOLD + PUBLIC types).
 *   2. From friends       — type=public spaces published by paired
 *                           peers (§D1a SPACE_DIRECTORY_SYNC).
 *   3. Global directory   — type=global spaces on the GFS.
 *
 * Primary UX: click a SpaceCard's action button. Local "open" joins
 * instantly; "request" pops a JoinRequestModal; remote spaces route
 * the request through federation (§D2); unpaired hosts deep-link to
 * the pairing flow.
 */
import { useEffect, useState } from 'preact/hooks'
import { useLocation } from 'preact-iso'
import { useTitle } from '@/store/pageTitle'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { addBase } from '@/baseUrl'
import { Button } from '@/components/Button'
import { SpaceListSkeleton } from '@/components/Skeleton'
import { showToast } from '@/components/Toast'
import { openPairing } from '@/components/PairingFlow'
import { openSpaceCreate } from '@/components/SpaceCreateDialog'
import { currentUser } from '@/store/auth'
import { cacheDirectoryEntries } from '@/store/spaceDirectory'
import type { DirectoryEntry, Space } from '@/types'
import { SpaceCard } from './SpaceCard'
import { JoinRequestModal } from './JoinRequestModal'

type Tab = 'household' | 'friends' | 'global'

const household = signal<DirectoryEntry[]>([])
const friends = signal<DirectoryEntry[]>([])
const global_ = signal<DirectoryEntry[]>([])
const loading = signal(true)
const activeTab = signal<Tab>('household')
const searchTerm = signal('')

interface MySubscription { space_id: string; subscribed_at: string }

async function loadAll() {
  loading.value = true
  try {
    const [rawLocal, rawFriends, rawGlobal, rawMine, rawSubs, rawPending] =
      await Promise.all([
        api.get('/api/spaces').catch(() => [] as Space[]),
        api.get('/api/peer_spaces').catch(() => [] as DirectoryEntry[]),
        api.get('/api/public_spaces').catch(() => [] as DirectoryEntry[]),
        api.get('/api/spaces').catch(() => [] as Space[]),
        api
          .get('/api/me/subscriptions')
          .catch(() => ({ subscriptions: [] as MySubscription[] })),
        api
          .get('/api/me/join-requests')
          .catch(() => ({ pending_space_ids: [] as string[] })),
      ])
    const myIds = new Set((rawMine as Space[]).map((s) => s.id))
    const subIds = new Set(
      ((rawSubs as { subscriptions: MySubscription[] }).subscriptions || []).map(
        (s) => s.space_id,
      ),
    )
    // Persisted "Request pending": the caller's own outstanding
    // join-requests, so a sent request survives this reload (the
    // optimistic in-memory flag set in onAction is otherwise lost on
    // the next mount). Mirrors the subscriptions merge above.
    const pendingIds = new Set(
      ((rawPending as { pending_space_ids: string[] }).pending_space_ids || []),
    )
    // Subscribers ARE members (role='subscriber') on the server — the
    // host puts them in space_members. Filter subscribed-only ids OUT
    // of "your memberships" so the card flips from "Open space" to
    // "🔔 Subscribed + Subscribe button toggle". Real members keep
    // already_member=true.
    const realMemberIds = new Set(
      [...myIds].filter((id) => !subIds.has(id)),
    )
    household.value = (rawLocal as Space[])
      .filter((s) => s.space_type === 'household' || s.space_type === 'public')
      .map((s) => ({
        space_id:           s.id,
        host_instance_id:   'local',
        host_display_name:  'Your household',
        host_is_paired:     true,
        name:               s.name,
        description:        s.description,
        emoji:              s.emoji,
        member_count:       0,
        scope:              s.space_type as 'household' | 'public',
        join_mode:          s.join_mode,
        // Readability lives on the space's features, not on its join mode.
        allow_subscribers:  !!s.features?.allow_subscribers,
        min_age:            0,
        category:           s.category,
        already_member:     realMemberIds.has(s.id),
        already_subscribed: subIds.has(s.id),
        request_pending:    pendingIds.has(s.id),
      }))
    friends.value = (rawFriends as DirectoryEntry[]).map((e) => ({
      ...e,
      scope:              'public' as const,
      already_subscribed: subIds.has(e.space_id),
      already_member:     realMemberIds.has(e.space_id),
      request_pending:    pendingIds.has(e.space_id),
    }))
    global_.value = (rawGlobal as DirectoryEntry[]).map((e) => ({
      ...e,
      scope: 'global' as const,
      // /api/public_spaces now carries the host's real join_mode (it used to
      // omit it, and this defaulted to 'request' — which advertised a join
      // path that may not exist). Fail closed if an older backend omits it.
      join_mode:          e.join_mode || 'invite_only',
      // …and its readability opt-in, which is what decides whether Subscribe
      // is offered at all. Absent ⇒ false ⇒ no Subscribe button, rather than
      // one that 403s.
      allow_subscribers:  !!e.allow_subscribers,
      already_subscribed: subIds.has(e.space_id),
      already_member:     realMemberIds.has(e.space_id),
      request_pending:    pendingIds.has(e.space_id),
    }))
    // Stash every directory entry so SpacePublicDetailPage can render
    // immediately when the user taps a card — no second round-trip
    // for data we just loaded.
    cacheDirectoryEntries([
      ...household.value, ...friends.value, ...global_.value,
    ])
  } finally {
    loading.value = false
  }
}

function filterBy(entries: DirectoryEntry[], term: string): DirectoryEntry[] {
  if (!term) return entries
  const t = term.toLowerCase()
  return entries.filter((e) =>
    e.name.toLowerCase().includes(t)
    || (e.description || '').toLowerCase().includes(t),
  )
}

export default function SpaceBrowserPage() {
  useTitle('Browse spaces')
  const loc = useLocation()
  const [activeModal, setActiveModal] = useState<DirectoryEntry | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  const [subscribeBusyIds, setSubscribeBusyIds] = useState<Set<string>>(
    () => new Set<string>(),
  )

  useEffect(() => {
    loadAll()
  }, [])

  const markSubscribeBusy = (space_id: string, busy: boolean) => {
    setSubscribeBusyIds((prev) => {
      const next = new Set(prev)
      if (busy) next.add(space_id)
      else next.delete(space_id)
      return next
    })
  }

  /** Flip the cached entries so the card re-renders with the new
   *  subscribe state without a full reload. Reload happens after the
   *  request resolves so counts stay accurate. */
  const patchEntry = (
    space_id: string,
    patch: Partial<DirectoryEntry>,
  ) => {
    for (const bucket of [household, friends, global_]) {
      const next = bucket.value.map((e) =>
        e.space_id === space_id ? { ...e, ...patch } : e,
      )
      if (next !== bucket.value) bucket.value = next
    }
  }

  const onAction = async (entry: DirectoryEntry, action: { kind: string }) => {
    if (action.kind === 'open') {
      window.location.href = addBase(`/spaces/${entry.space_id}`)
      return
    }
    if (action.kind === 'pair-first') {
      // Open the pairing flow pre-targeted at this household.
      openPairing('household')
      return
    }
    if (action.kind === 'subscribe' || action.kind === 'unsubscribe') {
      const subscribing = action.kind === 'subscribe'
      markSubscribeBusy(entry.space_id, true)
      // Optimistic: flip immediately so the UI feels instant.
      patchEntry(entry.space_id, { already_subscribed: subscribing })
      try {
        if (subscribing) {
          await api.post(`/api/spaces/${entry.space_id}/subscribe`, {})
          showToast(`Subscribed to ${entry.name}`, 'success')
        } else {
          await api.delete(`/api/spaces/${entry.space_id}/subscribe`)
          showToast(`Unsubscribed from ${entry.name}`, 'info')
        }
      } catch (exc) {
        // Revert optimism on failure.
        patchEntry(entry.space_id, { already_subscribed: !subscribing })
        showToast((exc as Error).message, 'error')
      } finally {
        markSubscribeBusy(entry.space_id, false)
      }
      return
    }
    if (action.kind === 'join') {
      if (entry.scope === 'household' || entry.host_instance_id === 'local') {
        try {
          // Local open-to-join: use the join-request endpoint; the
          // server auto-approves for JoinMode.OPEN via existing flow.
          await api.post(
            `/api/spaces/${entry.space_id}/join-requests`, {},
          )
          showToast(`Joined ${entry.name}`, 'success')
          await loadAll()
        } catch (exc) {
          showToast((exc as Error).message, 'error')
        }
      } else {
        try {
          await api.post(
            `/api/public_spaces/${entry.space_id}/join-request`,
            { host_instance_id: entry.host_instance_id },
          )
          showToast(
            `Request sent to ${entry.host_display_name}`, 'success',
          )
          entry.request_pending = true
          // Trigger reactivity.
          global_.value = [...global_.value]
          friends.value = [...friends.value]
        } catch (exc) {
          showToast((exc as Error).message, 'error')
        }
      }
      return
    }
    if (action.kind === 'request') {
      setActiveModal(entry)
    }
  }

  const onSubmitJoinRequest = async (message: string) => {
    if (!activeModal) return
    const e = activeModal
    if (e.scope === 'household' || e.host_instance_id === 'local') {
      await api.post(
        `/api/spaces/${e.space_id}/join-requests`, { message },
      )
      showToast(`Request sent for ${e.name}`, 'success')
    } else {
      await api.post(
        `/api/public_spaces/${e.space_id}/join-request`,
        { host_instance_id: e.host_instance_id, message },
      )
      showToast(`Request sent to ${e.host_display_name}`, 'success')
    }
    e.request_pending = true
    global_.value = [...global_.value]
    friends.value = [...friends.value]
    household.value = [...household.value]
  }

  const onRefreshGfs = async () => {
    setRefreshing(true)
    try {
      await api.post('/api/public_spaces/refresh', {})
      showToast('Directory refresh requested', 'info')
      // Wait a short beat before reloading so the refreshed poll lands.
      setTimeout(() => { void loadAll() }, 2000)
    } catch (exc) {
      showToast((exc as Error).message, 'error')
    } finally {
      setRefreshing(false)
    }
  }

  if (loading.value) return <SpaceListSkeleton count={6} />

  const term = searchTerm.value
  const searching = term.trim().length > 0
  const lists: Record<Tab, DirectoryEntry[]> = {
    household: filterBy(household.value, term),
    friends:   filterBy(friends.value,  term),
    global:    filterBy(global_.value,  term),
  }
  const totals: Record<Tab, number> = {
    household: household.value.length,
    friends:   friends.value.length,
    global:    global_.value.length,
  }
  const me = currentUser.value
  const canRefresh = me?.is_admin === true

  // Empty-state copy depends on whether the bucket is genuinely empty
  // or just filtered out by the search box.  Mixing the two confused
  // testers — they'd see "No global spaces found" while typing a typo
  // that hid 30 spaces, and reach for refresh instead of clearing the
  // search.
  const emptyCopy = (tab: Tab): { lead: string; hint?: string } => {
    if (searching) {
      return {
        lead: `No matches for "${term}" in ${
          tab === 'global' ? 'the global directory'
            : tab === 'friends' ? 'your friends'
            : 'your household'
        }.`,
        hint: 'Clear the search or pick a different tab.',
      }
    }
    if (tab === 'global') {
      return {
        lead: 'No global spaces yet.',
        hint: canRefresh
          ? 'Try refreshing the directory above.'
          : 'Ask a household admin to refresh the global directory.',
      }
    }
    if (tab === 'friends') {
      return {
        lead: 'None of your paired households have shared a public space yet.',
        hint: 'Pair with another household from Settings → Connections to start sharing.',
      }
    }
    return {
      lead: 'No spaces in your household yet.',
      hint: 'Create one to get started.',
    }
  }

  return (
    <div class="sh-space-browser">
      <header class="sh-browser-header">
        <div class="sh-browser-header__copy">
          <h1>Browse spaces</h1>
          <p class="sh-muted">
            Spaces are shared corners — a school-run, a holiday crew, a
            neighbourhood watch.  Browse what's already here, or
            create your own.
          </p>
        </div>
        <div class="sh-browser-header__actions">
          {canRefresh && (
            <Button
              variant="secondary" loading={refreshing}
              onClick={onRefreshGfs}
            >
              ⟳ Refresh global directory
            </Button>
          )}
          <Button onClick={openSpaceCreate}>+ Create space</Button>
        </div>
      </header>

      <div class="sh-browser-toolbar">
        <input
          class="sh-input"
          type="search"
          placeholder="Search by name or description…"
          value={term}
          onInput={(e) => {
            searchTerm.value = (e.target as HTMLInputElement).value
          }}
        />
        {searching && (
          <button
            type="button"
            class="sh-browser-toolbar__clear"
            onClick={() => { searchTerm.value = '' }}
          >
            Clear
          </button>
        )}
      </div>

      <nav class="sh-browser-tabs" role="tablist">
        <button
          role="tab"
          aria-selected={activeTab.value === 'household'}
          class={
            'sh-browser-tab'
            + (activeTab.value === 'household' ? ' sh-browser-tab--active' : '')
          }
          onClick={() => { activeTab.value = 'household' }}
        >
          🏠 Your household ({searching
            ? `${lists.household.length}/${totals.household}`
            : totals.household})
        </button>
        <button
          role="tab"
          aria-selected={activeTab.value === 'friends'}
          class={
            'sh-browser-tab'
            + (activeTab.value === 'friends' ? ' sh-browser-tab--active' : '')
          }
          onClick={() => { activeTab.value = 'friends' }}
        >
          🤝 From friends ({searching
            ? `${lists.friends.length}/${totals.friends}`
            : totals.friends})
        </button>
        <button
          role="tab"
          aria-selected={activeTab.value === 'global'}
          class={
            'sh-browser-tab'
            + (activeTab.value === 'global' ? ' sh-browser-tab--active' : '')
          }
          onClick={() => { activeTab.value = 'global' }}
        >
          🌐 Global ({searching
            ? `${lists.global.length}/${totals.global}`
            : totals.global})
        </button>
      </nav>

      <section class="sh-browser-grid" role="tabpanel">
        {lists[activeTab.value].length === 0 && (() => {
          const { lead, hint } = emptyCopy(activeTab.value)
          return (
            <div class="sh-browser-empty">
              <p class="sh-muted">{lead}</p>
              {hint && <p class="sh-muted">{hint}</p>}
              {!searching && activeTab.value === 'household' && (
                <Button onClick={openSpaceCreate}>+ Create your first space</Button>
              )}
              {!searching && activeTab.value === 'friends' && (
                <Button
                  variant="secondary"
                  onClick={() => openPairing('household')}
                >
                  🤝 Pair with another household
                </Button>
              )}
            </div>
          )
        })()}
        {lists[activeTab.value].map((e) => (
          <SpaceCard
            key={e.space_id}
            entry={e}
            onAction={onAction}
            subscribeBusy={subscribeBusyIds.has(e.space_id)}
            onCardTap={(picked) => loc.route(`/spaces/${picked.space_id}/about`)}
          />
        ))}
      </section>

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
