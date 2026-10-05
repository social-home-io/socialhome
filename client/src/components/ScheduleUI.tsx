/**
 * ScheduleUI — Doodle-style scheduling surface (§23.53).
 *
 * Consumed by :mod:`PostCard` when ``post.type === 'schedule'``.
 * Fetches the poll summary lazily via
 * ``GET /api/schedule-polls/{post_id}/summary`` (or its space twin)
 * and lets any member cast / retract a vote. The author additionally
 * gets a "Finalize" button on the row they've picked; finalize causes
 * the backend to auto-create a calendar event in the owning space if
 * the ``calendar`` feature is enabled.
 */
import { useEffect, useState } from 'preact/hooks'
import { api } from '@/api'
import { ws } from '@/ws'
import { Button } from './Button'
import { showToast } from './Toast'
import { confirmDialog } from '@/components/confirm'
import { t } from '@/i18n/i18n'

interface Slot {
  id: string
  slot_date: string
  start_time: string | null
  end_time: string | null
  position: number
}

interface Response {
  slot_id: string
  user_id: string
  availability: 'yes' | 'no' | 'maybe'
}

export interface ScheduleData {
  post_id:           string
  title:             string
  deadline:          string | null
  slots:             Slot[]
  responses:         Response[]
  finalized_slot_id: string | null
  closed:            boolean
  space_id:          string | null
}

interface Props {
  postId: string
  /** Post author — gates the finalize affordance. */
  authorUserId?: string
  currentUserId: string
  /** When set the UI talks to ``/api/spaces/{id}/...`` endpoints. */
  spaceId?: string | null
}

function baseUrl(postId: string, spaceId?: string | null): string {
  return spaceId
    ? `/api/spaces/${spaceId}/schedule-polls/${postId}`
    : `/api/schedule-polls/${postId}`
}

export function ScheduleUI(
  { postId, authorUserId, currentUserId, spaceId }: Props,
) {
  const [data, setData] = useState<ScheduleData | null>(null)
  // Mirror PollUI's load-state machine — the summary endpoint 404s
  // for any post created with type=schedule before the slots envelope
  // arrives, and the previous bare-catch left the renderer stuck on
  // "Loading schedule…" forever.  Surface the missing-slots case as
  // a calmer placeholder instead.
  const [loadState, setLoadState] = useState<'loading' | 'missing' | 'error' | 'ok'>('loading')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let stopped = false
    const load = async () => {
      try {
        const d = await api.get(
          spaceId
            ? `/api/spaces/${spaceId}/schedule-polls/${postId}/summary`
            : `/api/schedule-polls/${postId}/summary`,
        ) as ScheduleData
        if (stopped) return
        setData(d)
        setLoadState('ok')
      } catch (err: unknown) {
        if (stopped) return
        const status = (err as { status?: number })?.status
        const msg = (err as Error)?.message ?? ''
        if (status === 404 || msg.includes('404')) {
          setLoadState('missing')
        } else {
          setLoadState('error')
        }
      }
    }
    void load()
    const off1 = ws.on('schedule_poll.responded', (e) => {
      const d = e.data as { post_id: string }
      if (d.post_id === postId) void load()
    })
    const off2 = ws.on('schedule_poll.finalized', (e) => {
      const d = e.data as { post_id: string }
      if (d.post_id === postId) void load()
    })
    return () => { stopped = true; off1(); off2() }
  }, [postId, spaceId])

  if (loadState === 'loading') return (
    <div class="sh-schedule">
      <p class="sh-muted">{t('schedule.loading')}</p>
    </div>
  )
  if (loadState === 'missing') return (
    <div class="sh-schedule sh-schedule--missing">
      <p class="sh-muted">
        {t('schedule.missing')}
      </p>
    </div>
  )
  if (loadState === 'error' || !data) return (
    <div class="sh-schedule sh-schedule--error">
      <p class="sh-muted">{t('schedule.load_failed')}</p>
    </div>
  )

  if (data.slots.length === 0) {
    // Post body said "schedule" but the poll envelope never set
    // any slots — surface the same calming hint as the 404 path so
    // the post never reads as broken.
    return (
      <div class="sh-schedule sh-schedule--missing">
        <p class="sh-muted">
          {t('schedule.missing')}
        </p>
      </div>
    )
  }

  const myResponses = new Map(
    data.responses
      .filter(r => r.user_id === currentUserId)
      .map(r => [r.slot_id, r.availability]),
  )

  const slotSummary = (slotId: string) => {
    const yes   = data.responses.filter(r => r.slot_id === slotId && r.availability === 'yes').length
    const maybe = data.responses.filter(r => r.slot_id === slotId && r.availability === 'maybe').length
    const no    = data.responses.filter(r => r.slot_id === slotId && r.availability === 'no').length
    return { yes, maybe, no }
  }

  const respond = async (slotId: string, availability: string) => {
    if (busy || data.closed) return
    setBusy(true)
    try {
      const next = await api.post(
        `${baseUrl(postId, spaceId)}/respond`,
        { slot_id: slotId, response: availability },
      ) as ScheduleData
      setData(next)
    } catch (err: unknown) {
      showToast(
        t('schedule.vote_failed', { error: String((err as Error)?.message ?? err) }), 'error',
      )
    } finally {
      setBusy(false)
    }
  }

  const finalize = async (slotId: string) => {
    if (!await confirmDialog(t('schedule.finalize_confirm'))) return
    setBusy(true)
    try {
      const next = await api.post(
        `${baseUrl(postId, spaceId)}/finalize`,
        { slot_id: slotId },
      ) as ScheduleData
      setData(next)
      showToast(
        spaceId
          ? t('schedule.finalized_space')
          : t('schedule.finalized'),
        'success',
      )
    } catch (err: unknown) {
      showToast(
        t('schedule.finalize_failed', { error: String((err as Error)?.message ?? err) }), 'error',
      )
    } finally {
      setBusy(false)
    }
  }

  const isAuthor = !!authorUserId && authorUserId === currentUserId
  const deadlineLabel = _formatDeadline(data.deadline)

  // Peak "yes" count across slots drives the tally-bar width fraction
  // so the relatively-best option visually dominates.
  const peakYes = Math.max(
    1,
    ...data.slots.map(s => slotSummary(s.id).yes),
  )

  const finalizedSlot = data.finalized_slot_id
    ? data.slots.find(s => s.id === data.finalized_slot_id)
    : null

  return (
    <div class="sh-schedule" role="region" aria-label={t('schedule.region')}>
      <h4 class="sh-schedule-title">📅 {data.title}</h4>
      {deadlineLabel && (
        <div class="sh-schedule-deadline">{deadlineLabel}</div>
      )}
      {finalizedSlot && (
        <div class="sh-schedule-finalized">
          {t('schedule.confirmed')} <strong>{finalizedSlot.slot_date}</strong>
          {finalizedSlot.start_time && (
            <> · {finalizedSlot.start_time}
              {finalizedSlot.end_time ? `–${finalizedSlot.end_time}` : ''}
            </>
          )}
        </div>
      )}
      <div class="sh-schedule-slots">
        {data.slots.map(slot => {
          const summary = slotSummary(slot.id)
          const myVote = myResponses.get(slot.id)
          const isChosen = slot.id === data.finalized_slot_id
          const fillPct = Math.round((summary.yes / peakYes) * 100)
          const votedCls = myVote ? 'sh-schedule-slot--voted' : ''
          const chosenCls = isChosen ? 'sh-schedule-slot--chosen' : ''
          return (
            <div key={slot.id}
                 class={`sh-schedule-slot ${votedCls} ${chosenCls}`}>
              <span class="sh-schedule-slot-bar"
                    style={{ width: `${fillPct}%` }} />
              <div class="sh-schedule-head">
                <div class="sh-schedule-date">
                  {slot.slot_date}
                  {slot.start_time && (
                    <span>
                      {' · '}
                      {slot.start_time}
                      {slot.end_time ? `–${slot.end_time}` : ''}
                    </span>
                  )}
                </div>
                <div class="sh-schedule-votes" aria-label={t('schedule.tally')}>
                  <span>✅ {summary.yes}</span>
                  <span>🤔 {summary.maybe}</span>
                  <span>❌ {summary.no}</span>
                  {isChosen && (
                    <span class="sh-schedule-chosen-badge">{t('schedule.chosen')}</span>
                  )}
                </div>
              </div>
              {!data.closed && (
                <div class="sh-schedule-buttons">
                  {(['yes', 'maybe', 'no'] as const).map(a => (
                    <button key={a} type="button"
                      data-a={a}
                      class={`sh-schedule-btn ${myVote === a ? 'sh-schedule-btn--active' : ''}`}
                      disabled={busy}
                      // schedule.respond_yes / schedule.respond_maybe / schedule.respond_no
                      aria-label={t(`schedule.respond_${a}`)}
                      aria-pressed={myVote === a}
                      onClick={() => void respond(slot.id, a)}>
                      {a === 'yes' ? `✅ ${t('schedule.yes')}`
                        : a === 'maybe' ? `🤔 ${t('schedule.maybe')}` : `❌ ${t('schedule.no')}`}
                    </button>
                  ))}
                  {isAuthor && !isChosen && (
                    <Button
                      variant="secondary"
                      loading={busy}
                      onClick={() => void finalize(slot.id)}>
                      {t('schedule.pick')}
                    </Button>
                  )}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

function _formatDeadline(iso: string | null): string | null {
  if (!iso) return null
  const end = Date.parse(iso)
  if (Number.isNaN(end)) return null
  const diff = end - Date.now()
  if (diff <= 0) return `⌛ ${t('schedule.voting_closed')}`
  const mins = Math.floor(diff / 60_000)
  if (mins < 60)   return `⌛ ${t('poll.closes_in_min', { n: String(mins) })}`
  const hours = Math.floor(mins / 60)
  if (hours < 24)  return `⌛ ${t('poll.closes_in_hours', { n: String(hours) })}`
  const days = Math.floor(hours / 24)
  return `⌛ ${t('poll.closes_in_days', { n: String(days) })}`
}
