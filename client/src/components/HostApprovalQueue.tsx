/**
 * HostApprovalQueue — pending RSVP requests for capacity-limited events.
 *
 * Surfaces inside the calendar event detail card when the current user
 * is the event creator OR a space admin (Phase C). Each row shows the
 * requester's avatar, display name, requested-at timestamp, and
 * approve / deny buttons. Auto-collapses to an empty state when the
 * queue clears.
 *
 * Approve resolves the row to ``going`` (if a seat is free) or
 * ``waitlist`` (overflow). Deny clears the row entirely.
 */
import { useEffect } from 'preact/hooks'
import { signal } from '@preact/signals'
import { api } from '@/api'
import { Button } from '@/components/Button'
import { showToast } from '@/components/Toast'
import { t } from '@/i18n/i18n'
import { resolveAvatar, resolveDisplayName } from '@/utils/avatar'
import type { EventRsvp } from '@/types'

export interface HostApprovalQueueProps {
  eventId: string
  spaceId: string | null
  /** Optional occurrence_at filter; when omitted, shows pending across
   *  all occurrences (rare — most flows pin to a specific instance). */
  occurrenceAt?: string | null
}

interface PendingRow extends EventRsvp {
  status: 'requested'
}

const pendingByEvent = signal<Record<string, PendingRow[]>>({})
const loading = signal<Record<string, boolean>>({})
const acting = signal<Record<string, boolean>>({})

function cacheKey(eventId: string, occurrenceAt: string | null | undefined) {
  return occurrenceAt ? `${eventId}@${occurrenceAt}` : eventId
}

async function refresh(eventId: string, occurrenceAt?: string | null) {
  const key = cacheKey(eventId, occurrenceAt)
  loading.value = { ...loading.value, [key]: true }
  try {
    const params: Record<string, string> = {}
    if (occurrenceAt) params.occurrence_at = occurrenceAt
    const res = await api.get<{ pending: PendingRow[] }>(
      `/api/calendars/events/${eventId}/pending`,
      params,
    )
    pendingByEvent.value = {
      ...pendingByEvent.value,
      [key]: res.pending ?? [],
    }
  } catch {
    pendingByEvent.value = { ...pendingByEvent.value, [key]: [] }
  } finally {
    const next = { ...loading.value }
    delete next[key]
    loading.value = next
  }
}

export function HostApprovalQueue({
  eventId,
  spaceId,
  occurrenceAt,
}: HostApprovalQueueProps) {
  const key = cacheKey(eventId, occurrenceAt)

  useEffect(() => {
    refresh(eventId, occurrenceAt)
  }, [eventId, occurrenceAt])

  const rows = pendingByEvent.value[key] ?? []
  const isLoading = loading.value[key]

  if (isLoading && rows.length === 0) {
    return <div class="sh-host-queue sh-host-queue--loading" aria-busy="true" />
  }

  if (rows.length === 0) {
    return (
      <div class="sh-host-queue sh-host-queue--empty">
        {t('event.host_queue.empty')}
      </div>
    )
  }

  return (
    <section class="sh-host-queue" aria-label={t('event.host_queue.aria')}>
      <h4 class="sh-host-queue-heading">
        {t('event.host_queue.title', { n: String(rows.length) })}
      </h4>
      <ul class="sh-host-queue-list">
        {rows.map((row) => (
          <PendingRowItem
            key={`${row.user_id}@${row.occurrence_at}`}
            row={row}
            eventId={eventId}
            spaceId={spaceId}
          />
        ))}
      </ul>
    </section>
  )
}

function PendingRowItem({
  row,
  eventId,
  spaceId,
}: {
  row: PendingRow
  eventId: string
  spaceId: string | null
}) {
  const avatarUrl = resolveAvatar(spaceId, row.user_id, null)
  const name = resolveDisplayName(spaceId, row.user_id, row.user_id)
  const requestedAgo = formatRelative(row.updated_at)
  const actionKey = `${eventId}@${row.user_id}@${row.occurrence_at}`
  const isActing = acting.value[actionKey]

  const decide = async (action: 'approve' | 'deny') => {
    acting.value = { ...acting.value, [actionKey]: true }
    try {
      const res = await api.post<{
        ok: boolean
        new_status?: string
        action?: string
      }>(`/api/calendars/events/${eventId}/approve`, {
        user_id: row.user_id,
        occurrence_at: row.occurrence_at,
        action,
      })
      if (action === 'approve') {
        const landing = res.new_status === 'waitlist'
          ? t('event.host_queue.approved_to_waitlist', { name })
          : t('event.host_queue.approved_to_going', { name })
        showToast(landing, 'success')
      } else {
        showToast(t('event.host_queue.denied', { name }), 'success')
      }
      await refresh(eventId, row.occurrence_at)
    } catch (e) {
      const msg = (e as Error)?.message ?? t('event.host_queue.failed')
      showToast(msg, 'error')
    } finally {
      const next = { ...acting.value }
      delete next[actionKey]
      acting.value = next
    }
  }

  return (
    <li class="sh-host-queue-row">
      <div class="sh-host-queue-who">
        {avatarUrl ? (
          <img class="sh-avatar sh-avatar--sm" src={avatarUrl} alt="" />
        ) : (
          <span class="sh-avatar sh-avatar--sm sh-avatar--placeholder" aria-hidden="true">
            {name.charAt(0).toUpperCase()}
          </span>
        )}
        <div class="sh-host-queue-meta">
          <span class="sh-host-queue-name">{name}</span>
          <span class="sh-host-queue-when">{requestedAgo}</span>
        </div>
      </div>
      <div class="sh-host-queue-actions">
        <Button
          variant="primary"
          loading={isActing}
          onClick={() => decide('approve')}
        >
          {t('event.host_queue.approve')}
        </Button>
        <Button
          variant="secondary"
          disabled={isActing}
          onClick={() => decide('deny')}
        >
          {t('event.host_queue.deny')}
        </Button>
      </div>
    </li>
  )
}

function formatRelative(iso: string): string {
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return ''
  const now = Date.now()
  const seconds = Math.floor((now - then) / 1000)
  if (seconds < 60) return t('time.just_now')
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return t('time.minutes_ago', { n: String(minutes) })
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return t('time.hours_ago', { n: String(hours) })
  const days = Math.floor(hours / 24)
  return t('time.days_ago', { n: String(days) })
}
