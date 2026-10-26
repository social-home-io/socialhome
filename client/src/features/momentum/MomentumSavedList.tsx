/**
 * MomentumSavedList — the moments you saved (decision 6b), under Yours.
 *
 * A saved moment stays with you past its end until its author deletes it
 * (unfollowing them drops it too). Newest save first, ``before`` cursor
 * paging on ``saved_at``. Each row opens the moment; its Saved toggle
 * removes it from the list.
 *
 * Data: ``GET /api/moments/archive?scope=saved``. Mounted by
 * :class:`MomentumYoursTab` for ``?tab=yours&show=saved``.
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { useLocation } from 'preact-iso'
import { api } from '@/api'
import { Avatar } from '@/components/Avatar'
import { Button } from '@/components/Button'
import { MomentumArchiveSkeleton } from '@/components/Skeleton'
import { LoadErrorState, StaleNotice } from '@/components/LoadErrorState'
import { showToast } from '@/components/Toast'
import { INITIAL_LOAD, clockTime, loadFailed, loadSucceeded, type LoadStatus } from '@/utils/loadStatus'
import { addBase } from '@/baseUrl'
import { namedPersonLabel, namedPersonPicture } from '@/store/householdUsers'
import { ws } from '@/ws'
import type { Moment } from '@/types'
import { t, formatLocale } from '@/i18n/i18n'
import { MomentSaveToggle } from './MomentSaveToggle'

/** Rows per page of ``?scope=saved``. */
export const SAVED_PAGE = 30

const moments = signal<Moment[]>([])
const loading = signal<boolean>(true)
const hasMore = signal<boolean>(false)
const loadingOlder = signal<boolean>(false)
/** A failed load is never the empty list (``utils/loadStatus``). */
const status = signal<LoadStatus>(INITIAL_LOAD)

function fetchPage(before?: string): Promise<Moment[]> {
  const params: Record<string, string> = { scope: 'saved', limit: String(SAVED_PAGE) }
  if (before) params.before = before
  return api.get<Moment[]>('/api/moments/archive', params).then(rows => rows ?? [])
}

/** Load (or refresh) the newest page; older pages on screen stay below. */
function fetchFirst(initial: boolean): Promise<void> {
  return fetchPage()
    .then(rows => {
      const last = rows[rows.length - 1]
      const older = last
        ? moments.value.filter(m => (m.saved_at ?? '') < (last.saved_at ?? ''))
        : []
      moments.value = [...rows, ...older]
      if (initial || older.length === 0) hasMore.value = rows.length === SAVED_PAGE
      status.value = loadSucceeded(status.value)
    })
    .catch(() => { status.value = loadFailed(status.value) })
    .finally(() => { if (initial) loading.value = false })
}

async function loadOlder(): Promise<void> {
  const last = moments.value[moments.value.length - 1]
  if (!last?.saved_at || loadingOlder.value) return
  loadingOlder.value = true
  try {
    const rows = await fetchPage(last.saved_at)
    const seen = new Set(moments.value.map(m => m.id))
    moments.value = [...moments.value, ...rows.filter(m => !seen.has(m.id))]
    hasMore.value = rows.length === SAVED_PAGE
  } catch {
    showToast(t('load.household_quiet'), 'error')
  } finally {
    loadingOlder.value = false
  }
}

function when(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const sameYear = d.getFullYear() === new Date().getFullYear()
  return d.toLocaleString(formatLocale(), {
    day: 'numeric',
    month: 'short',
    ...(sameYear ? {} : { year: 'numeric' }),
    hour: 'numeric',
    minute: '2-digit',
  })
}

function SavedRow({ m, onOpen }: { m: Moment; onOpen: () => void }) {
  const name = namedPersonLabel(m.author_user_id, m.author)
  return (
    <li class="sh-momentum-yours-row sh-momentum-saved-row">
      <a
        href={addBase(`/momentum/${m.id}`)}
        class="sh-momentum-yours-link"
        onClick={(ev) => { ev.preventDefault(); onOpen() }}
      >
        <div class="sh-momentum-yours-head sh-momentum-saved-head">
          <Avatar name={name} src={namedPersonPicture(m.author_user_id, m.author)} size={28} />
          <strong>{name}</strong>
          <time class="sh-muted sh-momentum-yours-status" dateTime={m.created_at}>{when(m.created_at)}</time>
        </div>
        {m.content && <p class="sh-momentum-row-content">{m.content}</p>}
        {!m.content && !m.media_url && (
          <p class="sh-muted sh-momentum-yours-gone">{t('moment.yours.media_removed')}</p>
        )}
        {m.media_type === 'image' && m.media_url && (
          <img src={m.media_url} alt={m.content ? '' : t('moment.photo_from', { name })}
            loading="lazy" class="sh-momentum-yours-media" />
        )}
        {m.media_type === 'video' && m.media_url && (
          m.media_thumbnail_url
            ? <img src={m.media_thumbnail_url} alt={t('moment.video')}
                loading="lazy" class="sh-momentum-yours-media" />
            : <span class="sh-momentum-row-media sh-momentum-row-media--video">🎬 {t('moment.video')}</span>
        )}
      </a>
      <div class="sh-momentum-saved-foot">
        {m.saved_at && (
          <span class="sh-muted sh-momentum-yours-status">
            {t('moment.saved.saved_on', { time: when(m.saved_at) })}
          </span>
        )}
        <MomentSaveToggle
          moment={m}
          variant="chip"
          onChange={(saved) => {
            if (!saved) moments.value = moments.value.filter(x => x.id !== m.id)
          }}
        />
      </div>
    </li>
  )
}

export default function MomentumSavedList() {
  const loc = useLocation()

  useEffect(() => {
    loading.value = true
    status.value = INITIAL_LOAD
    moments.value = []
    hasMore.value = false
    void fetchFirst(true)
    // The author's delete removes a saved moment.
    const dispose = [ws.on('moment.deleted', () => { void fetchFirst(false) })]
    return () => { dispose.forEach(d => d()) }
  }, [])

  const intro = <p class="sh-muted sh-momentum-saved-intro">{t('moment.saved.intro')}</p>
  if (loading.value) return <>{intro}<MomentumArchiveSkeleton /></>

  const retry = () => void fetchFirst(false)
  if (status.value.failed) {
    return (
      <>
        {intro}
        <LoadErrorState
          message={t('moment.saved.load_failed_title')}
          detail={t('load.household_quiet')}
          retryLabel={t('load.try_again')}
          onRetry={retry}
        />
      </>
    )
  }

  return (
    <>
      {intro}
      {status.value.staleSince !== null && (
        <StaleNotice
          message={t('moment.inbox.stale', { time: clockTime(status.value.staleSince) })}
          onRetry={retry}
        />
      )}
      {moments.value.length === 0 ? (
        <div class="sh-empty-state">
          <h3>{t('moment.saved.empty_title')}</h3>
          <p>{t('moment.saved.empty_body')}</p>
        </div>
      ) : (
        <ul class="sh-momentum-list" aria-label={t('moment.saved.list_aria')}>
          {moments.value.map(m => (
            <SavedRow key={m.id} m={m} onOpen={() => loc.route(`/momentum/${m.id}`)} />
          ))}
        </ul>
      )}
      {hasMore.value && (
        <div class="sh-momentum-yours-more">
          <Button variant="secondary" loading={loadingOlder.value} onClick={() => void loadOlder()}>
            {t('moment.yours.load_older')}
          </Button>
        </div>
      )}
    </>
  )
}
