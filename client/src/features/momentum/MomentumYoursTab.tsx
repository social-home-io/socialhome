/**
 * MomentumYoursTab — your own moments (§Momentum, decision 6a).
 *
 * Every moment and reply you wrote on this household stays here for you,
 * also after others stop seeing it (others prune their copies at the
 * moment's expiry; this archive is local only). Each row says who still
 * sees it and until when. The top line shows how much your pictures and
 * videos use against the household's per-person limit — past it the
 * oldest pictures go first (the text stays).
 *
 * Data: ``GET /api/moments/archive?scope=own`` (newest first, ``before``
 * cursor paging) and ``GET /api/moments/archive/usage``.
 *
 * A Posted | Saved switch (``/momentum/yours?show=saved``) swaps the list for
 * the moments you saved (decision 6b, :class:`MomentumSavedList`); the
 * usage line counts both. Above the posted list, a Public sharing row links
 * to Settings › Privacy (decision 5).
 *
 * Mounted by :class:`MomentumPage` for ``/momentum/yours``.
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { useLocation } from 'preact-iso'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { MomentumArchiveSkeleton } from '@/components/Skeleton'
import { LoadErrorState, StaleNotice } from '@/components/LoadErrorState'
import { SegmentedSwitch } from '@/components/SegmentedSwitch'
import { showToast } from '@/components/Toast'
import { INITIAL_LOAD, clockTime, loadFailed, loadSucceeded, type LoadStatus } from '@/utils/loadStatus'
import { formatBytes } from '@/utils/bytes'
import { addBase } from '@/baseUrl'
import { getMomentsPreferences, momentRetentionLabel } from '@/utils/preferences'
import { ws } from '@/ws'
import type { Moment } from '@/types'
import { t, isOne, formatLocale } from '@/i18n/i18n'
import MomentumSavedList from './MomentumSavedList'
import { PublicSharingRow } from './PublicSharingRow'

/** Rows per page of ``?scope=own``. */
export const YOURS_PAGE = 30

export type YoursShow = 'posted' | 'saved'
const SHOWS: readonly YoursShow[] = ['posted', 'saved'] as const

/** Which list ``/momentum/yours`` shows: ``&show=saved`` = the moments you saved. */
export function yoursShowFromUrl(url: string): YoursShow {
  const params = new URLSearchParams(url.split('?')[1] ?? '')
  return params.get('show') === 'saved' ? 'saved' : 'posted'
}

const DAY_MS = 24 * 3_600_000

interface ArchiveUsage {
  used_bytes: number
  /** ``0`` = the household set no limit. */
  cap_bytes:  number
}

const moments = signal<Moment[]>([])
const loading = signal<boolean>(true)
const hasMore = signal<boolean>(false)
const loadingOlder = signal<boolean>(false)
const usage = signal<ArchiveUsage | null>(null)
/** A failed load is never the empty archive (``utils/loadStatus``). */
const status = signal<LoadStatus>(INITIAL_LOAD)

export type ArchiveStatus =
  | { kind: 'others'; until: number }
  | { kind: 'followers'; until: number }
  | { kind: 'kept' }

/** Who still sees a moment of yours: everyone who got it for its first
 *  24 hours, then only your followers until it expires, then just you. */
export function archiveStatus(
  m: Pick<Moment, 'created_at' | 'expires_at'>,
  now: number = Date.now(),
): ArchiveStatus {
  const expires = Date.parse(m.expires_at)
  const dayEnd = Date.parse(m.created_at) + DAY_MS
  if (Number.isFinite(dayEnd) && now < dayEnd) {
    return { kind: 'others', until: Number.isFinite(expires) ? Math.min(dayEnd, expires) : dayEnd }
  }
  if (Number.isFinite(expires) && now < expires) return { kind: 'followers', until: expires }
  return { kind: 'kept' }
}

function fetchPage(before?: string): Promise<Moment[]> {
  const params: Record<string, string> = { scope: 'own', limit: String(YOURS_PAGE) }
  if (before) params.before = before
  return api.get<Moment[]>('/api/moments/archive', params).then(rows => rows ?? [])
}

/** Load (or refresh, on a ``moment.*`` frame) the newest page; older
 *  pages already on screen stay below it. */
function fetchFirst(initial: boolean): Promise<void> {
  return fetchPage()
    .then(rows => {
      const last = rows[rows.length - 1]
      const older = last
        ? moments.value.filter(m => m.created_at < last.created_at)
        : []
      moments.value = [...rows, ...older]
      if (initial || older.length === 0) hasMore.value = rows.length === YOURS_PAGE
      status.value = loadSucceeded(status.value)
    })
    .catch(() => { status.value = loadFailed(status.value) })
    .finally(() => { if (initial) loading.value = false })
}

function fetchUsage(): void {
  api.get<ArchiveUsage>('/api/moments/archive/usage')
    .then(u => { usage.value = u ?? null })
    .catch(() => { usage.value = null })  // the size line is a nicety
}

async function loadOlder(): Promise<void> {
  const last = moments.value[moments.value.length - 1]
  if (!last || loadingOlder.value) return
  loadingOlder.value = true
  try {
    const rows = await fetchPage(last.created_at)
    const seen = new Set(moments.value.map(m => m.id))
    moments.value = [...moments.value, ...rows.filter(m => !seen.has(m.id))]
    hasMore.value = rows.length === YOURS_PAGE
  } catch {
    showToast(t('load.household_quiet'), 'error')
  } finally {
    loadingOlder.value = false
  }
}

function when(iso: string | number): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return String(iso)
  const sameYear = d.getFullYear() === new Date().getFullYear()
  return d.toLocaleString(formatLocale(), {
    weekday: 'short',
    day: 'numeric',
    month: 'short',
    ...(sameYear ? {} : { year: 'numeric' }),
    hour: 'numeric',
    minute: '2-digit',
  })
}

function statusLine(s: ArchiveStatus): string {
  switch (s.kind) {
    case 'others':    return t('moment.yours.until_others', { time: when(s.until) })
    case 'followers': return t('moment.yours.until_followers', { time: when(s.until) })
    case 'kept':      return t('moment.yours.kept')
  }
}

function UsageLine({ u }: { u: ArchiveUsage }) {
  const used = formatBytes(u.used_bytes)
  if (u.cap_bytes <= 0) {
    return <p class="sh-muted sh-momentum-yours-usage">{t('moment.yours.usage_unlimited', { used })}</p>
  }
  const cap = formatBytes(u.cap_bytes)
  return (
    <p class="sh-muted sh-momentum-yours-usage">
      {t('moment.yours.usage', { used, cap })}
    </p>
  )
}

function YoursRow({ m, onOpen }: { m: Moment; onOpen: () => void }) {
  const s = archiveStatus(m)
  const mediaGone = !m.content && !m.media_url
  return (
    <li class="sh-momentum-yours-row">
      <a
        href={addBase(`/momentum/${m.id}`)}
        class="sh-momentum-yours-link"
        onClick={(ev) => { ev.preventDefault(); onOpen() }}
      >
        <div class="sh-momentum-yours-head">
          <time dateTime={m.created_at}><strong>{when(m.created_at)}</strong></time>
          <span class="sh-muted sh-momentum-yours-status">{statusLine(s)}</span>
        </div>
        {m.content && <p class="sh-momentum-row-content">{m.content}</p>}
        {mediaGone && (
          <p class="sh-muted sh-momentum-yours-gone">{t('moment.yours.media_removed')}</p>
        )}
        {m.media_type === 'image' && m.media_url && (
          <img src={m.media_url} alt={m.content ? '' : t('moment.yours.your_photo')}
            loading="lazy" class="sh-momentum-yours-media" />
        )}
        {m.media_type === 'video' && m.media_url && (
          m.media_thumbnail_url
            ? <img src={m.media_thumbnail_url} alt={t('moment.video')}
                loading="lazy" class="sh-momentum-yours-media" />
            : <span class="sh-momentum-row-media sh-momentum-row-media--video">🎬 {t('moment.video')}</span>
        )}
        <div class="sh-momentum-yours-foot">
          <span class="sh-momentum-yours-tag">
            {m.parent_moment_id ? t('moment.yours.tag_reply') : t('moment.yours.tag_paired')}
          </span>
          {m.is_public && !m.parent_moment_id && (
            <span class="sh-momentum-yours-tag">{t('moment.yours.tag_public')}</span>
          )}
          <span class="sh-momentum-yours-counts">
            <span aria-label={t(isOne(m.reply_count) ? 'moment.detail.replies_one' : 'moment.detail.replies', { n: String(m.reply_count) })}>
              💬 {m.reply_count}
            </span>
            <span aria-label={t(isOne(m.reaction_count) ? 'moment.yours.reactions_one' : 'moment.yours.reactions', { n: String(m.reaction_count) })}>
              ❤️ {m.reaction_count}
            </span>
          </span>
        </div>
      </a>
    </li>
  )
}

export default function MomentumYoursTab() {
  const loc = useLocation()

  useEffect(() => {
    loading.value = true
    status.value = INITIAL_LOAD
    moments.value = []
    hasMore.value = false
    void fetchFirst(true)
    fetchUsage()
    const refresh = () => { void fetchFirst(false); fetchUsage() }
    const dispose = [
      ws.on('moment.created', refresh),
      ws.on('moment.deleted', refresh),
    ]
    return () => { dispose.forEach(d => d()) }
  }, [])

  const show = yoursShowFromUrl(loc.url)
  const header = (
    <>
      {show === 'posted' && <PublicSharingRow />}
      <h2>{t('moment.yours.heading')}</h2>
      <SegmentedSwitch<YoursShow>
        options={SHOWS}
        value={show}
        labels={{ posted: t('moment.yours.show_posted'), saved: t('moment.yours.show_saved') }}
        ariaLabel={t('moment.yours.show_aria')}
        class="sh-momentum-yours-switch"
        onChange={(next) => loc.route(next === 'saved' ? '/momentum/yours?show=saved' : '/momentum/yours', true)}
      />
      {show === 'posted' && (
        <p class="sh-muted">
          {t('moment.yours.intro', {
            period: momentRetentionLabel(getMomentsPreferences().follower_retention_days),
          })}
        </p>
      )}
      {usage.value && <UsageLine u={usage.value} />}
    </>
  )

  if (show === 'saved') {
    return (
      <div class="sh-momentum-archive">
        {header}
        <MomentumSavedList />
      </div>
    )
  }

  if (loading.value) return <MomentumArchiveSkeleton />

  const retry = () => void fetchFirst(false)

  if (status.value.failed) {
    return (
      <div class="sh-momentum-archive">
        {header}
        <LoadErrorState
          message={t('moment.yours.load_failed_title')}
          detail={t('load.household_quiet')}
          retryLabel={t('load.try_again')}
          onRetry={retry}
        />
      </div>
    )
  }

  return (
    <div class="sh-momentum-archive">
      {header}
      {status.value.staleSince !== null && (
        <StaleNotice
          message={t('moment.inbox.stale', { time: clockTime(status.value.staleSince) })}
          onRetry={retry}
        />
      )}
      {moments.value.length === 0 ? (
        <div class="sh-empty-state">
          <h3>{t('moment.yours.empty_title')}</h3>
          <p>{t('moment.yours.empty_body')}</p>
        </div>
      ) : (
        <ul class="sh-momentum-list" aria-label={t('moment.yours.list_aria')}>
          {moments.value.map(m => (
            <YoursRow key={m.id} m={m} onOpen={() => loc.route(`/momentum/${m.id}`)} />
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
    </div>
  )
}
