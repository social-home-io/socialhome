/**
 * HighlightArchiveTab — Highlights › Archive as a list of days (r3, H7).
 *
 * Highlights stay as long as their author chose (1–90 days), long after
 * they leave Today. A month grid showed that window as mostly empty
 * cells; this tab lists only the days that have something:
 *
 * - A **month select**, bounded to the months that have highlights (no
 *   paging into empty months, C15), and an **Only mine** toggle. Both
 *   live in the URL (``?month=2026-09&mine=1``), replaced in place, so
 *   Back from the viewer returns to the same list.
 * - **Day headings in local time** ("Sunday, 25 October · today"),
 *   grouped by :func:`archiveDays` — never the UTC ``highlight_date``
 *   (B23).
 * - **One row per person per day**: avatar, name, a frame strip (a
 *   video's poster), "N frames · seen by N" for your own, "ends in N
 *   days" from each highlight's ``expires_at``, the audience chip. The
 *   row is a plain link to the viewer; the router handles the click, so
 *   it adds one history entry (bug 22 pushed two).
 * - An **honest retention note**: each person decides how long theirs
 *   stay; yours stay N days (your setting), with a link to change it.
 *
 * Loading → day placeholders; a failed first load → ``LoadErrorState``
 * with Try again; a failed refresh keeps the list (``StaleNotice``);
 * nothing → an empty state. Data: ``GET /api/highlights`` already
 * returns every highlight still in its author's retention window.
 */
import { useEffect, useId } from 'preact/hooks'
import { signal } from '@preact/signals'
import { useLocation } from 'preact-iso'
import { api } from '@/api'
import { Avatar } from '@/components/Avatar'
import { HighlightArchiveSkeleton } from '@/components/Skeleton'
import { LoadErrorState, StaleNotice } from '@/components/LoadErrorState'
import { INITIAL_LOAD, clockTime, loadFailed, loadSucceeded, type LoadStatus } from '@/utils/loadStatus'
import { dayKeyOf } from '@/utils/calendar'
import { relativeFutureTime } from '@/utils/relativeTime'
import { currentUser } from '@/store/auth'
import { blockedUserIds, loadBlocks } from '@/store/blocks'
import { loadHouseholdUsers, namedPersonLabel, namedPersonPicture } from '@/store/householdUsers'
import { ws } from '@/ws'
import type { HighlightInboxItem } from '@/types'
import { addBase } from '@/baseUrl'
import { t, isOne } from '@/i18n/i18n'
import {
  archiveDays, archiveFilterFromUrl, archiveMonths, archiveUrl, dayHeading, monthLabel,
  type ArchiveDay, type ArchiveFilter, type ArchiveRow,
} from './archive'
import { retentionDays } from './todayAudience'
import { AudienceChip, FrameThumb, frameCount } from './rowParts'
import { SVG } from './icons'

const inbox = signal<HighlightInboxItem[]>([])
const loading = signal<boolean>(true)
/** A failed load is never an empty archive (``utils/loadStatus``). */
const status = signal<LoadStatus>(INITIAL_LOAD)

/** Frames shown on a row; the rest are "+N" on the last one. */
const STRIP_MAX = 5

/** Load (or refresh, on a ``highlight.*`` frame) every visible highlight. */
function fetchInbox(initial: boolean): Promise<void> {
  return api.get('/api/highlights')
    .then((rows: HighlightInboxItem[]) => {
      inbox.value = rows ?? []
      status.value = loadSucceeded(status.value)
    })
    .catch(() => { status.value = loadFailed(status.value) })
    .finally(() => { if (initial) loading.value = false })
}

const ICON = {
  eye: (<svg {...SVG} width={16} height={16}><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z" /><circle cx="12" cy="12" r="3" /></svg>),
  clock: (<svg {...SVG} width={16} height={16}><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></svg>),
}

function Row({ row }: { row: ArchiveRow }) {
  const realName = namedPersonLabel(row.authorId, row.highlight.author)
  const name = row.mine ? t('highlight.inbox.you') : realName
  const shown = row.frames.slice(0, STRIP_MAX)
  const more = row.frames.length - shown.length
  return (
    <li>
      <a class="sh-hl-arch-row" href={addBase(`/highlights/${row.openId}`)}>
        <span class="sh-hl-arch-row__head">
          <Avatar name={realName} src={namedPersonPicture(row.authorId, row.highlight.author)} size={32} />
          <span class="sh-hl-arch-row__name" dir="auto">{name}</span>
        </span>
        <span class="sh-hl-row__meta">
          <span class="sh-hl-row__facts">{frameCount(row.frames)}</span>
          {row.mine && (
            <span class="sh-hl-seen">
              {ICON.eye}
              {row.seenBy
                ? t(isOne(row.seenBy) ? 'highlight.today.seen_by_one' : 'highlight.today.seen_by',
                  { n: String(row.seenBy) })
                : t('highlight.today.not_seen')}
            </span>
          )}
          <span class="sh-hl-arch-row__ends">
            {ICON.clock}
            {t('highlight.today.ends', { when: relativeFutureTime(row.endsAt) })}
          </span>
          <AudienceChip highlight={row.highlight} mine={row.mine} />
        </span>
        <span class="sh-hl-arch-strip" aria-hidden="true">
          {shown.map((f, i) => (
            <span key={f.id} class="sh-hl-arch-strip__item">
              <FrameThumb frame={f} />
              {more > 0 && i === shown.length - 1 && (
                <span class="sh-hl-arch-strip__more">+{more}</span>
              )}
            </span>
          ))}
        </span>
      </a>
    </li>
  )
}

function Day({ day, todayKey }: { day: ArchiveDay; todayKey: string }) {
  const id = useId()
  return (
    <section class="sh-hl-arch-day" aria-labelledby={id}>
      <h2 id={id} class="sh-hl-arch-day__title">{dayHeading(day.key, todayKey)}</h2>
      <ul class="sh-hl-rows">
        {day.rows.map(r => <Row key={r.authorId} row={r} />)}
      </ul>
    </section>
  )
}

function RetentionNote() {
  const days = retentionDays()
  return (
    <p class="sh-muted sh-hl-arch-note">
      {t(isOne(days) ? 'highlight.archive.retention_one' : 'highlight.archive.retention', { n: String(days) })}
      {' '}
      <a href={addBase('/settings#highlights')}>{t('highlight.archive.retention_link')}</a>
    </p>
  )
}

export default function HighlightArchiveTab() {
  const { url, route } = useLocation()
  const me = currentUser.value?.user_id
  const filter = archiveFilterFromUrl(url)
  const selectId = useId()

  useEffect(() => {
    loading.value = true
    status.value = INITIAL_LOAD
    void loadBlocks()
    void loadHouseholdUsers()
    void fetchInbox(true)
    const dispose = [
      ws.on('highlight.frame_added',   () => { void fetchInbox(false) }),
      ws.on('highlight.frame_removed', () => { void fetchInbox(false) }),
      ws.on('highlight.removed',       () => { void fetchInbox(false) }),
      ws.on('highlight.audience_changed', () => { void fetchInbox(false) }),
    ]
    return () => { dispose.forEach(d => d()) }
  }, [])

  /** A filter change replaces the entry: Back still leaves the Archive. */
  const setFilter = (next: ArchiveFilter) => route(archiveUrl(next), true)

  if (loading.value) return <HighlightArchiveSkeleton />

  if (status.value.failed) {
    return (
      <div class="sh-highlights-page sh-hl-arch">
        <LoadErrorState
          message={t('highlight.inbox.load_failed_title')}
          detail={t('load.household_quiet')}
          retryLabel={t('load.try_again')}
          headingLevel={2}
          onRetry={() => void fetchInbox(false)}
        />
      </div>
    )
  }

  // The server already strips blocked authors; the local hide makes a
  // same-tab "Block @bob" land without a refresh.
  const blocked = blockedUserIds.value
  const items = inbox.value.filter(i => !blocked.has(i.highlight.author_user_id))
  const days = archiveDays(items, me, filter.onlyMine)
  const anything = filter.onlyMine ? archiveDays(items, me, false).length > 0 : days.length > 0

  if (!anything) {
    return (
      <div class="sh-highlights-page sh-hl-arch">
        <div class="sh-empty-state sh-hl-empty">
          <h2>{t('highlight.archive.empty_title')}</h2>
          <p>{t('highlight.archive.empty_body')}</p>
        </div>
        <RetentionNote />
      </div>
    )
  }

  const months = archiveMonths(days)
  const month = filter.month && months.includes(filter.month) ? filter.month : months[0] ?? null
  const shown = days.filter(d => d.key.startsWith(`${month}-`))
  const todayKey = dayKeyOf(new Date())

  return (
    <div class="sh-highlights-page sh-hl-arch">
      {status.value.staleSince !== null && (
        <StaleNotice
          message={t('highlight.inbox.stale', { time: clockTime(status.value.staleSince) })}
          onRetry={() => void fetchInbox(false)}
        />
      )}
      <div class="sh-hl-arch-bar">
        {months.length > 0 && (
          <span class="sh-hl-arch-month">
            <label for={selectId} class="sr-only">{t('highlight.archive.month_label')}</label>
            <select
              id={selectId}
              value={month ?? ''}
              onChange={e => setFilter({ ...filter, month: (e.target as HTMLSelectElement).value })}
            >
              {months.map(m => <option key={m} value={m}>{monthLabel(m)}</option>)}
            </select>
          </span>
        )}
        <button
          type="button"
          class={filter.onlyMine ? 'sh-hl-arch-toggle is-on' : 'sh-hl-arch-toggle'}
          aria-pressed={filter.onlyMine}
          // A month picked for everyone may hold nothing of yours.
          onClick={() => setFilter({ month: null, onlyMine: !filter.onlyMine })}
        >
          {t('highlight.archive.only_mine')}
        </button>
      </div>

      {shown.length === 0 ? (
        <div class="sh-empty-state sh-hl-empty">
          <h2>{t('highlight.archive.mine_empty_title')}</h2>
          <div class="sh-empty-state__cta-row">
            <a class="sh-btn sh-btn--primary sh-hl-btn" href={addBase('/highlights/new')}>
              {t('highlight.today.share')}
            </a>
            <button
              type="button"
              class="sh-btn sh-btn--secondary sh-hl-btn"
              onClick={() => setFilter({ month: null, onlyMine: false })}
            >
              {t('highlight.archive.show_all')}
            </button>
          </div>
        </div>
      ) : (
        shown.map(d => <Day key={d.key} day={d} todayKey={todayKey} />)
      )}
      <RetentionNote />
    </div>
  )
}
