import { useEffect, useState } from 'preact/hooks'
import { useTitle } from '@/store/pageTitle'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { addBase } from '@/baseUrl'
import { spaces, loadSpaces } from '@/store/spaces'
import type { Space } from '@/types'
import { SpaceListSkeleton } from '@/components/Skeleton'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'
import { instanceConfig } from '@/store/instance'
import { openSpaceCreate } from '@/components/SpaceCreateDialog'
import { RemoteInviteInboxBanner } from '@/components/RemoteInviteInboxBanner'
import { SideNavIcon } from '@/components/SideNavIcon'
import { openSpaceJoinByCode } from './SpaceJoinByCodeDialog'
import { isOne, t } from '@/i18n/i18n'
import { connectionState, ws } from '@/ws'
import { currentUser } from '@/store/auth'
import {
  applySpaceChatUnreadFrame,
  loadSpaceChatUnread,
  spaceChatDeleteNeedsReload,
  spaceChatUnreadOf,
} from '@/store/spaceChat'

/** One row in the caller's /api/me/subscriptions list. */
interface MySubscription { space_id: string; subscribed_at: string }

/** The space's kind ("private", "public", …) in the UI language; an
 *  unknown kind from a newer server shows as sent. */
function spaceTypeLabel(kind: string): string {
  const key = `space.visibility.${kind}`
  const label = t(key)
  return label === key ? kind : label
}

const subscribedIds = signal<Set<string>>(new Set())
const loading = signal(true)

async function loadAll() {
  loading.value = true
  try {
    await Promise.all([
      loadSpaces(),
      loadSpaceChatUnread(),
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

/** The small "unread in chat" pill on a space row — the space chat's
 *  honest count (muted → none, "Only @mentions" → mentions only). */
function ChatUnreadPill({ count }: { count: number }) {
  if (count <= 0) return null
  const label = t(isOne(count) ? 'spaces.list.chat_unread_one' : 'spaces.list.chat_unread', {
    n: String(count),
  })
  return (
    <>
      <span class="sh-space-card__chat-unread" title={label} aria-hidden="true">
        <SideNavIcon name="messages" />
        {count > 99 ? '99+' : count}
      </span>
      <span class="sr-only">{label}</span>
    </>
  )
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
      href={addBase(`/spaces/${space.id}`)}
      class={`sh-space-card sh-space-card--${space.space_type}`}
    >
      <span class="sh-space-emoji">{space.emoji || '🏠'}</span>
      <div class="sh-space-card__body">
        <div class="sh-space-card__title">
          <strong>{space.name}</strong>
          <ChatUnreadPill count={spaceChatUnreadOf(space.id)} />
        </div>
        {space.description && <p class="sh-muted">{space.description}</p>}
        <div class="sh-space-card__chips">
          <span class="sh-byline">{spaceTypeLabel(space.space_type)}</span>
          {subscribed && (
            <span
              class="sh-subscribed-pill"
              title={t('spaces.list.subscribed_pill_title')}
            >
              🔔 {t('spaces.list.subscribed_pill')}
            </span>
          )}
          {space.owner_instance_id
            && instanceConfig.value?.instance_id
            && space.owner_instance_id !== instanceConfig.value.instance_id && (
              <span
                class="sh-space-remote-chip"
                title={t('spaces.list.remote_title')}
              >
                🏘 {t('spaces.list.remote_chip')}
              </span>
            )}
        </div>
      </div>
      {subscribed && onUnsubscribe && (
        <button
          type="button"
          class="sh-subscribe-btn sh-subscribe-btn--on"
          disabled={busy}
          aria-label={t('spaces.list.unsubscribe_aria', { name: space.name })}
          title={t('space.subscriber.unsubscribe_title')}
          onClick={(ev) => {
            // Clicking the unsubscribe button must not navigate.
            ev.preventDefault()
            ev.stopPropagation()
            onUnsubscribe(space)
          }}
        >
          {busy
            ? <span class="sh-spinner-sm" aria-hidden="true" />
            : <><span aria-hidden="true">🔕</span> {t('space.subscriber.unsubscribe')}</>}
        </button>
      )}
    </a>
  )
}

export default function SpaceListPage() {
  useTitle(t('nav.spaces'))
  const [busyIds, setBusyIds] = useState<Set<string>>(() => new Set<string>())

  useEffect(() => {
    void loadAll()
  }, [])

  // Live chat activity: bump a space's dot by the same rules as its
  // Chat switch; a chat the list has no row for yet (just created by
  // its first message) re-reads the counts, as does a deleted message in
  // a chat that shows a count and a WS reconnect (frames missed while
  // the socket was down). Re-reads are coalesced in the store.
  useEffect(() => {
    const offMsg = ws.on('dm.message', (e) => {
      const res = applySpaceChatUnreadFrame(e.data, currentUser.value?.user_id)
      if (res === 'unknown') void loadSpaceChatUnread()
    })
    const offDel = ws.on('dm.message_deleted', (e) => {
      if (spaceChatDeleteNeedsReload(e.data)) void loadSpaceChatUnread()
    })
    let prevConn = connectionState.value
    const offConn = connectionState.subscribe((next) => {
      if (prevConn === 'reconnecting' && next === 'open') void loadSpaceChatUnread()
      prevConn = next
    })
    return () => { offMsg(); offDel(); offConn() }
  }, [])

  if (loading.value) return <SpaceListSkeleton />

  const memberSpaces = spaces.value.filter((s) => !subscribedIds.value.has(s.id))
  const subscribedSpaces = spaces.value.filter((s) => subscribedIds.value.has(s.id))

  const onUnsubscribe = async (s: Space) => {
    setBusyIds((prev) => new Set(prev).add(s.id))
    try {
      await api.delete(`/api/spaces/${s.id}/subscribe`)
      showToast(t('spaces.list.unsubscribed', { name: s.name }), 'info')
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
            onClick={() => { window.location.href = addBase('/spaces/browse') }}
          >
            🔭 {t('spaces.list.browse')}
          </Button>
          <Button variant="secondary" onClick={openSpaceJoinByCode}>
            🎟 {t('spaces.list.join_code')}
          </Button>
          <Button onClick={openSpaceCreate}>+ {t('spaces.list.create')}</Button>
        </div>
      </div>
      <RemoteInviteInboxBanner />
      {memberSpaces.length === 0 && subscribedSpaces.length === 0 && (
        <div class="sh-empty-state">
          <div aria-hidden="true">🏘️</div>
          <h3>{t('spaces.list.empty_title')}</h3>
          <p>{t('spaces.list.empty_body')}</p>
          <div class="sh-empty-state__cta-row">
            <Button onClick={openSpaceCreate}>
              + {t('spaces.list.create_first')}
            </Button>
            <Button
              variant="secondary"
              onClick={() => { window.location.href = addBase('/spaces/browse') }}
            >
              🔭 {t('spaces.list.browse_public')}
            </Button>
          </div>
        </div>
      )}

      {memberSpaces.length > 0 && (
        <section class="sh-spaces-section">
          {subscribedSpaces.length > 0 && (
            <h2 class="sh-spaces-section__title">{t('spaces.list.yours')}</h2>
          )}
          {memberSpaces.map((s) => (
            <SpaceRow key={s.id} space={s} subscribed={false} />
          ))}
        </section>
      )}

      {subscribedSpaces.length > 0 && (
        <section class="sh-spaces-section">
          <h2 class="sh-spaces-section__title">
            {t('spaces.list.subscribed')}
            <span class="sh-muted sh-spaces-section__hint">
              {' · '}{t('spaces.list.subscribed_hint')}
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
