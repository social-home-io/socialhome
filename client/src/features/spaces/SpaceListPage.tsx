import { useEffect, useState } from 'preact/hooks'
import { useTitle } from '@/store/pageTitle'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { spaces, loadSpaces } from '@/store/spaces'
import type { Space } from '@/types'
import { Spinner } from '@/components/Spinner'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'
import { openSpaceCreate } from '@/components/SpaceCreateDialog'
import { RemoteInviteInboxBanner } from '@/components/RemoteInviteInboxBanner'

/** One row in the caller's /api/me/subscriptions list. */
interface MySubscription { space_id: string; subscribed_at: string }

const subscribedIds = signal<Set<string>>(new Set())
const loading = signal(true)

async function loadAll() {
  loading.value = true
  try {
    await Promise.all([
      loadSpaces(),
      api
        .get('/api/me/subscriptions')
        .then((rawSubs) => {
          subscribedIds.value = new Set(
            ((rawSubs as { subscriptions: MySubscription[] }).subscriptions || [])
              .map(s => s.space_id),
          )
        })
        .catch(() => { /* leave set as-is */ }),
    ])
  } finally {
    loading.value = false
  }
}

function SpaceRow({
  space,
  subscribed,
  onUnsubscribe,
  busy,
}: {
  space: Space
  subscribed: boolean
  onUnsubscribe?: (s: Space) => void
  busy?: boolean
}) {
  return (
    <a
      href={`/spaces/${space.id}`}
      class={`sh-space-card sh-space-card--${space.space_type}`}
    >
      <span class="sh-space-emoji">{space.emoji || '🏠'}</span>
      <div class="sh-space-card__body">
        <strong>{space.name}</strong>
        {space.description && <p class="sh-muted">{space.description}</p>}
        <div class="sh-space-card__chips">
          <span class="sh-byline">{space.space_type}</span>
          {subscribed && (
            <span
              class="sh-subscribed-pill"
              title="You receive this space's updates (read-only)"
            >
              🔔 Subscribed
            </span>
          )}
        </div>
      </div>
      {subscribed && onUnsubscribe && (
        <button
          type="button"
          class="sh-subscribe-btn sh-subscribe-btn--on"
          disabled={busy}
          aria-label={`Unsubscribe from ${space.name}`}
          title="Stop receiving this space's updates."
          onClick={(ev) => {
            // Clicking the unsubscribe button must not navigate.
            ev.preventDefault()
            ev.stopPropagation()
            onUnsubscribe(space)
          }}
        >
          {busy
            ? <span class="sh-spinner-sm" aria-hidden="true" />
            : <><span aria-hidden="true">🔕</span> Unsubscribe</>}
        </button>
      )}
    </a>
  )
}

export default function SpaceListPage() {
  useTitle('Spaces')
  const [busyIds, setBusyIds] = useState<Set<string>>(() => new Set<string>())

  useEffect(() => {
    void loadAll()
  }, [])

  if (loading.value) return <Spinner />

  const memberSpaces = spaces.value.filter((s) => !subscribedIds.value.has(s.id))
  const subscribedSpaces = spaces.value.filter((s) => subscribedIds.value.has(s.id))

  const onUnsubscribe = async (s: Space) => {
    setBusyIds((prev) => new Set(prev).add(s.id))
    try {
      await api.delete(`/api/spaces/${s.id}/subscribe`)
      showToast(`Unsubscribed from ${s.name}`, 'info')
      await loadAll()
    } catch (exc) {
      showToast((exc as Error).message, 'error')
    } finally {
      setBusyIds((prev) => {
        const next = new Set(prev)
        next.delete(s.id)
        return next
      })
    }
  }

  return (
    <div class="sh-spaces">
      <div class="sh-page-header">
        <div class="sh-page-header__actions">
          <Button
            variant="secondary"
            onClick={() => { window.location.href = '/spaces/browse' }}
          >
            🔭 Browse spaces
          </Button>
          <Button onClick={openSpaceCreate}>+ Create space</Button>
        </div>
      </div>
      <RemoteInviteInboxBanner />
      {memberSpaces.length === 0 && subscribedSpaces.length === 0 && (
        <div class="sh-empty-state">
          <p>No spaces yet.</p>
          <p class="sh-muted">
            Create a space to share with friends and family, or{' '}
            <a href="/spaces/browse">browse public spaces</a> to subscribe.
          </p>
        </div>
      )}

      {memberSpaces.length > 0 && (
        <section class="sh-spaces-section">
          {subscribedSpaces.length > 0 && (
            <h2 class="sh-spaces-section__title">Your spaces</h2>
          )}
          {memberSpaces.map((s) => (
            <SpaceRow key={s.id} space={s} subscribed={false} />
          ))}
        </section>
      )}

      {subscribedSpaces.length > 0 && (
        <section class="sh-spaces-section">
          <h2 class="sh-spaces-section__title">
            Subscribed
            <span class="sh-muted sh-spaces-section__hint">
              · read-only — you won't be able to post
            </span>
          </h2>
          {subscribedSpaces.map((s) => (
            <SpaceRow
              key={s.id}
              space={s}
              subscribed={true}
              busy={busyIds.has(s.id)}
              onUnsubscribe={onUnsubscribe}
            />
          ))}
        </section>
      )}
    </div>
  )
}
