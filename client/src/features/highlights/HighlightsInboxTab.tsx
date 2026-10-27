/**
 * HighlightsInboxTab — Highlights › Today (§Highlights, r3 design).
 *
 * Rows, not rings (owner decision 8 — rings are the glance on Home):
 *
 * - **Your status card** first: today's frames, "seen by N", who sees
 *   it, Watch / Add frames — or "Share a highlight" when you shared
 *   nothing in the last 24 hours.
 * - **New**: people with frames you haven't watched. **Seen**: the rest.
 *   One row per person, however many highlight days they have (bug 15),
 *   built by :func:`todayView`. A row: avatar, name, the time of the
 *   newest frame (one format: "09:40" / "Yesterday 17:10"), how many
 *   frames, the audience chip (``audience_label``, never the ids), an
 *   "ends in 2 hours" warning, and still thumbnails (a video's poster).
 * - Today is the last 24 hours; older highlights are in the Archive
 *   (a link at the end).
 *
 * Loading → row placeholders; a failed first load → ``LoadErrorState``
 * with Try again; a failed refresh keeps the list (``StaleNotice``);
 * nothing today → an empty state with Share and the Archive.
 */
import { useEffect, useId, useState } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { HighlightRowsSkeleton } from '@/components/Skeleton'
import { Avatar } from '@/components/Avatar'
import { LoadErrorState, StaleNotice } from '@/components/LoadErrorState'
import { INITIAL_LOAD, clockTime, loadFailed, loadSucceeded, type LoadStatus } from '@/utils/loadStatus'
import { localSinceTime, relativeFutureTime } from '@/utils/relativeTime'
import { currentUser } from '@/store/auth'
import { blockedUserIds, loadBlocks } from '@/store/blocks'
import {
  namedPersonLabel,
  namedPersonPicture,
  loadHouseholdUsers,
} from '@/store/householdUsers'
import { ws } from '@/ws'
import type { HighlightFrame, HighlightInboxItem } from '@/types'
import { addBase } from '@/baseUrl'
import { t, isOne } from '@/i18n/i18n'
import { audienceChip, todayView, type TodayPerson } from './today'
import { CHIP_ICON, SVG } from './icons'

const inbox = signal<HighlightInboxItem[]>([])
const loading = signal<boolean>(true)
/** A failed load is never "no highlights" (``utils/loadStatus``). */
const status = signal<LoadStatus>(INITIAL_LOAD)

/** Load (or refresh, on a ``highlight.*`` frame) the inbox. */
function fetchInbox(initial: boolean): Promise<void> {
  return api.get('/api/highlights')
    .then((rows: HighlightInboxItem[]) => {
      inbox.value = rows ?? []
      status.value = loadSucceeded(status.value)
    })
    .catch(() => { status.value = loadFailed(status.value) })
    .finally(() => { if (initial) loading.value = false })
}

// ── Small pieces ────────────────────────────────────────────────────────

const ICON = {
  eye: (<svg {...SVG} width={16} height={16}><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z" /><circle cx="12" cy="12" r="3" /></svg>),
  clock: (<svg {...SVG} width={16} height={16}><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></svg>),
  play: (<svg {...SVG} width={18} height={18}><path d="M7 4l13 8-13 8z" fill="currentColor" /></svg>),
  plus: (<svg {...SVG} width={18} height={18}><path d="M12 5v14M5 12h14" /></svg>),
  chevron: (<svg {...SVG} width={20} height={20} class="sh-flip-rtl"><path d="M9 6l6 6-6 6" /></svg>),
}

/** The audience chip — a short label on its own wrap line. */
export function AudienceChip({ person, mine }: { person: TodayPerson; mine: boolean }) {
  const chip = audienceChip(person.highlight, mine)
  return (
    <span class="sh-hl-chip">
      {CHIP_ICON[chip.icon]}
      <span class="sr-only">{t('highlight.chip.aria', { who: chip.text })}</span>
      <span aria-hidden="true">{chip.text}</span>
    </span>
  )
}

/** One frame as a still: the picture, a video's poster with a play
 *  glyph, or a neutral tile when there is no poster (yet). */
function FrameThumb({ frame }: { frame: HighlightFrame }) {
  const [broken, setBroken] = useState(false)
  const isVideo = frame.frame_type === 'video'
  const src = isVideo ? frame.thumbnail_url : frame.media_url
  return (
    <span class={isVideo ? 'sh-hl-thumb sh-hl-thumb--video' : 'sh-hl-thumb'}>
      {src && !broken && (
        <img src={src} alt="" loading="lazy" onError={() => setBroken(true)} />
      )}
      {isVideo && <span class="sh-hl-thumb__play">{ICON.play}</span>}
    </span>
  )
}

function frameCount(frames: readonly HighlightFrame[]): string {
  const n = frames.length
  const allVideo = n > 0 && frames.every(f => f.frame_type === 'video')
  const key = allVideo ? 'highlight.today.videos' : 'highlight.frames'
  return t(isOne(n) ? `${key}_one` : key, { n: String(n) })
}

function personName(p: TodayPerson): string {
  return namedPersonLabel(p.authorId, p.highlight.author)
}

function PersonRow({ person }: { person: TodayPerson }) {
  const name = personName(person)
  const isNew = person.unseen > 0
  return (
    <li>
      <a class="sh-hl-row" href={addBase(`/highlights/${person.openId}`)}>
        <span class={isNew ? 'sh-hl-ring sh-hl-ring--new' : 'sh-hl-ring'}>
          <Avatar name={name} src={namedPersonPicture(person.authorId, person.highlight.author)} size={44} />
        </span>
        <span class="sh-hl-row__body">
          <span class="sh-hl-row__name"><bdi dir="auto">{name}</bdi></span>
          <span class="sh-hl-row__meta">
            <span class="sh-hl-row__facts">
              <bdi><time dateTime={person.lastAt}>{localSinceTime(person.lastAt)}</time></bdi>
              {' · '}{frameCount(person.frames)}
              {isNew && (
                <span class="sr-only">
                  {', '}{t(isOne(person.unseen) ? 'highlight.today.new_count_one' : 'highlight.today.new_count',
                    { n: String(person.unseen) })}
                </span>
              )}
            </span>
            {person.endsAt && (
              <span class="sh-hl-ends">
                {ICON.clock}
                {t('highlight.today.ends', { when: relativeFutureTime(person.endsAt) })}
              </span>
            )}
            <AudienceChip person={person} mine={false} />
          </span>
        </span>
        <span class="sh-hl-row__thumbs" aria-hidden="true">
          {person.frames.slice(0, 3).reverse().map(f => <FrameThumb key={f.id} frame={f} />)}
        </span>
        <span class="sh-hl-row__chev" aria-hidden="true">{ICON.chevron}</span>
      </a>
    </li>
  )
}

function Section({ kind, title, people }: { kind: 'new' | 'seen'; title: string; people: TodayPerson[] }) {
  const id = useId()
  if (people.length === 0) return null
  return (
    <section class="sh-hl-section" aria-labelledby={id} data-section={kind}>
      <div class="sh-hl-section__head">
        <h2 id={id}>{title}</h2>
        {kind === 'new' && (
          <span class="sh-hl-section__meta">
            {t(isOne(people.length) ? 'highlight.today.people_one' : 'highlight.today.people',
              { n: String(people.length) })}
          </span>
        )}
      </div>
      <ul class="sh-hl-rows">
        {people.map(p => <PersonRow key={p.authorId} person={p} />)}
      </ul>
    </section>
  )
}

function StatusCard({ mine }: { mine: TodayPerson | null }) {
  const id = useId()
  return (
    <section class={mine ? 'sh-hl-status' : 'sh-hl-status sh-hl-status--none'} aria-labelledby={id}>
      {mine && (
        <span class="sh-hl-status__thumbs" aria-hidden="true">
          {mine.frames.slice(0, 3).reverse().map(f => <FrameThumb key={f.id} frame={f} />)}
        </span>
      )}
      <div class="sh-hl-status__body">
        <h2 id={id} class="sh-hl-status__title">{t('highlight.today.yours_title')}</h2>
        {mine ? (
          <p class="sh-hl-row__meta">
            <span class="sh-hl-row__facts">
              <bdi><time dateTime={mine.lastAt}>{localSinceTime(mine.lastAt)}</time></bdi>
              {' · '}{frameCount(mine.frames)}{' · '}
              <span class="sh-hl-seen">
                {ICON.eye}
                {mine.seenBy
                  ? t(isOne(mine.seenBy) ? 'highlight.today.seen_by_one' : 'highlight.today.seen_by',
                    { n: String(mine.seenBy) })
                  : t('highlight.today.not_seen')}
              </span>
            </span>
            {mine.endsAt && (
              <span class="sh-hl-ends">
                {ICON.clock}
                {t('highlight.today.ends', { when: relativeFutureTime(mine.endsAt) })}
              </span>
            )}
            <AudienceChip person={mine} mine />
          </p>
        ) : (
          <>
            <p class="sh-hl-status__line">{t('highlight.today.none_title')}</p>
            <p class="sh-muted sh-hl-status__line">{t('highlight.today.none_body')}</p>
          </>
        )}
      </div>
      <div class="sh-hl-status__actions">
        {mine ? (
          <>
            <a class="sh-btn sh-btn--secondary sh-hl-btn" href={addBase(`/highlights/${mine.openId}`)}>
              {ICON.play}{t('highlight.today.watch')}
            </a>
            <a class="sh-btn sh-btn--secondary sh-hl-btn" href={addBase('/highlights/new')}>
              {ICON.plus}{t('highlight.today.add_frames')}
            </a>
          </>
        ) : (
          <a class="sh-btn sh-btn--primary sh-hl-btn" href={addBase('/highlights/new')}>
            {ICON.plus}{t('highlight.today.share')}
          </a>
        )}
      </div>
    </section>
  )
}

export default function HighlightsInboxTab() {
  const me = currentUser.value?.user_id

  useEffect(() => {
    loading.value = true
    status.value = INITIAL_LOAD
    void loadBlocks()  // populate the optimistic block-id set
    void loadHouseholdUsers()  // resolve display names + avatars from raw user_ids
    void fetchInbox(true)
    // Live updates — both local writes and federated arrivals fan a
    // narrow ``highlight.*`` frame; we just refetch so the audience filter
    // and unseen counts stay server-authoritative.
    const dispose = [
      ws.on('highlight.frame_added',   () => { void fetchInbox(false) }),
      ws.on('highlight.frame_removed', () => { void fetchInbox(false) }),
      ws.on('highlight.removed',       () => { void fetchInbox(false) }),
      ws.on('highlight.audience_changed', () => { void fetchInbox(false) }),
    ]
    return () => { dispose.forEach(d => d()) }
  }, [])

  if (loading.value) return <HighlightRowsSkeleton />

  if (status.value.failed) {
    return (
      <div class="sh-highlights-page sh-hl-today">
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

  // Belt + braces: the server already strips blocked authors. The local
  // hide makes a same-tab "Block @bob" land without a refresh.
  const blocked = blockedUserIds.value
  const items = inbox.value.filter(i => !blocked.has(i.highlight.author_user_id))
  const view = todayView(items, me)
  const nobody = view.fresh.length === 0 && view.seen.length === 0

  return (
    <div class="sh-highlights-page sh-hl-today">
      {status.value.staleSince !== null && (
        <StaleNotice
          message={t('highlight.inbox.stale', { time: clockTime(status.value.staleSince) })}
          onRetry={() => void fetchInbox(false)}
        />
      )}
      {(view.mine || !nobody) && <StatusCard mine={view.mine} />}
      {nobody ? (
        <div class="sh-empty-state sh-hl-empty">
          <h2>{t('highlight.today.empty_title')}</h2>
          <p>{t('highlight.today.empty_body')}</p>
          <div class="sh-empty-state__cta-row">
            {!view.mine && (
              <a class="sh-btn sh-btn--primary sh-hl-btn" href={addBase('/highlights/new')}>
                {ICON.plus}{t('highlight.today.share')}
              </a>
            )}
            <a class="sh-btn sh-btn--secondary sh-hl-btn" href={addBase('/highlights/archive')}>
              {t('highlight.today.open_archive')}
            </a>
          </div>
        </div>
      ) : (
        <>
          <Section kind="new" title={t('highlight.today.section_new')} people={view.fresh} />
          <Section kind="seen" title={t('highlight.today.section_seen')} people={view.seen} />
          <a class="sh-hl-archive-link" href={addBase('/highlights/archive')}>
            {t('highlight.today.archive_link')}{ICON.chevron}
          </a>
        </>
      )}
    </div>
  )
}
