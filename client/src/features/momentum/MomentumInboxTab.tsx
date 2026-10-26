/**
 * MomentumInboxTab — the Moments list (§Momentum, UX r3 PR 8).
 *
 * One list of top-level moments (``GET /api/moments?top_level=1``): each
 * thread once, under its root, with its newest reply nested — replies
 * never eat the page (B13). ``Load older`` pages with the ``before``
 * cursor (B14). A hashtag filter (``/momentum?tag=…``, the host's chip)
 * goes to the server with the list.
 *
 * On top, the composer entry with the honest reach sub-line — where a
 * moment goes, narrower for a protected account (its moments reach its
 * household and paired households only). States: skeleton, error card
 * with Try again, stale notice after a failed refresh, empty. At the end,
 * what "everything" means: the last 24 hours, and moments from people
 * you follow kept as long as their authors chose (1–30 days, per author).
 *
 * ``moment.*`` WS frames refresh the pages already loaded.
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { Avatar } from '@/components/Avatar'
import { Button } from '@/components/Button'
import { openMomentumComposer } from '@/components/MomentumComposerDialog'
import { MomentumInboxSkeleton } from '@/components/Skeleton'
import { LoadErrorState, StaleNotice } from '@/components/LoadErrorState'
import { INITIAL_LOAD, clockTime, loadFailed, loadSucceeded, type LoadStatus } from '@/utils/loadStatus'
import { blockedUserIds, loadBlocks } from '@/store/blocks'
import { householdDisplayName, householdPictureUrl, loadHouseholdUsers } from '@/store/householdUsers'
import { currentUser } from '@/store/auth'
import { ws } from '@/ws'
import type { Moment } from '@/types'
import { t } from '@/i18n/i18n'
import { MomentCard } from './MomentCard'

/** Moments per page. */
export const INBOX_PAGE = 30
/** The server's page cap — a refresh re-reads at most this many. */
const MAX_PAGE = 100

const moments = signal<Moment[]>([])
const loading = signal<boolean>(true)
const hasMore = signal<boolean>(false)
const loadingOlder = signal<boolean>(false)
/** A failed load is never "No moments yet" (``utils/loadStatus``). */
const status = signal<LoadStatus>(INITIAL_LOAD)
/** The filter the loaded list belongs to. */
let listTag: string | null = null

function params(tag: string | null, limit: number, before?: string): Record<string, string> {
  const p: Record<string, string> = { top_level: '1', limit: String(limit) }
  if (tag) p.tag = tag
  if (before) p.before = before
  return p
}

/** First page, or — on a refresh — every page loaded so far (up to the
 *  server's cap), so a new moment doesn't drop what "Load older" added. */
function fetchFirst(tag: string | null, refresh: boolean): Promise<void> {
  const limit = refresh ? Math.min(MAX_PAGE, Math.max(INBOX_PAGE, moments.value.length)) : INBOX_PAGE
  return api.get<Moment[]>('/api/moments', params(tag, limit))
    .then(rows => {
      if (tag !== listTag) return
      const list = rows ?? []
      moments.value = list
      hasMore.value = list.length >= limit
      status.value = loadSucceeded(status.value)
    })
    .catch(() => { if (tag === listTag) status.value = loadFailed(status.value) })
    .finally(() => { if (tag === listTag) loading.value = false })
}

async function loadOlder(tag: string | null): Promise<void> {
  const last = moments.value[moments.value.length - 1]
  if (!last || loadingOlder.value) return
  loadingOlder.value = true
  try {
    const rows = await api.get<Moment[]>('/api/moments', params(tag, INBOX_PAGE, last.created_at)) ?? []
    if (tag !== listTag) return
    const seen = new Set(moments.value.map(m => m.id))
    moments.value = [...moments.value, ...rows.filter(m => !seen.has(m.id))]
    hasMore.value = rows.length >= INBOX_PAGE
  } catch {
    status.value = loadFailed(status.value)
  } finally {
    loadingOlder.value = false
  }
}

function patch(id: string, p: Partial<Moment>) {
  moments.value = moments.value.map(m => m.id === id ? { ...m, ...p } : m)
}

function drop(id: string) {
  moments.value = moments.value.filter(m => m.id !== id)
}

export default function MomentumInboxTab({ tag }: { tag: string | null }) {
  const me = currentUser.value?.user_id

  useEffect(() => {
    listTag = tag
    loading.value = true
    status.value = INITIAL_LOAD
    moments.value = []
    hasMore.value = false
    void loadBlocks()
    void loadHouseholdUsers()  // names + pictures for this household's people
    void fetchFirst(tag, false)
    const refresh = () => { void fetchFirst(tag, true) }
    const dispose = [
      ws.on('moment.created', refresh),
      ws.on('moment.deleted', refresh),
      ws.on('moment.reaction_changed', refresh),
    ]
    return () => { dispose.forEach(d => d()) }
  }, [tag])

  const blocked = blockedUserIds.value
  const visible = moments.value.filter(m => !blocked.has(m.author_user_id) && !m.parent_moment_id)
  const retry = () => void fetchFirst(tag, true)

  return (
    <div class="sh-momentum">
      <button
        type="button"
        class="sh-momentum-entry"
        aria-label={t('moment.composer.placeholder')}
        aria-describedby="sh-momentum-entry-reach"
        onClick={() => openMomentumComposer()}
      >
        <Avatar name={me ? householdDisplayName(me) : '?'} src={householdPictureUrl(me)} size={40} />
        <span class="sh-momentum-entry__text">
          <span class="sh-momentum-entry__prompt">{t('moment.composer.placeholder')}</span>
          <span id="sh-momentum-entry-reach" class="sh-momentum-entry__reach">
            {t(currentUser.value?.protected ? 'moment.entry.reach_protected' : 'moment.entry.reach')}
          </span>
        </span>
        <svg class="sh-momentum-entry__icon" width="22" height="22" viewBox="0 0 24 24" fill="none"
          stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
          aria-hidden="true" focusable="false">
          <rect x="3" y="3" width="18" height="18" rx="2" /><circle cx="9" cy="9" r="2" />
          <path d="m21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21" />
        </svg>
      </button>

      {loading.value ? <MomentumInboxSkeleton /> : (
        <>
          {status.value.failed && (
            <LoadErrorState
              message={t('moment.inbox.load_failed_title')}
              detail={t('load.household_quiet')}
              retryLabel={t('load.try_again')}
              onRetry={retry}
            />
          )}
          {status.value.staleSince !== null && (
            <StaleNotice
              message={t('moment.inbox.stale', { time: clockTime(status.value.staleSince) })}
              onRetry={retry}
            />
          )}

          {!status.value.failed && visible.length === 0 && (
            tag ? (
              <div class="sh-empty-state">
                <p>{t('moment.archive.empty_tag', { tag })}</p>
              </div>
            ) : (
              <div class="sh-empty-state">
                <div aria-hidden="true">🌅</div>
                <h3>{t('moment.inbox.empty_title')}</h3>
                <p>{t('moment.inbox.empty_body')}</p>
              </div>
            )
          )}

          {visible.length > 0 && (
            <ul class="sh-momentum-cards" aria-label={t('moment.inbox.list_aria')}>
              {visible.map(m => (
                <li key={m.id}>
                  <MomentCard m={m} me={me} onPatch={patch} onDeleted={drop} />
                </li>
              ))}
            </ul>
          )}

          {hasMore.value && (
            <div class="sh-momentum-more">
              <Button variant="secondary" loading={loadingOlder.value} onClick={() => void loadOlder(tag)}>
                {t('moment.yours.load_older')}
              </Button>
            </div>
          )}
          {!hasMore.value && !tag && visible.length > 0 && (
            <p class="sh-muted sh-momentum-end">{t('moment.inbox.end')}</p>
          )}
        </>
      )}
    </div>
  )
}
